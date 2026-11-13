"""Stage 8 Improve (Technical Proposal 2.3.3.2 and 2.3.4 stage 8; 2.2.3.2 two change classes).

Three triggers, data-informed as the default:
  periodic         quarterly Lightcast refresh (cron) re-verifies every skill ID and re-pulls demand
  event-triggered  a framework, exam objective, tool or regulatory change entered by a person
  data-informed    thresholds on the Measure stage signals
Each trigger raises a ticket with an AI-proposed response. A person decides (routine or material change);
approved tickets reopen the affected asset as a new revision, which passes all five gates before release.
"""
from __future__ import annotations

import datetime as dt

from .. import config
from ..clients import lightcast, llm
from ..db import get_record, list_records, log, put_record, session


def _next_ticket_id(s, program_id):
    return f"T{len(list_records(s, program_id, 'ticket')) + 1:03d}"


def raise_ticket(s, program_id, signal, detail, suggests, target, proposal, change_class, trigger, release, dedupe):
    for t in list_records(s, program_id, "ticket"):
        if t.data.get("dedupe") == dedupe:
            return None
    tid = _next_ticket_id(s, program_id)
    put_record(s, program_id, "ticket", tid, {
        "ticket_id": tid, "trigger": trigger, "signal": signal, "detail": detail, "suggests": suggests, "target_asset": target,
        "ai_proposed_response": proposal, "proposed_change_class": change_class, "change_class": change_class, "status": "proposed",
        "release": release, "dedupe": dedupe, "opened_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "decision": None},
        status="proposed")
    log(s, program_id, "worker:improve", "improve", f"Ticket {tid} raised ({trigger}): {signal} on {target or 'Program'}.")
    return tid


def _propose(program_id, signal, detail, fallback):
    if llm.available():
        out = llm.complete("improve.proposal", "You propose one concrete content change for an instructional designer to review.",
                           f"Signal: {signal}\nDetail: {detail}\nWrite two sentences: the change and why.", program_id=program_id, max_tokens=200)
        if out:
            return out.strip()
    return fallback


def run_scan(program_id: int, payload: dict):
    release = payload.get("release", "1.0")
    T = config.THRESHOLDS
    with session() as s:
        m = get_record(s, program_id, "metrics", release).data
        assets = {r.key: r.data for r in list_records(s, program_id, "asset")}
    made = 0
    with session() as s:
        for md in m["modules"]:
            if md["launched"] >= 20 and md["dropoff_pct"] > T["module_dropoff_pct"]:
                d = f"{md['dropoff_pct']} percent of learners who opened {md['module_id']} left the Program there (threshold {T['module_dropoff_pct']})."
                made += bool(raise_ticket(s, program_id, "Module drop-off above threshold", d, "Content is too long, unclear, or poorly sequenced at that point",
                                          md["asset"], _propose(program_id, "drop-off", d, "Split the longest screen into two shorter screens and add a worked example before the practice task, so the learner sees the task done once before attempting it."),
                                          "material", "data-informed", release, f"drop:{md['asset']}:{release}"))
            if md["time_ratio"] and (md["time_ratio"] > T["time_on_task_ratio_high"] or md["time_ratio"] < T["time_on_task_ratio_low"]):
                d = f"Median time on {md['module_id']} is {md['time_ratio']}x the designed time ({md['median_min']} vs {md['expected_min']} minutes)."
                made += bool(raise_ticket(s, program_id, "Time-on-task anomaly", d, "Content is harder, easier, or more confusing than designed", md["asset"],
                                          _propose(program_id, "time-on-task", d, "Add step-by-step scaffolding to the lab instructions and a checkpoint after each step; re-check the reading level."),
                                          "routine", "data-informed", release, f"time:{md['asset']}:{release}"))
        for it in m["items"]:
            disc = it["discrimination"]
            bad = (disc is not None and disc < T["item_discrimination_min"]) or it["p_value"] < T["item_p_low"] or it["p_value"] > T["item_p_high"]
            if bad and it["n"] >= 30:
                why = ("Stronger learners are not more likely to answer it correctly." if (disc is not None and disc < T["item_discrimination_min"])
                       else "Almost no one answers it correctly." if it["p_value"] < T["item_p_low"] else "Almost everyone answers it correctly; it does not separate learners.")
                d = f"{it['item_id']}: p-value {it['p_value']}, discrimination {disc}. {why}"
                made += bool(raise_ticket(s, program_id, "Item difficulty or discrimination out of range", d, "Item is ambiguous, miskeyed, or not measuring the objective",
                                          f"A-{it['assessment']}", _propose(program_id, "item statistics", d, f"Rewrite the stem of {it['item_id']} so only one option is defensible and replace the distractor most chosen by high scorers; SME confirms the key."),
                                          "routine", "data-informed", release, f"item:{it['item_id']}:{release}"))
        for tc in m["tutor_clusters"]:
            if tc["questions"] >= T["tutor_cluster_min"]:
                d = f"{tc['questions']} tutor questions on '{tc['topic']}' from {tc['asset']} in this release."
                made += bool(raise_ticket(s, program_id, "Tutor question cluster on one topic", d, "A content gap or an unclear explanation", tc["asset"],
                                          _propose(program_id, "tutor cluster", d, f"Add a short explanation and an FAQ screen on {tc['topic']} drafted from the source pack, with one extra practice item."),
                                          "routine", "data-informed", release, f"tutor:{tc['asset']}:{tc['topic']}:{release}"))
        c = m["completion"]
        if c["within_150_pct"] < c["target_150_pct"]:
            raise_ticket(s, program_id, "Completion below the 150 percent target", f"{c['within_150_pct']} percent within 150 percent of expected time.",
                         "Program-level pacing or difficulty issue", "", "Review the drop-off and time-on-task tickets first; adjust pace support nudges.",
                         "material", "data-informed", release, f"completion:{release}")
        put_record(s, program_id, "scan", release, {"release": release, "tickets_raised": made}, status="done")
        log(s, program_id, "worker:improve", "improve", f"Release {release} scanned against thresholds: {made} ticket(s) raised for human decision.")


def run_refresh(program_id: int, payload: dict):
    """Quarterly Lightcast refresh (periodic trigger). Verifies IDs, stamps the taxonomy version, diffs demand."""
    with session() as s:
        sm = dict(get_record(s, program_id, "skills_map").data)
        prog_release = None
        rels = list_records(s, program_id, "release")
        if rels:
            prog_release = sorted(r.data["version"] for r in rels)[-1]
    lc = lightcast.client(program_id)
    ver = lc.version()
    missing, demand_shift = [], []
    for sk in sm["skills"]:
        if sk.get("lightcast_skill_id"):
            try:
                lc.get_skill(sk["lightcast_skill_id"])
            except Exception:
                missing.append(sk["skill_id"])
    names = [sk.get("lightcast_skill_name") or sk["name"] for sk in sm["skills"]]
    spec = sm["occupation"].get("lot_specialized_occupation_name") or sm["occupation"].get("lot_occupation_name", "")
    md = lc.jpa_skill_demand(names, spec + f" {dt.date.today().year}Q{(dt.date.today().month - 1) // 3 + 1}", "MD")
    if md:
        for sk, n in zip(sm["skills"], names):
            new = (md["skills"].get(n) or {}).get("unique_postings")
            old = sk.get("unique_postings_md")
            if old and new and abs(new - old) / old > 0.25:
                demand_shift.append((sk["skill_id"], sk["name"], old, new))
    quarter = f"{dt.date.today().year}Q{(dt.date.today().month - 1) // 3 + 1}"
    with session() as s:
        for sid in missing:
            raise_ticket(s, program_id, "Lightcast skill ID no longer resolves", f"{sid} did not verify against taxonomy {ver.get('version')}.",
                         "Taxonomy change", "", "Re-resolve the skill; the LXA picks the replacement ID from the alternatives.", "routine",
                         "periodic", prog_release, f"lcid:{sid}:{quarter}")
        for sid, name, old, new in demand_shift[:6]:
            raise_ticket(s, program_id, "External signal: labor market shift", f"{name} ({sid}) Maryland postings moved from {old} to {new}.",
                         "Content currency or tier at risk", "", f"Re-score {sid}; if its tier changes, adjust practice weight and assessment coverage.",
                         "material", "periodic", prog_release, f"demand:{sid}:{quarter}")
        sm["last_refresh"] = {"quarter": quarter, "taxonomy_version": ver.get("version"), "ids_verified": len(sm["skills"]) - len(missing),
                              "ids_missing": missing, "demand_shifts": len(demand_shift), "at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"}
        put_record(s, program_id, "skills_map", "main", sm)
        log(s, program_id, "worker:skills", "improve", f"Quarterly Lightcast refresh {quarter}: {len(sm['skills']) - len(missing)} IDs verified, "
            f"{len(missing)} missing, {len(demand_shift)} demand shifts over 25 percent.")


def raise_event(program_id: int, title: str, detail: str, target: str, change_class: str = "material"):
    with session() as s:
        rels = list_records(s, program_id, "release")
        rel = sorted(r.data["version"] for r in rels)[-1] if rels else None
        return raise_ticket(s, program_id, f"Event: {title}", detail, "Content currency at risk", target,
                            "Compare the affected content to the new source; draft the changed screens and items for SME validation.",
                            change_class, "event-triggered", rel, f"event:{title}:{target}:{dt.datetime.utcnow().isoformat()}")
