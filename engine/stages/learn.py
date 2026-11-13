"""Stage 6 Learn (Technical Proposal 2.3.3.3 and 2.3.4 stage 6).

Two parts:
  tutor_answer()   the contextual AI tutor: answers only from approved content, Socratic by default for practice,
                   declines summative work, redirects out-of-scope questions, always offers a path to a person.
  run_cohort()     a synthetic learner cohort that exercises the released Program and writes xAPI statements, so the
                   Measure and Improve stages have real signals to work on before live UMGC learners arrive.
                   In production the same statements arrive from the delivery platform's LRS.
"""
from __future__ import annotations

import hashlib
import math
import random
import re

from ..clients import llm
from ..db import Program, Statement, get_record, list_records, log, put_record, session
from .qa import asset_text

SUMMATIVE = re.compile(r"\b(do|complete|finish|write|solve|answer|give me)\b.*\b(lab|exam|quiz|capstone|assessment|knowledge check|test)\b", re.I)
SENSITIVE = re.compile(r"\b(hack (my|into)|someone's password|steal|ddos (a|my)|self-harm|suicide|kill|weapon)\b", re.I)


STOP = set("the and for what how does with this that are can you explain when would use using used configure set setup best way about into from your have need tell show give make get why which who where there their them then than will just like know more some any".split())


def _tok(t):
    return [w for w in re.findall(r"[a-z0-9+]{3,}", t.lower()) if w not in STOP]


def approved_passages(s, program_id):
    out = []
    for r in list_records(s, program_id, "asset"):
        a = r.data
        if a["asset_type"] != "module" or a.get("state") != "validated":
            continue
        c = a["revisions"][str(a["revision"])]
        for sc in c.get("screens", []):
            txt = " ".join(b.get("text", "") for b in sc.get("blocks", []))
            out.append({"module": a["ref_id"], "title": a["title"], "section": sc.get("heading"), "text": txt,
                        "tokens": set(_tok(sc.get("heading", "") + " " + txt)), "head": set(_tok(sc.get("heading", "")))})
    return out


def tutor_answer(program_id: int, question: str, mode: str = "socratic", module: str | None = None, log_it=True):
    with session() as s:
        passages = approved_passages(s, program_id)
    q = question.strip()
    if SENSITIVE.search(q):
        return {"category": "sensitive", "answer": "I can't help with that. If you are worried about your safety or someone else's, "
                "please use the 'talk to a person' link and a UMGC advisor will follow up.", "sources": [], "declined": True}
    if SUMMATIVE.search(q):
        return {"category": "summative", "declined": True, "sources": [],
                "answer": "I can't complete graded work for you, but I can help you think it through. What is the first piece of evidence you would "
                          "look at, and which skill from this Module does it call for?"}
    qt = set(_tok(q))
    pool = [p for p in passages if (not module or p["module"] == module)]
    df = {}
    for p in pool:
        for w in p["tokens"]:
            df[w] = df.get(w, 0) + 1
    N = max(1, len(pool))
    idf = lambda w: math.log((N + 1) / (df.get(w, 0) + 0.5))
    def score(p):
        ov = qt & p["tokens"]
        return sum(idf(w) for w in ov) / math.sqrt(max(1, len(qt))) + 0.8 * len(qt & p.get("head", set())), len(ov)
    scored = sorted(((score(p), p) for p in pool), key=lambda t: -t[0][0])
    # in scope only when the question shares distinctive vocabulary with approved content
    top = [p for (sc, n), p in scored[:3] if sc >= 1.6 and (n >= 2 or sc >= 2.2)]
    if not top:
        return {"category": "out_of_scope", "declined": True, "sources": [],
                "answer": "That topic is outside this Program's approved content, so I won't guess. Your instructor or a UMGC coach can point you "
                          "to the right resource, or ask me about anything in the current Course."}
    sources = [{"module": p["module"], "section": p["section"]} for p in top]
    if llm.available():
        ctx = "\n\n".join(f"[Module {p['module']} | {p['section']}] {p['text']}" for p in top)
        style = ("Socratic mode: ask one guiding question and give a hint, do not give the full answer." if mode == "socratic"
                 else "Explain clearly in 3 to 5 sentences.")
        txt = llm.complete("tutor.answer", "You are the AI Study Assistant inside a UMGC noncredit Course. Answer only from the passages. " + style,
                           f"PASSAGES\n{ctx}\n\nLEARNER QUESTION\n{q}", program_id=program_id if log_it else None, max_tokens=500)
        if txt:
            return {"category": "in_scope", "answer": txt, "sources": sources, "declined": False}
    p = top[0]
    first = re.split(r"(?<=\.)\s", p["text"])[0]
    if mode == "socratic":
        ans = f"Good question. Module {p['module']} ('{p['section']}') covers this: {first} Before I say more: how would you apply that to the scenario you are working on?"
    else:
        ans = f"From Module {p['module']} ('{p['section']}'): " + " ".join(re.split(r"(?<=\.)\s", p["text"])[:2])
    return {"category": "in_scope", "answer": ans, "sources": sources, "declined": False}


def _h01(s):
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def hotspots(bp):
    """Where the synthetic cohort plants the signals the Improve stage must find (only while revision 1 is live)."""
    cs = bp["courses"]
    mid = cs[max(0, len(cs) // 2 - 1)]
    late = cs[max(0, len(cs) - 3)]
    kc = next((a["assessment_id"] for a in bp["assessments"] if a["course_id"] == mid["course_id"] and a["type"] == "knowledge check"), None)
    first_mods = cs[0]["modules"]
    return {"dropoff_module": mid["modules"][min(1, len(mid["modules"]) - 1)]["module_id"],
            "slow_module": late["modules"][-1]["module_id"],
            "ambiguous_item": f"{kc}-I03" if kc else None,
            "tutor_module": first_mods[min(len(first_mods) - 1, 5)]["module_id"]}


def run_cohort(program_id: int, payload: dict):
    n_learners = int(payload.get("learners", 120))
    release = payload.get("release", "1.0")
    with session() as s:
        bp = get_record(s, program_id, "blueprint").data
        sm = get_record(s, program_id, "skills_map").data
        assets = {r.key: r.data for r in list_records(s, program_id, "asset")}
        s.query(Statement).filter(Statement.program_id == program_id).delete()
    rng = random.Random(f"{program_id}-{release}")
    hs = hotspots(bp)
    rev = lambda key: assets.get(key, {}).get("revision", 1)
    skills = {x["skill_id"]: x for x in sm["skills"]}
    modules = [(c, m) for c in bp["courses"] for m in c["modules"]]
    exp_min = {}
    for c in bp["courses"]:
        per = (c.get("online_hours", 0) + c.get("practice_hours", 0) + c.get("reading_hours", 0)) * 60 / max(1, len(c["modules"]))
        for m in c["modules"]:
            exp_min[m["module_id"]] = round(per, 1)
    items_by_course = {}
    for a in assets.values():
        if a["asset_type"] == "assessment" and a["type"] == "knowledge check":
            content = a["revisions"].get(str(a["revision"]), {})
            items_by_course.setdefault(a["course_id"], []).append((a, content.get("items", [])))
    rows = []
    for i in range(n_learners):
        L = f"L{i + 1:03d}"
        theta = rng.gauss(0, 1)
        pace = math.exp(rng.gauss(0.03, 0.28))
        total = 0.0
        done_course = None
        dropped = False
        for idx, (c, m) in enumerate(modules):
            mid = m["module_id"]
            p_drop = 0.003 + (0.16 if (mid == hs["dropoff_module"] and rev(f"M-{mid}") == 1) else 0) + (0.01 if theta < -1.3 else 0)
            ratio = 2.4 if (mid == hs["slow_module"] and rev(f"M-{mid}") == 1) else 1.0
            dur = exp_min[mid] * pace * ratio * (1 + rng.gauss(0, 0.12))
            rows.append(dict(program_id=program_id, learner=L, verb="launched", object_id=f"M-{mid}", extra={"release": release}))
            if rng.random() < p_drop:
                rows.append(dict(program_id=program_id, learner=L, verb="dropped", object_id=f"M-{mid}", duration_min=round(dur * 0.4, 1), extra={"release": release}))
                dropped = True
                break
            total += dur
            rows.append(dict(program_id=program_id, learner=L, verb="completed", object_id=f"M-{mid}", duration_min=round(dur, 1),
                             extra={"release": release, "expected_min": exp_min[mid]}))
            # tutor questions on this Module's skills
            nq = sum(1 for _ in range(3) if rng.random() < 0.04)
            if mid == hs["tutor_module"] and rev(f"M-{mid}") == 1:
                nq += 1 if rng.random() < 0.55 else 0
            for _ in range(nq):
                sk = skills.get(rng.choice(m["skill_ids"])) if m.get("skill_ids") else None
                rows.append(dict(program_id=program_id, learner=L, verb="asked", object_id=f"M-{mid}",
                                 extra={"release": release, "topic": sk["name"] if sk else m["title"], "skill_id": sk["skill_id"] if sk else None}))
            last_of_course = idx + 1 == len(modules) or modules[idx + 1][0]["course_id"] != c["course_id"]
            if last_of_course and done_course != c["course_id"]:
                done_course = c["course_id"]
                for a, items in items_by_course.get(c["course_id"], []):
                    correct = 0
                    for it in items:
                        b = (_h01(it["item_id"]) - 0.5) * 2.4
                        if it["item_id"] == hs["ambiguous_item"] and a["revision"] == 1:
                            pc = 0.55 - 0.18 * math.tanh(theta)
                        else:
                            pc = 1 / (1 + math.exp(-(1.3 * (theta - b) + 0.9)))
                        ok = rng.random() < pc
                        correct += ok
                        rows.append(dict(program_id=program_id, learner=L, verb="answered", object_id=it["item_id"], result_success=ok,
                                         extra={"release": release, "assessment": a["ref_id"], "skill_id": it.get("skill_id")}))
                    if items:
                        sc = correct / len(items)
                        rows.append(dict(program_id=program_id, learner=L, verb="scored", object_id=a["asset_id"], result_score=round(sc, 3),
                                         result_success=sc >= 0.7, extra={"release": release}))
        if not dropped:
            rows.append(dict(program_id=program_id, learner=L, verb="completed", object_id="PROGRAM", duration_min=round(total, 1),
                             extra={"release": release, "expected_min": round(sum(exp_min.values()), 1)}))
    with session() as s:
        s.bulk_insert_mappings(Statement, rows)
        put_record(s, program_id, "cohort", release, {"release": release, "learners": n_learners, "statements": len(rows), "hotspots": hs}, status="done")
        log(s, program_id, "worker:learn", "learn", f"Release {release}: synthetic cohort of {n_learners} learners produced {len(rows)} xAPI statements "
            f"(the delivery platform's LRS replaces this feed with live UMGC learners).")
