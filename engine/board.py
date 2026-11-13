"""The workflow board: Figure 19 of the Technical Proposal (and the AI-enabled workflow diagram), four lanes by
eight stages, with the live status of the Program written into each cell."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from .db import Gate, Statement, get_record, list_records

BOARD = [
    {"key": "define", "n": 1, "name": "Define skills", "timing": "W1-2", "refs": "Att. C 5.1.3 | II.4.3",
     "umgc": ("UMGC sets the target", "Program lead names the role, learners and pathway. Gate 1 approves the skills map.", ["magic.employers", "umgc.skills_map"]),
     "pod": ("Skills mapping", "SMEs approve every skill statement; three or more Maryland employers validate relevance and level."),
     "ai": ("AI skills extraction", "Lightcast LOT occupation, Job Postings Analytics demand, extract/trace on every ingested source, one Lightcast ID per skill."),
     "lp": ("Brightspace sandbox", "Sandbox access confirmed for the build.")},
    {"key": "design", "n": 2, "name": "Design Blueprint", "timing": "W1-2", "refs": "Att. C 5.1.4, 5.2.1",
     "umgc": ("Gate 2: Blueprint approved", "Completion definition, assessment strategy and Attachment D plan signed off in 5 business days.", ["umgc.blueprint"]),
     "pod": ("Learning architect designs", "Outcomes, Module map, assessments and rubrics, accessibility plan, traceability matrix."),
     "ai": ("AI design assistant", "Drafts objectives, sequence and assessment allocation; checks the five coverage rules and the prerequisite DAG."),
     "lp": ("Seat-time map", "Seat-time map and completion definition set.")},
    {"key": "build", "n": 3, "name": "Build with AI", "timing": "W3-8", "refs": "Att. C 5.2 | TP 2.3.3.1",
     "umgc": ("UMGC review windows", "Prototype Module, then Sprint 1 and Sprint 2 content, 5 business days each.", ["umgc.prototype", "umgc.sprint1", "umgc.sprint2"]),
     "pod": ("Authoring zone and validation gates", "Magic SMEs and designers write and revise in the authoring zone; five gates on every asset. Nothing ships without sign-off."),
     "ai": ("AI-assisted drafting", "Lessons, scenarios, item banks, rubrics, alt text from the approved source pack with versioned prompts."),
     "lp": ("Build in sandbox", "Build in the Brightspace sandbox, LTI 1.3.")},
    {"key": "qa", "n": 4, "name": "Assure quality", "timing": "W9-10", "refs": "Att. C 5.4 | Att. A 8",
     "umgc": ("QA report accepted", "UMGC reviews the QA report, release scores and defect log; may run its own accessibility test.", ["umgc.qa"]),
     "pod": ("QA and accessibility tests", "Screen reader and keyboard testing, second-SME accuracy, learner experience testing."),
     "ai": ("AI quality checks", "WCAG lint, readability, terminology, bias screen, Lightcast trace of built text, 100-point release score."),
     "lp": ("Test accounts", "Test accounts run the full learner path.")},
    {"key": "deliver", "n": 5, "name": "Deliver", "timing": "W11-12", "refs": "Att. C 5.5 | Att. A 1",
     "umgc": ("Launch approval", "Launch readiness report, administrator training and the credit evaluation packet are delivered.", ["umgc.launch"]),
     "pod": ("Deploy and launch", "Production release of the approved content version; support goes live."),
     "ai": ("AI services switched on", "Tutor evaluation set passes before deployment; tutor grounded only in the released Course version."),
     "lp": ("Live in Brightspace", "LTI 1.3 launch, SSO, roster sync (NRPS), grade passback (AGS).")},
    {"key": "learn", "n": 6, "name": "Learn", "timing": "Live", "refs": "TP 2.3.3.3 | II.3",
     "umgc": ("Instructor in the loop", "Guardrails set at design time; instructors control AI in credit variants.", []),
     "pod": ("Learner support", "Tier 1 and 2 help, at-risk outreach and escalation within service levels."),
     "ai": ("AI tutor and adaptive practice", "Answers grounded in the Course, hints instead of graded answers, next steps."),
     "lp": ("Working adult learner", "Learns on any device; stops and resumes where they left off.")},
    {"key": "measure", "n": 7, "name": "Measure", "timing": "Monthly", "refs": "II.4.4 | Att. A 2",
     "umgc": ("UMGC dashboards", "Engagement, progress, completion at 100 and 150 percent of expected time.", []),
     "pod": ("Monthly performance review", "Performance and service-level reports; Continuous Improvement Plan."),
     "ai": ("AI learning analytics", "At-risk learners, weak items, Module drop-off, common tutor questions."),
     "lp": ("Learning record store", "xAPI for every start, attempt, score and minute on task.")},
    {"key": "improve", "n": 8, "name": "Improve", "timing": "Quarterly and on triggers", "refs": "TP 2.3.3.2",
     "umgc": ("UMGC approves changes", "Material changes approved before release; change log and release notes.", []),
     "pod": ("SME-approved updates", "Maintenance releases pass the same five validation gates as new builds."),
     "ai": ("AI refresh signals", "Quarterly Lightcast refresh, event triggers and learner-data thresholds draft update tickets."),
     "lp": ("Updates reach learners", "Version-controlled releases, no relaunch.")},
]

GOVERNANCE = ["Grounded only in UMGC-approved course content", "Prompt and model versions logged and tested", "Every human approval recorded for audit",
              "Accuracy, bias and accessibility evaluations", "FERPA-compliant handling of learner data", "Portable content: SCORM, xAPI and QTI packages"]


def live_status(s, pid, p):
    order = [b["key"] for b in BOARD]
    cur = order.index(p.stage) if p.stage in order else 0
    gates = {g.gate_key: g for g in s.execute(select(Gate).where(Gate.program_id == pid)).scalars()}
    sm = get_record(s, pid, "skills_map")
    bp = get_record(s, pid, "blueprint")
    qa = get_record(s, pid, "qa_report")
    assets = [r.data for r in list_records(s, pid, "asset")]
    rels = list_records(s, pid, "release")
    tickets = [t.data for t in list_records(s, pid, "ticket")]
    st = Counter(a.get("state") for a in assets)
    vpend = Counter(g.evidence.get("gate") for g in gates.values() if g.kind == "validation" and g.status == "pending")
    out = {}
    for i, b in enumerate(BOARD):
        state = "done" if i < cur else ("cur" if i == cur else "todo")
        if p.release_version and b["key"] in ("define", "design", "build", "qa", "deliver"):
            state = "done"
        if p.release_version and b["key"] in ("learn", "measure", "improve"):
            state = "cur" if b["key"] == p.stage else "done"
        short = {"magic.employers": "Employer validation", "umgc.skills_map": "Gate 1", "umgc.blueprint": "Gate 2", "umgc.prototype": "Prototype",
                 "umgc.sprint1": "Sprint 1 window", "umgc.sprint2": "Sprint 2 window", "umgc.qa": "QA report", "umgc.launch": "Launch"}
        g_list = [(short.get(k, k), gates[k].status) for k in b["umgc"][2] if k in gates]
        cell = {"state": state, "gates": g_list, "ai": "", "pod": "", "lp": ""}
        if b["key"] == "define" and sm:
            d = sm.data
            cell["ai"] = f"{len(d['skills'])} skills, {sum(1 for k in d['skills'] if k.get('resolution_status') == 'resolved')} resolved; taxonomy {d.get('taxonomy_version')}"
            cell["pod"] = f"{len(d.get('employers', []))} employers recorded"
        if b["key"] == "design" and bp:
            d = bp.data
            cell["ai"] = f"{sum(1 for r in d['coverage']['rules'] if r['passed'])}/5 coverage rules; DAG {'ok' if d['dag']['acyclic'] else 'cycle'}"
            cell["lp"] = f"{d['seat_time']['total_hours']} learner hours"
        if b["key"] == "build" and assets:
            cell["ai"] = f"{len(assets)} assets; {st.get('queued', 0) + st.get('drafted', 0)} drafting, {st.get('checked', 0)} in checks"
            cell["pod"] = f"{st.get('validated', 0)} validated; awaiting " + (", ".join(f"G{k}:{v}" for k, v in sorted(vpend.items())) or "none")
        if b["key"] == "qa" and qa:
            d = qa.data["summary"]
            cell["ai"] = f"{d['courses_releasable']}/{d['courses']} Courses release-ready; {d['links_evidenced']}/{d['links']} skill links evidenced"
        if b["key"] == "deliver" and rels:
            last = sorted(rels, key=lambda r: [int(x) for x in r.key.split(".")])[-1]
            ev = last.data.get("tutor_evaluation") or {}
            cell["ai"] = f"Release {last.key} ({last.status})" + (f"; tutor eval {'passed' if ev.get('passed') else 'failed'}" if ev else "")
        if b["key"] == "learn" and p.release_version:
            n = s.query(Statement).filter(Statement.program_id == pid).count()
            cell["lp"] = f"{n} xAPI statements"
        if b["key"] == "measure" and p.release_version:
            m = get_record(s, pid, "metrics", p.release_version)
            if m:
                c = m.data["completion"]
                cell["umgc"] = f"{c['within_150_pct']} percent complete within 150 percent (target {c['target_150_pct']})"
        if b["key"] == "improve" and tickets:
            mat = [t for t in tickets if (t.get("decision") or {}).get("role") == "UMGC" and t.get("change_class") == "material"]
            cell["umgc"] = f"{len(mat)} material change(s) approved by UMGC"
            rel = [t for t in tickets if t["status"].startswith("released")]
            cell["pod"] = f"{len(rel)} ticket(s) released through the five gates"
            c = Counter(t["status"].split(" in ")[0] if t["status"].startswith("released") else t["status"] for t in tickets)
            cell["ai"] = ", ".join(f"{v} {k}" for k, v in c.items())
        out[b["key"]] = cell
    return out
