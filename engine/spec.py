"""Task Order and Program specification (RFP Attachment C 1 to 4), milestones (Attachment C 7), and the deliverables
register with acceptance (Attachment C 6 and 8; Appendix C-1 Section 5; UMGC Q&A: two review rounds, 5-business-day
turnaround)."""
from __future__ import annotations

import datetime as dt

from .db import Program, get_record, list_records, locked, log, put_record, session

ASSET_MIX = {"video_minutes_per_program": "60 to 90", "scenarios_per_course": "2 to 3", "labs_per_course": "1 where the subject allows",
             "practice_items_per_course": "40 to 60", "applied_exercise_per_course": "1", "capstone": "1 per Program"}
DEFAULT_ATTRIBUTES = ["Military-affiliated status", "Age band", "State of residence", "Employment status", "First-generation status"]


def default_spec(brief: dict) -> dict:
    occ = brief.get("occupation", {}) or {}
    sample = brief.get("sample")
    weeks = brief.get("duration_weeks") or (8 if sample else 5)
    weekly = brief.get("weekly_hours") or (22.5 if sample else 10)
    start = brief.get("start_date") or (dt.date.today() + dt.timedelta(days=(7 - dt.date.today().weekday()) % 7 or 7)).isoformat()
    return {
        "task_order": {"number": brief.get("task_order", "92303-1"), "program_name": brief.get("name", ""), "issued": dt.date.today().isoformat(),
                       "start_date": start, "review_window_days": 5, "acceptance_days": 10, "approval_authority": "[UMGC content approval authority, named in the Task Order]",
                       "sme_model": "Magic-provided SMEs", "recurring_months": 12, "background": brief.get("background", "")},
        "program": {"subject_area": brief.get("subject_area") or ("Cybersecurity" if sample else ""), "occupation": occ.get("title", ""),
                    "lot_occupation": occ.get("lot_occupation_name", ""), "lot_specialized": occ.get("lot_specialized_occupation_name", ""),
                    "credential": brief.get("credential", ""), "audience": brief.get("audience") or "Working adult learners, including military-affiliated learners",
                    "modality": "Online, asynchronous, self-paced (RFP II.5.8)", "duration_weeks": int(weeks), "weekly_hours": float(weekly),
                    "courses": int(brief.get("courses") or weeks), "pass_mark": 80, "attempts": 3, "capstone": True,
                    "required_modules": brief.get("required_modules", ""), "assessment_requirements": brief.get("assessment_requirements", ""),
                    "asset_mix": dict(ASSET_MIX)},
        "attributes": list(DEFAULT_ATTRIBUTES),
    }


def get_spec(s, pid: int) -> dict:
    r = get_record(s, pid, "spec")
    if r:
        return r.data
    p = s.get(Program, pid)
    d = default_spec(p.brief)
    put_record(s, pid, "spec", "main", d, status="draft")
    return d


def total_hours(spec) -> float:
    pr = spec["program"]
    return round(float(pr["duration_weeks"]) * float(pr["weekly_hours"]), 1)


def completion_definition(spec) -> str:
    pr = spec["program"]
    cap = " and submit the capstone" if pr.get("capstone") else ""
    return (f"Finish all required Modules, pass the summative assessment at {pr['pass_mark']} percent or higher within {pr['attempts']} attempts{cap}. "
            f"Expected time is {total_hours(spec)} learner hours over {pr['duration_weeks']} weeks at {pr['weekly_hours']} hours a week; completion is "
            f"measured at 100 and 150 percent of that time.")


# ------------------------------------------------------------------ milestones (Attachment C 7; TP 2.2.1.1 12-week factory)
MILESTONES = [("M1", "Project Kickoff", 1), ("M1b", "Design Blueprint Approved", 2), ("M2", "Initial Content Submitted", 5),
              ("M3", "Platform Configuration Complete", 8), ("M4", "Testing Complete", 10), ("M5", "Program Launch", 12)]


def add_business_days(d: dt.date, n: int) -> dt.date:
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def milestones(spec, overrides: dict | None = None) -> list[dict]:
    start = dt.date.fromisoformat(spec["task_order"]["start_date"])
    out = []
    for code, name, wk in MILESTONES:
        due = start + dt.timedelta(days=7 * wk - 3)
        if overrides and overrides.get(code):
            due = dt.date.fromisoformat(overrides[code])
        out.append({"code": code, "name": name, "week": wk, "due": due.isoformat()})
    return out


# ------------------------------------------------------------------ deliverables register (Attachment C 6 and 8)
ACCEPT = {
    "planning": ["Addresses all requirements in the Task Order", "Includes all required sections and supporting documentation",
                 "No material errors, omissions or inconsistencies", "Includes a requirements traceability matrix", "Submitted in required formats"],
    "content": ["Meets approved learning objectives", "Aligns to workforce competencies", "Meets accessibility requirements",
                "Includes all required components", "Passes quality review"],
    "technology": ["Meets functional requirements", "Contains no critical defects", "Includes required documentation"],
    "testing": ["All required testing completed", "Test results documented", "Defects, including accessibility limitations, remediated or accepted by UMGC",
                "Quality assurance requirements satisfied", "Validation demonstrates readiness for deployment"],
    "launch": ["Production Program deployed", "Training delivered", "Launch readiness confirmed"],
    "recurring": ["Delivered within 5 business days of month end", "Covers all agreed measures", "Actions proposed and owned"],
}
DELIVERABLES = [
    # code, name, phase, acceptance group, milestone, how it is accepted, evidence page, generated document key
    ("6.1.1", "Project Management Plan", "Discovery and Planning", "planning", "M1", "register", "setup", "pmp"),
    ("6.1.2", "Program Design Blueprint", "Discovery and Planning", "planning", "M1b", "gate:umgc.blueprint", "design", None),
    ("6.1.3", "Implementation Schedule", "Discovery and Planning", "planning", "M1", "register", "setup", "schedule"),
    ("6.2.1", "Learning Content Modules", "Content Design and Development", "content", "M2", "gate:umgc.sprint2", "develop", None),
    ("6.2.2", "Assessments", "Content Design and Development", "content", "M2", "gate:umgc.sprint2", "develop", None),
    ("6.2.3", "Interactive Learning Assets", "Content Design and Development", "content", "M2", "gate:umgc.sprint2", "develop", None),
    ("6.2.4", "Instructor and Learner Guides", "Content Design and Development", "content", "M3", "register", "develop", "guides"),
    ("6.3.1", "Configured Learning Environment", "Platform Configuration", "technology", "M3", "register", "learner", "environment"),
    ("6.3.2", "Analytics Dashboard", "Platform Configuration", "technology", "M3", "register", "insights", None),
    ("6.3.3", "Integration Documentation", "Platform Configuration", "technology", "M3", "register", "learner", "integration"),
    ("6.4.1", "Test Plan", "Testing and Quality Assurance", "testing", "M4", "register", "quality", "testplan"),
    ("6.4.2", "Quality Assurance Report", "Testing and Quality Assurance", "testing", "M4", "gate:umgc.qa", "quality", None),
    ("6.4.3", "Defect Resolution Log", "Testing and Quality Assurance", "testing", "M4", "gate:umgc.qa", "quality", None),
    ("6.5.1", "Production Program", "Deployment and Launch", "launch", "M5", "gate:umgc.launch", "release", None),
    ("6.5.2", "Training Materials", "Deployment and Launch", "launch", "M5", "register", "release", "training"),
    ("6.5.3", "Launch Readiness Report", "Deployment and Launch", "launch", "M5", "gate:umgc.launch", "release", "readiness"),
    ("6.6.1", "Monthly Performance Reports", "Recurring Support", "recurring", None, "register", "improve", "monthly"),
    ("6.6.2", "Monthly Service Level Reports", "Recurring Support", "recurring", None, "register", "improve", "sla"),
    ("6.6.3", "Continuous Improvement Plans", "Recurring Support", "recurring", None, "register", "improve", "cip"),
    ("6.6.4", "Maintenance Releases", "Recurring Support", "recurring", None, "gate:release", "release", None),
]


def deliverables(s, pid: int) -> list[dict]:
    from sqlalchemy import select
    from .db import Gate
    spec = get_spec(s, pid)
    ms = {m["code"]: m for m in milestones(spec, (get_record(s, pid, "schedule") or _N).data.get("overrides"))}
    gates = {g.gate_key: g for g in s.execute(select(Gate).where(Gate.program_id == pid)).scalars()}
    regs = {r.key: r.data for r in list_records(s, pid, "deliverable")}
    rels = list_records(s, pid, "release")
    out = []
    for code, name, phase, grp, mcode, how, page, doc in DELIVERABLES:
        reg = regs.get(code, {"rounds": []})
        rounds = reg.get("rounds", [])
        last = rounds[-1] if rounds else None
        if how.startswith("gate:"):
            gk = how[5:]
            if gk == "release":
                status = f"{len(rels)} release(s)" if rels else "not started"
                accepted = bool(rels)
            else:
                g = gates.get(gk)
                status = ("accepted" if g and g.status == "approved" else "in UMGC review" if g and g.status == "pending" else "in progress")
                accepted = bool(g and g.status == "approved")
            mode = "Accepted through the UMGC gate"
        else:
            if not last:
                status, accepted = "not submitted", False
            elif last.get("decision") == "accepted":
                status, accepted = "accepted", True
            elif last.get("decision") == "returned":
                status, accepted = f"returned in round {last['round']}", False
            else:
                status, accepted = f"in UMGC review (round {last['round']}, due {last['due']})", False
            mode = "Submitted and accepted in this register"
        out.append({"code": code, "name": name, "phase": phase, "criteria": ACCEPT[grp], "milestone": ms.get(mcode, {}).get("name") if mcode else "Monthly",
                    "due": ms.get(mcode, {}).get("due") if mcode else "Within 5 business days of month end", "how": how, "mode": mode, "page": page,
                    "doc": doc, "status": status, "accepted": accepted, "rounds": rounds})
    return out


class _N:
    data: dict = {}


def submit_deliverable(pid: int, code: str, by: str, note: str = "") -> str:
    with locked(pid) as s:
        spec = get_spec(s, pid)
        r = get_record(s, pid, "deliverable", code)
        d = dict(r.data) if r else {"code": code, "rounds": []}
        rounds = list(d.get("rounds", []))
        if rounds and not rounds[-1].get("decision"):
            return "This deliverable is already in UMGC review."
        if rounds and rounds[-1].get("decision") == "accepted":
            return "This deliverable is already accepted."
        today = dt.date.today()
        due = add_business_days(today, int(spec["task_order"].get("review_window_days", 5)))
        rounds.append({"round": len(rounds) + 1, "submitted_at": today.isoformat(), "submitted_by": by, "note": note, "due": due.isoformat(), "decision": None})
        d["rounds"] = rounds
        put_record(s, pid, "deliverable", code, d, status="submitted")
        log(s, pid, by, "delivery", f"Deliverable {code} submitted to UMGC (round {len(rounds)}); review due {due.isoformat()}.")
    return ""


def decide_deliverable(pid: int, code: str, decision: str, by: str, note: str) -> str:
    with locked(pid) as s:
        r = get_record(s, pid, "deliverable", code)
        if not r or not r.data.get("rounds") or r.data["rounds"][-1].get("decision"):
            return "Nothing is waiting for a decision on this deliverable."
        if decision == "return" and not note.strip():
            return "Say what must change when returning a deliverable."
        d = dict(r.data)
        rounds = list(d["rounds"])
        last = dict(rounds[-1])
        last.update({"decision": "accepted" if decision == "accept" else "returned", "decided_by": by, "decided_at": dt.date.today().isoformat(), "decision_note": note})
        rounds[-1] = last
        d["rounds"] = rounds
        put_record(s, pid, "deliverable", code, d, status=last["decision"])
        log(s, pid, by, "delivery", f"Deliverable {code} {last['decision']} (round {last['round']})" + (f": {note}" if note else "") + ".")
    return ""
