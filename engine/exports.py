"""Downloads: the traceability workbook (Exhibit I shape plus build, QA and provenance sheets), the JSON trace,
and the release content package (manifest, HTML per Module, QTI 2.1 items, LTI config, provenance log)."""
from __future__ import annotations

import html
import io
import json
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .db import ApiCall, Gate, Program, get_record, list_records, session

PURPLE = "4404BF"


SHEET_NOTES: dict = {}   # sheet title -> one-line description, collected for the Data_Dictionary sheet


def _blank(v):
    return v is None or v == "" or v == [] or v == {}


def _sheet(wb, title, header, rows, blank=None, empty="No rows yet.", note=""):
    """Every cell is filled: a blank becomes an explicit status (per column if given), and an empty sheet carries a
    row saying why it is empty and when it fills."""
    ws = wb.create_sheet(title[:31])
    SHEET_NOTES[title[:31]] = (note, header)
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=PURPLE)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    blank = blank or {}
    if not rows:
        ws.append([f"No rows yet: {empty}"] + ["not applicable until rows exist"] * (len(header) - 1))
        ws.cell(2, 1).font = Font(italic=True, color=PURPLE)
    for r in rows:
        out = []
        for h, v in zip(header, list(r) + [None] * (len(header) - len(r))):
            if _blank(v):
                v = blank.get(h, blank.get("*", "not recorded"))
            out.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
        ws.append(out)
    for i, h in enumerate(header, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, max(12, len(str(h)) + 4))
    ws.freeze_panes = "A2"
    return ws


def _j(v):
    return ", ".join(v) if isinstance(v, list) else v


def workbook(program_id: int) -> bytes:
    from .spec import get_spec, total_hours, completion_definition
    from .stages.qa import DIMENSIONS
    SHEET_NOTES.clear()
    with session() as s:
        p = s.get(Program, program_id)
        sm = (get_record(s, program_id, "skills_map") or None)
        bp = (get_record(s, program_id, "blueprint") or None)
        qa = (get_record(s, program_id, "qa_report") or None)
        assets = [r.data for r in list_records(s, program_id, "asset")]
        tickets = [r.data for r in list_records(s, program_id, "ticket")]
        rels = [r.data for r in list_records(s, program_id, "release")]
        srcs = [r.data for r in list_records(s, program_id, "source")]
        brief_srcs = p.brief.get("sources", [])
        gates = list(s.query(Gate).filter(Gate.program_id == program_id).order_by(Gate.id))
        calls = list(s.query(ApiCall).filter(ApiCall.program_id == program_id).order_by(ApiCall.id).limit(5000))
        spec = get_spec(s, program_id)
        olr = get_record(s, program_id, "outline")
        from .outline import weekly_map
        wmap = weekly_map(s, program_id) or []
        from .studio import team
        tm_people = team(s, program_id)["members"]
        name = p.name
        stage = p.stage
    sm = sm.data if sm else {"skills": [], "competencies": [], "occupation": {}}
    bp = bp.data if bp else {}
    demand_blank = "pending: Lightcast Job Postings Analytics not licensed" if sm.get("demand_mode") == "not licensed" else "no postings signal returned"
    later = lambda what: f"fills at {what} (Program is at stage: {stage})"
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    pr = spec["program"]
    for row in [["UMGC Skills Content Engine: traceability workbook"], ["Program", name],
                ["Task Order", spec["task_order"].get("number")],
                ["Duration", f"{pr['duration_weeks']} weeks at {pr['weekly_hours']} learner hours a week ({total_hours(spec)} hours), {pr['courses']} Courses"],
                ["Completion definition", completion_definition(spec)],
                ["Taxonomy", f"Lightcast Open Skills {sm.get('taxonomy_version') or 'not yet retrieved'} (Lightcast {sm.get('lightcast_mode') or 'not yet called'}, demand {sm.get('demand_mode') or 'not yet called'})"],
                ["Retrieval date", sm.get("retrieval_date") or "not yet retrieved"],
                ["Downward trace", "Occupation > Skills (ranked by demand) > Competencies > Objectives > Courses and Modules > Assessments > Credential"],
                ["Upward trace", "Assessment item > objective > competency > Lightcast skill ID > postings > occupation"],
                ["Blank cells", "There are none. A cell that has no value yet says why (for example 'not yet reached' or 'pending decision'). See Data_Dictionary."],
                ["Mock data", "Any ID or demand figure beginning MOCK comes from the offline stand-in and is replaced by live Lightcast calls at Discovery."]]:
        ws.append(row)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 120
    _sheet(wb, "Program_Spec", ["field", "value"],
           [[k, v] for k, v in spec["task_order"].items()] + [[k, json.dumps(v) if isinstance(v, (dict, list)) else v] for k, v in pr.items()]
           + [["learner_attributes", ", ".join(spec.get("attributes", []))]],
           blank={"*": "not set in the Task Order"}, note="Task Order and Program specification (RFP Attachment C 1 to 4)")
    o = sm.get("occupation", {})
    _sheet(wb, "Occupation", ["title", "lot_occupation_id", "lot_occupation_name", "lot_specialized_occupation_name", "soc_crosswalk", "geography",
                              "experience_band", "postings_md_total", "postings_us_total", "source"],
           [[o.get("title") or pr.get("occupation"), o.get("lot_occupation_id"), o.get("lot_occupation_name") or pr.get("lot_occupation"),
             o.get("lot_specialized_occupation_name") or pr.get("lot_specialized"), o.get("soc_crosswalk"), o.get("geography") or "Maryland and US",
             o.get("experience_band"), sm.get("demand_total_md"), sm.get("demand_total_us"), o.get("lot_source")]],
           blank={"soc_crosswalk": "not required: Lightcast LOT is the occupation taxonomy", "experience_band": "entry level (0 to 2 years) unless set",
                  "postings_md_total": demand_blank, "postings_us_total": demand_blank, "*": later("Stage 1 Define")},
           note="Target occupation in the Lightcast Occupation Taxonomy (LOT)")
    srows = [[x.get("id"), x.get("title"), x.get("type"), "Task Order brief", "approved (brief)", len(x.get("text", "")), "", "", ""] for x in brief_srcs]
    srows += [[x.get("source_id"), x.get("title"), x.get("source_type"), x.get("uploaded_by"), x.get("status"), x.get("chars"),
               len(x.get("skills", [])), x.get("license"), (x.get("decision") or {}).get("by")] for x in srcs]
    _sheet(wb, "Sources", ["source_id", "title", "type", "added_by", "status", "characters", "lightcast_skills_found", "license_note", "approved_by"], srows,
           blank={"lightcast_skills_found": "counted in the skills map extraction log", "license_note": "UMGC-owned or public", "approved_by": "pending SME approval",
                  "*": "not recorded"}, empty="no sources yet; upload or paste in Skills Studio > Sources.", note="Source pack used for Lightcast extraction and AI grounding")
    tm = bp.get("trace_matrix", {})
    _sheet(wb, "Skills", ["skill_id", "lightcast_skill_id", "lightcast_skill_name", "skill_type", "category", "subcategory", "statement", "competency_id",
                          "tier", "tier_from_score", "exit_level", "unique_postings_md", "unique_postings_us", "postings_share_pct", "significance",
                          "demand_norm", "credential_weight", "sme_criticality", "entry_level_fit", "priority_score", "resolution_status",
                          "resolution_score", "extraction_confidence", "source_spans", "taught_in", "times_assessed", "taxonomy_version", "retrieval_date"],
           [[k.get("skill_id"), k.get("lightcast_skill_id"), k.get("lightcast_skill_name") or k.get("name"), k.get("skill_type"), k.get("category"), k.get("subcategory"),
             k.get("statement"), k.get("competency_id"), k.get("tier"), k.get("tier_from_score"), k.get("exit_level"), k.get("unique_postings_md"),
             k.get("unique_postings_us"), k.get("postings_share_pct"), k.get("significance"), k.get("demand_norm"), k.get("credential_weight"),
             k.get("sme_criticality"), k.get("entry_level_fit"), k.get("priority_score"), k.get("resolution_status"), k.get("resolution_score"),
             k.get("extraction_confidence"), "; ".join(f"{x['source_id']}[{x['start']}:{x['end']}] '{x['surface_form']}'" for x in k.get("source_spans", [])),
             _j(tm.get(k["skill_id"], {}).get("taught_in", [])), tm.get(k["skill_id"], {}).get("times_assessed"), k.get("taxonomy_version"),
             k.get("retrieval_date")] for k in sm["skills"]],
           blank={"lightcast_skill_id": "unresolved: SME to pick from alternatives", "skill_type": "not returned by Lightcast", "category": "not returned by Lightcast",
                  "subcategory": "not returned by Lightcast", "statement": "SME to write", "unique_postings_md": demand_blank, "unique_postings_us": demand_blank,
                  "postings_share_pct": demand_blank, "significance": demand_blank, "demand_norm": "demand pending; score reweighted to SME, credential, entry fit",
                  "credential_weight": "3 (default: not in exam objectives)", "resolution_score": "set by reviewer", "extraction_confidence": "added by SME (not extracted from sources)",
                  "source_spans": "SME justification: added by reviewer", "taught_in": later("Stage 2 Design") if not bp else "not yet in a Module",
                  "times_assessed": later("Stage 2 Design") if not bp else 0, "*": "not recorded"},
           empty=later("Stage 1 Define"), note="Skills map: one Lightcast skill ID per skill, with demand, score and tier")
    _sheet(wb, "Competencies", ["competency_id", "name", "definition", "exit_level", "performance_criteria", "evidence", "skill_ids"],
           [[c.get("competency_id"), c.get("name"), c.get("definition"), c.get("exit_level"), c.get("performance_criteria"), c.get("evidence"),
             _j(c.get("skill_ids"))] for c in sm.get("competencies", [])],
           blank={"definition": "LXA to write", "performance_criteria": "LXA to write", "evidence": "assessments mapped in Trace_Matrix", "skill_ids": "no skills assigned yet"},
           empty=later("Stage 1 Define"), note="Competencies grouping the skills, with exit level and evidence")
    _sheet(wb, "Objectives", ["objective_id", "level", "statement", "bloom_level", "competency_id", "skill_ids", "module_ids"],
           [[x["objective_id"], x.get("level"), x["statement"], x.get("bloom_level"), x.get("competency_id"), _j(x.get("skill_ids")), _j(x.get("module_ids"))]
            for x in bp.get("objectives", [])],
           blank={"level": "module objective", "competency_id": "not mapped: ID to set", "skill_ids": "not mapped: ID to set", "module_ids": "not yet placed in a Module"},
           empty=later("Stage 2 Design"), note="Learning objectives mapped to skills, competencies and Modules")
    rows = []
    for c in bp.get("courses", []):
        rows.append([c["course_id"], c["name"], c.get("learner_hours"), c.get("sprint"), "(Course row)", "(Course row)", "Course", "(see Modules)", "(see Modules)",
                     _j(c.get("assessment_ids"))])
        for m in c["modules"]:
            rows.append([c["course_id"], c["name"], "(see Course row)", c.get("sprint"), m["module_id"], m["title"], m.get("type"), _j(m.get("objective_ids")),
                         _j(m.get("skill_ids")), _j(m.get("assessment_ids"))])
    _sheet(wb, "Courses_Modules", ["course_id", "course_name", "learner_hours", "sprint", "module_id", "module_title", "module_type", "objective_ids",
                                   "skill_ids", "assessment_ids"], rows,
           blank={"objective_ids": "none mapped", "skill_ids": "none mapped", "assessment_ids": "no graded assessment in this Module", "*": "not set"},
           empty=later("Stage 2 Design"), note="Program > Course > Module structure")
    orows = (olr.data["rows"] if olr else [])
    _sheet(wb, "Outline", ["week", "module_id", "title", "objective_ids", "objective_verbs", "skill_ids", "skill_verbs", "alignment", "screens", "offline_reading",
                           "reading_min", "practice", "practice_min", "online_min", "assessments"],
           [[r.get("week"), r["module_id"], r.get("title"), r.get("objective_ids"), r.get("objective_verbs"), r.get("skill_ids"), r.get("skill_verbs"), r.get("alignment"),
             str(r.get("screens") or "").replace("\n", " | "), str(r.get("reading") or "").replace("\n", " | "), r.get("reading_min"),
             str(r.get("practice") or "").replace("\n", " | "), r.get("practice_min"), r.get("online_min"), r.get("assessments")] for r in orows],
           blank={"assessments": "practice only (no graded assessment)", "objective_verbs": "no objective mapped", "skill_verbs": "no skill mapped", "*": 0},
           empty=later("the Module outline (after Gate 2)"), note="Module outline: objectives and verbs mapped to Lightcast skills, screens, offline reading, practice")
    wrows = []
    for w in wmap:
        for m in w["modules"]:
            wrows.append([w["week"], "Online Module", f"M-{m['module_id']} {m['title']}", m.get("online_min"), m["state"], m["authors"]])
        for mid, x in w["reading"]:
            wrows.append([w["week"], "Offline reading", f"{mid}: {x}", "", "planned", "Magic (outline)"])
        for mid, x in w["practice"]:
            wrows.append([w["week"], "Practice", f"{mid}: {x}", "", "planned", "Magic (outline)"])
        for a in w["assessments"]:
            wrows.append([w["week"], "Assessment", f"A-{a['assessment_id']} {a['name']}", a["minutes"], a["state"], "Magic"])
        wrows.append([w["week"], "Week total", f"{round(w['total'] / 60, 1)} h of {round(w['target'] / 60, 1)} h planned ({w['fit']})", w["total"], f"{w['built']}/{w['pieces']} validated", ""])
    _sheet(wb, "Weekly_Map", ["week", "kind", "item", "minutes", "state", "put_together_by"], wrows,
           blank={"minutes": "within the Module time", "*": "not recorded"}, empty=later("the Module outline (after Gate 2)"),
           note="Everything in each week: online Modules, offline reading, practice, assessments, against weekly learner hours")
    _sheet(wb, "Assessments", ["assessment_id", "name", "type", "course_id", "auto_grade", "persistent_state", "items_count", "rubric_rows", "objective_ids",
                               "skill_ids_measured", "skill_ids_practiced", "performance_task"],
           [[a["assessment_id"], a["name"], a["type"], a["course_id"], a.get("auto_grade"), "Y" if a.get("persistent_state") else "N", a.get("items_count"),
             a.get("rubric_rows"), _j(a.get("objective_ids")), _j(a.get("measured")), _j(a.get("practiced")), "Y" if a.get("performance_task") else "N"]
            for a in bp.get("assessments", [])],
           blank={"rubric_rows": 0, "objective_ids": "none mapped", "skill_ids_measured": "none (practice only)", "skill_ids_practiced": "none", "*": "not set"},
           empty=later("Stage 2 Design"), note="Assessments with grading mode, persistence and skills measured")
    _sheet(wb, "Prerequisites", ["from_id", "to_id", "relation_type", "rationale"],
           [[x["from_id"], x["to_id"], x["relation_type"], x.get("rationale")] for x in bp.get("prerequisites", [])],
           blank={"rationale": "sequence set in the Blueprint"}, empty=later("Stage 2 Design"), note="Prerequisite graph (checked acyclic)")
    st = bp.get("seat_time", {}).get("rows", [])
    _sheet(wb, "Seat_Time", ["course_id", "course", "online_hours", "reading_hours", "practice_hours", "total_hours", "method"],
           [[r["course_id"], r["name"], r["online"], r["reading"], r["practice"], r["total"],
             "Learner engagement hours: online instruction + reading + practice, estimated from word counts, media minutes and task time (COMAR seat-time map)"] for r in st],
           blank={"*": 0}, empty=later("Stage 2 Design"), note="Seat-time map and method")
    aids = [a["assessment_id"] for a in bp.get("assessments", [])]
    _sheet(wb, "Trace_Matrix", ["skill_id", "tier"] + aids + ["times_assessed", "times_practiced", "performance_task", "coverage_met"],
           [[k["skill_id"], k.get("tier")] + [tm.get(k["skill_id"], {}).get("cells", {}).get(a) or "-" for a in aids]
            + [tm.get(k["skill_id"], {}).get("times_assessed", 0), tm.get(k["skill_id"], {}).get("times_practiced", 0),
               "Y" if tm.get(k["skill_id"], {}).get("assessed_in_performance_task") else "N",
               "N" if any(k["skill_id"] in r["failures"] for r in bp.get("coverage", {}).get("rules", [])) else "Y"] for k in sm["skills"]] if bp else [],
           blank={"*": "-"}, empty=later("Stage 2 Design"), note="Skill by assessment: A assessed, P practiced, - not in this assessment")
    _sheet(wb, "Coverage_Rules", ["rule", "passed", "failures"],
           [[r["rule"], "Y" if r["passed"] else "N", _j(r["failures"]) or "none"] for r in bp.get("coverage", {}).get("rules", [])],
           empty=later("Stage 2 Design"), note="The five coverage rules")
    _sheet(wb, "Credentials", ["credential", "issuer", "skills_aligned", "evidence"],
           [[c.get("name") or c.get("credential"), c.get("issuer"), _j(c.get("skill_ids")) or "all Core skills", c.get("evidence")] for c in bp.get("credentials", [])]
           or ([[pr.get("credential"), "named in the Task Order", "all Core skills", "practice exam and capstone"]] if pr.get("credential") else []),
           blank={"issuer": "named in the Task Order", "evidence": "practice exam and capstone"}, empty="no industry credential named in the Program specification.",
           note="Industry credential alignment")
    arows = []
    for a in assets:
        ch = a.get("checks", {}).get(str(a["revision"]), {})
        tr = ch.get("skills_trace", {})
        arows.append([a["asset_id"], a["title"], a["asset_type"], a["course_id"], a.get("sprint"), a["revision"], a.get("state"),
                      _j(a["skill_ids"]), _j(tr.get("declared_evidenced", [])), _j(tr.get("declared_not_evidenced", [])) or "none",
                      _j(tr.get("evidenced_not_declared", [])) or "none", ch.get("reading_level", {}).get("grade"),
                      sum(1 for f in ch.get("findings", []) if f["severity"] in ("critical", "high"))])
    _sheet(wb, "Declared_vs_Observed", ["asset", "title", "type", "course_id", "sprint", "revision", "state", "declared_skills", "declared_and_evidenced",
                                        "declared_not_evidenced", "evidenced_not_declared", "reading_grade", "critical_high_findings"], arows,
           blank={"declared_and_evidenced": "checks not yet run", "reading_grade": "checks not yet run", "*": "not recorded"},
           empty=later("Stage 3 Build"), note="Declared skills against skills Lightcast finds in the built text")
    q = qa.data if qa else {}
    _sheet(wb, "Release_Scores", ["course_id", "course", "score", "open_critical", "release"] + [d for d, _ in DIMENSIONS],
           [[x["course_id"], x["course"], x["score"], x["open_critical"], "Y" if x["release"] else "N"] + [d["points"] for d in x["dimensions"]]
            for x in q.get("release_scores", [])], empty=later("Stage 4 Assure quality"), note="100-point release standard per Course")
    _sheet(wb, "Defect_Log", ["asset", "revision", "check", "severity", "gate", "message", "status"],
           [[d["asset"], d["revision"], d["check"], d["severity"], d["gate"], d["message"], d["status"]] for d in q.get("defects", [])],
           empty=later("Stage 4 Assure quality") if not qa else "no findings were raised.", note="Defect resolution log")
    prow = []
    for a in assets:
        for rev, pv in sorted(a.get("provenance", {}).items(), key=lambda t: int(t[0])):
            g = pv.get("gates", {})
            prow.append([a["asset_id"], int(rev), pv.get("ai_contribution"), pv.get("model"), pv.get("prompt_id"), pv.get("prompt_version"),
                         pv.get("source_pack_version"), pv.get("taxonomy_version"), pv.get("drafted_at"), "; ".join(pv.get("feedback_applied", []))[:500]]
                        + [f"{g[k]['decision']} by {g[k]['by']} {g[k]['at']}" if k in g else "" for k in ("g1", "g2", "g3", "g4", "g5")])
    gb = "not yet reached"
    _sheet(wb, "Provenance", ["asset", "revision", "ai_contribution", "model", "prompt_id", "prompt_version", "source_pack_version", "taxonomy_version",
                              "drafted_at", "feedback_applied", "gate1_sme", "gate2_instructional", "gate3_accessibility", "gate4_bias", "gate5_editorial"], prow,
           blank={"prompt_version": "template (no prompt)", "feedback_applied": "first draft", "gate1_sme": gb, "gate2_instructional": gb, "gate3_accessibility": gb,
                  "gate4_bias": gb, "gate5_editorial": gb, "*": "not recorded"}, empty=later("Stage 3 Build"), note="Human and AI contribution per asset revision")
    _sheet(wb, "Gates", ["gate", "kind", "owner_role", "status", "decided_by", "note", "opened_at", "decided_at"],
           [[g.label, g.kind, g.owner_role, g.status, g.decided_by, g.note, str(g.opened_at)[:19], str(g.decided_at or "")[:19]] for g in gates],
           blank={"decided_by": "pending decision", "note": "no note", "decided_at": "pending decision"}, empty=later("the first gate"), note="Every gate decision")
    _sheet(wb, "Tickets", ["ticket", "trigger", "signal", "detail", "target_asset", "ai_proposed_response", "change_class", "status", "decided_by", "release"],
           [[t["ticket_id"], t["trigger"], t["signal"], t["detail"], t["target_asset"], t["ai_proposed_response"], t["change_class"], t["status"],
             (t.get("decision") or {}).get("by"), t.get("release")] for t in tickets],
           blank={"target_asset": "Program-level", "decided_by": "pending decision", "*": "not recorded"}, empty=later("Stage 8 Improve"),
           note="Improvement tickets: periodic, event-triggered, data-informed")
    _sheet(wb, "Releases", ["version", "created_at", "change_class", "assets", "changed_assets", "tickets"],
           [[r["version"], r["created_at"], r.get("change_class"), len(r["manifest"]), _j(r.get("changed_assets", [])) or "all (initial release)",
             _j(r.get("tickets", [])) or "none"] for r in rels], empty=later("Stage 5 Deliver"), note="Numbered releases")
    _sheet(wb, "Team_SMEs", ["name", "role", "organization", "expertise", "credentials", "courses"],
           [[m.get("name"), m.get("role"), m.get("org"), m.get("expertise"), m.get("credentials"), _j(m.get("courses"))] for m in tm_people],
           blank={"credentials": "to be provided with the Attachment D packet", "courses": "all Courses", "*": "not recorded"},
           note="Program team; Magic SMEs are vendor-provided")
    _sheet(wb, "API_Calls", ["at", "service", "mode", "method", "path", "purpose", "status", "ms"],
           [[str(c.at)[:19], c.service, c.mode, c.method, c.path, c.purpose, c.status, c.ms] for c in calls],
           empty="no external calls yet.", note="Every Lightcast and Claude call")
    dd = wb.create_sheet("Data_Dictionary")
    dd.append(["sheet", "what it holds", "columns"])
    for c in dd[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=PURPLE)
    for t, (nt, hdr) in SHEET_NOTES.items():
        dd.append([t, nt or t, ", ".join(hdr)])
    dd.column_dimensions["A"].width = 24
    dd.column_dimensions["B"].width = 60
    dd.column_dimensions["C"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def trace_json(program_id: int) -> bytes:
    with session() as s:
        sm = get_record(s, program_id, "skills_map")
        bp = get_record(s, program_id, "blueprint")
        qa = get_record(s, program_id, "qa_report")
        out = {"skills_map": sm.data if sm else None, "blueprint": bp.data if bp else None,
               "qa": {k: v for k, v in (qa.data if qa else {}).items() if k != "defects"}}
    return json.dumps(out, indent=1, default=str).encode()


def _module_html(a, c):
    parts = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(a['title'])}</title></head><body><main>",
             f"<h1>{html.escape(a['title'])}</h1>"]
    for sc in c.get("screens", []):
        parts.append(f"<section><h2>{html.escape(sc.get('heading', ''))}</h2>")
        for b in sc.get("blocks", []):
            parts.append(f"<p>{html.escape(b.get('text', ''))} <small>[{', '.join(b.get('citations', []))}]</small></p>")
        if sc.get("media"):
            parts.append(f"<figure role='img' aria-label='{html.escape(sc['media'].get('alt', ''))}'><figcaption>{html.escape(sc['media'].get('alt', ''))}</figcaption></figure>")
        parts.append("</section>")
    for pr in c.get("practice", []):
        parts.append(f"<aside><h3>Practice</h3><p>{html.escape(pr['prompt'])}</p></aside>")
    parts.append("</main></body></html>")
    return "\n".join(parts)


def _qti(a, c):
    out = []
    for it in c.get("items", []):
        choices = "".join(f"<simpleChoice identifier='{o['id']}'>{html.escape(o['text'])}</simpleChoice>" for o in it["options"])
        out.append((it["item_id"], f"""<?xml version="1.0" encoding="UTF-8"?>
<assessmentItem xmlns="http://www.imsglobal.org/xsd/imsqti_v2p1" identifier="{it['item_id']}" title="{it['item_id']}" adaptive="false" timeDependent="false">
  <responseDeclaration identifier="RESPONSE" cardinality="single" baseType="identifier"><correctResponse><value>{it['key']}</value></correctResponse></responseDeclaration>
  <outcomeDeclaration identifier="SCORE" cardinality="single" baseType="float"/>
  <itemBody><choiceInteraction responseIdentifier="RESPONSE" shuffle="true" maxChoices="1"><prompt>{html.escape(it['stem'])}</prompt>{choices}</choiceInteraction></itemBody>
  <responseProcessing template="http://www.imsglobal.org/question/qti_v2p1/rptemplates/match_correct"/>
</assessmentItem>"""))
    return out


def package(program_id: int, version: str) -> bytes:
    with session() as s:
        p = s.get(Program, program_id)
        rel = get_record(s, program_id, "release", version).data
        assets = {r.key: r.data for r in list_records(s, program_id, "asset")}
    buf = io.BytesIO()
    res = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for key, rev in rel["manifest"].items():
            a = assets[key]
            c = a["revisions"].get(str(rev), {})
            if a["asset_type"] == "module":
                z.writestr(f"content/{key}.html", _module_html(a, c))
                res.append((key, f"content/{key}.html", "webcontent"))
            elif c.get("items"):
                for iid, x in _qti(a, c):
                    z.writestr(f"qti/{iid}.xml", x)
                    res.append((iid, f"qti/{iid}.xml", "imsqti_item_xmlv2p1"))
            else:
                z.writestr(f"tasks/{key}.json", json.dumps(c, indent=1))
                res.append((key, f"tasks/{key}.json", "webcontent"))
        man = ["<?xml version='1.0' encoding='UTF-8'?>",
               f"<manifest identifier='UMGC-{program_id}-{version}' xmlns='http://www.imsglobal.org/xsd/imscp_v1p1'>",
               f"<metadata><schema>UMGC Skills Content Engine</schema><schemaversion>{version}</schemaversion></metadata><organizations/><resources>"]
        man += [f"<resource identifier='{html.escape(i)}' type='{t}' href='{h}'><file href='{h}'/></resource>" for i, h, t in res]
        man.append("</resources></manifest>")
        z.writestr("imsmanifest.xml", "\n".join(man))
        z.writestr("lti_tool_config.json", json.dumps(rel["lti_tool_config"], indent=1))
        z.writestr("provenance_log.json", json.dumps(rel["provenance"], indent=1))
        z.writestr("release.json", json.dumps({k: v for k, v in rel.items() if k not in ("provenance",)}, indent=1, default=str))
        z.writestr("README.txt", f"{p.name}\nRelease {version}\nStandards-based package owned by UMGC: HTML5 content, QTI 2.1 items, "
                                 "IMS content package manifest, LTI 1.3 tool configuration, provenance log.\n")
    return buf.getvalue()
