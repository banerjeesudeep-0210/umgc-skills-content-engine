"""Stage 3 Build (Technical Proposal 2.2.1.1 Weeks 3 to 8; 2.3.4 stage 3).

AI does: drafts Modules, scenarios, items, alt text and captions from the source pack only, with a citation to
the source item behind every passage. Each asset is one job, so the build pool fans out across workers.
People decide: five validation gates with named owners, then the UMGC sprint review windows.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from ..clients import llm
from ..db import locked, Program, get_record, log, put_record, session
from .define import source_pack_version

ORGS = ["Tidewater Health System", "Bayside Family Dental", "Harborview Community College", "Patapsco Regional Bank",
        "Chesapeake Community Credit Union"]


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:6], 16)


def plan_assets(s, program_id: int, bp: dict, skills_map: dict):
    """Create one asset record per Module and per assessment (idempotent)."""
    made = 0
    proto = bp.get("prototype_module")
    from ..studio import team
    members = team(s, program_id)["members"]
    smes = [m for m in members if "SME" in m["role"] and m["courses"] and m["courses"] != [c["course_id"] for c in bp["courses"]]] or \
        [m for m in members if "SME" in m["role"]]
    designer = next((m["name"] for m in members if "Instructional Designer" in m["role"]), "")
    lead = next((m["name"] for m in members if m["role"].startswith("UMGC")), "")

    def owners(course_id):
        sme = next((m["name"] for m in smes if course_id in m.get("courses", [])), smes[0]["name"] if smes else "")
        return {"author": designer, "sme": sme, "reviewer": lead}
    for c in bp["courses"]:
        for m in c["modules"]:
            key = f"M-{m['module_id']}"
            if get_record(s, program_id, "asset", key):
                continue
            put_record(s, program_id, "asset", key, {
                "asset_id": key, "asset_type": "module", "ref_id": m["module_id"], "course_id": c["course_id"],
                "course_name": c["name"], "sprint": c["sprint"], "title": m["title"], "type": m.get("type", "lesson"),
                "objective_ids": m.get("objective_ids", []), "skill_ids": m.get("skill_ids", []),
                "assessment_ids": m.get("assessment_ids", []), "prototype": m["module_id"] == proto,
                "revision": 1, "state": "queued", "feedback": [], "revisions": {}, "checks": {}, "provenance": {}, "gate_log": [],
                "owners": owners(c["course_id"])}, status="queued")
            made += 1
    for a in bp["assessments"]:
        key = f"A-{a['assessment_id']}"
        if get_record(s, program_id, "asset", key):
            continue
        put_record(s, program_id, "asset", key, {
            "asset_id": key, "asset_type": "assessment", "ref_id": a["assessment_id"], "course_id": a["course_id"],
            "sprint": a.get("sprint", 2), "title": a["name"], "type": a["type"], "objective_ids": a.get("objective_ids", []),
            "skill_ids": a.get("measured", []), "practiced": a.get("practiced", []), "items_count": a.get("items_count", 1),
            "rubric_rows": a.get("rubric_rows", 0), "auto_grade": a.get("auto_grade"), "performance_task": a.get("performance_task"),
            "prototype": False, "revision": 1, "state": "queued", "feedback": [], "revisions": {}, "checks": {}, "provenance": {}, "gate_log": [],
            "owners": owners(a["course_id"])},
            status="queued")
        made += 1
    return made


def run(program_id: int, payload: dict):
    key = payload["asset"]
    with session() as s:
        p = s.get(Program, program_id)
        brief = dict(p.brief)
        sm = get_record(s, program_id, "skills_map").data
        bp = get_record(s, program_id, "blueprint").data
        rec = get_record(s, program_id, "asset", key)
        a = dict(rec.data)
        ol = get_record(s, program_id, "outline")
    rev = a["revision"]
    if ol and a["asset_type"] == "module":
        a["_outline"] = next((r for r in ol.data.get("rows", []) if r["module_id"] == a["ref_id"]), None)
    skills = {sk["skill_id"]: sk for sk in sm["skills"]}
    objectives = {o["objective_id"]: o for o in bp["objectives"]}
    comps = {c["competency_id"]: c for c in sm["competencies"]}
    feedback = [f for f in a.get("feedback", []) if f.get("revision") == rev]

    content, ai = None, "Template-drafted (mock mode)"
    if llm.available():
        content = _llm_draft(program_id, a, brief, skills, objectives, feedback)
        ai = "AI-drafted" if content else ai
    if not content:
        content = (_mock_module if a["asset_type"] == "module" else _mock_assessment)(a, brief, skills, objectives, comps, rev, feedback)

    prov = {"revision": rev, "ai_contribution": ai, "model": llm.model_name(),
            "prompt_id": "build.module" if a["asset_type"] == "module" else "build.assessment",
            "prompt_version": llm.PROMPTS["build.module" if a["asset_type"] == "module" else "build.assessment"],
            "source_pack_version": source_pack_version(brief, program_id), "taxonomy_version": sm.get("taxonomy_version"),
            "drafted_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "drafted_by": payload.get("_worker", "worker:build"),
            "feedback_applied": [f["note"] for f in feedback], "gates": {}}
    with locked(program_id) as s:
        rec = get_record(s, program_id, "asset", key)
        a = dict(rec.data)
        a["revisions"] = dict(a.get("revisions", {}))
        a["revisions"][str(rev)] = content
        a["provenance"] = dict(a.get("provenance", {}))
        a["provenance"][str(rev)] = prov
        a["state"] = "drafted"
        put_record(s, program_id, "asset", key, a, status="drafted")
        log(s, program_id, payload.get("_worker", "worker:build"), "build", f"{key} revision {rev} drafted ({ai}).")


# ------------------------------------------------------------------ templates (mock mode)
def _cite(text, src="SRC-EXAM"):
    return {"text": text, "citations": [src]}


def _mock_module(a, brief, skills, objectives, comps, rev, feedback):
    org = ORGS[_h(a["asset_id"]) % len(ORGS)]
    sks = [skills[s] for s in a["skill_ids"] if s in skills]
    objs = [objectives[o] for o in a["objective_ids"] if o in objectives]
    screens = [{"heading": "What you will be able to do", "blocks": [_cite(
        "By the end of this Module you will be able to: " + "; ".join(o["statement"] for o in objs) + ".", "SRC-UMGC")],
        "media": None}]
    for sk in sks:
        comp = comps.get(sk.get("competency_id"), {})
        body = (f"{sk['name']} is part of {comp.get('name', 'this competency').lower()}. In practice, an entry-level analyst is expected to "
                f"{sk['statement'][0].lower() + sk['statement'][1:]}. ")
        if sk.get("related"):
            body += f"You will also see it alongside {sk['related']}. "
        if sk["name"] == "Firewall" and rev == 1:
            body += "A common first step is to whitelist the business applications the clinic depends on and deny everything else. "
        screens.append({"heading": sk["name"], "blocks": [_cite(body)],
                        "media": {"type": "diagram", "alt": "" if (_h(a["asset_id"] + sk["skill_id"]) % 9 == 0 and rev == 1)
                                  else f"Diagram showing how {sk['name']} fits into the analyst's daily workflow at {org}"}})
        screens.append({"heading": f"At {org}: {sk['name']} on the job", "blocks": [_cite(
            f"The security team at {org} uses {sk['name']} when {comp.get('performance_criteria', 'it completes the task').lower()} "
            f"Your task mirrors theirs: {sk['statement'].lower()}.", "SRC-EXAM")], "media": None})
    if feedback or rev > 1:
        screens.insert(1, {"heading": "Worked example", "blocks": [_cite(
            "Here is the task done step by step before you try it: read the evidence, name the skill you are applying, "
            "record what you found, and state the next action.", "SRC-EXAM")], "media": None})
    practice = [{"prompt": f"Try it: {sk['statement']}. Write two sentences on what you would check first.",
                 "skill_id": sk["skill_id"], "feedback": "Compare your answer with the model response in the AI Study Assistant panel."}
                for sk in sks]
    summary = "Key ideas: " + ", ".join(sk["name"] for sk in sks) + "."
    reading = []
    ol = a.get("_outline")
    if ol:  # follow the approved Module outline: its screen plan, offline reading and practice
        by_head = {sc["heading"]: sc for sc in screens}
        planned = []
        for h in [x.strip() for x in str(ol.get("screens") or "").splitlines() if x.strip()]:
            if h.lower() == "summary":
                continue
            sk = next((k for k in sks if h.startswith(k["name"]) or k["name"] in h), None)
            if h == "What you will be able to do":
                planned.append(screens[0])
            elif sk and h.lower().startswith("on the job"):
                src = by_head.get(f"At {org}: {sk['name']} on the job")
                planned.append({**src, "heading": h} if src else {"heading": h, "blocks": [_cite(f"At {org}, {sk['statement'].lower()}.")], "media": None})
            elif sk:
                src = by_head.get(sk["name"])
                planned.append({**src, "heading": h} if src else {"heading": h, "blocks": [_cite(sk["statement"] + ".")], "media": None})
            elif h.lower() == "worked example":
                planned.append({"heading": h, "blocks": [_cite("Here is the task done step by step before you try it: read the evidence, name the skill you are "
                                                              "applying, record what you found, and state the next action.")], "media": None})
            elif h.lower().startswith("check your understanding"):
                planned.append({"heading": h, "blocks": [_cite("Answer three quick questions before the practice: " +
                                                              "; ".join(f"what does {k['name']} help you do?" for k in sks[:3]) + ".", "SRC-EXAM")], "media": None})
            else:
                planned.append({"heading": h, "blocks": [_cite(f"{h}: " + " ".join(k["statement"] + "." for k in sks[:2]))], "media": None})
        if planned:
            screens = planned
        lines = [x.strip() for x in str(ol.get("practice") or "").splitlines() if x.strip()]
        if lines:
            practice = [{"prompt": "Try it: " + re.sub(r"^S\d+\.\d+:\s*", "", ln), "skill_id": (re.match(r"(S\d+\.\d+)", ln) or [None, ""])[1] if re.match(r"(S\d+\.\d+)", ln) else "",
                         "feedback": "Compare your answer with the model response in the AI Study Assistant panel."} for ln in lines]
        reading = [x.strip() for x in str(ol.get("reading") or "").splitlines() if x.strip()]
    return {"title": a["title"], "module_type": a["type"], "scenario_org": org, "screens": screens, "practice": practice,
            "summary": summary, "reading": reading, "estimated_minutes": (float(ol.get("online_min") or 0) if ol else 0) or 30 + 15 * len(sks)}


def _mock_assessment(a, brief, skills, objectives, comps, rev, feedback):
    sks = [skills[s] for s in a["skill_ids"] if s in skills]
    if a["type"] in ("knowledge check", "practice exam"):
        n = min(a.get("items_count") or 5, 12)
        items = []
        for i in range(n):
            sk = sks[i % len(sks)] if sks else None
            if not sk:
                break
            peers = [x for x in skills.values() if x["skill_id"] != sk["skill_id"]]
            peers = sorted(peers, key=lambda x: (x.get("competency_id") != sk.get("competency_id"), _h(x["skill_id"] + str(i))))[:3]
            opts = [{"id": "A", "text": sk["statement"], "correct": True, "rationale": "Correct: this is what the skill requires."}]
            for j, pr in enumerate(peers):
                opts.append({"id": "BCD"[j], "text": pr["statement"], "correct": False,
                             "rationale": f"Plausible but describes {pr['name']}, not {sk['name']}. Reveals the misconception that the two are interchangeable."})
            rot = (_h(a["asset_id"] + str(i)) % 4)
            opts = opts[rot:] + opts[:rot]
            for j, o in enumerate(opts):
                o["id"] = "ABCD"[j]
            obj = next((o for o in a["objective_ids"] if sk["skill_id"] in (objectives.get(o) or {}).get("skill_ids", [])),
                       a["objective_ids"][0] if a["objective_ids"] else "")
            stems = ["A new analyst is asked about {n}. Which statement best describes the task?",
                     "Which task shows {n} applied correctly on the job?",
                     "Your team lead asks you to use {n} today. What are you expected to do?",
                     "Which statement matches the {n} skill as this Program defines it?"]
            stem = stems[(i // max(1, len(sks))) % len(stems)].format(n=sk["name"])
            items.append({"item_id": f"{a['ref_id']}-I{i + 1:02d}", "stem": stem,
                          "options": opts, "key": next(o["id"] for o in opts if o["correct"]), "skill_id": sk["skill_id"], "objective_id": obj})
        note = f"Full bank of {a.get('items_count')} items is generated in production; this build carries {len(items)}." if (a.get("items_count") or 0) > n else ""
        return {"title": a["title"], "kind": "items", "items": items, "objectives_text": _objs_text(a, objectives), "passing": "80 percent" if a["type"] == "practice exam" else "score",
                "note": note}
    rows = [{"criterion": f"{sk['name']}: {sk['statement']}", "levels": ["Not demonstrated", "Developing", "Meets", "Exceeds"],
             "skill_id": sk["skill_id"]} for sk in sks] or [{"criterion": "Completes the task", "levels": ["No", "Yes"], "skill_id": ""}]
    org = ORGS[_h(a["asset_id"]) % len(ORGS)]
    return {"title": a["title"], "kind": "performance", "scenario_org": org, "objectives_text": _objs_text(a, objectives),
            "prompt": f"{org} needs your help. " + " ".join(f"{sk['statement']}." for sk in sks[:4]),
            "steps": ["Read the scenario brief and evidence files", "Complete the task in the persistent lab environment",
                      "Record your findings in the template", "Submit; the result auto-grades to pass/fail at the rubric threshold"],
            "rubric": rows, "auto_grade": "pass/fail at 3 of 4 on every row, full rubric score kept"}


def _objs_text(a, objectives):
    return "This assessment measures: " + "; ".join(objectives[o]["statement"] for o in a["objective_ids"] if o in objectives) + "."


def _llm_draft(program_id, a, brief, skills, objectives, feedback):
    sks = [skills[s] for s in a["skill_ids"] if s in skills]
    from ..sources import source_pack
    with session() as s:
        srcs = source_pack(s, program_id)
    pack = "\n\n".join(f"[{src['id']}] {src['title']}\n{src['text'][:6000]}" for src in srcs)
    spec = {"asset": a["asset_id"], "title": a["title"], "type": a["type"],
            "objectives": [objectives[o]["statement"] for o in a["objective_ids"] if o in objectives],
            "skills": [{"id": s["skill_id"], "lightcast_name": s["name"], "statement": s["statement"]} for s in sks],
            "reviewer_feedback_to_apply": [f["note"] for f in feedback]}
    if a.get("_outline"):
        ol = a["_outline"]
        spec.update({"approved_outline": {"screens_in_order": [x for x in str(ol.get("screens") or "").splitlines() if x.strip()],
                                          "objective_verbs": ol.get("objective_verbs"), "offline_reading": [x for x in str(ol.get("reading") or "").splitlines() if x.strip()],
                                          "practice": [x for x in str(ol.get("practice") or "").splitlines() if x.strip()], "online_minutes": ol.get("online_min")}})
    if a["asset_type"] == "module":
        shape = ('{"title":"","module_type":"","scenario_org":"","screens":[{"heading":"","blocks":[{"text":"","citations":["SRC-EXAM"]}],'
                 '"media":{"type":"diagram","alt":""}}],"practice":[{"prompt":"","skill_id":"","feedback":""}],"summary":"","reading":[""],"estimated_minutes":45}')
        ask = ("Write the Module. Follow approved_outline when present: one screen per planned screen, in order, at the objective verbs' level; "
               "include the offline reading list in 'reading'. Use each skill's Lightcast name verbatim at least once. Every media element needs alt text.")
    elif a["type"] in ("knowledge check", "practice exam"):
        shape = ('{"title":"","kind":"items","items":[{"item_id":"","stem":"","options":[{"id":"A","text":"","correct":true,"rationale":""}],'
                 '"key":"A","skill_id":"","objective_id":""}],"passing":"","note":""}')
        ask = f"Write {min(a.get('items_count') or 5, 12)} multiple-choice items with distractor rationale for every option."
    else:
        shape = ('{"title":"","kind":"performance","scenario_org":"","prompt":"","steps":[],"rubric":[{"criterion":"","levels":[],"skill_id":""}],'
                 '"auto_grade":"pass/fail at threshold"}')
        ask = "Write the performance task and an analytic rubric with one row per skill."
    out = llm.complete("build.module" if a["asset_type"] == "module" else "build.assessment",
                       "You draft noncredit workforce learning content for UMGC working adult and military-affiliated learners. "
                       "Fictional organizations only.",
                       f"SOURCE PACK\n{pack}\n\nSPEC\n{json.dumps(spec)}\n\n{ask}\nReturn JSON shaped like: {shape}",
                       program_id=program_id, want_json=True, max_tokens=6000)
    return out if isinstance(out, dict) and (out.get("screens") or out.get("items") or out.get("rubric")) else None
