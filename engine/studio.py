"""Authoring and collaboration zone, write side (RFP 4.1.2 content authoring; 4.3.3 SMEs; TP 2.2.1.3 collaboration).

  Drafts      Every asset has a working copy. People write or rewrite screens, items and rubrics; AI assist drafts a
              block from the approved source pack on request. Saving keeps the draft; Submit turns it into a new
              revision that goes through automated checks and the five validation gates like any AI draft.
  Comments    Threaded comments anchored to a screen or item, with @mentions and resolve. UMGC reviewers comment
              here during review windows; nothing is decided in email.
  Ownership   Each asset has an author (ID), a Magic SME and a reviewer. SMEs are vendor-provided (Magic), per UMGC.
  Team        The Program roster: Magic SMEs with their expertise and the Courses they own, plus Magic and UMGC roles.
After launch, a submitted revision becomes an 'author revision' ticket: routine changes publish under standing
approval, material changes wait for a UMGC approver, exactly like data-driven tickets.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re

from sqlalchemy import select

from .db import Gate, Program, get_record, list_records, locked, log, now, put_record, session

ROLES = ["Magic SME (vendor-provided)", "Magic Instructional Designer", "Magic Learning Experience Architect", "Magic Accessibility Specialist",
         "Magic Editor", "Magic Project Manager", "UMGC Program Lead", "UMGC Reviewer"]


def _now():
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def content_from_form(a: dict, form: dict) -> dict:
    """Rebuild an asset's content from the editor form fields."""
    base = dict(a["revisions"].get(str(a["revision"])) or {})
    if a["asset_type"] == "module":
        screens = []
        for i in range(0, 80):
            h, txt = form.get(f"s{i}_heading", "").strip(), form.get(f"s{i}_text", "").strip()
            if not (h or txt):
                continue
            if form.get(f"s{i}_delete"):
                continue
            cites = [c.strip() for c in form.get(f"s{i}_cites", "").split(",") if c.strip()]
            alt = form.get(f"s{i}_alt", "").strip()
            media = {"type": form.get(f"s{i}_mtype", "diagram") or "diagram", "alt": alt} if (alt or form.get(f"s{i}_hasmedia")) else None
            screens.append({"heading": h, "blocks": [{"text": txt, "citations": cites}], "media": media})
        practice = [{"prompt": ln.strip(), "skill_id": "", "feedback": ""} for ln in form.get("practice", "").splitlines() if ln.strip()]
        base.update({"screens": screens, "practice": practice, "summary": form.get("summary", "").strip()})
    elif base.get("items") is not None or a["type"] in ("knowledge check", "practice exam"):
        items = []
        for i in range(0, 60):
            stem = form.get(f"i{i}_stem", "").strip()
            if not stem or form.get(f"i{i}_delete"):
                continue
            key = form.get(f"i{i}_key", "A")
            opts = []
            for L in "ABCD":
                tx = form.get(f"i{i}_{L}", "").strip()
                if tx:
                    opts.append({"id": L, "text": tx, "correct": L == key, "rationale": form.get(f"i{i}_{L}_why", "").strip()})
            items.append({"item_id": form.get(f"i{i}_id") or f"{a['ref_id']}-I{len(items) + 1:02d}", "stem": stem, "options": opts, "key": key,
                          "skill_id": form.get(f"i{i}_skill", ""), "objective_id": form.get(f"i{i}_obj", "")})
        base.update({"items": items, "kind": "items"})
    else:
        rubric = []
        for ln in form.get("rubric", "").splitlines():
            if ln.strip():
                parts = [x.strip() for x in ln.split("|")]
                rubric.append({"criterion": parts[0], "levels": parts[1:] or ["Not demonstrated", "Developing", "Meets", "Exceeds"],
                               "skill_id": (re.search(r"\b(S\d+\.\d+)\b", parts[0]) or [None, ""])[1] if re.search(r"\b(S\d+\.\d+)\b", parts[0]) else ""})
        base.update({"prompt": form.get("prompt", "").strip(), "steps": [x.strip() for x in form.get("steps", "").splitlines() if x.strip()],
                     "rubric": rubric, "kind": "performance"})
    base["title"] = form.get("title", base.get("title", a["title"])).strip() or a["title"]
    return base


def save_draft(program_id: int, key: str, content: dict, by: str, role: str, base_rev: int) -> str:
    with locked(program_id) as s:
        r = get_record(s, program_id, "asset", key)
        a = dict(r.data)
        dr = a.get("draft")
        if dr and dr.get("edited_by") and dr["edited_by"] != by and dr.get("base_revision") == a["revision"]:
            pass  # shared working copy: the last save wins, the history below keeps every save
        a["draft"] = {"content": content, "base_revision": base_rev, "edited_by": by, "role": role, "edited_at": _now(),
                      "history": (dr or {}).get("history", [])[-19:] + [{"by": by, "at": _now()}]}
        put_record(s, program_id, "asset", key, a)
        log(s, program_id, by or "author", "author", f"Draft of {key} saved by {by or 'unnamed'} ({role}).")
    return ""


def apply_draft(s, pid, key, content, by, role, base_rev, source):
    """Turn a draft into a new revision that enters automated checks and the five gates."""
    r = get_record(s, pid, "asset", key)
    a = dict(r.data)
    old = a["revision"]
    rev = old + 1
    for g in s.execute(select(Gate).where(Gate.program_id == pid, Gate.status == "pending", Gate.gate_key.like(f"val:{key}:%"))).scalars():
        g.status, g.decided_by, g.note, g.decided_at = "returned", by, f"Superseded by author revision r{rev}", now()
    prev = a["provenance"].get(str(old), {})
    a["revisions"] = dict(a["revisions"])
    a["revisions"][str(rev)] = content
    a["provenance"] = dict(a["provenance"])
    a["provenance"][str(rev)] = {
        "revision": rev, "ai_contribution": ("AI-drafted, human-edited" if str(prev.get("ai_contribution", "")).startswith(("AI", "Template"))
                                             else "Human-authored"),
        "model": prev.get("model", "none"), "prompt_id": prev.get("prompt_id", "author"), "prompt_version": prev.get("prompt_version", ""),
        "source_pack_version": prev.get("source_pack_version"), "taxonomy_version": prev.get("taxonomy_version"),
        "drafted_at": _now(), "drafted_by": f"{by} ({role})", "feedback_applied": [f"Author revision from r{base_rev} via {source}"], "gates": {}}
    a["revision"] = rev
    a["state"] = "drafted"
    a["draft"] = None
    a["feedback"] = list(a.get("feedback", [])) + [{"revision": rev, "note": f"Author revision by {by}", "source": source, "at": _now()}]
    put_record(s, pid, "asset", key, a, status="drafted")
    log(s, pid, by or "author", "author", f"{key} revision {rev} submitted by {by} ({role}); automated checks and validation gates follow.")


def submit_draft(program_id: int, key: str, by: str, role: str, change_class: str = "routine", note: str = "") -> str:
    from .orchestrator import advance
    from .stages.improve import raise_ticket
    if not by:
        return "Tell us who you are (top right) before submitting"
    with locked(program_id) as s:
        p = s.get(Program, program_id)
        r = get_record(s, program_id, "asset", key)
        a = r.data
        dr = a.get("draft")
        if not dr:
            return "No draft to submit"
        if a.get("state") in ("queued", "drafted"):
            return "A worker is drafting or checking this asset right now. Submit again in a moment."
        if dr.get("base_revision") != a["revision"]:
            return f"The asset moved to revision {a['revision']} after you started from r{dr.get('base_revision')}. Open the draft, review the new revision, and save again."
        if not p.release_version:
            apply_draft(s, program_id, key, dr["content"], by, role, dr["base_revision"], "authoring zone")
            msg = ""
        else:
            rel = p.release_version
            tid = raise_ticket(s, program_id, "Author revision from the authoring zone", note or f"{by} revised {key}.", "Human-authored change", key,
                               f"Publish {by}'s revision of {key} after it clears automated checks and the five validation gates.",
                               change_class, "event-triggered", rel, f"author:{key}:{dr['edited_at']}")
            if tid:
                t = get_record(s, program_id, "ticket", tid)
                d = dict(t.data)
                d.update({"draft_content": dr["content"], "draft_by": by, "draft_role": role, "draft_base": dr["base_revision"]})
                put_record(s, program_id, "ticket", tid, d)
            msg = f"ticket:{tid}"
    advance(program_id)
    return msg


# ------------------------------------------------------------------ comments, owners, team
def add_comment(program_id: int, key: str, anchor: str, body: str, by: str, role: str, parent: str = "") -> str:
    if not body.strip():
        return "Write a comment first"
    with locked(program_id) as s:
        n = len(list_records(s, program_id, "comment")) + 1
        cid = f"CM{n:04d}"
        mentions = re.findall(r"@([A-Za-z][\w .'-]{1,40}?)(?=[\s,.;:!?]|$)", body)
        put_record(s, program_id, "comment", cid, {"comment_id": cid, "asset": key, "anchor": anchor, "body": body.strip(), "by": by or "unnamed",
                                                   "role": role, "mentions": [m.strip() for m in mentions], "parent": parent, "status": "open",
                                                   "at": _now()}, status="open")
        log(s, program_id, by or "reviewer", "author", f"Comment on {key}{' / ' + anchor if anchor else ''}: {body.strip()[:120]}")
    return ""


def resolve_comment(program_id: int, cid: str, by: str) -> str:
    with locked(program_id) as s:
        r = get_record(s, program_id, "comment", cid)
        d = dict(r.data)
        d["status"], d["resolved_by"], d["resolved_at"] = "resolved", by, _now()
        put_record(s, program_id, "comment", cid, d, status="resolved")
    return ""


def set_owners(program_id: int, key: str, author: str, sme: str, reviewer: str, by: str) -> str:
    with locked(program_id) as s:
        r = get_record(s, program_id, "asset", key)
        a = dict(r.data)
        a["owners"] = {"author": author, "sme": sme, "reviewer": reviewer}
        put_record(s, program_id, "asset", key, a)
        log(s, program_id, by or "PM", "author", f"{key} owners: author {author or '-'}, SME {sme or '-'}, reviewer {reviewer or '-'}.")
    return ""


def team(s, program_id: int) -> dict:
    r = get_record(s, program_id, "team", "main")
    if r:
        return r.data
    p = s.get(Program, program_id)
    bp = get_record(s, program_id, "blueprint")
    courses = [c["course_id"] for c in bp.data["courses"]] if bp else []
    spec = "cybersecurity operations" if p.brief.get("sample") else (p.brief.get("occupation", {}).get("title") or "the subject")
    members = [
        {"name": "[PLACEHOLDER: Magic SME 1]", "role": "Magic SME (vendor-provided)", "org": "Magic",
         "expertise": f"{spec}: networks, threats, identity", "credentials": "[PLACEHOLDER: e.g. Security+, CySA+]", "courses": courses[:4], "location": "[PLACEHOLDER]"},
        {"name": "[PLACEHOLDER: Magic SME 2]", "role": "Magic SME (vendor-provided)", "org": "Magic",
         "expertise": f"{spec}: operations, response, governance", "credentials": "[PLACEHOLDER: e.g. CISSP, GCIH]", "courses": courses[4:], "location": "[PLACEHOLDER]"},
        {"name": "[PLACEHOLDER: Second-SME accuracy reviewer]", "role": "Magic SME (vendor-provided)", "org": "Magic", "expertise": "QA accuracy pass (Weeks 9-10)",
         "credentials": "[PLACEHOLDER]", "courses": courses, "location": "[PLACEHOLDER]"},
        {"name": "[PLACEHOLDER: Learning Experience Architect]", "role": "Magic Learning Experience Architect", "org": "Magic", "expertise": "Skills map, Blueprint", "credentials": "", "courses": courses, "location": ""},
        {"name": "[PLACEHOLDER: Instructional Designer]", "role": "Magic Instructional Designer", "org": "Magic", "expertise": "Module design", "credentials": "", "courses": courses, "location": ""},
        {"name": "[PLACEHOLDER: Accessibility Specialist]", "role": "Magic Accessibility Specialist", "org": "Magic", "expertise": "WCAG 2.1 AA", "credentials": "[PLACEHOLDER: CPACC]", "courses": courses, "location": ""},
        {"name": "[PLACEHOLDER: Editor]", "role": "Magic Editor", "org": "Magic", "expertise": "Style, voice, provenance", "credentials": "", "courses": courses, "location": ""},
        {"name": "[PLACEHOLDER: UMGC Program Lead]", "role": "UMGC Program Lead", "org": "UMGC", "expertise": "Approves Blueprint and content", "credentials": "", "courses": courses, "location": ""},
    ]
    return {"members": members, "note": "Subject matter experts are provided by Magic (vendor responsibility). UMGC reviewers comment and approve at UMGC gates."}


def save_team(program_id: int, members: list[dict], by: str):
    with locked(program_id) as s:
        put_record(s, program_id, "team", "main", {"members": members, "note": team(s, program_id).get("note", "")})
        log(s, program_id, by or "PM", "author", f"Program team updated ({len(members)} people).")


def tracker_csv(program_id: int) -> bytes:
    """Asset tracker for import into UMGC's Smartsheet (TP 2.2.1.3: work inside UMGC's tools)."""
    with session() as s:
        assets = [r.data for r in list_records(s, program_id, "asset")]
        comments = [r.data for r in list_records(s, program_id, "comment")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Asset", "Title", "Type", "Course", "Sprint", "Revision", "State", "Author", "Magic SME", "Reviewer", "Open comments", "Draft in progress", "Gates passed"])
    for a in assets:
        o = a.get("owners") or {}
        pv = a["provenance"].get(str(a["revision"]), {})
        w.writerow([a["asset_id"], a["title"], a["type"], a["course_id"], a.get("sprint"), a["revision"], a.get("state"), o.get("author", ""), o.get("sme", ""),
                    o.get("reviewer", ""), sum(1 for c in comments if c["asset"] == a["asset_id"] and c["status"] == "open"),
                    "yes" if a.get("draft") else "", len(pv.get("gates", {}))])
    return buf.getvalue().encode()
