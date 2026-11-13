"""Stage 7 Measure (Technical Proposal 2.4.2 and 2.3.4 stage 7).

Computes from xAPI statements: completion at 100 and 150 percent of expected time (Workforce Pell target 70
percent within 150 percent), Module drop-off, time-on-task ratio, item difficulty and discrimination
(point-biserial), assessment pass rates, tutor question clusters, and a sampled tutor quality score.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict

from sqlalchemy import select

from .. import config
from ..db import Statement, get_record, log, put_record, session


def _pbis(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx == 0 or sy == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n * sx * sy), 3)


def run(program_id: int, payload: dict):
    release = payload.get("release", "1.0")
    with session() as s:
        bp = get_record(s, program_id, "blueprint").data
        st = list(s.execute(select(Statement).where(Statement.program_id == program_id)).scalars())
    learners = sorted({x.learner for x in st})
    n = len(learners) or 1
    # completion
    prog = [x for x in st if x.verb == "completed" and x.object_id == "PROGRAM"]
    exp = prog[0].extra.get("expected_min", 1) if prog else 1
    within100 = sum(1 for x in prog if x.duration_min <= exp)
    within150 = sum(1 for x in prog if x.duration_min <= 1.5 * exp)
    completion = {"learners": n, "completed": len(prog), "completed_pct": round(100 * len(prog) / n, 1),
                  "within_100_pct": round(100 * within100 / n, 1), "within_150_pct": round(100 * within150 / n, 1),
                  "target_150_pct": config.THRESHOLDS["completion_target_pct_150"], "expected_hours": round(exp / 60, 1)}
    # modules
    launched, completed, dropped, durs, expected = Counter(), Counter(), Counter(), defaultdict(list), {}
    for x in st:
        if x.object_id.startswith("M-"):
            if x.verb == "launched":
                launched[x.object_id] += 1
            elif x.verb == "completed":
                completed[x.object_id] += 1
                durs[x.object_id].append(x.duration_min)
                expected[x.object_id] = x.extra.get("expected_min")
            elif x.verb == "dropped":
                dropped[x.object_id] += 1
    modules = []
    for c in bp["courses"]:
        for m in c["modules"]:
            k = f"M-{m['module_id']}"
            med = statistics.median(durs[k]) if durs[k] else 0
            ratio = round(med / expected[k], 2) if expected.get(k) else None
            modules.append({"asset": k, "module_id": m["module_id"], "title": m["title"], "course_id": c["course_id"], "launched": launched[k],
                            "completed": completed[k], "dropoff_pct": round(100 * dropped[k] / launched[k], 1) if launched[k] else 0,
                            "median_min": round(med, 1), "expected_min": expected.get(k), "time_ratio": ratio})
    # items
    scores = defaultdict(dict)
    for x in st:
        if x.verb == "scored":
            scores[x.object_id][x.learner] = x.result_score
    by_item = defaultdict(list)
    for x in st:
        if x.verb == "answered":
            by_item[x.object_id].append((x.learner, 1 if x.result_success else 0, x.extra.get("assessment")))
    items = []
    for iid, rs in sorted(by_item.items()):
        a = rs[0][2]
        tot = scores.get(f"A-{a}", {})
        xs = [r[1] for r in rs if r[0] in tot]
        ys = [tot[r[0]] for r in rs if r[0] in tot]
        items.append({"item_id": iid, "assessment": a, "n": len(rs), "p_value": round(sum(r[1] for r in rs) / len(rs), 3), "discrimination": _pbis(xs, ys)})
    assessments = []
    for k, d in sorted(scores.items()):
        v = list(d.values())
        assessments.append({"asset": k, "n": len(v), "mean": round(sum(v) / len(v), 3), "pass_rate_pct": round(100 * sum(1 for y in v if y >= 0.7) / len(v), 1)})
    # tutor
    topics = Counter((x.extra.get("topic"), x.object_id) for x in st if x.verb == "asked")
    tutor = [{"topic": t, "asset": a, "questions": c} for (t, a), c in topics.most_common(15)]
    sample_scores = {"sampled": min(20, sum(topics.values())), "accuracy": 3.7, "grounding": 3.8, "instructional": 3.5, "accessibility": 3.9, "safety": 4.0,
                     "note": "Scored 1 to 4 by the Content Quality Owner on a weekly sample (mock scores until live sampling begins)."}
    data = {"release": release, "completion": completion, "modules": modules, "items": items, "assessments": assessments,
            "tutor_clusters": tutor, "tutor_sample": sample_scores, "statements": len(st)}
    with session() as s:
        put_record(s, program_id, "metrics", release, data, status="done")
        log(s, program_id, "worker:measure", "measure", f"Release {release} measured: {completion['within_150_pct']} percent completed within 150 percent "
            f"of expected time (target {completion['target_150_pct']}); {len(items)} items analyzed; {sum(topics.values())} tutor questions.")
