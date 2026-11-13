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
    with session() as s:
        p = s.get(Program, program_id)
        brief = dict(p.brief)
        sm = dict(get_record(s, program_id, "skills_map").data)
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
                              "Draft one Course per competency, 4 to 6 Modules each, a knowledge check and a lab per Course, and a capstone. "
                              'Return {"objectives":[{"objective_id":"1.1","level":"module objective","statement":"","bloom_level":"","competency_id":"C1","skill_ids":[],"module_ids":[]}],'
                              '"courses":[{"course_id":"CRS1","name":"","learner_hours":10,"online_hours":4,"reading_hours":2,"practice_hours":4,"assessment_ids":[],'
                              '"modules":[{"module_id":"1.1","title":"","type":"lesson","objective_ids":[],"skill_ids":[],"assessment_ids":[]}]}],'
                              '"assessments":[{"assessment_id":"KC1","name":"","type":"knowledge check","course_id":"CRS1","auto_grade":"score","items_count":10,'
                              '"rubric_rows":0,"objective_ids":[],"measured":[],"practiced":[],"performance_task":false}],'
                              '"prerequisites":[{"from_id":"CRS1","to_id":"CRS2","relation_type":"course_sequence","rationale":""}],"credentials":[]}',
                              program_id=program_id, want_json=True, max_tokens=12000)
            origin = "Claude draft" if bp else ""
        if not bp:
            bp = _template_blueprint(sm)
            origin = "Template draft (mock mode)"

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
    bp["completion_definition"] = ("Finish all required Modules, pass the summative assessment at 80 percent or higher within three attempts, "
                                   "and submit the capstone. Completion is measured at 100 and 150 percent of expected time.")
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


def _template_blueprint(sm):
    objectives, courses, assessments, prereqs = [], [], [], []
    skills_by_c = defaultdict(list)
    for s in sm["skills"]:
        skills_by_c[s["competency_id"]].append(s)
    for ci, comp in enumerate(sm["competencies"], 1):
        sks = skills_by_c[comp["competency_id"]]
        cid = f"CRS{ci}"
        mods = []
        for j, sk in enumerate(sks, 1):
            oid = f"{ci}.{j}"
            objectives.append({"objective_id": oid, "level": "module objective", "statement": sk["statement"], "bloom_level": "Apply",
                               "competency_id": comp["competency_id"], "skill_ids": [sk["skill_id"]], "module_ids": [oid]})
            mods.append({"module_id": oid, "title": f"Lesson {oid}: {sk['name']}", "type": "lesson", "objective_ids": [oid],
                         "skill_ids": [sk["skill_id"]], "assessment_ids": []})
        mods.append({"module_id": f"{ci}.{len(sks) + 1}", "title": f"Applied lab: {comp['name']}", "type": "lab",
                     "objective_ids": [o["objective_id"] for o in objectives if o["competency_id"] == comp["competency_id"]],
                     "skill_ids": [s["skill_id"] for s in sks], "assessment_ids": [f"LAB{ci}"]})
        ids = [s["skill_id"] for s in sks]
        assessments += [
            {"assessment_id": f"KC{ci}", "name": f"Knowledge Check {ci}", "type": "knowledge check", "course_id": cid, "auto_grade": "score",
             "items_count": max(5, len(ids) * 2), "rubric_rows": 0, "objective_ids": [m["module_id"] for m in mods[:-1]], "measured": ids,
             "practiced": [], "performance_task": False},
            {"assessment_id": f"LAB{ci}", "name": f"Lab {ci}", "type": "lab", "course_id": cid, "auto_grade": "pass/fail", "items_count": 1,
             "rubric_rows": 4, "objective_ids": [m["module_id"] for m in mods[:-1]], "measured": ids, "practiced": [], "performance_task": True}]
        courses.append({"course_id": cid, "name": f"Course {ci}: {comp['name']}", "learner_hours": 10, "online_hours": 4,
                        "reading_hours": 2, "practice_hours": 4, "assessment_ids": [f"KC{ci}", f"LAB{ci}"], "modules": mods})
        if ci > 1:
            prereqs.append({"from_id": f"CRS{ci - 1}", "to_id": cid, "relation_type": "course_sequence", "rationale": "Sequenced release"})
    return {"objectives": objectives, "courses": courses, "assessments": assessments, "prerequisites": prereqs, "credentials": []}
