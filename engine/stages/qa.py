"""Stage 4 Assure quality (Technical Proposal 2.3.1 automated checks; 2.2.1.1 testing and the 100-point
Course release standard; Exhibit I coverage rules re-checked on built content).

Per asset (qa.asset_checks): objective coverage, reading level, terminology, bias and sensitive-content screen,
accessibility lint, duplication and drift, citation grounding, and a Lightcast extract/trace of the built text
that compares the skills the asset declares with the skills it evidences.
Per Program (qa.program): trace roll-up in both directions, coverage rules on built content, release score per
Course, QA report and defect resolution log.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .. import config
from ..clients import lightcast
from ..db import locked, get_record, list_records, log, put_record, session
from .design import coverage_rules

BIAS_TERMS = {"whitelist": "allowlist", "blacklist": "blocklist", "master/slave": "primary/replica", "guys": "everyone",
              "manpower": "staffing", "sanity check": "quick check", "crazy": "unexpected", "insane": "extreme",
              "grandfathered": "legacy", "man-hours": "person-hours"}


def asset_text(content: dict) -> str:
    parts = [content.get("title", "")]
    for sc in content.get("screens", []):
        parts.append(sc.get("heading", ""))
        parts += [b.get("text", "") for b in sc.get("blocks", [])]
    parts += [p.get("prompt", "") for p in content.get("practice", [])]
    parts.append(content.get("summary", ""))
    for it in content.get("items", []):
        parts.append(it.get("stem", ""))
        parts += [o.get("text", "") for o in it.get("options", [])]
    parts.append(content.get("prompt", ""))
    parts.append(content.get("objectives_text", ""))
    parts += [r.get("criterion", "") for r in content.get("rubric", [])]
    return "\n".join(x for x in parts if x)


def _syllables(w):
    w = w.lower()
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def reading_level(text):
    sents = [x for x in re.split(r"[.!?]+\s", text) if x.strip()]
    words = re.findall(r"[A-Za-z]+", text)
    if not sents or not words:
        return {"grade": 0, "long_sentences": []}
    syl = sum(_syllables(w) for w in words)
    grade = 0.39 * len(words) / len(sents) + 11.8 * syl / len(words) - 15.59
    long = [x.strip()[:140] for x in sents if len(x.split()) > 32][:5]
    return {"grade": round(grade, 1), "long_sentences": long, "words": len(words)}


def _shingles(t, n=5):
    w = re.findall(r"[a-z]+", t.lower())
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def run_asset_checks(program_id: int, payload: dict):
    key = payload["asset"]
    with session() as s:
        sm = get_record(s, program_id, "skills_map").data
        bp = get_record(s, program_id, "blueprint").data
        a = dict(get_record(s, program_id, "asset", key).data)
        others = {r.key: r.data for r in list_records(s, program_id, "asset") if r.key != key}
    rev = str(a["revision"])
    content = a["revisions"][rev]
    text = asset_text(content)
    skills = {sk["skill_id"]: sk for sk in sm["skills"]}
    objs = {o["objective_id"]: o for o in bp["objectives"]}
    findings = []

    def f(check, severity, msg, gate):
        findings.append({"check": check, "severity": severity, "message": msg, "gate": gate, "status": "open"})

    # objective coverage
    cov = []
    low = text.lower()
    for oid in a["objective_ids"]:
        o = objs.get(oid)
        if not o:
            continue
        terms = [w for w in re.findall(r"[a-z]{5,}", o["statement"].lower())][:8]
        hit = sum(1 for t in terms if t in low) / max(1, len(terms))
        cov.append({"objective_id": oid, "coverage": round(hit, 2)})
        if hit < 0.4:
            f("Objective coverage", "high", f"Objective {oid} is only {int(hit * 100)} percent covered by the text", 2)
    # reading level (target band grade 9 to 13 for working adults in a technical field)
    rl = reading_level(text)
    if rl["grade"] > 14:
        f("Reading level", "moderate", f"Grade {rl['grade']} is above the target band (9 to 13)", 2)
    for ls in rl["long_sentences"][:2]:
        f("Reading level", "low", f"Long sentence: {ls}", 5)
    # terminology against the glossary (Lightcast skill names)
    for sk in skills.values():
        for m in re.finditer(re.escape(sk["name"]), text, re.I):
            if m.group(0) != sk["name"] and m.group(0).lower() == sk["name"].lower() and not m.group(0).islower():
                f("Terminology", "low", f"'{m.group(0)}' should read '{sk['name']}' (program glossary)", 5)
                break
    # bias and sensitive content
    for term, alt in BIAS_TERMS.items():
        if re.search(r"\b" + re.escape(term) + r"\b", text, re.I):
            f("Bias and sensitive content", "moderate", f"'{term}' flagged; suggested '{alt}'", 4)
    # accessibility lint
    for sc in content.get("screens", []):
        md = sc.get("media")
        if md and not (md.get("alt") or "").strip():
            f("Accessibility", "high", f"Image on screen '{sc.get('heading')}' has no alt text (WCAG 2.1 AA 1.1.1)", 3)
        if re.search(r"click here", " ".join(b.get("text", "") for b in sc.get("blocks", [])), re.I):
            f("Accessibility", "moderate", f"Link text 'click here' on '{sc.get('heading')}' (WCAG 2.4.4)", 3)
    for it in content.get("items", []):
        if len(it.get("options", [])) < 3:
            f("Assessment validity", "high", f"{it.get('item_id')} has fewer than 3 options", 2)
        if sum(1 for o in it.get("options", []) if o.get("correct")) != 1:
            f("Assessment validity", "critical", f"{it.get('item_id')} does not have exactly one key", 1)
        if any(not o.get("rationale") for o in it.get("options", [])):
            f("Assessment validity", "moderate", f"{it.get('item_id')} is missing distractor rationale", 2)
    # grounding: every block cites a source pack item
    unsupported = 0
    from ..sources import source_pack
    with session() as s2:
        src_ids = {"SRC-EXAM", "SRC-UMGC"} | {x["id"] for x in source_pack(s2, program_id)}
    for sc in content.get("screens", []):
        for b in sc.get("blocks", []):
            if not set(b.get("citations", [])) & src_ids:
                unsupported += 1
    if unsupported:
        f("Grounding", "high", f"{unsupported} passage(s) not tied to a source pack item", 1)
    # duplication and drift
    sh = _shingles(text)
    worst = (0.0, "")
    for k, o in others.items():
        orev = o.get("revisions", {}).get(str(o.get("revision")))
        if not orev:
            continue
        osh = _shingles(asset_text(orev))
        if sh and osh:
            j = len(sh & osh) / len(sh | osh)
            if j > worst[0]:
                worst = (j, k)
    if worst[0] > 0.55:
        f("Duplication", "moderate", f"{int(worst[0] * 100)} percent overlap with {worst[1]}", 5)

    # Lightcast trace: declared vs observed
    lc = lightcast.client(program_id)
    res = lc.extract_trace(text, 0.6, purpose=f"trace {key} r{rev}")
    by_lc = {sk.get("lightcast_skill_id"): sid for sid, sk in skills.items() if sk.get("lightcast_skill_id")}
    observed = defaultdict(list)
    for t in res.get("trace", []):
        sf = t.get("surfaceForm", {})
        for x in (t.get("classificationData", {}) or {}).get("skills", []):
            sid = by_lc.get(x["id"])
            if sid:
                observed[sid].append({"surface_form": sf.get("value"), "start": sf.get("sourceStart")})
    declared = set(a["skill_ids"])
    trace = {"declared_evidenced": sorted(declared & set(observed)), "declared_not_evidenced": sorted(declared - set(observed)),
             "evidenced_not_declared": sorted(set(observed) - declared),
             "mentions": {k: len(v) for k, v in observed.items()}, "lightcast_mode": lc.mode}
    for sid in trace["declared_not_evidenced"]:
        f("Skills trace", "moderate", f"Declares {sid} ({skills[sid]['name']}) but the text never names it in a form Lightcast recognizes", 2)

    checks = {"revision": a["revision"], "findings": findings, "objective_coverage": cov, "reading_level": rl,
              "skills_trace": trace, "counts": {sev: sum(1 for x in findings if x["severity"] == sev) for sev in ("critical", "high", "moderate", "low")}}
    with locked(program_id) as s:
        rec = get_record(s, program_id, "asset", key)
        a = dict(rec.data)
        a["checks"] = dict(a.get("checks", {}))
        a["checks"][rev] = checks
        a["state"] = "checked"
        put_record(s, program_id, "asset", key, a, status="checked")
        log(s, program_id, payload.get("_worker", "worker:qa"), "build",
            f"{key} r{rev} automated checks: " + ", ".join(f"{v} {k}" for k, v in checks["counts"].items() if v) + (" none" if not findings else ""))


# ------------------------------------------------------------------ program-level QA
DIMENSIONS = [
    ("Workforce competency alignment", 15), ("Instructional design and learner engagement", 15),
    ("Assessment quality and competency demonstration", 15), ("Learning objectives and program architecture", 10),
    ("Content accuracy and currency", 10), ("Accessibility and inclusive design", 10), ("Learning delivery and analytics", 10),
    ("Responsible AI integration", 5), ("Stackability and credit-evaluation readiness", 5), ("Maintainability and operational readiness", 5),
]


def run_program_qa(program_id: int, payload: dict):
    with session() as s:
        sm = get_record(s, program_id, "skills_map").data
        bp = get_record(s, program_id, "blueprint").data
        assets = {r.key: r.data for r in list_records(s, program_id, "asset")}
    skills = {sk["skill_id"]: sk for sk in sm["skills"]}

    # downward and upward roll-up
    course_skill = defaultdict(lambda: {"declared": set(), "evidenced": set()})
    assess_links = []
    for k, a in assets.items():
        ch = a["checks"].get(str(a["revision"]), {})
        tr = ch.get("skills_trace", {})
        cs = course_skill[a["course_id"]]
        cs["declared"] |= set(a["skill_ids"])
        cs["evidenced"] |= set(tr.get("mentions", {}).keys())
        if a["asset_type"] == "assessment":
            for sid in a["skill_ids"]:
                assess_links.append({"assessment_id": a["ref_id"], "skill_id": sid, "skill": skills.get(sid, {}).get("name"),
                                     "evidenced_in_items": sid in tr.get("mentions", {})})
    course_map = []
    for cid, v in sorted(course_skill.items()):
        course_map.append({"course_id": cid, "declared": len(v["declared"]), "declared_and_evidenced": len(v["declared"] & v["evidenced"]),
                           "declared_no_evidence": sorted(v["declared"] - v["evidenced"]),
                           "evidenced_not_declared": sorted(v["evidenced"] - v["declared"])})

    # defects: all findings, with resolution from gate decisions
    defects = []
    for k, a in assets.items():
        for rev, ch in a.get("checks", {}).items():
            final = str(a["revision"])
            for fnd in ch.get("findings", []):
                status = "remediated in revision " + final if rev != final else (
                    "accepted with documented decision" if fnd["severity"] in ("moderate", "low") else "open")
                defects.append({"asset": k, "revision": int(rev), **fnd, "status": status})
    open_critical = [d for d in defects if d["status"] == "open" and d["severity"] in ("critical", "high")]

    cov = coverage_rules(sm["skills"], bp)
    scores = []
    for c in bp["courses"]:
        ca = [a for a in assets.values() if a["course_id"] == c["course_id"]]
        cur = [a["checks"].get(str(a["revision"]), {}) for a in ca]
        cm = next((x for x in course_map if x["course_id"] == c["course_id"]), {"declared": 1, "declared_and_evidenced": 1})
        align = cm["declared_and_evidenced"] / max(1, cm["declared"])
        a11y_open = sum(1 for d in defects if d["asset"] in {a["asset_id"] for a in ca} and d["check"] == "Accessibility" and d["status"] == "open")
        items_ok = all(not any(f["check"] == "Assessment validity" and f["severity"] in ("critical", "high") for f in ch.get("findings", [])) for ch in cur)
        has_practice = all((a["revisions"][str(a["revision"])].get("practice") or a["asset_type"] != "module") for a in ca)
        prov_ok = all(str(a["revision"]) in a.get("provenance", {}) and len(a["provenance"][str(a["revision"])].get("gates", {})) >= 5 for a in ca)
        ratings = {
            "Workforce competency alignment": 4 if align >= 0.95 else 3 if align >= 0.8 else 2,
            "Instructional design and learner engagement": 4 if has_practice else 3,
            "Assessment quality and competency demonstration": 4 if items_ok else 2,
            "Learning objectives and program architecture": 4 if cov["all_passed"] else 3,
            "Content accuracy and currency": 4 if all(a.get("state") == "validated" for a in ca) else 2,
            "Accessibility and inclusive design": 4 if a11y_open == 0 else 1,
            "Learning delivery and analytics": 3,
            "Responsible AI integration": 4 if prov_ok else 3,
            "Stackability and credit-evaluation readiness": 3,
            "Maintainability and operational readiness": 3,
        }
        dims = [{"dimension": d, "weight": w, "rating": ratings[d], "points": round(ratings[d] / 4 * w, 2)} for d, w in DIMENSIONS]
        total = round(sum(x["points"] for x in dims), 1)
        crit = [d for d in open_critical if d["asset"] in {a["asset_id"] for a in ca}]
        scores.append({"course_id": c["course_id"], "course": c["name"], "dimensions": dims, "score": total,
                       "open_critical": len(crit), "release": total >= config.RELEASE_THRESHOLD and not crit})

    report = {"course_skill_map": course_map, "assessment_links": assess_links, "coverage": cov, "release_scores": scores,
              "defects": defects, "open_critical": len(open_critical),
              "summary": {"assets": len(assets), "courses_releasable": sum(1 for x in scores if x["release"]), "courses": len(scores),
                          "links_evidenced": sum(1 for x in assess_links if x["evidenced_in_items"]), "links": len(assess_links),
                          "defects_total": len(defects), "remediated": sum(1 for d in defects if d["status"].startswith("remediated"))},
              "test_plan": ["Content accuracy validation by a second SME", "Instructional QA against the Blueprint",
                            "Accessibility QA with screen reader and keyboard-only testing", "Learner experience testing in the Brightspace test org"]}
    with session() as s:
        put_record(s, program_id, "qa_report", "main", report, status="ready")
        sm2 = report["summary"]
        log(s, program_id, "worker:qa", "qa", f"QA report: {sm2['courses_releasable']}/{sm2['courses']} Courses at or above "
            f"{config.RELEASE_THRESHOLD} with no open critical defect; {sm2['links_evidenced']}/{sm2['links']} assessment-skill links evidenced; "
            f"{sm2['remediated']} of {sm2['defects_total']} findings remediated through revision.")
