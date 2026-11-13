"""The workflow: eight stages, UMGC approval gates, five validation gates per asset (Technical Proposal 2.3.4).

advance() is idempotent. It inspects a Program's records and gates, opens whatever gate is due and enqueues
whatever job is due. Workers call it after every job; the dashboard calls it after every human decision.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import Gate, Program, get_record, list_records, locked, log, now, put_record, session
from .queue import enqueue
from .stages.build import plan_assets

STAGES = [
    ("define", "1. Define skills"), ("design", "2. Design Blueprint"), ("build", "3. Build"), ("qa", "4. Assure quality"),
    ("deliver", "5. Deliver"), ("learn", "6. Learn"), ("measure", "7. Measure"), ("improve", "8. Improve"),
]

UMGC_GATES = {
    "magic.employers": ("Employer validation of the skills map (three employers, Maryland-based where available)", "Learning Experience Architect", "magic"),
    "umgc.skills_map": ("Gate 1: UMGC approves the skills map and competencies", "UMGC Program Lead", "umgc"),
    "umgc.blueprint": ("Gate 2: UMGC approves the Program Design Blueprint (review window 1, 5 business days)", "UMGC Program Lead", "umgc"),
    "umgc.prototype": ("Prototype Module review (Week 4): becomes the reference standard", "UMGC Program Lead", "umgc"),
    "umgc.sprint1": ("Review window 2: Sprint 1 content (5 business days)", "UMGC content approval authority", "umgc"),
    "umgc.sprint2": ("Review window 3: Sprint 2 content (5 business days)", "UMGC content approval authority", "umgc"),
    "umgc.qa": ("Review window 4: Quality Assurance Report (5 business days)", "UMGC Program Lead with accessibility office", "umgc"),
    "umgc.launch": ("Launch readiness sign-off", "UMGC Program Sponsor", "umgc"),
}

VALIDATION_GATES = {
    1: ("SME accuracy", "Magic Subject Matter Expert (vendor-provided)"),
    2: ("Instructional fit", "Instructional Designer and Learning Experience Architect"),
    3: ("Accessibility", "QA and Accessibility Specialist"),
    4: ("Bias and appropriateness", "Instructional Designer with Accessibility Lead"),
    5: ("Final editorial", "Editor with Project Manager"),
}


HUMAN = ("Human-authored", "AI-drafted, human-edited")


def reopen(a: dict, note: str, source: str, by: str) -> dict:
    """Send returned work back. AI drafts go back to the build worker with the note; a person's revision goes back to
    that person as a working draft, so a reviewer's return never lets the AI overwrite human writing."""
    rev = a["revision"]
    pv = a.get("provenance", {}).get(str(rev), {})
    if pv.get("ai_contribution") in HUMAN:
        a["state"] = "author"
        a["draft"] = {"content": a["revisions"][str(rev)], "base_revision": rev, "edited_by": pv.get("drafted_by", "").split(" (")[0],
                      "role": "", "edited_at": now().isoformat(timespec="seconds"), "returned_note": f"{source}: {note}", "history": []}
        a["feedback"] = list(a.get("feedback", [])) + [{"revision": rev, "note": note, "source": source, "at": now().isoformat(), "to": "author"}]
    else:
        a["revision"] = rev + 1
        a["state"] = "queued"
        a["feedback"] = list(a.get("feedback", [])) + [{"revision": rev + 1, "note": note, "source": source, "at": now().isoformat()}]
    return a


def ensure_gate(s, program_id, gate_key, label, owner, kind, evidence=None) -> Gate:
    g = s.execute(select(Gate).where(Gate.program_id == program_id, Gate.gate_key == gate_key)).scalar_one_or_none()
    if g:
        return g
    sp = s.begin_nested()
    try:
        g = Gate(program_id=program_id, gate_key=gate_key, label=label, owner_role=owner, kind=kind, evidence=evidence or {})
        s.add(g)
        s.flush()
        sp.commit()
        log(s, program_id, "orchestrator", kind, f"Gate opened: {label} (owner: {owner}).")
    except IntegrityError:
        sp.rollback()
        g = s.execute(select(Gate).where(Gate.program_id == program_id, Gate.gate_key == gate_key)).scalar_one()
    return g


def umgc_gate(s, program_id, key, evidence=None):
    label, owner, kind = UMGC_GATES[key]
    return ensure_gate(s, program_id, key, label, owner, kind, evidence)


def val_gate_key(asset_key, rev, n):
    return f"val:{asset_key}:r{rev}:g{n}"


def drive_asset(s, program_id, rec) -> bool:
    """Move one asset along: queued -> drafted -> checked -> g1..g5 -> validated. Returns True when validated."""
    a = rec.data
    st, rev = a.get("state"), a["revision"]
    if st == "validated":
        return True
    if st == "queued":
        enqueue(s, program_id, "build", "build.asset", f"build:{rec.key}:r{rev}", {"asset": rec.key})
    elif st == "drafted":
        enqueue(s, program_id, "qa", "qa.asset_checks", f"checks:{rec.key}:r{rev}", {"asset": rec.key})
    elif st == "checked":
        a = dict(a)
        a["state"] = "g1"
        put_record(s, program_id, "asset", rec.key, a, status="g1")
        _open_val(s, program_id, rec.key, rev, 1, a)
    elif st and st.startswith("g"):
        n = int(st[1:])
        g = s.execute(select(Gate).where(Gate.program_id == program_id, Gate.gate_key == val_gate_key(rec.key, rev, n))).scalar_one_or_none()
        if g is not None and g.status == "approved":  # heal a state that lagged behind its gate decision
            a = dict(a)
            a["state"] = "validated" if n == 5 else f"g{n + 1}"
            put_record(s, program_id, "asset", rec.key, a, status=a["state"])
            return drive_asset(s, program_id, get_record(s, program_id, "asset", rec.key))
        _open_val(s, program_id, rec.key, rev, n, a)
    return False


def _open_val(s, program_id, key, rev, n, a):
    label, owner = VALIDATION_GATES[n]
    ch = a.get("checks", {}).get(str(rev), {})
    ev = {"asset": key, "revision": rev, "gate": n, "findings": [f for f in ch.get("findings", []) if f.get("gate") == n]}
    ensure_gate(s, program_id, val_gate_key(key, rev, n), f"Gate {n} {label}: {key} r{rev}", owner, "validation", ev)


def _set_stage(s, p, stage, status="running"):
    if p.stage != stage or p.status != status:
        p.stage, p.status = stage, status


def advance(program_id: int):
    with locked(program_id) as s:
        p = s.get(Program, program_id)
        if p is None or p.status == "paused":
            return
        _advance(s, p)


def _approved(g):
    return g.status == "approved"


def _advance(s, p):
    pid = p.id
    sm = get_record(s, pid, "skills_map")
    if not sm:
        _set_stage(s, p, "define")
        enqueue(s, pid, "skills", "skills.define", "skills.define:1")
        return
    if not _approved(umgc_gate(s, pid, "magic.employers")):
        _set_stage(s, p, "define", "awaiting human")
        return
    if not _approved(umgc_gate(s, pid, "umgc.skills_map")):
        _set_stage(s, p, "define", "awaiting UMGC")
        return
    bp = get_record(s, pid, "blueprint")
    if not bp:
        _set_stage(s, p, "design")
        enqueue(s, pid, "design", "design.blueprint", "design.blueprint:1")
        return
    if not _approved(umgc_gate(s, pid, "umgc.blueprint")):
        _set_stage(s, p, "design", "awaiting UMGC")
        return

    # Build: prototype, then Sprint 1, then Sprint 2
    _set_stage(s, p, "build")
    plan_assets(s, pid, bp.data, sm.data)
    assets = list_records(s, pid, "asset")
    proto = [r for r in assets if r.data.get("prototype")]
    if proto and not drive_asset(s, pid, proto[0]):
        return
    if proto and not _approved(umgc_gate(s, pid, "umgc.prototype")):
        _set_stage(s, p, "build", "awaiting UMGC")
        return
    for sprint in (1, 2):
        batch = [r for r in assets if r.data.get("sprint") == sprint and not r.data.get("prototype")]
        done = [drive_asset(s, pid, r) for r in batch]
        if not all(done):
            any_human = any((r.data.get("state") or "").startswith("g") for r in batch)
            any_author = any(r.data.get("state") == "author" for r in batch)
            _set_stage(s, p, "build", "awaiting author" if any_author and not any_human else ("awaiting validation" if any_human else "running"))
            return
        if not _approved(umgc_gate(s, pid, f"umgc.sprint{sprint}")):
            _set_stage(s, p, "build", "awaiting UMGC")
            return

    qa = get_record(s, pid, "qa_report")
    if not qa:
        _set_stage(s, p, "qa")
        enqueue(s, pid, "qa", "qa.program", "qa.program:1")
        return
    if not _approved(umgc_gate(s, pid, "umgc.qa")):
        _set_stage(s, p, "qa", "awaiting UMGC")
        return

    rels = sorted(list_records(s, pid, "release"), key=lambda r: [int(x) for x in r.key.split(".")])
    if not rels:
        _set_stage(s, p, "deliver")
        enqueue(s, pid, "deliver", "deliver.release", "deliver.release:1.0", {"version": "1.0"})
        return
    if not _approved(umgc_gate(s, pid, "umgc.launch")):
        _set_stage(s, p, "deliver", "awaiting UMGC")
        return
    if rels[0].status == "built":  # first release goes live on the UMGC launch sign-off; later ones in the ticket branch below
        rels[0].status = "live"
        log(s, pid, "orchestrator", "deliver", f"Release {rels[0].key} is live (UMGC launch approval).")
    live = [r for r in rels if r.status == "live"]
    cur = live[-1].key if live else rels[-1].key
    p.release_version = cur

    # Learn, Measure, Improve on the live release
    if not get_record(s, pid, "cohort", cur):
        _set_stage(s, p, "learn")
        enqueue(s, pid, "learn", "learn.cohort", f"learn.cohort:{cur}", {"release": cur})
        return
    if not get_record(s, pid, "metrics", cur):
        _set_stage(s, p, "measure")
        enqueue(s, pid, "measure", "measure.compute", f"measure.compute:{cur}", {"release": cur})
        return
    if not get_record(s, pid, "scan", cur):
        _set_stage(s, p, "improve")
        enqueue(s, pid, "improve", "improve.scan", f"improve.scan:{cur}", {"release": cur})
        return

    # tickets: approved -> reopen assets as new revisions -> gates -> next release
    tickets = list_records(s, pid, "ticket")
    for t in tickets:
        d = t.data
        if d["status"] == "approved":
            if d.get("target_asset"):
                ar = get_record(s, pid, "asset", d["target_asset"])
                if ar and d.get("draft_content"):  # an author's revision from the authoring zone: no redraft, straight to checks
                    from .studio import apply_draft
                    apply_draft(s, pid, ar.key, d["draft_content"], d.get("draft_by", ""), d.get("draft_role", ""), d.get("draft_base"), f"ticket {d['ticket_id']}")
                elif ar:
                    a = dict(ar.data)
                    a["revision"] += 1
                    a["state"] = "queued"
                    a["feedback"] = list(a.get("feedback", [])) + [{"revision": a["revision"], "note": d["ai_proposed_response"],
                                                                   "source": f"ticket {d['ticket_id']}", "at": now().isoformat()}]
                    put_record(s, pid, "asset", ar.key, a, status="queued")
            d = dict(d)
            d["status"] = "in revision" if d.get("target_asset") else "closed"
            put_record(s, pid, "ticket", t.key, d, status=d["status"])
    tickets = list_records(s, pid, "ticket")
    inrev = [t for t in tickets if t.data["status"] == "in revision"]
    if inrev:
        _set_stage(s, p, "improve")
        targets = {t.data["target_asset"] for t in inrev}
        recs = [r for r in list_records(s, pid, "asset") if r.key in targets]
        if not all([drive_asset(s, pid, r) for r in recs]):
            _set_stage(s, p, "improve", "awaiting validation" if any((r.data.get("state") or "").startswith("g") for r in recs) else "running")
            return
        major, minor = cur.split(".")
        nxt = f"{major}.{int(minor) + 1}"
        if not get_record(s, pid, "release", nxt):
            cls = "material" if any(t.data["change_class"] == "material" for t in inrev) else "routine"
            enqueue(s, pid, "deliver", "deliver.release", f"deliver.release:{nxt}",
                    {"version": nxt, "tickets": [t.key for t in inrev], "change_class": cls})
            _set_stage(s, p, "deliver")
            return
        nr = get_record(s, pid, "release", nxt)
        if nr.status == "built":
            for t in inrev:
                d = dict(t.data)
                d["status"] = f"released in {nxt}"
                put_record(s, pid, "ticket", t.key, d, status="released")
            for o in rels:
                if o.status == "live" and o.key != nxt:
                    o.status = "superseded"
            nr.status = "live"
            p.release_version = nxt
            log(s, pid, "orchestrator", "deliver", f"Release {nxt} is live ({nr.data.get('change_class')} change"
                + (", UMGC approval recorded on the ticket" if nr.data.get("change_class") == "material" else ", standing approval") + ").")
            s.flush()
            _advance(s, p)
            return
        return
    pending = [t for t in tickets if t.data["status"] == "proposed"]
    _set_stage(s, p, "improve", "awaiting decision" if pending else "live, monitoring")


# ------------------------------------------------------------------ human decisions
ROLE_RULES = {1: "SME", 2: ("Instructional Designer", "Learning Experience Architect"), 3: "Accessibility", 4: ("Instructional Designer", "Accessibility"), 5: ("Editor", "Project Manager")}


def role_allowed(gate_kind, gate_no, role):
    """Seat check when the reviewer has told us who they are. Autopilot and API calls pass no role."""
    if not role:
        return True
    if gate_kind == "umgc":
        return role.startswith("UMGC")
    if gate_kind == "magic":
        return "Learning Experience Architect" in role
    need = ROLE_RULES.get(gate_no, "")
    need = need if isinstance(need, tuple) else (need,)
    return any(n in role for n in need)


def decide_gate(program_id: int, gate_key: str, decision: str, by: str, note: str = "", force: bool = False, role: str = "") -> str:
    """decision: approve or return. Returns an error string, or '' on success."""
    with locked(program_id) as s:
        g = s.execute(select(Gate).where(Gate.program_id == program_id, Gate.gate_key == gate_key)).scalar_one_or_none()
        if not g:
            return "Gate not found"
        if g.status != "pending":
            return f"Gate already {g.status}"
        if not by.strip():
            return "Enter the reviewer's name"
        if not role_allowed(g.kind, (g.evidence or {}).get("gate"), role):
            return f"{g.label} is decided by {g.owner_role}. You are signed in as {role}."
        if g.kind == "validation":
            key = g.evidence["asset"]
            rec = get_record(s, program_id, "asset", key)
            a = dict(rec.data)
            n, rev = g.evidence["gate"], g.evidence["revision"]
            blocking = [f for f in g.evidence.get("findings", []) if f["severity"] in ("critical", "high")]
            soft = [f for f in g.evidence.get("findings", []) if f["severity"] == "moderate"]
            if decision == "approve" and blocking:
                return "This gate has critical or high findings. Exit criteria do not allow approval: return it for revision."
            if decision == "approve" and soft and not note.strip() and not force:
                return "Moderate findings need a documented decision: add a note."
            g.status, g.decided_by, g.note, g.decided_at = ("approved" if decision == "approve" else "returned"), by, note, now()
            prov = dict(a["provenance"])
            pv = dict(prov.get(str(rev), {}))
            pv["gates"] = dict(pv.get("gates", {}))
            pv["gates"][f"g{n}"] = {"gate": VALIDATION_GATES[n][0], "decision": g.status, "by": by, "role": role or g.owner_role, "note": note,
                                    "at": now().isoformat(timespec="seconds")}
            prov[str(rev)] = pv
            a["provenance"] = prov
            a["gate_log"] = list(a.get("gate_log", [])) + [{"revision": rev, "gate": n, "decision": g.status, "by": by, "note": note}]
            if decision == "approve":
                a["state"] = "validated" if n == 5 else f"g{n + 1}"
            else:
                fix = note or "; ".join(f["message"] for f in blocking + soft) or "Revise per reviewer"
                a = reopen(a, fix, f"gate {n}", by)
            put_record(s, program_id, "asset", key, a, status=a["state"])
            log(s, program_id, by, "build", f"{g.label}: {g.status}" + (f" ({note})" if note else ""))
        else:
            g.status, g.decided_by, g.note, g.decided_at = ("approved" if decision == "approve" else "returned"), by, note, now()
            log(s, program_id, by, g.kind, f"{g.label}: {g.status}" + (f" ({note})" if note else ""))
            if decision != "approve":
                _handle_umgc_return(s, program_id, gate_key, note)
    advance(program_id)
    return ""


def _handle_umgc_return(s, pid, gate_key, note):
    """A returned UMGC gate reopens the work behind it and a fresh gate opens when the work is redone."""
    g = s.execute(select(Gate).where(Gate.program_id == pid, Gate.gate_key == gate_key)).scalar_one()
    g.gate_key = f"{gate_key}:returned:{int(now().timestamp())}"
    if gate_key in ("umgc.skills_map", "magic.employers"):
        r = get_record(s, pid, "skills_map")
        r.status = "returned"
    elif gate_key == "umgc.blueprint":
        r = get_record(s, pid, "blueprint")
        d = dict(r.data)
        d["umgc_feedback"] = list(d.get("umgc_feedback", [])) + [note]
        r.data = d
    elif gate_key.startswith("umgc.sprint") or gate_key == "umgc.prototype":
        sprint = {"umgc.sprint1": 1, "umgc.sprint2": 2}.get(gate_key)
        for r in list_records(s, pid, "asset"):
            if (sprint and r.data.get("sprint") == sprint and not r.data.get("prototype")) or (gate_key == "umgc.prototype" and r.data.get("prototype")):
                a = reopen(dict(r.data), note or "UMGC review feedback", gate_key, "UMGC")
                put_record(s, pid, "asset", r.key, a, status=a["state"])
    elif gate_key == "umgc.qa":
        rerun(s, pid, "qa_report", "qa.program:1")


def rerun(s, pid, kind, dedupe):
    """Throw away a stage output and its job so advance() regenerates it."""
    from .db import Job
    r = get_record(s, pid, kind)
    if r:
        s.delete(r)
    for j in s.execute(select(Job).where(Job.program_id == pid, Job.dedupe_key == dedupe)).scalars():
        s.delete(j)
    s.flush()


def bulk_approve(program_id: int, gate_no: int, by: str, note: str = "", role: str = "") -> dict:
    with session() as s:
        gs = list(s.execute(select(Gate).where(Gate.program_id == program_id, Gate.kind == "validation", Gate.status == "pending")).scalars())
        keys = [g.gate_key for g in gs if g.evidence.get("gate") == gate_no]
    ok, skipped = 0, []
    for k in keys:
        err = decide_gate(program_id, k, "approve", by, note or "Bulk approval: findings reviewed", force=True, role=role)
        if err:
            skipped.append((k, err))
        else:
            ok += 1
    return {"approved": ok, "skipped": skipped}


def decide_ticket(program_id: int, ticket_id: str, decision: str, by: str, role: str, change_class: str, note: str) -> str:
    with locked(program_id) as s:
        r = get_record(s, program_id, "ticket", ticket_id)
        if not r:
            return "Ticket not found"
        d = dict(r.data)
        if d["status"] != "proposed":
            return f"Ticket already {d['status']}"
        if not by.strip():
            return "Enter the decision maker's name"
        if decision == "approve" and change_class == "material" and role != "UMGC":
            return "Material changes need UMGC approval first (Technical Proposal 2.2.3.2). Record the UMGC approver."
        d["status"] = "approved" if decision == "approve" else "rejected"
        d["change_class"] = change_class
        if note:
            d["ai_proposed_response"] = d["ai_proposed_response"] + " Reviewer note: " + note
        d["decision"] = {"by": by, "role": role, "decision": d["status"], "class": change_class, "note": note, "at": now().isoformat(timespec="seconds")}
        put_record(s, program_id, "ticket", ticket_id, d, status=d["status"])
        log(s, program_id, by, "improve", f"Ticket {ticket_id} {d['status']} as a {change_class} change ({role}).")
    advance(program_id)
    return ""
