"""AI governance (Technical Proposal 2.3.3: governed AI; RFP Attachment A 9 to 11: data, security, identity).

Workspace-level registers kept in the settings table, so they apply to every Program:
  models         which model does what, data handling, live or mock
  prompts        every prompt ID with its version, owner, and the evaluation it must pass before a version change
  evaluations    evaluation sets and every run (tutor set at release; asset checks on every revision)
  incidents      AI incidents: report, triage, resolve, with the asset or release affected
  changes        change log for models, prompts and evaluation thresholds
  subprocessors  services that touch UMGC data and what they receive
"""
from __future__ import annotations

import json

from sqlalchemy import func, select

from . import config
from .clients import llm
from .db import ApiCall, Setting, list_records, now, session


def _get(key, default):
    with session() as s:
        r = s.get(Setting, f"gov:{key}")
        if r and r.value:
            try:
                return json.loads(r.value)
            except Exception:
                pass
    return default


def _put(key, value):
    with session() as s:
        r = s.get(Setting, f"gov:{key}")
        if r is None:
            s.add(Setting(key=f"gov:{key}", value=json.dumps(value)))
        else:
            r.value = json.dumps(value)


PROMPT_INFO = {
    "skills.cluster": ("Stage 1 Define", "Groups Lightcast skills into competencies and writes skill statements", "SME review of every statement; Gate 1"),
    "design.blueprint": ("Stage 2 Design", "Drafts Courses, Modules, objectives and assessment allocation", "Five coverage rules and DAG check; Gate 2"),
    "build.module": ("Stage 3 Build", "Drafts Module screens from the approved source pack with citations", "Automated checks and five validation gates"),
    "build.assessment": ("Stage 3 Build", "Drafts items, keys, rationales and rubrics mapped to skills", "Automated checks and five validation gates"),
    "improve.proposal": ("Stage 8 Improve", "Drafts the proposed response on an improvement ticket", "Ticket decision by change class"),
    "tutor.answer": ("Stage 6 Learn", "Answers learners from the released Course content only", "Tutor evaluation set at 100 percent before release"),
}


def models():
    return _get("models", [
        {"name": config.ANTHROPIC_MODEL, "provider": "Anthropic (Claude API)", "use": "Drafting (skills statements, Blueprint, Modules, items), tutor answers, ticket proposals",
         "mode": "live" if config.LLM_LIVE else "mock (template drafting; no model calls)", "data": "Source pack excerpts and draft text; no learner PII sent",
         "retention": "API inputs are not used for model training (commercial terms)", "approved_by": "Magic AI Governance Lead", "status": "approved"},
        {"name": "Lightcast Open Skills, LOT, Job Postings Analytics", "provider": "Lightcast", "use": "Skill extraction with trace, skill resolution, occupation, demand",
         "mode": "live" if config.LIGHTCAST_LIVE else "mock (MOCK- IDs)", "data": "Source text for extraction; skill and title names",
         "retention": "Per UMGC Lightcast license", "approved_by": "UMGC (UMGC license)", "status": "approved"},
    ])


def prompts():
    stored = _get("prompts", {})
    usage = {}
    with session() as s:
        for purpose, n in s.execute(select(ApiCall.purpose, func.count()).where(ApiCall.service == "claude").group_by(ApiCall.purpose)).all():
            pid = (purpose or "").split(" v")[0]
            usage[pid] = usage.get(pid, 0) + n
    out = []
    for pid, ver in llm.PROMPTS.items():
        stage, what, check = PROMPT_INFO.get(pid, ("", "", ""))
        st = stored.get(pid, {})
        out.append({"prompt_id": pid, "version": ver, "stage": stage, "purpose": what, "release_check": check, "owner": st.get("owner", "Magic AI Governance Lead"),
                    "last_eval": st.get("last_eval", ""), "calls": usage.get(pid, 0), "notes": st.get("notes", "")})
    return out


def eval_sets(s=None):
    from .db import Program, get_record
    from .stages.deliver import tutor_eval_set
    rows = [{"set": "Asset checks", "scope": "Every revision of every asset", "cases": "Citations, accessibility lint, reading level, terminology, bias terms, similarity, Lightcast trace",
             "threshold": "No critical or high finding at gates; release score 85 or higher", "runs_on": "qa worker"}]
    with session() as s:
        for p in s.execute(select(Program)).scalars():
            bp, sm = get_record(s, p.id, "blueprint"), get_record(s, p.id, "skills_map")
            if bp and sm:
                rows.append({"set": f"Tutor evaluation set: {p.name}", "scope": "Before each release that changes Modules",
                             "cases": f"{len(tutor_eval_set(bp.data, sm.data))} cases: in scope, summative refusal, out of scope, sensitive",
                             "threshold": "100 percent expected behavior", "runs_on": "deliver worker"})
    return rows


def eval_runs():
    from .db import Program
    out = []
    with session() as s:
        for p in s.execute(select(Program)).scalars():
            for r in list_records(s, p.id, "release"):
                ev = r.data.get("tutor_evaluation")
                if ev:
                    n = len(ev.get("cases", []))
                    ok = sum(1 for c in ev.get("cases", []) if c.get("passed"))
                    out.append({"program": p.name, "release": r.key, "at": r.data.get("created_at", ""), "passed": ok, "total": n,
                                "by_category": ", ".join(f"{k} {v['passed']}/{v['total']}" for k, v in ev.get("by_category", {}).items()),
                                "result": "pass" if ev.get("passed") else "fail"})
    return out


def incidents():
    return _get("incidents", [])


def report_incident(title, detail, severity, program, asset, by):
    rows = incidents()
    iid = f"AI-{len(rows) + 1:03d}"
    rows.append({"id": iid, "title": title, "detail": detail, "severity": severity, "program": program, "asset": asset, "status": "open",
                 "reported_by": by, "reported_at": now().isoformat(timespec="minutes"), "resolution": "", "resolved_by": ""})
    _put("incidents", rows)
    log_change(f"Incident {iid} reported: {title}", by)
    return iid


def resolve_incident(iid, resolution, by):
    rows = incidents()
    for r in rows:
        if r["id"] == iid:
            if not resolution.strip():
                return "Describe the resolution."
            r.update({"status": "resolved", "resolution": resolution, "resolved_by": by, "resolved_at": now().isoformat(timespec="minutes")})
    _put("incidents", rows)
    log_change(f"Incident {iid} resolved", by)
    return ""


def changes():
    return _get("changes", [{"at": "2026-11-13", "by": "Magic AI Governance Lead", "change": "Prompt register baselined at the versions shown; tutor threshold 100 percent."}])


def log_change(change, by):
    rows = changes()
    rows.append({"at": now().isoformat(timespec="minutes"), "by": by, "change": change})
    _put("changes", rows[-300:])


def save_prompt_note(pid, owner, notes, by):
    st = _get("prompts", {})
    st[pid] = {**st.get(pid, {}), "owner": owner, "notes": notes, "last_eval": st.get(pid, {}).get("last_eval", "")}
    _put("prompts", st)
    log_change(f"Prompt {pid}: owner {owner}; {notes[:120]}", by)


SUBPROCESSORS = [
    {"name": "Render (render.com)", "role": "Hosting for this engine: web service, workers, Postgres", "data": "Program records, drafts, audit log, user accounts (no learner PII in this engine)", "region": "US (Oregon)"},
    {"name": "Anthropic", "role": "Claude API for drafting and the tutor", "data": "Source excerpts, draft text, learner questions without identity", "region": "US"},
    {"name": "Lightcast", "role": "Skills, occupation and demand data (UMGC license)", "data": "Source text for extraction; skill names", "region": "US"},
    {"name": "UMGC delivery platform (Brightspace)", "role": "Delivers released content; holds learner identity, roster and grades", "data": "Released packages; xAPI returned for analytics", "region": "UMGC"},
]

DATA_HANDLING = [
    "This engine holds no learner PII. Analytics use pseudonymous learner IDs; the delivery platform holds identity (FERPA).",
    "AI drafts are grounded only in the approved source pack; every claim carries a source citation.",
    "Every AI call is logged with prompt ID and version; every asset revision records model, prompt, source pack and taxonomy versions.",
    "No asset reaches a learner without the five validation gates; no release without UMGC approval or a standing approval for routine change.",
    "API keys live only in the hosting provider's environment settings, never in code or in the browser.",
    "Sessions are signed and expire after 12 hours; passwords are stored as salted PBKDF2 hashes; permissions are checked on the server for every action.",
]
