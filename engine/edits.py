"""Saving the shared grids (Skills Studio and Design Studio) back to the workflow records.

Skills and competencies are editable until UMGC approves Gate 1; the Blueprint until UMGC approves Gate 2. After that
the grid is read-only until someone with the reopen permission reopens it, which reopens the gate and is logged.
Every save recomputes what depends on the edit: priority scores and tiers, the trace matrix, the five coverage rules,
the prerequisite graph and the seat-time map.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from .db import Gate, get_record, locked, log, now, put_record
from .stages.design import coverage_rules, dag_check, finalize, seat_time, trace_matrix

_LAST: dict = {}


def throttled(key, seconds=300) -> bool:
    """True when this key has not fired in the last `seconds` (autosave runs every few seconds; the audit log should not)."""
    import time
    t = time.time()
    if t - _LAST.get(key, 0) < seconds:
        return False
    _LAST[key] = t
    return True


def snapshot(s, pid, kind, data, by, note=""):
    """Version history: one snapshot per editor per five minutes of editing, plus one at every reopen and approval."""
    if not throttled(("snap", pid, kind, by)) and not note:
        return
    stamp = now().strftime("%Y%m%dT%H%M%S")
    put_record(s, pid, f"history:{kind}", f"{stamp}:{by}"[:120], {"at": now().isoformat(timespec="seconds"), "by": by, "note": note, "data": data})


def history(s, pid, kind):
    from .db import list_records
    return [r.data | {"key": r.key} for r in reversed(list_records(s, pid, f"history:{kind}"))]


L = lambda v: [x.strip() for x in re.split(r"[,;]", str(v or "")) if x.strip()]


def gate_status(s, pid, key):
    g = s.execute(select(Gate).where(Gate.program_id == pid, Gate.gate_key == key)).scalar_one_or_none()
    return g.status if g else None


def lock_state(s, pid, what: str) -> dict:
    key = {"skills": "umgc.skills_map", "competencies": "umgc.skills_map", "outline": "magic.outline"}.get(what, "umgc.blueprint")
    st = gate_status(s, pid, key)
    return {"locked": st == "approved", "gate": key, "gate_status": st or "not open"}


# ------------------------------------------------------------------ grid definitions (columns shared with the browser)
TIERS, EXITS = ["Core", "Supporting", "Awareness"], ["Aware", "Working", "Proficient", "Advanced"]


def skill_columns(comp_ids):
    return [
        {"key": "skill_id", "label": "ID", "width": 64},
        {"key": "name", "label": "Lightcast skill name", "width": 210, "help": "Exact name in Lightcast Open Skills; changing it re-resolves the Lightcast ID"},
        {"key": "lightcast_skill_id", "label": "Lightcast ID", "width": 150, "help": "Filled by the Lightcast API; type an ID to override"},
        {"key": "statement", "label": "Skill statement (measurable)", "type": "textarea", "width": 320},
        {"key": "competency_id", "label": "Competency", "type": "select", "options": comp_ids, "width": 90},
        {"key": "tier", "label": "Tier", "type": "select", "options": TIERS, "width": 110},
        {"key": "exit_level", "label": "Exit level", "type": "select", "options": EXITS, "width": 110},
        {"key": "sme_criticality", "label": "SME criticality (1-5)", "type": "number", "width": 80},
        {"key": "entry_level_fit", "label": "Entry fit (1-5)", "type": "number", "width": 80},
        {"key": "priority_score", "label": "Priority", "readonly": True, "width": 70},
        {"key": "resolution_status", "label": "Lightcast match", "readonly": True, "width": 110},
    ]


COMP_COLUMNS = [
    {"key": "competency_id", "label": "ID", "width": 60}, {"key": "name", "label": "Competency", "width": 220},
    {"key": "definition", "label": "Definition", "type": "textarea", "width": 300},
    {"key": "exit_level", "label": "Exit level", "type": "select", "options": EXITS, "width": 110},
    {"key": "performance_criteria", "label": "Performance criteria", "type": "textarea", "width": 260},
    {"key": "evidence", "label": "Evidence", "type": "textarea", "width": 200},
]

COURSE_COLUMNS = [
    {"key": "course_id", "label": "ID", "width": 70}, {"key": "name", "label": "Course", "width": 260},
    {"key": "learner_hours", "label": "Learner hours", "type": "number", "width": 90},
    {"key": "online_hours", "label": "Online", "type": "number", "width": 70}, {"key": "reading_hours", "label": "Reading", "type": "number", "width": 70},
    {"key": "practice_hours", "label": "Practice", "type": "number", "width": 70},
    {"key": "sprint", "label": "Sprint", "type": "select", "options": [1, 2], "width": 70},
]
MODULE_TYPES = ["lesson", "scenario", "lab", "clinic", "drill", "practice bank", "applied exercise", "media", "review"]
ASSESS_TYPES = ["knowledge check", "lab", "applied exercise", "discussion", "report", "ticket", "practice exam", "capstone"]


def module_columns(course_ids):
    return [
        {"key": "module_id", "label": "Module", "width": 70}, {"key": "course_id", "label": "Course", "type": "select", "options": course_ids, "width": 90},
        {"key": "title", "label": "Title", "width": 260}, {"key": "type", "label": "Type", "type": "select", "options": MODULE_TYPES, "width": 120},
        {"key": "minutes", "label": "Minutes", "type": "number", "width": 70},
        {"key": "objective_ids", "label": "Objectives", "width": 120}, {"key": "skill_ids", "label": "Skills", "width": 150},
    ]


OBJ_COLUMNS = [
    {"key": "objective_id", "label": "ID", "width": 64}, {"key": "statement", "label": "Learning objective", "type": "textarea", "width": 360},
    {"key": "bloom_level", "label": "Bloom level", "type": "select", "options": ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"], "width": 110},
    {"key": "competency_id", "label": "Competency", "width": 90}, {"key": "skill_ids", "label": "Skills", "width": 140},
    {"key": "module_ids", "label": "Modules", "width": 120},
]


def assess_columns(course_ids):
    return [
        {"key": "assessment_id", "label": "ID", "width": 70}, {"key": "name", "label": "Assessment", "width": 200},
        {"key": "type", "label": "Type", "type": "select", "options": ASSESS_TYPES, "width": 130},
        {"key": "course_id", "label": "Course", "type": "select", "options": course_ids, "width": 90},
        {"key": "items_count", "label": "Items", "type": "number", "width": 60},
        {"key": "measured", "label": "Skills measured", "width": 170}, {"key": "practiced", "label": "Skills practiced", "width": 130},
        {"key": "performance_task", "label": "Performance task", "type": "select", "options": ["no", "yes"], "width": 90},
        {"key": "auto_grade", "label": "Auto-grade", "type": "select", "options": ["score", "pass/fail"], "width": 100},
        {"key": "objective_ids", "label": "Objectives", "width": 130},
    ]


# ------------------------------------------------------------------ rows out
def skills_rows(sm):
    return [{"_id": k["skill_id"], **{c["key"]: k.get(c["key"], "") for c in skill_columns([])}, "name": k.get("lightcast_skill_name") or k.get("name")} for k in sm["skills"]]


def comp_rows(sm):
    return [{"_id": c["competency_id"], **{x["key"]: c.get(x["key"], "") for x in COMP_COLUMNS}} for c in sm.get("competencies", [])]


def course_rows(bp):
    return [{"_id": c["course_id"], **{x["key"]: c.get(x["key"], "") for x in COURSE_COLUMNS}} for c in bp["courses"]]


def module_rows(bp):
    out = []
    for c in bp["courses"]:
        for m in c["modules"]:
            out.append({"_id": m["module_id"], "module_id": m["module_id"], "course_id": c["course_id"], "title": m["title"], "type": m.get("type", "lesson"),
                        "minutes": m.get("minutes", ""), "objective_ids": ", ".join(m.get("objective_ids", [])), "skill_ids": ", ".join(m.get("skill_ids", []))})
    return out


def obj_rows(bp):
    return [{"_id": o["objective_id"], "objective_id": o["objective_id"], "statement": o["statement"], "bloom_level": o.get("bloom_level", ""),
             "competency_id": o.get("competency_id", ""), "skill_ids": ", ".join(o.get("skill_ids", [])), "module_ids": ", ".join(o.get("module_ids", []))}
            for o in bp["objectives"]]


def assess_rows(bp):
    return [{"_id": a["assessment_id"], "assessment_id": a["assessment_id"], "name": a["name"], "type": a["type"], "course_id": a["course_id"],
             "items_count": a.get("items_count", 1), "measured": ", ".join(a.get("measured", [])), "practiced": ", ".join(a.get("practiced", [])),
             "performance_task": "yes" if a.get("performance_task") else "no", "auto_grade": a.get("auto_grade", "score"),
             "objective_ids": ", ".join(a.get("objective_ids", []))} for a in bp["assessments"]]


# ------------------------------------------------------------------ saves
def _num(v, d=None):
    try:
        return float(v)
    except Exception:
        return d


def save_skills(pid: int, rows: list[dict], by: str) -> dict:
    from .clients import lightcast
    from .stages.define import WEIGHTS, _tier
    with locked(pid) as s:
        if lock_state(s, pid, "skills")["locked"]:
            return {"error": "The skills map is approved at Gate 1. Reopen it to edit."}
        r = get_record(s, pid, "skills_map")
        sm = dict(r.data)
        old = {k["skill_id"]: k for k in sm["skills"]}
        comp_ids = {c["competency_id"] for c in sm.get("competencies", [])}
        lc = lightcast.client(pid)
        out, seen, counters, patches = [], set(), {}, []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            sid = str(row.get("skill_id") or "").strip()
            comp = str(row.get("competency_id") or "").strip() or (sorted(comp_ids)[0] if comp_ids else "C1")
            if not sid or sid in seen:
                n = counters.get(comp, sum(1 for k in out if k.get("competency_id") == comp)) + 1
                sid = f"S{comp.lstrip('C')}.{n}"
                while sid in seen or sid in old and old[sid].get("lightcast_skill_name") != name:
                    n += 1
                    sid = f"S{comp.lstrip('C')}.{n}"
                counters[comp] = n
            seen.add(sid)
            k = dict(old.get(sid) or {"skill_id": sid, "source_spans": [], "sme_status": "added by reviewer"})
            prev_name = k.get("lightcast_skill_name") or k.get("name")
            before = {f: k.get(f) for f in ("statement", "competency_id", "tier", "exit_level", "sme_criticality", "entry_level_fit", "lightcast_skill_id")}
            k.update({"skill_id": sid, "name": name, "statement": str(row.get("statement") or "").strip(), "competency_id": comp,
                      "tier": row.get("tier") or k.get("tier") or "Supporting", "exit_level": row.get("exit_level") or k.get("exit_level") or "Working",
                      "sme_criticality": _num(row.get("sme_criticality"), k.get("sme_criticality", 3)),
                      "entry_level_fit": _num(row.get("entry_level_fit"), k.get("entry_level_fit", 4))})
            typed_id = str(row.get("lightcast_skill_id") or "").strip()
            if name != prev_name or not k.get("lightcast_skill_id"):
                hits = lc.search(name, 4)
                top = hits[0] if hits else None
                k.update({"lightcast_skill_id": top["id"] if top else "", "lightcast_skill_name": top["name"] if top else name,
                          "skill_type": ((top or {}).get("type") or {}).get("name", ""), "category": ((top or {}).get("category") or {}).get("name", ""),
                          "subcategory": ((top or {}).get("subcategory") or {}).get("name", ""),
                          "resolution_score": (top or {}).get("score", 0.9 if top else 0),
                          "resolution_status": "resolved" if top and (top.get("score", 0.9) >= 0.8) else ("review" if top else "unresolved"),
                          "alternatives": [f"{h['name']} ({h['id']})" for h in hits[1:4]]})
                occ = (sm.get("occupation") or {})
                dm = lc.jpa_skill_demand([k["lightcast_skill_name"]], occ.get("lot_specialized_occupation_name") or occ.get("lot_occupation_name", ""), "MD") or {}
                d1 = (dm.get("skills") or {}).get(k["lightcast_skill_name"]) or {}
                mx = max([x.get("postings_share_pct") or 0 for x in sm["skills"]] + [d1.get("postings_share_pct") or 0, 1])
                k.update({"unique_postings_md": d1.get("unique_postings"), "postings_share_pct": d1.get("postings_share_pct"),
                          "significance": d1.get("significance"), "demand_mode": "mock" if dm.get("mock") else ("live" if dm else "not licensed"),
                          "demand_norm": round(5 * d1["postings_share_pct"] / mx, 2) if d1.get("postings_share_pct") is not None else None,
                          "taxonomy_version": sm.get("taxonomy_version"), "retrieval_date": now().date().isoformat()})
            elif typed_id and typed_id != k.get("lightcast_skill_id"):
                k.update({"lightcast_skill_id": typed_id, "resolution_status": "set by reviewer"})
            d = k.get("demand_norm")
            score = (WEIGHTS["demand"] * d + WEIGHTS["sme"] * (k["sme_criticality"] or 3) + WEIGHTS["credential"] * (k.get("credential_weight") or 3)
                     + WEIGHTS["entry"] * (k["entry_level_fit"] or 4)) if d is not None else \
                (0.5 * (k["sme_criticality"] or 3) + 0.333 * (k.get("credential_weight") or 3) + 0.167 * (k["entry_level_fit"] or 4))
            k["priority_score"] = round(score, 2)
            k["tier_from_score"] = _tier(score)
            if sid not in old or name != prev_name or any(k.get(f) != v for f, v in before.items()):
                k["sme_status"] = f"edited by {by}"
            out.append(k)
            patches.append({"_id": row.get("_id"), "skill_id": sid, "lightcast_skill_id": k.get("lightcast_skill_id", ""),
                            "priority_score": k["priority_score"], "resolution_status": k.get("resolution_status", "")})
        sm["skills"] = out
        for c in sm.get("competencies", []):
            c["skill_ids"] = [k["skill_id"] for k in out if k["competency_id"] == c["competency_id"]]
        put_record(s, pid, "skills_map", "main", sm)
        snapshot(s, pid, "skills_map", sm, by)
        summary = _recompute_blueprint(s, pid, sm)
        if throttled(("log", pid, "skills", by)):
            log(s, pid, by, "define", f"Skills map edited in the shared grid: {len(out)} skills.")
    return {"ok": True, "skills": len(out), "patches": patches, **summary}


def save_competencies(pid: int, rows: list[dict], by: str) -> dict:
    with locked(pid) as s:
        if lock_state(s, pid, "competencies")["locked"]:
            return {"error": "The skills map is approved at Gate 1. Reopen it to edit."}
        r = get_record(s, pid, "skills_map")
        sm = dict(r.data)
        out = []
        for i, row in enumerate(rows, 1):
            if not str(row.get("name") or "").strip():
                continue
            cid = str(row.get("competency_id") or f"C{i}").strip()
            out.append({"competency_id": cid, "name": row.get("name", ""), "definition": row.get("definition", ""), "exit_level": row.get("exit_level") or "Proficient",
                        "performance_criteria": row.get("performance_criteria", ""), "evidence": row.get("evidence", ""),
                        "skill_ids": [k["skill_id"] for k in sm["skills"] if k.get("competency_id") == cid]})
        sm["competencies"] = out
        patches = [{"_id": r.get("_id"), "competency_id": c["competency_id"]} for r, c in zip([r for r in rows if str(r.get("name") or "").strip()], out)]
        put_record(s, pid, "skills_map", "main", sm)
        snapshot(s, pid, "skills_map", sm, by)
        if throttled(("log", pid, "comps", by)):
            log(s, pid, by, "define", f"Competencies edited in the shared grid: {len(out)}.")
    return {"ok": True, "competencies": len(out), "patches": patches}


def _recompute_blueprint(s, pid, sm) -> dict:
    r = get_record(s, pid, "blueprint")
    if not r:
        return {}
    bp = dict(r.data)
    bp["trace_matrix"] = trace_matrix(sm["skills"], bp)
    bp["coverage"] = coverage_rules(sm["skills"], bp)
    bp["dag"] = dag_check(bp["prerequisites"])
    bp["seat_time"] = seat_time(bp["courses"])
    finalize(bp)
    put_record(s, pid, "blueprint", "main", bp)
    return {"coverage_passed": sum(1 for x in bp["coverage"]["rules"] if x["passed"]), "coverage_failures": [x for x in bp["coverage"]["rules"] if not x["passed"]]}


def save_blueprint_part(pid: int, part: str, rows: list[dict], by: str) -> dict:
    with locked(pid) as s:
        if lock_state(s, pid, "blueprint")["locked"]:
            return {"error": "The Blueprint is approved at Gate 2. Reopen it to edit."}
        r = get_record(s, pid, "blueprint")
        bp = dict(r.data)
        sm = get_record(s, pid, "skills_map").data
        if part == "courses":
            old = {c["course_id"]: c for c in bp["courses"]}
            new = []
            for i, row in enumerate(rows, 1):
                if not str(row.get("name") or "").strip():
                    continue
                cid = str(row.get("course_id") or f"CRS{i}").strip()
                c = dict(old.get(cid) or {"course_id": cid, "modules": [], "assessment_ids": []})
                c.update({"course_id": cid, "name": row["name"], "learner_hours": _num(row.get("learner_hours"), 0), "online_hours": _num(row.get("online_hours"), 0),
                          "reading_hours": _num(row.get("reading_hours"), 0), "practice_hours": _num(row.get("practice_hours"), 0),
                          "sprint": int(_num(row.get("sprint"), 1) or 1)})
                new.append(c)
            bp["courses"] = new
            seq = [p for p in bp["prerequisites"] if p["relation_type"] != "course_sequence"]
            seq += [{"from_id": new[i - 1]["course_id"], "to_id": new[i]["course_id"], "relation_type": "course_sequence", "rationale": "Course order"}
                    for i in range(1, len(new))]
            bp["prerequisites"] = seq
        elif part == "modules":
            by_course = {c["course_id"]: [] for c in bp["courses"]}
            for row in rows:
                if not str(row.get("title") or "").strip():
                    continue
                cid = row.get("course_id") or (bp["courses"][0]["course_id"] if bp["courses"] else "CRS1")
                mid = str(row.get("module_id") or "").strip() or f"{cid.replace('CRS', '')}.{len(by_course.get(cid, [])) + 1}"
                by_course.setdefault(cid, []).append({"module_id": mid, "title": row["title"], "type": row.get("type") or "lesson",
                                                      "minutes": _num(row.get("minutes"), None), "objective_ids": L(row.get("objective_ids")),
                                                      "skill_ids": L(row.get("skill_ids")), "assessment_ids": []})
            for c in bp["courses"]:
                c["modules"] = by_course.get(c["course_id"], [])
            for a in bp["assessments"]:
                for c in bp["courses"]:
                    for m in c["modules"]:
                        if m["type"] in ("lab", "applied exercise") and a["course_id"] == c["course_id"] and a["type"] in ("lab", "applied exercise"):
                            m["assessment_ids"] = sorted(set(m["assessment_ids"] + [a["assessment_id"]]))
        elif part == "objectives":
            bp["objectives"] = [{"objective_id": str(row.get("objective_id") or f"O{i}").strip(), "level": "module objective", "statement": row["statement"],
                                 "bloom_level": row.get("bloom_level") or "Apply", "competency_id": row.get("competency_id", ""),
                                 "skill_ids": L(row.get("skill_ids")), "module_ids": L(row.get("module_ids"))}
                                for i, row in enumerate(rows, 1) if str(row.get("statement") or "").strip()]
        elif part == "assessments":
            bp["assessments"] = [{"assessment_id": str(row.get("assessment_id") or f"A{i}").strip(), "name": row["name"], "type": row.get("type") or "knowledge check",
                                  "course_id": row.get("course_id") or "", "items_count": int(_num(row.get("items_count"), 1) or 1),
                                  "rubric_rows": len(L(row.get("measured"))) if row.get("performance_task") == "yes" else 0,
                                  "objective_ids": L(row.get("objective_ids")), "measured": L(row.get("measured")), "practiced": L(row.get("practiced")),
                                  "performance_task": row.get("performance_task") == "yes", "auto_grade": row.get("auto_grade") or "score"}
                                 for i, row in enumerate(rows, 1) if str(row.get("name") or "").strip()]
            for c in bp["courses"]:
                c["assessment_ids"] = [a["assessment_id"] for a in bp["assessments"] if a["course_id"] == c["course_id"]]
        idkey, namekey = {"courses": ("course_id", "name"), "modules": ("module_id", "title"), "objectives": ("objective_id", "statement"),
                          "assessments": ("assessment_id", "name")}[part]
        saved = [m for c in bp["courses"] for m in c["modules"]] if part == "modules" else bp[part]
        kept = [r for r in rows if str(r.get(namekey) or "").strip()]
        if part == "modules":
            order = {m["title"]: m for m in saved}
            patches = [{"_id": r.get("_id"), idkey: order[r[namekey]][idkey]} for r in kept if r[namekey] in order]
        else:
            patches = [{"_id": r.get("_id"), idkey: x[idkey]} for r, x in zip(kept, saved)]
        put_record(s, pid, "blueprint", "main", bp)
        summary = _recompute_blueprint(s, pid, sm)
        snapshot(s, pid, "blueprint", get_record(s, pid, "blueprint").data, by)
        if throttled(("log", pid, part, by)):
            log(s, pid, by, "design", f"Blueprint {part} edited in the shared grid ({len(kept)} rows).")
    return {"ok": True, "patches": patches, **summary}


def reopen(pid: int, what: str, by: str, note: str) -> str:
    """Reopen an approved skills map (Gate 1) or Blueprint (Gate 2) for edits. The approval is recorded as superseded."""
    key = {"skills": "umgc.skills_map", "outline": "magic.outline"}.get(what, "umgc.blueprint")
    if not note.strip():
        return "Say why it is being reopened."
    with locked(pid) as s:
        g = s.execute(select(Gate).where(Gate.program_id == pid, Gate.gate_key == key)).scalar_one_or_none()
        if not g or g.status != "approved":
            return "It is not approved, so it is already editable."
        rec = get_record(s, pid, {"skills": "skills_map", "outline": "outline"}.get(what, "blueprint"))
        snapshot(s, pid, rec.kind, rec.data, by, note=f"Approved version before reopen: {note}")
        g.gate_key = f"{key}:reopened:{int(now().timestamp())}"
        g.note = (g.note or "") + f" | Reopened by {by}: {note}"
        log(s, pid, by, {"skills": "define", "outline": "build"}.get(what, "design"),
            f"{ {'skills': 'Skills map', 'outline': 'Module outline'}.get(what, 'Blueprint') } reopened for edits: {note}")
    from .orchestrator import advance
    advance(pid)
    return ""
