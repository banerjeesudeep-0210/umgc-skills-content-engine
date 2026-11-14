"""Quality records people keep alongside the automated checks.

  Sampling        The Content Quality Owner samples AI-assisted assets each week and scores six criteria 1 to 4
                  (Technical Proposal 2.3.1). Scores roll into the monthly performance report.
  Accessibility   Assistive technology test record per asset (screen reader, keyboard-only, zoom, captions) and a
                  limitation register with remediation plan and date (RFP Attachment A 8; Attachment C 5.4.1).
  Demographics    Completion and pace by the learner attributes UMGC defines at implementation (RFP II.4.4.1).
                  Groups under 10 learners are suppressed. The attributes on the simulated cohort are synthetic.
  Reports         The eight standard reports with CSV export (Technical Proposal 2.4.2).
"""
from __future__ import annotations

import csv
import hashlib
import io
import random
from collections import Counter, defaultdict

from sqlalchemy import select

from .db import Program, Statement, get_record, list_records, locked, log, now, put_record, session

CRITERIA = [("accuracy", "Accuracy"), ("grounding", "Grounded in approved sources"), ("alignment", "Skills and objective alignment"),
            ("instruction", "Instructional quality"), ("accessibility", "Accessibility"), ("safety", "Bias and safety")]
AT_TESTS = [("nvda", "Screen reader: NVDA with Firefox"), ("voiceover", "Screen reader: VoiceOver with Safari"), ("keyboard", "Keyboard only"),
            ("zoom", "200 percent zoom and reflow"), ("contrast", "Contrast and non-color cues"), ("captions", "Captions and transcripts")]


# ------------------------------------------------------------------ sampling
def sample_queue(s, pid, n=8):
    """A reproducible weekly sample of AI-assisted assets at their current revision, weighted to recent revisions."""
    week = now().strftime("%G-W%V")
    assets = [r.data for r in list_records(s, pid, "asset")]
    ai = [a for a in assets if str(a.get("provenance", {}).get(str(a["revision"]), {}).get("ai_contribution", "")).startswith(("AI", "Template"))]
    rng = random.Random(f"{pid}-{week}")
    pick = rng.sample(ai, min(n, len(ai)))
    scores = {r.key: r.data for r in list_records(s, pid, "sample")}
    out = []
    for a in pick:
        k = f"{week}:{a['asset_id']}:r{a['revision']}"
        out.append({"key": k, "week": week, "asset": a["asset_id"], "title": a["title"], "revision": a["revision"], "type": a["type"],
                    "ai": a["provenance"].get(str(a["revision"]), {}).get("ai_contribution"), "score": scores.get(k)})
    return out, week


def save_sample(pid, key, scores: dict, note, by) -> str:
    vals = {}
    for c, _ in CRITERIA:
        try:
            v = int(scores.get(c, 0))
        except Exception:
            v = 0
        if v not in (1, 2, 3, 4):
            return "Score every criterion from 1 to 4."
        vals[c] = v
    if min(vals.values()) <= 2 and not note.strip():
        return "A score of 1 or 2 needs a note saying what is wrong; it becomes a ticket."
    with locked(pid) as s:
        week, asset, rev = key.split(":")
        put_record(s, pid, "sample", key, {"key": key, "week": week, "asset": asset, "revision": rev, "scores": vals, "mean": round(sum(vals.values()) / 6, 2),
                                           "note": note, "by": by, "at": now().isoformat(timespec="minutes")}, status="scored")
        log(s, pid, by, "quality", f"Sample scored: {asset} {rev}, mean {round(sum(vals.values()) / 6, 2)} of 4.")
        if min(vals.values()) <= 2:
            from .stages.improve import raise_ticket
            p = s.get(Program, pid)
            raise_ticket(s, pid, "Quality sample below standard", note, "Content quality", asset,
                         f"Revise {asset}: {note}", "routine", "data-informed", p.release_version or "pre-release", f"sample:{key}")
    return ""


def sample_summary(s, pid):
    rows = [r.data for r in list_records(s, pid, "sample")]
    if not rows:
        return None
    by_c = {c: round(sum(r["scores"][c] for r in rows) / len(rows), 2) for c, _ in CRITERIA}
    return {"n": len(rows), "by_criterion": by_c, "mean": round(sum(by_c.values()) / 6, 2), "below": sum(1 for r in rows if min(r["scores"].values()) <= 2)}


# ------------------------------------------------------------------ accessibility test record and limitations
def a11y_records(s, pid):
    return {r.key: r.data for r in list_records(s, pid, "a11y")}


def save_a11y(pid, asset, results: dict, note, by) -> str:
    with locked(pid) as s:
        a = get_record(s, pid, "asset", asset)
        if not a:
            return "Asset not found."
        res = {k: results.get(k, "not tested") for k, _ in AT_TESTS}
        put_record(s, pid, "a11y", asset, {"asset": asset, "revision": a.data["revision"], "results": res, "note": note, "by": by,
                                          "at": now().isoformat(timespec="minutes")}, status="tested")
        fails = [lbl for k, lbl in AT_TESTS if res[k] == "fail"]
        log(s, pid, by, "quality", f"Assistive technology test recorded for {asset} r{a.data['revision']}" + (f": {len(fails)} failure(s)" if fails else ": pass") + ".")
    return ""


def limitations(s, pid):
    return [r.data for r in list_records(s, pid, "limitation")]


def save_limitation(pid, asset, issue, criterion, plan, due, by) -> str:
    if not (issue.strip() and plan.strip() and due.strip()):
        return "Describe the limitation, the remediation plan and the date."
    with locked(pid) as s:
        n = len(list_records(s, pid, "limitation")) + 1
        lid = f"LIM-{n:03d}"
        put_record(s, pid, "limitation", lid, {"id": lid, "asset": asset, "issue": issue, "criterion": criterion, "plan": plan, "due": due,
                                               "status": "open", "by": by, "at": now().isoformat(timespec="minutes")}, status="open")
        log(s, pid, by, "quality", f"Accessibility limitation {lid} on {asset}: {issue[:100]} (remediation by {due}).")
    return ""


def close_limitation(pid, lid, by) -> str:
    with locked(pid) as s:
        r = get_record(s, pid, "limitation", lid)
        d = dict(r.data)
        d.update({"status": "remediated", "closed_by": by, "closed_at": now().isoformat(timespec="minutes")})
        put_record(s, pid, "limitation", lid, d, status="remediated")
        log(s, pid, by, "quality", f"Limitation {lid} remediated.")
    return ""


# ------------------------------------------------------------------ demographics
SYNTH = {
    "Military-affiliated status": ["Active duty", "Veteran", "Military spouse or dependent", "Not military-affiliated"],
    "Age band": ["18 to 24", "25 to 34", "35 to 44", "45 and over"],
    "State of residence": ["Maryland", "Virginia", "District of Columbia", "Other state", "Outside the US"],
    "Employment status": ["Employed full time", "Employed part time", "Not employed"],
    "First-generation status": ["First-generation", "Not first-generation"],
}


def _attr(learner, attr, pid):
    opts = SYNTH.get(attr) or ["Group A", "Group B", "Group C"]
    h = int(hashlib.md5(f"{pid}:{attr}:{learner}".encode()).hexdigest(), 16)
    return opts[h % len(opts)]


def demographics(s, pid, attributes):
    st = list(s.execute(select(Statement).where(Statement.program_id == pid, Statement.verb.in_(["completed", "dropped", "launched"]))).scalars())
    learners = sorted({x.learner for x in st})
    if not learners:
        return []
    prog = {x.learner: x for x in st if x.verb == "completed" and x.object_id == "PROGRAM"}
    exp = next(iter(prog.values())).extra.get("expected_min", 1) if prog else 1
    out = []
    for attr in attributes:
        groups = defaultdict(list)
        for L in learners:
            groups[_attr(L, attr, pid)].append(L)
        rows = []
        for g, ls in sorted(groups.items()):
            n = len(ls)
            if n < 10:
                rows.append({"group": g, "n": n, "suppressed": True})
                continue
            done = [prog[L] for L in ls if L in prog]
            rows.append({"group": g, "n": n, "completed_pct": round(100 * len(done) / n, 1),
                         "within_150_pct": round(100 * sum(1 for x in done if x.duration_min <= 1.5 * exp) / n, 1),
                         "median_hours": round(sorted(x.duration_min for x in done)[len(done) // 2] / 60, 1) if done else None, "suppressed": False})
        overall = round(100 * sum(1 for L in learners if L in prog and prog[L].duration_min <= 1.5 * exp) / len(learners), 1)
        gaps = [r for r in rows if not r["suppressed"] and r["within_150_pct"] < overall - 10]
        out.append({"attribute": attr, "rows": rows, "overall_150": overall, "gaps": [r["group"] for r in gaps]})
    return out


# ------------------------------------------------------------------ eight standard reports (CSV)
REPORTS = [
    ("progress", "Progress and completion", "Per learner: Modules completed, Program completion, time against expected"),
    ("engagement", "Engagement", "Per Module: launches, completions, drop-off, median minutes against expected"),
    ("atrisk", "At-risk learners", "Learners behind pace, stalled, or with low assessment scores"),
    ("items", "Item analysis", "Per item: difficulty (p-value), discrimination, flag"),
    ("effectiveness", "Content effectiveness", "Per Module: drop-off, time ratio, tutor questions, open tickets"),
    ("tutor", "Tutor question clusters", "Most-asked topics by Module"),
    ("credential", "Credential readiness", "Per learner: Core skills assessed at Proficient, practice exam score"),
    ("xapi", "xAPI statement export", "Every statement in the learning record store feed"),
]


def report_rows(s, pid, key):
    from .db import Program
    p = s.get(Program, pid)
    rel = p.release_version or "1.0"
    m = get_record(s, pid, "metrics", rel)
    m = m.data if m else {}
    st = list(s.execute(select(Statement).where(Statement.program_id == pid)).scalars())
    if key == "progress":
        by = defaultdict(lambda: {"modules": 0, "minutes": 0.0, "program": "no"})
        exp = 0
        for x in st:
            if x.verb == "completed" and x.object_id.startswith("M-"):
                by[x.learner]["modules"] += 1
                by[x.learner]["minutes"] += x.duration_min or 0
            if x.verb == "completed" and x.object_id == "PROGRAM":
                by[x.learner]["program"] = "yes"
                exp = x.extra.get("expected_min", exp)
        return ["learner", "modules_completed", "hours_on_task", "program_completed", "expected_hours"], \
            [[L, d["modules"], round(d["minutes"] / 60, 1), d["program"], round(exp / 60, 1)] for L, d in sorted(by.items())]
    if key == "engagement":
        return ["module", "title", "course", "launched", "completed", "dropoff_pct", "median_min", "expected_min", "time_ratio"], \
            [[x["module_id"], x["title"], x["course_id"], x["launched"], x["completed"], x["dropoff_pct"], x["median_min"], x["expected_min"], x["time_ratio"]]
             for x in m.get("modules", [])]
    if key == "atrisk":
        scores = defaultdict(list)
        dropped, mods = set(), Counter()
        for x in st:
            if x.verb == "scored":
                scores[x.learner].append(x.result_score or 0)
            if x.verb == "dropped":
                dropped.add(x.learner)
            if x.verb == "completed" and x.object_id.startswith("M-"):
                mods[x.learner] += 1
        mx = max(mods.values()) if mods else 0
        rows = []
        for L in sorted({x.learner for x in st}):
            reasons = []
            if L in dropped:
                reasons.append("stopped before finishing")
            if scores[L] and min(scores[L]) < 0.7:
                reasons.append(f"scored {round(100 * min(scores[L]))} percent on an assessment")
            if mx and mods[L] < 0.5 * mx and L not in dropped:
                reasons.append("behind pace")
            if reasons:
                rows.append([L, mods[L], "; ".join(reasons), "Learner success outreach within 2 business days"])
        return ["learner", "modules_completed", "reason", "action"], rows
    if key == "items":
        return ["item", "assessment", "responses", "p_value", "discrimination", "flag"], \
            [[x["item_id"], x["assessment"], x["n"], x["p_value"], x["discrimination"],
              "review" if (x["discrimination"] is not None and x["discrimination"] < 0.15) or x["p_value"] < 0.3 or x["p_value"] > 0.95 else ""]
             for x in m.get("items", [])]
    if key == "effectiveness":
        tq = Counter(c["asset"] for c in m.get("tutor_clusters", []) for _ in range(c["questions"]))
        tickets = Counter(t.data.get("target_asset") for t in list_records(s, pid, "ticket") if t.data["status"] in ("proposed", "approved", "in revision"))
        return ["module", "title", "dropoff_pct", "time_ratio", "tutor_questions", "open_tickets"], \
            [[x["asset"], x["title"], x["dropoff_pct"], x["time_ratio"], tq.get(x["asset"], 0), tickets.get(x["asset"], 0)] for x in m.get("modules", [])]
    if key == "tutor":
        return ["topic", "module", "questions"], [[c["topic"], c["asset"], c["questions"]] for c in m.get("tutor_clusters", [])]
    if key == "credential":
        sm = get_record(s, pid, "skills_map")
        core = {k["skill_id"] for k in (sm.data["skills"] if sm else []) if k.get("tier") == "Core"}
        ok = defaultdict(lambda: [0, 0])
        for x in st:
            if x.verb == "answered" and x.extra.get("skill_id") in core:
                ok[x.learner][0] += 1 if x.result_success else 0
                ok[x.learner][1] += 1
        return ["learner", "core_skill_items_correct", "core_skill_items", "core_mastery_pct", "ready"], \
            [[L, a, b, round(100 * a / b, 1) if b else "", "yes" if b and a / b >= 0.8 else "not yet"] for L, (a, b) in sorted(ok.items())]
    if key == "xapi":
        return ["id", "learner", "verb", "object", "score", "success", "duration_min", "release"], \
            [[x.id, x.learner, x.verb, x.object_id, x.result_score if x.result_score is not None else "", "" if x.result_success is None else x.result_success,
              x.duration_min if x.duration_min is not None else "", (x.extra or {}).get("release", "")] for x in st[:20000]]
    return [], []


def report_csv(s, pid, key) -> bytes:
    head, rows = report_rows(s, pid, key)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    if not rows:
        w.writerow(["No data yet: this report fills once a cohort runs on a live release."] + [""] * (len(head) - 1))
    w.writerows(rows)
    return buf.getvalue().encode()
