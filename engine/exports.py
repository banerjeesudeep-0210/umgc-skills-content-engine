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


def _sheet(wb, title, header, rows):
    ws = wb.create_sheet(title[:31])
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=PURPLE)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append([json.dumps(v) if isinstance(v, (dict, list)) else v for v in r])
    for i, h in enumerate(header, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, max(12, len(str(h)) + 4))
    ws.freeze_panes = "A2"
    return ws


def _j(v):
    return ", ".join(v) if isinstance(v, list) else v


def workbook(program_id: int) -> bytes:
    with session() as s:
        p = s.get(Program, program_id)
        sm = (get_record(s, program_id, "skills_map") or None)
        bp = (get_record(s, program_id, "blueprint") or None)
        qa = (get_record(s, program_id, "qa_report") or None)
        assets = [r.data for r in list_records(s, program_id, "asset")]
        tickets = [r.data for r in list_records(s, program_id, "ticket")]
        rels = [r.data for r in list_records(s, program_id, "release")]
        gates = list(s.query(Gate).filter(Gate.program_id == program_id).order_by(Gate.id))
        calls = list(s.query(ApiCall).filter(ApiCall.program_id == program_id).order_by(ApiCall.id).limit(5000))
        name = p.name
    sm = sm.data if sm else {"skills": [], "competencies": [], "occupation": {}}
    bp = bp.data if bp else {}
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    for row in [["UMGC Skills Content Engine: traceability workbook"], ["Program", name],
                ["Taxonomy", f"Lightcast Open Skills {sm.get('taxonomy_version', '')} (Lightcast {sm.get('lightcast_mode', '')}, demand {sm.get('demand_mode', '')})"],
                ["Retrieval date", sm.get("retrieval_date", "")],
                ["Downward trace", "Occupation > Skills (ranked by demand) > Competencies > Objectives > Courses and Modules > Assessments > Credential"],
                ["Upward trace", "Assessment item > objective > competency > Lightcast skill ID > postings > occupation"],
                ["Mock data", "Any ID or demand figure beginning MOCK comes from the offline stand-in and is replaced by live Lightcast calls at Discovery."]]:
        ws.append(row)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 120
    o = sm.get("occupation", {})
    _sheet(wb, "Occupation", ["title", "lot_occupation_id", "lot_occupation_name", "lot_specialized_occupation_name", "soc_crosswalk", "geography",
                              "experience_band", "postings_md_total", "postings_us_total", "source"],
           [[o.get("title"), o.get("lot_occupation_id"), o.get("lot_occupation_name"), o.get("lot_specialized_occupation_name"), o.get("soc_crosswalk"),
             o.get("geography"), o.get("experience_band"), sm.get("demand_total_md"), sm.get("demand_total_us"), o.get("lot_source")]])
    tm = bp.get("trace_matrix", {})
    _sheet(wb, "Skills", ["skill_id", "lightcast_skill_id", "lightcast_skill_name", "skill_type", "category", "subcategory", "statement", "competency_id",
                          "tier", "tier_from_score", "exit_level", "unique_postings_md", "unique_postings_us", "postings_share_pct", "significance",
                          "demand_norm", "credential_weight", "sme_criticality", "entry_level_fit", "priority_score", "resolution_status",
                          "resolution_score", "extraction_confidence", "source_spans", "taught_in", "times_assessed", "taxonomy_version", "retrieval_date"],
           [[k.get("skill_id"), k.get("lightcast_skill_id"), k.get("lightcast_skill_name"), k.get("skill_type"), k.get("category"), k.get("subcategory"),
             k.get("statement"), k.get("competency_id"), k.get("tier"), k.get("tier_from_score"), k.get("exit_level"), k.get("unique_postings_md"),
             k.get("unique_postings_us"), k.get("postings_share_pct"), k.get("significance"), k.get("demand_norm"), k.get("credential_weight"),
             k.get("sme_criticality"), k.get("entry_level_fit"), k.get("priority_score"), k.get("resolution_status"), k.get("resolution_score"),
             k.get("extraction_confidence"), "; ".join(f"{x['source_id']}[{x['start']}:{x['end']}] '{x['surface_form']}'" for x in k.get("source_spans", [])),
             _j(tm.get(k["skill_id"], {}).get("taught_in", [])), tm.get(k["skill_id"], {}).get("times_assessed"), k.get("taxonomy_version"),
             k.get("retrieval_date")] for k in sm["skills"]])
    _sheet(wb, "Competencies", ["competency_id", "name", "definition", "exit_level", "performance_criteria", "evidence", "skill_ids"],
           [[c.get("competency_id"), c.get("name"), c.get("definition"), c.get("exit_level"), c.get("performance_criteria"), c.get("evidence"),
             _j(c.get("skill_ids"))] for c in sm.get("competencies", [])])
    _sheet(wb, "Objectives", ["objective_id", "level", "statement", "bloom_level", "competency_id", "skill_ids", "module_ids"],
           [[x["objective_id"], x.get("level"), x["statement"], x.get("bloom_level"), x.get("competency_id"), _j(x.get("skill_ids")), _j(x.get("module_ids"))]
            for x in bp.get("objectives", [])])
    rows = []
    for c in bp.get("courses", []):
        rows.append([c["course_id"], c["name"], c.get("learner_hours"), c.get("sprint"), "", "", "", "", "", _j(c.get("assessment_ids"))])
        for m in c["modules"]:
            rows.append([c["course_id"], "", "", "", m["module_id"], m["title"], m.get("type"), _j(m.get("objective_ids")), _j(m.get("skill_ids")),
                         _j(m.get("assessment_ids"))])
    _sheet(wb, "Courses_Modules", ["course_id", "course_name", "learner_hours", "sprint", "module_id", "module_title", "module_type", "objective_ids",
                                   "skill_ids", "assessment_ids"], rows)
    _sheet(wb, "Assessments", ["assessment_id", "name", "type", "course_id", "auto_grade", "items_count", "rubric_rows", "objective_ids",
                               "skill_ids_measured", "skill_ids_practiced", "performance_task"],
           [[a["assessment_id"], a["name"], a["type"], a["course_id"], a.get("auto_grade"), a.get("items_count"), a.get("rubric_rows"),
             _j(a.get("objective_ids")), _j(a.get("measured")), _j(a.get("practiced")), "Y" if a.get("performance_task") else "N"]
            for a in bp.get("assessments", [])])
    _sheet(wb, "Prerequisites", ["from_id", "to_id", "relation_type", "rationale"],
           [[x["from_id"], x["to_id"], x["relation_type"], x.get("rationale")] for x in bp.get("prerequisites", [])])
    aids = [a["assessment_id"] for a in bp.get("assessments", [])]
    _sheet(wb, "Trace_Matrix", ["skill_id", "tier"] + aids + ["times_assessed", "times_practiced", "performance_task", "coverage_met"],
           [[k["skill_id"], k.get("tier")] + [tm.get(k["skill_id"], {}).get("cells", {}).get(a, "") for a in aids]
            + [tm.get(k["skill_id"], {}).get("times_assessed"), tm.get(k["skill_id"], {}).get("times_practiced"),
               "Y" if tm.get(k["skill_id"], {}).get("assessed_in_performance_task") else "N",
               "N" if any(k["skill_id"] in r["failures"] for r in bp.get("coverage", {}).get("rules", [])) else "Y"] for k in sm["skills"]])
    _sheet(wb, "Coverage_Rules", ["rule", "passed", "failures"],
           [[r["rule"], "Y" if r["passed"] else "N", _j(r["failures"])] for r in bp.get("coverage", {}).get("rules", [])])
    arows = []
    for a in assets:
        ch = a.get("checks", {}).get(str(a["revision"]), {})
        tr = ch.get("skills_trace", {})
        arows.append([a["asset_id"], a["title"], a["asset_type"], a["course_id"], a.get("sprint"), a["revision"], a.get("state"),
                      _j(a["skill_ids"]), _j(tr.get("declared_evidenced", [])), _j(tr.get("declared_not_evidenced", [])),
                      _j(tr.get("evidenced_not_declared", [])), ch.get("reading_level", {}).get("grade"),
                      sum(1 for f in ch.get("findings", []) if f["severity"] in ("critical", "high"))])
    _sheet(wb, "Declared_vs_Observed", ["asset", "title", "type", "course_id", "sprint", "revision", "state", "declared_skills", "declared_and_evidenced",
                                        "declared_not_evidenced", "evidenced_not_declared", "reading_grade", "critical_high_findings"], arows)
    if qa:
        q = qa.data
        _sheet(wb, "Release_Scores", ["course_id", "course", "score", "open_critical", "release"] + [d for d, _ in __import__("engine.stages.qa", fromlist=["DIMENSIONS"]).DIMENSIONS],
               [[x["course_id"], x["course"], x["score"], x["open_critical"], "Y" if x["release"] else "N"] + [d["points"] for d in x["dimensions"]]
                for x in q["release_scores"]])
        _sheet(wb, "Defect_Log", ["asset", "revision", "check", "severity", "gate", "message", "status"],
               [[d["asset"], d["revision"], d["check"], d["severity"], d["gate"], d["message"], d["status"]] for d in q["defects"]])
    prow = []
    for a in assets:
        for rev, pv in sorted(a.get("provenance", {}).items(), key=lambda t: int(t[0])):
            g = pv.get("gates", {})
            prow.append([a["asset_id"], int(rev), pv.get("ai_contribution"), pv.get("model"), pv.get("prompt_id"), pv.get("prompt_version"),
                         pv.get("source_pack_version"), pv.get("taxonomy_version"), pv.get("drafted_at"), "; ".join(pv.get("feedback_applied", []))[:500]]
                        + [f"{g[k]['decision']} by {g[k]['by']} {g[k]['at']}" if k in g else "" for k in ("g1", "g2", "g3", "g4", "g5")])
    _sheet(wb, "Provenance", ["asset", "revision", "ai_contribution", "model", "prompt_id", "prompt_version", "source_pack_version", "taxonomy_version",
                              "drafted_at", "feedback_applied", "gate1_sme", "gate2_instructional", "gate3_accessibility", "gate4_bias", "gate5_editorial"], prow)
    _sheet(wb, "Gates", ["gate", "kind", "owner_role", "status", "decided_by", "note", "opened_at", "decided_at"],
           [[g.label, g.kind, g.owner_role, g.status, g.decided_by, g.note, str(g.opened_at)[:19], str(g.decided_at or "")[:19]] for g in gates])
    _sheet(wb, "Tickets", ["ticket", "trigger", "signal", "detail", "target_asset", "ai_proposed_response", "change_class", "status", "decided_by", "release"],
           [[t["ticket_id"], t["trigger"], t["signal"], t["detail"], t["target_asset"], t["ai_proposed_response"], t["change_class"], t["status"],
             (t.get("decision") or {}).get("by"), t.get("release")] for t in tickets])
    _sheet(wb, "Releases", ["version", "created_at", "change_class", "assets", "changed_assets", "tickets"],
           [[r["version"], r["created_at"], r.get("change_class"), len(r["manifest"]), _j(r.get("changed_assets", [])), _j(r.get("tickets", []))] for r in rels])
    _sheet(wb, "API_Calls", ["at", "service", "mode", "method", "path", "purpose", "status", "ms"],
           [[str(c.at)[:19], c.service, c.mode, c.method, c.path, c.purpose, c.status, c.ms] for c in calls])
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
