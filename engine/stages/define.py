"""Stage 1 Define skills (Technical Proposal 2.2 Skills mapping, steps 1 to 10; 2.3.4 stage 1).

AI does: names the target occupation in the Lightcast Occupation Taxonomy, pulls 12 months of demand from
Job Postings Analytics, runs Lightcast extraction (with trace spans) on the source pack, resolves every skill
to one Lightcast skill ID, scores and tiers it.
People decide: SMEs approve each skill statement; three employers validate; UMGC approves at Gate 1.
"""
from __future__ import annotations

import datetime as dt
import hashlib

from .. import config
from ..clients import lightcast, llm
from ..db import Program, log, put_record, session

WEIGHTS = {"demand": 0.40, "sme": 0.30, "credential": 0.20, "entry": 0.10}


def source_pack_version(brief: dict, program_id: int | None = None) -> str:
    if program_id:
        from ..sources import pack_version, source_pack
        with session() as s:
            return pack_version(source_pack(s, program_id))
    raw = "".join(s.get("id", "") + s.get("text", "") for s in brief.get("sources", []))
    return "SP-" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def _tier(score):
    if score is None:
        return ""
    return "Core" if score >= 3.5 else ("Supporting" if score >= 2.5 else "Awareness")


def run(program_id: int, payload: dict):
    from ..sources import source_pack
    with session() as s:
        p = s.get(Program, program_id)
        brief = dict(p.brief)
        pack = source_pack(s, program_id)
    lc = lightcast.client(program_id)
    occ_in = brief.get("occupation", {})
    ver = lc.version()
    today = dt.date.today().isoformat()

    # Step 1-2: occupation in LOT
    occ = dict(occ_in)
    lot = lc.normalize_title(occ_in.get("title") or occ_in.get("lot_occupation_name", ""))
    if lot:
        occ["lot_occupation_id"] = lot.get("lot_occupation_id") or (lot.get("id") if isinstance(lot, dict) else None)
        occ["lot_source"] = "Lightcast Titles API normalize" + (" (MOCK)" if lot.get("mock") else "")
    else:
        occ["lot_occupation_id"] = "[fetched at Discovery: licensed LOT]"
        occ["lot_source"] = "Licensed LOT not enabled"

    # Step 3: extraction with trace on every source item
    spans_by_id: dict[str, list] = {}
    names_by_id: dict[str, str] = {}
    extraction_log = []
    for src in pack:
        res = lc.extract_trace(src.get("text", ""), 0.6, purpose=f"extract/trace {src.get('id')}")
        text = src.get("text", "")
        for t in res.get("trace", []):
            sf = t.get("surfaceForm", {})
            for sk in (t.get("classificationData", {}) or {}).get("skills", []):
                names_by_id[sk["id"]] = sk["name"]
                a, b = sf.get("sourceStart", 0), sf.get("sourceEnd", 0)
                ctx = text[max(0, a - 40):min(len(text), b + 40)].replace("\n", " ")
                spans_by_id.setdefault(sk["id"], []).append({"source_id": src.get("id"), "start": a, "end": b,
                                                             "surface_form": sf.get("value"), "context": ctx})
        extraction_log.append({"source_id": src.get("id"), "title": src.get("title"), "chars": len(text),
                               "skills_found": len(res.get("skills", [])), "mentions": len(res.get("trace", []))})

    # Step 4: candidate catalog (SME-curated reference, Claude clustering, or template)
    ref = (brief.get("reference") or {})
    if ref.get("skills"):
        catalog = [dict(sk) for sk in ref["skills"]]
        competencies = [dict(c) for c in ref.get("competencies", [])]
        catalog_origin = "Sample Program reference catalog (Exhibit I), validated against extraction"
    else:
        catalog, competencies, catalog_origin = None, None, ""
        cands = sorted(names_by_id.items(), key=lambda kv: -len(spans_by_id.get(kv[0], [])))
        if llm.available():
            out = llm.complete("skills.cluster",
                               "You are a learning experience architect building a skills map for a noncredit workforce Program.",
                               f"Occupation: {occ_in}\nCredential: {brief.get('credential')}\n"
                               f"Lightcast skills extracted (name: mentions): {[(n, len(spans_by_id.get(i, []))) for i, n in cands]}\n"
                               "Group them into 5 to 8 competencies. For each skill write one measurable skill statement. "
                               'Return {"competencies":[{"competency_id":"C1","name":"","definition":"","exit_level":"Proficient",'
                               '"performance_criteria":"","evidence":"","skill_ids":["S1.1"]}],"skills":[{"skill_id":"S1.1","name":"<exact Lightcast name>",'
                               '"statement":"","competency_id":"C1","exit_level":"Proficient"}]}',
                               program_id=program_id, want_json=True, max_tokens=6000)
            if out and out.get("skills"):
                catalog, competencies, catalog_origin = out["skills"], out["competencies"], "Claude clustering of Lightcast extraction"
        if catalog is None:
            catalog, competencies = _template_catalog(cands, spans_by_id)
            catalog_origin = "Template clustering of Lightcast extraction (mock mode)"

    # Step 5: resolve every catalog skill to one Lightcast skill ID
    for sk in catalog:
        hits = lc.search(sk["name"], 5)
        top = hits[0] if hits else None
        score = (top or {}).get("score", 0.9 if top and top["name"].lower() == sk["name"].lower() else 0.7) if top else 0
        sk["lightcast_skill_id"] = top["id"] if top else ""
        sk["lightcast_skill_name"] = top["name"] if top else ""
        sk["skill_type"] = ((top or {}).get("type") or {}).get("name", "")
        sk["category"] = ((top or {}).get("category") or {}).get("name", "")
        sk["subcategory"] = ((top or {}).get("subcategory") or {}).get("name", "")
        sk["info_url"] = (top or {}).get("infoUrl", "")
        sk["resolution_score"] = round(score, 3)
        sk["resolution_status"] = "resolved" if score >= 0.8 else ("review" if top else "unresolved")
        sk["alternatives"] = [f"{h['name']} ({h['id']})" for h in hits[1:4]]
        sk["source_spans"] = spans_by_id.get(sk["lightcast_skill_id"], [])[:6]
        sk["extraction_confidence"] = round(min(0.99, 0.6 + 0.1 * len(spans_by_id.get(sk["lightcast_skill_id"], []))), 3) \
            if sk["lightcast_skill_id"] in spans_by_id else None
        sk["taxonomy_version"] = ver.get("version")
        sk["retrieval_date"] = today

    # Step 6: demand from Job Postings Analytics (Maryland and national)
    names = [sk["lightcast_skill_name"] or sk["name"] for sk in catalog]
    spec = occ_in.get("lot_specialized_occupation_name") or occ_in.get("lot_occupation_name", "")
    md = lc.jpa_skill_demand(names, spec, "MD")
    us = lc.jpa_skill_demand(names, spec, None)
    demand_mode = "mock" if (md or {}).get("mock") else ("live" if md else "not licensed")
    max_share = max([((md or {}).get("skills", {}).get(n, {}) or {}).get("postings_share_pct") or 0 for n in names] + [0])

    # Step 7: score and tier (40 demand, 30 SME criticality, 20 credential weight, 10 entry-level fit)
    exam_counts = {}
    for sk in catalog:
        exam_counts[sk["skill_id"]] = sum(1 for sp in sk["source_spans"] if sp["source_id"] == "SRC-EXAM") or \
            len([1 for sp in sk["source_spans"]])
    max_exam = max(list(exam_counts.values()) + [1])
    for sk, n in zip(catalog, names):
        d_md = ((md or {}).get("skills", {}) or {}).get(n) or {}
        d_us = ((us or {}).get("skills", {}) or {}).get(n) or {}
        sk["unique_postings_md"] = d_md.get("unique_postings")
        sk["unique_postings_us"] = d_us.get("unique_postings")
        sk["postings_share_pct"] = d_md.get("postings_share_pct")
        sk["significance"] = d_md.get("significance")
        sk["demand_mode"] = demand_mode
        sk["demand_norm"] = round(5 * sk["postings_share_pct"] / max_share, 2) if (max_share and sk["postings_share_pct"] is not None) else None
        sk["credential_weight"] = round(1 + 4 * exam_counts[sk["skill_id"]] / max_exam, 2)
        sk.setdefault("sme_criticality", {"Core": 5, "Supporting": 4, "Awareness": 2}.get(sk.get("tier"), 3))
        sk.setdefault("entry_level_fit", 4)
        if sk["demand_norm"] is not None:
            score = (WEIGHTS["demand"] * sk["demand_norm"] + WEIGHTS["sme"] * sk["sme_criticality"]
                     + WEIGHTS["credential"] * sk["credential_weight"] + WEIGHTS["entry"] * sk["entry_level_fit"])
        else:  # demand pending Discovery: reweight the other three
            score = (0.5 * sk["sme_criticality"] + 0.333 * sk["credential_weight"] + 0.167 * sk["entry_level_fit"])
        sk["priority_score"] = round(score, 2)
        sk["tier_from_score"] = _tier(score)
        if not sk.get("tier"):
            sk["tier"] = sk["tier_from_score"]
        sk["tier_source"] = "SME-confirmed" if sk.get("tier") == sk["tier_from_score"] else "SME override of computed tier"
        sk.setdefault("exit_level", {"Core": "Proficient", "Supporting": "Working", "Awareness": "Aware"}.get(sk["tier"], "Working"))
        sk["sme_status"] = "proposed"

    in_catalog = {sk["lightcast_skill_id"] for sk in catalog}
    candidates = [{"lightcast_skill_id": i, "name": n, "mentions": len(spans_by_id.get(i, []))}
                  for i, n in names_by_id.items() if i not in in_catalog]

    data = {
        "taxonomy_version": ver.get("version"), "retrieval_date": today, "lightcast_mode": lc.mode,
        "demand_mode": demand_mode, "demand_total_md": (md or {}).get("total_postings"),
        "demand_total_us": (us or {}).get("total_postings"),
        "occupation": occ, "credential": brief.get("credential"), "catalog_origin": catalog_origin,
        "source_pack_version": source_pack_version(brief, program_id), "skills": catalog, "competencies": competencies,
        "candidates_not_in_catalog": candidates, "extraction_log": extraction_log,
        "employers": [], "weights": WEIGHTS,
    }
    with session() as s:
        put_record(s, program_id, "skills_map", "main", data, status="proposed")
        n_res = sum(1 for sk in catalog if sk["resolution_status"] == "resolved")
        log(s, program_id, "worker:skills", "define",
            f"Skills map drafted: {len(catalog)} skills in {len(competencies)} competencies, {n_res} resolved to Lightcast IDs "
            f"(taxonomy {ver.get('version')}, Lightcast {lc.mode}, demand {demand_mode}). Awaiting SME, employer and UMGC review.")


def _template_catalog(cands, spans):
    comps: dict[str, list] = {}
    for i, (sid, name) in enumerate(cands[:40]):
        comps.setdefault(f"C{min(8, i // 5 + 1)}", []).append((sid, name))
    catalog, competencies = [], []
    for ci, (cid, items) in enumerate(sorted(comps.items()), 1):
        ids = []
        for j, (_, name) in enumerate(items, 1):
            sid = f"S{ci}.{j}"
            ids.append(sid)
            catalog.append({"skill_id": sid, "name": name, "statement": f"Apply {name} to an entry-level work task and explain the result",
                            "competency_id": cid, "exit_level": "Proficient" if j <= 3 else "Working"})
        competencies.append({"competency_id": cid, "name": f"Competency {ci}: " + ", ".join(n for _, n in items[:2]),
                             "definition": "Groups the skills listed", "exit_level": "Proficient",
                             "performance_criteria": "Completes the performance task to rubric threshold",
                             "evidence": "Lab and knowledge check", "skill_ids": ids})
    return catalog, competencies
