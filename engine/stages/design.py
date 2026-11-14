"""Stage 2 Design Blueprint (Technical Proposal 2.2.1.1 Week 2; 2.3.4 stage 2).

AI does: drafts objectives, the Course and Module sequence, assessment allocation and prerequisites.
Checks run automatically: the five coverage rules, the prerequisite DAG, the seat-time map, sprint plan.
People decide: ID edits, SME confirms level, UMGC approves at Gate 2.
"""
from __future__ import annotations

from collections import defaultdict

from ..clients import llm
from ..db import Program, get_record, log, put_record, session


def run(program_id: int, payload: dict):
    from ..spec import completion_definition, get_spec
    with session() as s:
        p = s.get(Program, program_id)
        brief = dict(p.brief)
        sm = dict(get_record(s, program_id, "skills_map").data)
        spec = get_spec(s, program_id)
    weeks = int(spec["program"]["duration_weeks"])
    weekly = float(spec["program"]["weekly_hours"])
    n_courses = int(spec["program"].get("courses") or weeks)
    ref = brief.get("reference") or {}
    skills = sm["skills"]
    origin = ""
    if ref.get("courses"):
        bp = {k: ref[k] for k in ("objectives", "courses", "assessments", "prerequisites", "credentials")}
        origin = "Sample Program reference (Exhibit H and I), loaded as the AI draft"
    else:
        bp = None
        if llm.available():
            bp = llm.complete("design.blueprint", "You are an instructional designer drafting a Program Design Blueprint.",
                              f"Skills: {[(s['skill_id'], s['name'], s['tier'], s['competency_id']) for s in skills]}\n"
                              f"Competencies: {[(c['competency_id'], c['name']) for c in sm['competencies']]}\n"
                              f"The Program runs {weeks} weeks at {weekly} learner hours a week. Draft exactly {n_courses} Courses (UMGC vocabulary: "
                              f"Program > Course > Module), in teaching order, each about {round(weeks * weekly / n_courses, 1)} learner hours, 4 to 6 Modules each, "
                              "a knowledge check and a persistent lab or applied exercise per Course, and a capstone in the last Course. "
                              "Every Core skill must be measured at least twice, once in a performance task. "
                              'Return {"objectives":[{"objective_id":"1.1","level":"module objective","statement":"","bloom_level":"","competency_id":"C1","skill_ids":[],"module_ids":[]}],'
                              '"courses":[{"course_id":"CRS1","name":"","learner_hours":10,"online_hours":4,"reading_hours":2,"practice_hours":4,"assessment_ids":[],'
                              '"modules":[{"module_id":"1.1","title":"","type":"lesson","objective_ids":[],"skill_ids":[],"assessment_ids":[]}]}],'
                              '"assessments":[{"assessment_id":"KC1","name":"","type":"knowledge check","course_id":"CRS1","auto_grade":"score","items_count":10,'
                              '"rubric_rows":0,"objective_ids":[],"measured":[],"practiced":[],"performance_task":false}],'
                              '"prerequisites":[{"from_id":"CRS1","to_id":"CRS2","relation_type":"course_sequence","rationale":""}],"credentials":[]}',
                              program_id=program_id, want_json=True, max_tokens=12000)
            origin = "Claude draft" if bp else ""
        if not bp:
            bp = _template_blueprint(sm, n_courses, weeks * weekly)
            origin = f"Template draft (mock mode): {n_courses} Courses over {weeks} weeks"

    # sprint plan: first half of the Courses in Sprint 1, the rest in Sprint 2; first Module is the prototype
    courses = bp["courses"]
    half = (len(courses) + 1) // 2
    for i, c in enumerate(courses):
        c["sprint"] = 1 if i < half else 2
    for a in bp["assessments"]:
        c = next((c for c in courses if c["course_id"] == a["course_id"]), None)
        a["sprint"] = c["sprint"] if c else 2
    bp["prototype_module"] = courses[0]["modules"][0]["module_id"] if courses and courses[0]["modules"] else None

    bp["trace_matrix"] = trace_matrix(skills, bp)
    bp["coverage"] = coverage_rules(skills, bp)
    bp["dag"] = dag_check(bp["prerequisites"])
    bp["seat_time"] = seat_time(courses)
    bp["completion_definition"] = completion_definition(spec)
    bp["spec_snapshot"] = {"duration_weeks": weeks, "weekly_hours": weekly, "courses": n_courses, "total_hours": round(weeks * weekly, 1)}
    finalize(bp)
    bp["origin"] = origin
    with session() as s:
        put_record(s, program_id, "blueprint", "main", bp, status="proposed")
        ok = sum(1 for r in bp["coverage"]["rules"] if r["passed"])
        log(s, program_id, "worker:design", "design",
            f"Blueprint drafted ({origin}): {len(courses)} Courses, {sum(len(c['modules']) for c in courses)} Modules, "
            f"{len(bp['assessments'])} assessments. Coverage rules {ok}/5 passed; prerequisite graph "
            f"{'is acyclic' if bp['dag']['acyclic'] else 'HAS A CYCLE'}.")


def trace_matrix(skills, bp):
    """Skill x assessment: A assessed, P practiced, I introduced (in a Module but not assessed)."""
    m = {}
    for sk in skills:
        row = {}
        for a in bp["assessments"]:
            if sk["skill_id"] in a.get("measured", []):
                row[a["assessment_id"]] = "A"
            elif sk["skill_id"] in a.get("practiced", []):
                row[a["assessment_id"]] = "P"
        taught = [md["module_id"] for c in bp["courses"] for md in c["modules"] if sk["skill_id"] in md.get("skill_ids", [])]
        perf = any(sk["skill_id"] in a.get("measured", []) and a.get("performance_task") for a in bp["assessments"])
        m[sk["skill_id"]] = {"cells": row, "times_assessed": sum(1 for v in row.values() if v == "A"),
                             "times_practiced": sum(1 for v in row.values() if v == "P"), "taught_in": taught,
                             "assessed_in_performance_task": perf}
    return m


def coverage_rules(skills, bp):
    tm = trace_matrix(skills, bp)
    cat = {s["skill_id"] for s in skills}
    r1 = [s["skill_id"] for s in skills if s.get("tier") == "Core" and not (
        s.get("exit_level") in ("Proficient", "Advanced") and tm[s["skill_id"]]["times_assessed"] >= 2
        and tm[s["skill_id"]]["assessed_in_performance_task"])]
    r2 = [s["skill_id"] for s in skills if s.get("tier") == "Supporting" and tm[s["skill_id"]]["times_assessed"] < 1]
    r3 = sorted({sid for a in bp["assessments"] for sid in a.get("measured", []) + a.get("practiced", []) if sid not in cat})
    r4 = [s["skill_id"] for s in skills if not (s.get("unique_postings_md") or s.get("extraction_source") or s.get("source_spans"))]
    dag = dag_check(bp["prerequisites"])
    rules = [
        {"rule": "Every Core skill taught to Proficient and assessed at least twice, once in a performance task", "failures": r1},
        {"rule": "Every Supporting skill assessed at least once", "failures": r2},
        {"rule": "No assessment measures a skill outside the catalog", "failures": r3},
        {"rule": "Every skill has a Lightcast posting signal or a recorded SME justification", "failures": r4},
        {"rule": "Prerequisites form a directed acyclic graph", "failures": dag["cycle"]},
    ]
    for r in rules:
        r["passed"] = not r["failures"]
    return {"rules": rules, "all_passed": all(r["passed"] for r in rules)}


def dag_check(prereqs):
    g = defaultdict(list)
    nodes = set()
    for p in prereqs:
        g[p["from_id"]].append(p["to_id"])
        nodes |= {p["from_id"], p["to_id"]}
    color, cycle = {}, []

    def visit(n, path):
        color[n] = 1
        for m in g[n]:
            if color.get(m) == 1:
                cycle.extend(path[path.index(m):] + [m] if m in path else [n, m])
                return True
            if color.get(m) is None and visit(m, path + [m]):
                return True
        color[n] = 2
        return False
    for n in sorted(nodes):
        if color.get(n) is None and visit(n, [n]):
            break
    by_type = defaultdict(int)
    for p in prereqs:
        by_type[p["relation_type"]] += 1
    return {"acyclic": not cycle, "cycle": cycle, "edges": len(prereqs), "by_type": dict(by_type)}


def seat_time(courses):
    rows = [{"course_id": c["course_id"], "name": c["name"], "online": c.get("online_hours", 0), "reading": c.get("reading_hours", 0),
             "practice": c.get("practice_hours", 0), "total": c.get("learner_hours", 0)} for c in courses]
    return {"rows": rows, "total_hours": round(sum(r["total"] for r in rows), 1)}


def finalize(bp):
    """Asset properties UMGC asked for (Q&A): labs persist between sessions; graded work auto-grades to at least pass/fail."""
    for a in bp["assessments"]:
        a.setdefault("auto_grade", "score" if a.get("type") in ("knowledge check", "practice exam") else "pass/fail")
        a["persistent_state"] = a.get("type") in ("lab", "capstone", "applied exercise") or bool(a.get("performance_task"))
        a["graded"] = True
    for c in bp["courses"]:
        for m in c["modules"]:
            m.setdefault("minutes", round((c.get("learner_hours") or 0) * 60 / max(1, len(c["modules"]))))
    return bp


def _template_blueprint(sm, n_courses=None, total_hours=None):
    """One Course per week: skills are spread across the Courses in competency order; each Course gets a knowledge check
    and a lab, and the last Course carries a capstone that measures every Core skill again."""
    skills = sorted(sm["skills"], key=lambda s: (s.get("competency_id", ""), s["skill_id"]))
    n = max(1, int(n_courses or len({s.get("competency_id") for s in skills}) or 1))
    per = max(1, -(-len(skills) // n))
    chunks = [skills[i * per:(i + 1) * per] for i in range(n)]
    hours = round((total_hours or n * 10) / n, 1)
    comps = {c["competency_id"]: c for c in sm.get("competencies", [])}
    objectives, courses, assessments, prereqs = [], [], [], []
    for ci, sks in enumerate(chunks, 1):
        cid = f"CRS{ci}"
        cname = ", ".join(dict.fromkeys(comps.get(s.get("competency_id"), {}).get("name", "") for s in sks if s)) or f"Course {ci}"
        mods = []
        for j, sk in enumerate(sks, 1):
            oid = f"{ci}.{j}"
            objectives.append({"objective_id": oid, "level": "module objective", "statement": sk["statement"], "bloom_level": "Apply",
                               "competency_id": sk.get("competency_id"), "skill_ids": [sk["skill_id"]], "module_ids": [oid]})
            mods.append({"module_id": oid, "title": f"{sk['name']}", "type": "lesson", "objective_ids": [oid], "skill_ids": [sk["skill_id"]], "assessment_ids": []})
        ids = [s["skill_id"] for s in sks]
        lab_mod = f"{ci}.{len(sks) + 1}"
        mods.append({"module_id": lab_mod, "title": f"Applied lab: Week {ci}", "type": "lab", "objective_ids": [m["module_id"] for m in mods],
                     "skill_ids": ids, "assessment_ids": [f"LAB{ci}"]})
        assessments += [
            {"assessment_id": f"KC{ci}", "name": f"Knowledge Check {ci}", "type": "knowledge check", "course_id": cid, "auto_grade": "score",
             "items_count": max(5, len(ids) * 2), "rubric_rows": 0, "objective_ids": [m["module_id"] for m in mods[:-1]], "measured": ids,
             "practiced": [], "performance_task": False},
            {"assessment_id": f"LAB{ci}", "name": f"Lab {ci}", "type": "lab", "course_id": cid, "auto_grade": "pass/fail", "items_count": 1,
             "rubric_rows": len(ids), "objective_ids": [m["module_id"] for m in mods[:-1]], "measured": ids, "practiced": [], "performance_task": True}]
        courses.append({"course_id": cid, "name": f"Week {ci}: {cname}"[:120], "learner_hours": hours, "online_hours": round(hours * 0.35, 1),
                        "reading_hours": round(hours * 0.2, 1), "practice_hours": round(hours * 0.45, 1), "assessment_ids": [f"KC{ci}", f"LAB{ci}"], "modules": mods})
        if ci > 1:
            prereqs.append({"from_id": f"CRS{ci - 1}", "to_id": cid, "relation_type": "course_sequence", "rationale": "Weekly release order"})
    core = [s["skill_id"] for s in skills if s.get("tier") == "Core"]
    if courses:
        assessments.append({"assessment_id": "CAPT", "name": "Program Capstone", "type": "capstone", "course_id": courses[-1]["course_id"],
                            "auto_grade": "pass/fail", "items_count": 1, "rubric_rows": len(core), "objective_ids": [o["objective_id"] for o in objectives],
                            "measured": core, "practiced": [], "performance_task": True})
        courses[-1]["assessment_ids"].append("CAPT")
    return {"objectives": objectives, "courses": courses, "assessments": assessments, "prerequisites": prereqs, "credentials": []}
