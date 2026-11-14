"""Generated documents (RFP Attachment C deliverables and Attachment D). Each document is built from the Program's
records at the moment it is generated, shown in the product, and downloadable as Word. A generated document is a
draft for the owner to review; acceptance happens in the deliverables register.

Each builder returns a list of blocks: ("h", text) heading, ("p", text) paragraph, ("ul", [items]),
("table", header, rows).
"""
from __future__ import annotations

import io
from collections import Counter

from .db import Gate, Program, get_record, list_records, session
from .spec import DELIVERABLES, completion_definition, get_spec, milestones, total_hours

DOCS = {
    "pmp": ("Project Management Plan", "6.1.1"), "schedule": ("Implementation Schedule", "6.1.3"), "guides": ("Instructor and Learner Guides", "6.2.4"),
    "environment": ("Configured Learning Environment record", "6.3.1"), "integration": ("Integration Documentation", "6.3.3"), "testplan": ("Test Plan", "6.4.1"),
    "training": ("Training Materials", "6.5.2"), "readiness": ("Launch Readiness Report", "6.5.3"), "monthly": ("Monthly Performance Report", "6.6.1"),
    "sla": ("Monthly Service Level Report", "6.6.2"), "cip": ("Continuous Improvement Plan", "6.6.3"), "attd": ("Attachment D credit evaluation packet", "II.5.9"),
}


def _ctx(s, pid):
    p = s.get(Program, pid)
    spec = get_spec(s, pid)
    g = lambda k: (get_record(s, pid, k) or None)
    sm, bp, qa = g("skills_map"), g("blueprint"), g("qa_report")
    from .studio import team
    rels = sorted([r for r in list_records(s, pid, "release")], key=lambda r: [int(x) for x in r.key.split(".")])
    rel = p.release_version or (rels[-1].key if rels else None)
    met = get_record(s, pid, "metrics", rel) if rel else None
    return {"p": p, "spec": spec, "pr": spec["program"], "to": spec["task_order"], "sm": sm.data if sm else None, "bp": bp.data if bp else None,
            "qa": qa.data if qa else None, "team": team(s, pid)["members"], "rels": rels, "rel": rel, "met": met.data if met else None,
            "ms": milestones(spec, (get_record(s, pid, "schedule").data.get("overrides") if get_record(s, pid, "schedule") else None)),
            "tickets": [t.data for t in list_records(s, pid, "ticket")], "assets": [a.data for a in list_records(s, pid, "asset")],
            "gates": list(s.query(Gate).filter(Gate.program_id == pid, Gate.kind != "validation").order_by(Gate.id))}


def _need(c, what):
    return [("p", f"This section fills when {what} exists. Generate the document again after that step.")]


def pmp(c):
    pr, to = c["pr"], c["to"]
    b = [("h", "1. Scope"), ("p", f"Design, develop, launch and support the noncredit workforce Program '{c['p'].name}' under Task Order {to['number']}: "
                                  f"{pr['duration_weeks']} weeks, {pr['weekly_hours']} learner hours a week ({total_hours(c['spec'])} hours), {pr['courses']} Courses, "
                                  f"aligned to {pr.get('credential') or 'the credential named in the Task Order'} and the occupation {pr.get('occupation') or '(set in Setup)'}."),
         ("p", "Completion definition: " + completion_definition(c["spec"])),
         ("h", "2. Approach"), ("p", "The eight-stage AI-enabled workflow (Technical Proposal 2.3.4): Define skills, Design Blueprint, Build, Assure quality, Deliver, Learn, "
                                     "Measure, Improve. Skills come from Lightcast (UMGC's taxonomy); every asset passes five validation gates; UMGC approves at its gates."),
         ("h", "3. Milestones"), ("table", ["Milestone", "Week", "Due"], [[m["name"], m["week"], m["due"]] for m in c["ms"]]),
         ("h", "4. Deliverables and acceptance"),
         ("table", ["Code", "Deliverable", "Phase"], [[d[0], d[1], d[2]] for d in DELIVERABLES]),
         ("p", f"UMGC reviews each submission within {to.get('review_window_days', 5)} business days; up to two review rounds; acceptance within "
               f"{to.get('acceptance_days', 10)} business days of a compliant submission."),
         ("h", "5. Team and responsibilities"), ("table", ["Name", "Role", "Organization", "Expertise"], [[m["name"], m["role"], m.get("org", ""), m.get("expertise", "")] for m in c["team"]]),
         ("p", "Subject matter experts are provided by Magic (vendor responsibility). UMGC reviewers comment in the engine and decide UMGC gates."),
         ("h", "6. Communication and governance"), ("ul", ["Weekly status with the UMGC Program Lead", "Review windows run in the engine; no decisions in email",
                                                         "Monthly performance and service level reports during recurring support", "AI governance per the Governance workspace"]),
         ("h", "7. Risks, assumptions, issues, dependencies"),
         ("table", ["Item", "Type", "Owner", "Mitigation"], [
             ["Lightcast Job Postings Analytics license timing", "Dependency", "UMGC", "Score reweighted until demand is available; refresh when licensed"],
             ["SME availability for Gate 1 accuracy reviews", "Risk", "Magic PM", "Two named SMEs plus a second-SME reviewer"],
             ["UMGC review windows slipping", "Risk", "Both", "Five-business-day windows tracked in the deliverables register"],
             ["Brightspace sandbox access", "Dependency", "UMGC", "Requested at kickoff"]])]
    return b


def schedule(c):
    rows = [[m["code"], m["name"], m["week"], m["due"]] for m in c["ms"]]
    b = [("h", "Milestones"), ("table", ["Code", "Milestone", "Week", "Due"], rows), ("h", "Build sequence")]
    if c["bp"]:
        b.append(("table", ["Course", "Name", "Sprint", "Modules", "Learner hours"],
                  [[x["course_id"], x["name"], x.get("sprint"), len(x["modules"]), x.get("learner_hours")] for x in c["bp"]["courses"]]))
    else:
        b += _need(c, "the Blueprint")
    b += [("h", "UMGC review windows"), ("ul", ["Gate 1 skills map (Week 2)", "Gate 2 Blueprint (Week 2)", "Prototype Module (Week 4)", "Sprint 1 content (Week 6)",
                                              "Sprint 2 content (Week 8)", "QA report (Week 10)", "Launch readiness (Week 12)"])]
    return b


def guides(c):
    if not c["bp"]:
        return _need(c, "the Blueprint")
    bp, pr = c["bp"], c["pr"]
    b = [("h", "Learner Guide"), ("p", f"Welcome to {c['p'].name}. Plan on about {pr['weekly_hours']} hours a week for {pr['duration_weeks']} weeks. "
                                        "The Program is online and self-paced: you can stop at any point and pick up where you left off."),
         ("p", "How to finish: " + completion_definition(c["spec"])),
         ("table", ["Course", "What you will learn", "Hours", "Graded work"],
          [[x["course_id"], x["name"], x.get("learner_hours"), ", ".join(a["name"] for a in bp["assessments"] if a["course_id"] == x["course_id"]) or "Practice only"]
           for x in bp["courses"]]),
         ("ul", ["The AI tutor answers questions from the Course content and gives hints, not answers, on graded work.",
                 "Labs save your work between sessions.", "Every graded activity is scored automatically, at least pass or fail.",
                 "Accessibility: all content works with screen readers and keyboard only; videos have captions and transcripts."]),
         ("h", "Instructor and Facilitator Guide"),
         ("p", "This Program is designed to run without an instructor. Where UMGC assigns a facilitator or offers a credit variant, use this guide."),
         ("table", ["Assessment", "Type", "Course", "Grading", "Skills measured"],
          [[a["name"], a["type"], a["course_id"], a.get("auto_grade", "score"), ", ".join(a.get("measured", []))] for a in bp["assessments"]]),
         ("ul", ["Monitor the at-risk report weekly; outreach within 2 business days.", "Tutor guardrails are set at design time; in credit variants the instructor controls AI use.",
                 "Report content issues in the engine; they become tickets reviewed by Magic and, for material changes, UMGC."])]
    return b


def environment(c):
    return [("h", "Delivery environment"), ("p", "Content runs on Magic's hosted delivery platform, launched from UMGC's Brightspace by LTI 1.3 Advantage."),
            ("table", ["Item", "Setting"], [["Launch", "LTI 1.3 Advantage, resource link and deep linking"], ["Identity", "UMGC SSO through the LTI launch (no separate learner password)"],
                                            ["Roster", "Names and Role Provisioning Services"], ["Grades", "Assignment and Grade Services, auto-graded at least pass/fail"],
                                            ["Learning records", "xAPI statements to the learning record store"], ["Progress", "Saved continuously; labs persist between sessions"],
                                            ["Release in production", c["rel"] or "not yet released"], ["Test org", "Brightspace test org with test learner accounts"]])]


def integration(c):
    lti = (c["rels"][-1].data.get("lti_tool_config") if c["rels"] else None) or {}
    b = [("h", "LTI 1.3 tool configuration")]
    b.append(("table", ["Field", "Value"], [[k, ", ".join(v) if isinstance(v, list) else (str(v) if not isinstance(v, dict) else ", ".join(f"{a}={x}" for a, x in v.items()))]
                                             for k, v in lti.items()]) if lti else ("p", "Generated with the first release."))
    b += [("h", "xAPI profile"), ("table", ["Verb", "Object", "When"], [["launched", "Module", "Learner opens a Module"], ["completed", "Module or Program", "Learner finishes"],
                                                                         ["answered", "Item", "Each response, with success"], ["scored", "Assessment", "Score and pass or fail"],
                                                                         ["asked", "Module", "Learner asks the tutor (topic only, no identity)"], ["dropped", "Module", "Inactivity beyond the threshold"]]),
          ("h", "Data flows"), ("ul", ["Brightspace to delivery platform: LTI launch with context and role", "Delivery platform to Brightspace: grades (AGS)",
                                      "Delivery platform to engine: pseudonymous xAPI for analytics", "Engine to delivery platform: released content package and tool configuration"])]
    return b


def testplan(c):
    return [("h", "Scope"), ("p", "Every Module, assessment and interactive asset in the release candidate, on desktop and mobile."),
            ("table", ["Test", "Method", "Owner", "Exit criterion"], [
                ["Content accuracy", "Second-SME review of every asset", "Magic SME (second reviewer)", "No open accuracy finding"],
                ["Instructional QA", "Check against the approved Blueprint and coverage rules", "Instructional Designer", "Five coverage rules pass"],
                ["Accessibility", "Automated WCAG 2.1 AA lint; NVDA, VoiceOver, keyboard-only, 200 percent zoom", "Accessibility Specialist", "No open critical or high defect; limitations have a plan"],
                ["Assessment validity", "Item key and rationale review; pilot statistics after launch", "Instructional Designer", "No item with a wrong or ambiguous key"],
                ["Learner experience", "Full path in the Brightspace test org", "Project Manager", "Learner can finish and is graded"],
                ["AI tutor", "Tutor evaluation set", "AI Governance Lead", "100 percent expected behavior"],
                ["Release standard", "100-point rubric per Course", "Content Quality Owner", "85 or higher, no open critical defect"]])]


def training(c):
    return [("h", "Administrator and facilitator training"), ("ul", ["Launching the Program from Brightspace", "Reading the dashboard and the eight reports",
                                                                     "Responding to at-risk learners", "Raising a content issue and following it to release",
                                                                     "Approving material changes (UMGC roles)", "Tutor guardrails and how to report an AI incident"]),
            ("p", "Format: two 60-minute live sessions, recorded, with this guide and a quick-reference sheet.")]


def readiness(c):
    if not c["rels"]:
        return _need(c, "the first release")
    lr = c["rels"][0].data.get("launch_readiness", {}).get("checklist", [])
    return [("h", "Launch readiness checklist"), ("table", ["Item", "Ready"], [[x["item"], "Yes" if x["ok"] else "No"] for x in lr]),
            ("h", "Release scores"), ("table", ["Course", "Score", "Release"], [[x["course"], x["score"], "Yes" if x["release"] else "No"] for x in (c["qa"] or {}).get("release_scores", [])])]


def monthly(c):
    if not c["met"]:
        return _need(c, "a live release with learner data")
    m = c["met"]
    co = m["completion"]
    from .quality import sample_summary
    with session() as s:
        ss = sample_summary(s, c["p"].id)
    b = [("h", "Learner outcomes"), ("table", ["Measure", "Value", "Target"], [["Completed", f"{co['completed_pct']} percent", ""],
                                                                              ["Completed within 100 percent of expected time", f"{co['within_100_pct']} percent", ""],
                                                                              ["Completed within 150 percent of expected time", f"{co['within_150_pct']} percent", f"{co['target_150_pct']} percent"]]),
         ("h", "Content effectiveness"), ("table", ["Module", "Drop-off", "Time ratio"], [[x["title"], f"{x['dropoff_pct']} percent", x["time_ratio"]]
                                                                                         for x in sorted(m["modules"], key=lambda x: -x["dropoff_pct"])[:8]]),
         ("h", "Tutor"), ("table", ["Topic", "Questions"], [[t["topic"], t["questions"]] for t in m["tutor_clusters"][:8]]),
         ("h", "AI quality sample"), ("p", f"{ss['n']} assets sampled; mean {ss['mean']} of 4; {ss['below']} below standard." if ss else "No samples scored this month."),
         ("h", "Actions"), ("ul", [f"{t['ticket_id']}: {t['signal']} ({t['status']})" for t in c["tickets"][-10:]] or ["No tickets this month."])]
    return b


def sla(c):
    return [("h", "Service levels (RFP Attachment B)"), ("table", ["Measure", "Target", "Result", "Source"], [
        ["Platform availability", "99.5 percent per calendar month", "reported from uptime monitoring", "Delivery platform monitoring"],
        ["Tier 1 response", "within 4 business hours", "reported from the help desk", "Help desk"],
        ["Critical defect resolution", "within 1 business day", f"{sum(1 for t in c['tickets'] if t.get('change_class') == 'routine')} routine tickets this period", "Engine tickets"],
        ["Monthly reports", "within 5 business days of month end", "on time", "Deliverables register"]]),
        ("p", "Availability and help desk figures come from the delivery platform and help desk tools and are entered at month end.")]


def cip(c):
    t = c["tickets"]
    by = Counter(x["trigger"] for x in t)
    return [("h", "Continuous Improvement Plan"), ("p", f"{len(t)} tickets: " + ", ".join(f"{k} {v}" for k, v in by.items()) if t else "No tickets yet."),
            ("table", ["Ticket", "Signal", "Target", "Class", "Status"], [[x["ticket_id"], x["signal"], x.get("target_asset") or "Program", x["change_class"], x["status"]] for x in t]),
            ("ul", ["Quarterly Lightcast refresh of skills and demand", "Event-triggered updates (credential objective changes, tool changes)",
                    "Data-informed updates when thresholds trip (drop-off, item discrimination, time ratio, tutor clusters)"])]


def attd(c):
    if not (c["bp"] and c["sm"]):
        return _need(c, "the skills map and Blueprint")
    bp, pr = c["bp"], c["pr"]
    smes = [m for m in c["team"] if "SME" in m["role"]]
    b = [("h", "1. Syllabi"), ("table", ["Course", "Description", "Learner hours", "Objectives"],
                                [[x["course_id"] + " " + x["name"], f"{len(x['modules'])} Modules", x.get("learner_hours"),
                                  ", ".join(sorted({o for m in x["modules"] for o in m.get("objective_ids", [])}))] for x in bp["courses"]]),
         ("h", "2. Assessments and grading"), ("table", ["Assessment", "Type", "Grading", "Skills measured"],
                                                [[a["name"], a["type"], a.get("auto_grade", "score"), ", ".join(a.get("measured", []))] for a in bp["assessments"]]),
         ("p", f"Pass mark {pr['pass_mark']} percent within {pr['attempts']} attempts."),
         ("h", "3. Subject matter expert credentials"), ("table", ["Name", "Expertise", "Credentials"], [[m["name"], m.get("expertise", ""), m.get("credentials", "")] for m in smes]),
         ("h", "4. Seat time"), ("table", ["Course", "Online", "Reading", "Practice", "Total"],
                                  [[r["name"], r["online"], r["reading"], r["practice"], r["total"]] for r in bp.get("seat_time", {}).get("rows", [])]),
         ("p", f"Total {bp.get('seat_time', {}).get('total_hours')} learner hours. Method: online instruction, reading and practice estimated from word counts, media minutes and task time."),
         ("h", "5. Credential sample"), ("p", f"Aligned industry credential: {pr.get('credential') or 'none named'}. Certificate of completion issued by UMGC."),
         ("h", "6. Effective date"), ("p", f"Effective with release {c['rel'] or '1.0'} (launch)."),
         ("h", "7. Prerequisites"), ("p", "None for entry; Courses are taken in sequence: " + " then ".join(x["course_id"] for x in bp["courses"]) + "."),
         ("h", "8. On-the-job training hours"), ("p", "Not applicable: no OJT component in this Program unless added by Task Order.")]
    return b


BUILDERS = {"pmp": pmp, "schedule": schedule, "guides": guides, "environment": environment, "integration": integration, "testplan": testplan,
            "training": training, "readiness": readiness, "monthly": monthly, "sla": sla, "cip": cip, "attd": attd}


def build(pid, key):
    with session() as s:
        c = _ctx(s, pid)
        blocks = BUILDERS[key](c)
        title = DOCS[key][0]
        return {"title": title, "code": DOCS[key][1], "program": c["p"].name, "blocks": blocks}


def to_docx(doc) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor
    d = Document()
    st = d.styles["Normal"]
    st.font.name = "Poppins"
    st.font.size = Pt(10.5)
    h = d.add_heading(f"{doc['title']}", 0)
    for r in h.runs:
        r.font.color.rgb = RGBColor(0x44, 0x04, 0xBF)
    d.add_paragraph(f"{doc['program']}  |  Deliverable {doc['code']}  |  Generated by the UMGC Skills Content Engine (draft for review)")
    for b in doc["blocks"]:
        if b[0] == "h":
            hh = d.add_heading(b[1], 1)
            for r in hh.runs:
                r.font.color.rgb = RGBColor(0x44, 0x04, 0xBF)
        elif b[0] == "p":
            d.add_paragraph(b[1])
        elif b[0] == "ul":
            for it in b[1]:
                d.add_paragraph(str(it), style="List Bullet")
        elif b[0] == "table":
            t = d.add_table(rows=1, cols=len(b[1]))
            t.style = "Light Grid Accent 1"
            for i, x in enumerate(b[1]):
                t.rows[0].cells[i].text = str(x)
            for row in b[2] or [["none"] + [""] * (len(b[1]) - 1)]:
                cells = t.add_row().cells
                for i, x in enumerate(row):
                    cells[i].text = "" if x is None else str(x)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
