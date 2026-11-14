"""Module outline and weekly map (Development Studio, between the approved Blueprint and the build).

The outline is the content plan for every Module: which objectives it teaches, each objective's action verb and Bloom
level, the Lightcast skills behind them and each skill statement's verb, whether the two verbs align with the skill's
exit level, the screens planned, the offline reading, the practice and the assessment it feeds. Magic drafts it (AI),
the Instructional Designer and SMEs edit it together in a shared grid, and the Instructional Designer or Learning
Experience Architect approves it. Build drafts each Module from its approved outline row.

The weekly map rolls the outline up by week (one Course per week by default): online Module minutes, offline reading,
practice and assessment time against the weekly learner hours in the Program specification, with the build state and
authors of every piece.
"""
from __future__ import annotations

import re

from .db import get_record, list_records, locked, log, now, put_record, session

BLOOM = {
    "Remember": "define identify list name recall recognize state label match locate record".split(),
    "Understand": "explain describe summarize classify compare interpret discuss distinguish paraphrase illustrate outline".split(),
    "Apply": "apply configure calculate demonstrate use implement perform execute install operate solve run set deploy".split(),
    "Analyze": "analyze examine investigate differentiate triage correlate inspect diagnose troubleshoot categorize detect trace".split(),
    "Evaluate": "evaluate assess justify prioritize recommend select critique judge validate verify defend".split(),
    "Create": "create design develop write build produce plan construct draft compose formulate".split(),
}
ORDER = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]
VERB_LEVEL = {v: lvl for lvl, vs in BLOOM.items() for v in vs}
EXIT_MIN = {"Aware": "Remember", "Working": "Understand", "Proficient": "Apply", "Advanced": "Analyze"}
ASSESS_MIN = {"knowledge check": 30, "practice exam": 90, "lab": 120, "applied exercise": 90, "capstone": 240, "discussion": 45, "report": 90, "ticket": 60}
L = lambda v: [x.strip() for x in re.split(r"[,;]", str(v or "")) if x.strip()]


def verb_of(statement: str) -> str:
    w = re.findall(r"[A-Za-z]+", statement or "")
    for x in w[:4]:
        if x.lower() in VERB_LEVEL:
            return x.capitalize()
    return w[0].capitalize() if w else ""


def level_of(verb: str, fallback: str = "") -> str:
    return VERB_LEVEL.get((verb or "").lower(), fallback)


def alignment(obj_levels: list[str], skills: list[dict]) -> str:
    """The highest objective level in the Module must reach each skill's exit level."""
    if not obj_levels:
        return "no objective mapped"
    top = max(ORDER.index(x) for x in obj_levels if x in ORDER) if any(x in ORDER for x in obj_levels) else -1
    short = [k["skill_id"] for k in skills if ORDER.index(EXIT_MIN.get(k.get("exit_level"), "Remember")) > top]
    return "aligned" if not short else "verbs below exit level for " + ", ".join(short)


COLUMNS = [
    {"key": "week", "label": "Week", "type": "number", "width": 60},
    {"key": "module_id", "label": "Module", "width": 70, "readonly": True},
    {"key": "title", "label": "Module title", "width": 220},
    {"key": "objective_ids", "label": "Objectives", "width": 100},
    {"key": "objective_verbs", "label": "Objective verbs (Bloom)", "width": 190, "readonly": True},
    {"key": "skill_ids", "label": "Skills", "width": 110},
    {"key": "skill_verbs", "label": "Skill statement verbs", "width": 190, "readonly": True},
    {"key": "alignment", "label": "Verb alignment", "width": 150, "readonly": True},
    {"key": "screens", "label": "Screens planned (one per line)", "type": "textarea", "width": 280},
    {"key": "reading", "label": "Offline reading (one per line)", "type": "textarea", "width": 250},
    {"key": "reading_min", "label": "Reading min", "type": "number", "width": 80},
    {"key": "practice", "label": "Practice (one per line)", "type": "textarea", "width": 250},
    {"key": "practice_min", "label": "Practice min", "type": "number", "width": 80},
    {"key": "online_min", "label": "Online min", "type": "number", "width": 80},
    {"key": "assessments", "label": "Assessment it feeds", "width": 120},
]


def _derive(row, objs, skills):
    oids, sids = L(row.get("objective_ids")), L(row.get("skill_ids"))
    o_list = [objs[o] for o in oids if o in objs]
    s_list = [skills[k] for k in sids if k in skills]
    o_lv = []
    ov = []
    for o in o_list:
        v = verb_of(o["statement"])
        lv = level_of(v, o.get("bloom_level", ""))
        o_lv.append(lv)
        ov.append(f"{o['objective_id']} {v} ({lv or 'unknown'})")
    sv = [f"{k['skill_id']} {verb_of(k.get('statement', ''))} (exit {k.get('exit_level', '')})" for k in s_list]
    row.update({"objective_verbs": "; ".join(ov), "skill_verbs": "; ".join(sv), "alignment": alignment(o_lv, s_list)})
    return row


def draft(pid: int) -> dict:
    """Draft the outline from the approved Blueprint, skills map, specification and approved source pack."""
    from .sources import source_pack
    from .spec import get_spec
    with session() as s:
        bp = get_record(s, pid, "blueprint").data
        sm = get_record(s, pid, "skills_map").data
        spec = get_spec(s, pid)
        pack = source_pack(s, pid)
        srcs = {r.data["source_id"]: r.data for r in list_records(s, pid, "source")}
    skills = {k["skill_id"]: k for k in sm["skills"]}
    objs = {o["objective_id"]: o for o in bp["objectives"]}
    rows = []
    for wk, c in enumerate(bp["courses"], 1):
        mods = c["modules"] or []
        n = max(1, len(mods))
        read_total = round((c.get("reading_hours") or 0) * 60)
        prac_total = round((c.get("practice_hours") or 0) * 60)
        course_asm = [a for a in bp["assessments"] if a["course_id"] == c["course_id"]]
        # the week's learner hours hold everything: online Modules, offline reading, practice and graded assessments
        week_min = round((c.get("learner_hours") or 0) * 60)
        assess_total = sum(ASSESS_MIN.get(a["type"], 45) for a in course_asm)
        if read_total + prac_total + assess_total > week_min:  # keep reading and practice in proportion, inside the week
            scale = max(0.0, (week_min * 0.6 - assess_total)) / max(1, read_total + prac_total)
            read_total, prac_total = round(read_total * scale), round(prac_total * scale)
        online_total = max(0, week_min - read_total - prac_total - assess_total)
        for m in mods:
            sks = [skills[k] for k in m.get("skill_ids", []) if k in skills]
            names = [(k.get("lightcast_skill_name") or k.get("name") or "") for k in sks]
            reading = []
            for src in pack:
                text = src.get("text", "")
                hits = [nm for nm in names if nm and nm.lower() in text.lower()]
                if hits:
                    sec = ""
                    d = srcs.get(src["id"])
                    if d:
                        ch = next((x for x in d.get("chunks", []) if any(h.lower() in (x.get("text") or "").lower() for h in hits)), None)
                        sec = f", section {ch['chunk_id']}: {ch.get('heading') or ''}".rstrip(": ") if ch else ""
                    reading.append(f"[{src['id']}] {src.get('title')}{sec} ({', '.join(hits[:3])})")
            reading = reading[:4] or [f"[{(pack[0]['id'] if pack else 'SRC-EXAM')}] Reading on {', '.join(names[:3]) or m['title']}"]
            screens = ["What you will be able to do"]
            for k in sks:
                nm = k.get("lightcast_skill_name") or k.get("name")
                screens += [f"{nm}: concept and why it matters", f"On the job: {nm} in a workplace scenario"]
            screens += ["Worked example", "Check your understanding", "Summary"]
            practice = [f"{k['skill_id']}: {k.get('statement', '')}" for k in sks] or ["Apply the Module's objectives to a short scenario"]
            feeds = [a["assessment_id"] for a in course_asm if set(a.get("measured", []) + a.get("practiced", [])) & set(m.get("skill_ids", []))]
            row = {"_id": m["module_id"], "week": wk, "course_id": c["course_id"], "module_id": m["module_id"], "title": m["title"],
                   "objective_ids": ", ".join(m.get("objective_ids", [])), "skill_ids": ", ".join(m.get("skill_ids", [])),
                   "screens": "\n".join(screens), "reading": "\n".join(reading), "reading_min": round(read_total / n),
                   "practice": "\n".join(practice), "practice_min": round(prac_total / n), "online_min": max(0, round(online_total / n)),
                   "assessments": ", ".join(feeds)}
            rows.append(_derive(row, objs, skills))
    data = {"rows": rows, "drafted_at": now().isoformat(timespec="seconds"), "weekly_hours": spec["program"]["weekly_hours"],
            "weeks": spec["program"]["duration_weeks"]}
    with locked(pid) as s:
        put_record(s, pid, "outline", "main", data, status="draft")
        log(s, pid, "worker:design", "build", f"Module outline drafted: {len(rows)} Modules over {len(bp['courses'])} weeks, objectives mapped to Lightcast skills and verbs.")
    return data


def run(program_id: int, payload: dict):
    draft(program_id)


def save(pid: int, rows: list[dict], by: str) -> dict:
    from .edits import lock_state, snapshot, throttled
    with locked(pid) as s:
        if lock_state(s, pid, "outline")["locked"]:
            return {"error": "The outline is approved. Reopen it to edit."}
        r = get_record(s, pid, "outline")
        d = dict(r.data)
        bp = get_record(s, pid, "blueprint").data
        sm = get_record(s, pid, "skills_map").data
        skills = {k["skill_id"]: k for k in sm["skills"]}
        objs = {o["objective_id"]: o for o in bp["objectives"]}
        old = {x["module_id"]: x for x in d["rows"]}
        out, patches = [], []
        for row in rows:
            mid = row.get("module_id")
            if mid not in old:
                continue
            nr = dict(old[mid])
            for c in COLUMNS:
                if not c.get("readonly") and c["key"] in row:
                    v = row[c["key"]]
                    if c.get("type") == "number":
                        try:
                            v = float(v) if v not in ("", None) else 0
                        except Exception:
                            v = 0
                    nr[c["key"]] = v
            _derive(nr, objs, skills)
            out.append(nr)
            patches.append({"_id": row.get("_id"), "objective_verbs": nr["objective_verbs"], "skill_verbs": nr["skill_verbs"], "alignment": nr["alignment"]})
        d["rows"] = out
        put_record(s, pid, "outline", "main", d)
        snapshot(s, pid, "outline", d, by)
        if throttled(("log", pid, "outline", by)):
            log(s, pid, by, "build", "Module outline edited in the shared grid.")
    return {"ok": True, "patches": patches, "misaligned": sum(1 for x in out if x["alignment"] != "aligned")}


def weekly_map(s, pid):
    """Everything in each week: outline plan plus the built assets, their state and who put them together."""
    from .spec import get_spec
    o = get_record(s, pid, "outline")
    bp = get_record(s, pid, "blueprint")
    if not (o and bp):
        return None
    spec = get_spec(s, pid)
    assets = {r.key: r.data for r in list_records(s, pid, "asset")}
    target = float(spec["program"]["weekly_hours"]) * 60
    weeks = {}
    asm_min = ASSESS_MIN
    for row in o.data["rows"]:
        w = weeks.setdefault(int(float(row.get("week") or 1)), {"week": int(float(row.get("week") or 1)), "modules": [], "reading": [], "practice": [],
                                                               "assessments": [], "online": 0, "reading_min": 0, "practice_min": 0, "assess_min": 0})
        a = assets.get(f"M-{row['module_id']}")
        pv = (a or {}).get("provenance", {}).get(str((a or {}).get("revision", 1)), {})
        w["modules"].append({**row, "state": (a or {}).get("state", "not planned"), "revision": (a or {}).get("revision"),
                             "authors": ", ".join(pv.get("co_editors") or [x for x in [pv.get("drafted_by", "")] if x]) or "not drafted",
                             "ai": pv.get("ai_contribution", "")})
        w["online"] += float(row.get("online_min") or 0)
        w["reading_min"] += float(row.get("reading_min") or 0)
        w["practice_min"] += float(row.get("practice_min") or 0)
        w["reading"] += [(row["module_id"], x) for x in str(row.get("reading") or "").splitlines() if x.strip()]
        w["practice"] += [(row["module_id"], x) for x in str(row.get("practice") or "").splitlines() if x.strip()]
    for wk, c in enumerate(bp.data["courses"], 1):
        w = weeks.get(wk)
        if not w:
            continue
        w["course"] = f"{c['course_id']} {c['name']}"
        for x in bp.data["assessments"]:
            if x["course_id"] == c["course_id"]:
                a = assets.get(f"A-{x['assessment_id']}") or {}
                mins = asm_min.get(x["type"], 45)
                w["assessments"].append({**x, "state": a.get("state", "not planned"), "minutes": mins})
                w["assess_min"] += mins
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        w["total"] = round(w["online"] + w["reading_min"] + w["practice_min"] + w["assess_min"])
        w["target"] = round(target)
        w["fit"] = "on plan" if abs(w["total"] - target) <= 0.1 * target else ("over plan" if w["total"] > target else "under plan")
        w["built"] = sum(1 for m in w["modules"] if m["state"] == "validated") + sum(1 for a in w["assessments"] if a["state"] == "validated")
        w["pieces"] = len(w["modules"]) + len(w["assessments"])
        out.append(w)
    return out
