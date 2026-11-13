"""Dashboard and API (Render web service).

Every page is server-rendered. People act at the gates; workers do the rest in the background.
"""
from __future__ import annotations

import base64
import json
import secrets
from collections import Counter, defaultdict
from pathlib import Path

from urllib.parse import quote, unquote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .. import config
from ..db import ApiCall, Event, Gate, Job, Program, Statement, WorkerBeat, get_record, init_db, list_records, now, session
from ..orchestrator import STAGES, VALIDATION_GATES, advance, bulk_approve, decide_gate, decide_ticket, rerun
from ..programs import autopilot_step, create_program, new_brief, sample_brief
from ..queue import enqueue, retry_failed
from ..stages import improve, learn
from .. import exports, sources, studio
from ..board import BOARD, GOVERNANCE, live_status

app = FastAPI(title="UMGC Skills Content Engine")
T = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
T.env.globals.update(config=config, STAGES=STAGES, VALIDATION_GATES=VALIDATION_GATES, enumerate=enumerate, sorted=sorted, len=len,
                     json=json, dict=dict, round=round, max=max, min=min, sum=sum, any=any, all=all)


@app.on_event("startup")
def _startup():
    init_db()
    if config.EMBEDDED_WORKERS:
        from ..worker import start_embedded
        start_embedded()


@app.middleware("http")
async def _auth(request: Request, call_next):
    if config.APP_PASSWORD and request.url.path not in ("/healthz",):
        h = request.headers.get("authorization", "")
        ok = False
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, config.APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Login required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Skills Content Engine"'})
    return await call_next(request)


def _r(url):
    return RedirectResponse(url, status_code=303)


def me(request) -> dict:
    raw = unquote(request.cookies.get("who", ""))
    name, _, role = raw.partition("|")
    return {"name": name, "role": role}


def ctx(request, program_id=None, **kw):
    d = {"request": request, "flash": request.query_params.get("msg", ""), "err": request.query_params.get("err", ""),
         "me": me(request), "ROLES": studio.ROLES}
    if program_id:
        with session() as s:
            p = s.get(Program, program_id)
            d["p"] = p
            d["pending_gates"] = s.query(Gate).filter(Gate.program_id == program_id, Gate.status == "pending").count()
            d["pending_umgc"] = s.query(Gate).filter(Gate.program_id == program_id, Gate.status == "pending", Gate.kind != "validation").count()
            d["jobs_open"] = s.query(Job).filter(Job.program_id == program_id, Job.status.in_(["queued", "running"])).count()
            d["jobs_failed"] = s.query(Job).filter(Job.program_id == program_id, Job.status == "failed").count()
            d["tickets_open"] = sum(1 for t in list_records(s, program_id, "ticket") if t.data["status"] == "proposed")
    d.update(kw)
    return d


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------ home, workers, jobs
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with session() as s:
        progs = list(s.execute(select(Program).order_by(Program.id.desc())).scalars())
        workers = list(s.execute(select(WorkerBeat).order_by(WorkerBeat.pool, WorkerBeat.name)).scalars())
        q = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    fresh = lambda w: (now() - w.last_seen).total_seconds() < 90
    return T.TemplateResponse(request, "index.html", ctx(request, progs=progs, workers=workers, fresh=fresh, q=q))


@app.post("/programs/sample")
def start_sample():
    pid = create_program(sample_brief())
    return _r(f"/p/{pid}?msg=Sample Program started. Workers are drafting the skills map.")


@app.get("/programs/new", response_class=HTMLResponse)
def new_form(request: Request):
    return T.TemplateResponse(request, "new.html", ctx(request))


@app.post("/programs/new")
def new_submit(name: str = Form(...), occupation: str = Form(...), lot: str = Form(""), specialized: str = Form(""), credential: str = Form(""),
               src1_title: str = Form("Credential exam objectives"), src1: str = Form(""), src2_title: str = Form("UMGC program outcomes"), src2: str = Form(""),
               src3_title: str = Form("Job posting sample"), src3: str = Form("")):
    sources = [{"id": f"SRC-{i}", "type": t, "title": t, "text": x} for i, (t, x) in
               enumerate([(src1_title, src1), (src2_title, src2), (src3_title, src3)], 1) if x.strip()]
    if sources:
        sources[0]["id"] = "SRC-EXAM"
        if len(sources) > 1:
            sources[1]["id"] = "SRC-UMGC"
    pid = create_program(new_brief(name, occupation, lot, specialized, credential, sources))
    return _r(f"/p/{pid}?msg=Program created.")


@app.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, program: int | None = None):
    with session() as s:
        q = select(Job).order_by(Job.id.desc()).limit(300)
        if program:
            q = q.where(Job.program_id == program)
        rows = list(s.execute(q).scalars())
    return T.TemplateResponse(request, "jobs.html", ctx(request, rows=rows))


@app.post("/jobs/retry")
def jobs_retry(program: int | None = Form(None)):
    n = retry_failed(program)
    if program:
        advance(program)
    return _r(f"/jobs?msg=Requeued {n} failed job(s)")


@app.get("/how", response_class=HTMLResponse)
def how(request: Request):
    return T.TemplateResponse(request, "how.html", ctx(request))


# ------------------------------------------------------------------ program pages
@app.get("/p/{pid}", response_class=HTMLResponse)
def program(request: Request, pid: int):
    with session() as s:
        gates = list(s.query(Gate).filter(Gate.program_id == pid, Gate.kind != "validation").order_by(Gate.id))
        vpend = Counter(g.evidence.get("gate") for g in s.query(Gate).filter(Gate.program_id == pid, Gate.kind == "validation", Gate.status == "pending"))
        events = list(s.query(Event).filter(Event.program_id == pid).order_by(Event.id.desc()).limit(40))
        assets = [r.data for r in list_records(s, pid, "asset")]
        jobq = dict(s.execute(select(Job.queue, func.count()).where(Job.program_id == pid, Job.status.in_(["queued", "running"])).group_by(Job.queue)).all())
        sm = get_record(s, pid, "skills_map")
        rels = [r for r in list_records(s, pid, "release")]
        p = s.get(Program, pid)
        live = live_status(s, pid, p)
    states = Counter(a.get("state") for a in assets)
    return T.TemplateResponse(request, "program.html", ctx(request, pid, gates=gates, vpend=vpend, events=events, states=states, n_assets=len(assets),
                                                  jobq=jobq, sm=sm.data if sm else None, rels=rels, BOARD=BOARD, GOV=GOVERNANCE, live=live))


@app.post("/p/{pid}/gate")
def gate_decide(request: Request, pid: int, gate_key: str = Form(...), decision: str = Form(...), by: str = Form(""), note: str = Form(""), back: str = Form("")):
    err = decide_gate(pid, gate_key, decision, by or me(request)["name"], note, role=me(request)["role"])
    back = back or f"/p/{pid}"
    sep = "&" if "?" in back else "?"
    return _r(f"{back}{sep}err={err}" if err else f"{back}{sep}msg=Decision recorded")


@app.post("/p/{pid}/bulk")
def gate_bulk(request: Request, pid: int, gate_no: int = Form(...), by: str = Form(""), note: str = Form("")):
    by = by or me(request)["name"]
    if not by.strip():
        return _r(f"/p/{pid}/build?err=Enter the reviewer's name")
    res = bulk_approve(pid, gate_no, by, note, role=me(request)["role"])
    msg = f"Gate {gate_no}: {res['approved']} approved"
    if res["skipped"]:
        msg += f"; {len(res['skipped'])} blocked by findings and need an individual decision"
    return _r(f"/p/{pid}/build?msg={msg}")


@app.post("/p/{pid}/autopilot")
def autopilot(pid: int):
    n = autopilot_step(pid, "Demo autopilot")
    return _r(f"/p/{pid}?msg=Autopilot recorded {n} decision(s). Gates with blocking findings were returned for revision.")


@app.post("/p/{pid}/pause")
def pause(pid: int):
    with session() as s:
        p = s.get(Program, pid)
        p.status = "running" if p.status == "paused" else "paused"
    advance(pid)
    return _r(f"/p/{pid}")


@app.get("/p/{pid}/skills", response_class=HTMLResponse)
def skills(request: Request, pid: int):
    with session() as s:
        sm = get_record(s, pid, "skills_map")
        bp = get_record(s, pid, "blueprint")
    return T.TemplateResponse(request, "skills.html", ctx(request, pid, sm=sm.data if sm else None, tm=(bp.data.get("trace_matrix", {}) if bp else {})))


@app.post("/p/{pid}/skills/edit")
def skills_edit(pid: int, skill_id: str = Form(...), statement: str = Form(""), tier: str = Form(""), sme_criticality: int = Form(0),
                lightcast_skill_id: str = Form(""), by: str = Form("")):
    with session() as s:
        r = get_record(s, pid, "skills_map")
        d = json.loads(json.dumps(r.data))
        for sk in d["skills"]:
            if sk["skill_id"] == skill_id:
                if statement:
                    sk["statement"] = statement
                if tier:
                    sk["tier"] = tier
                if sme_criticality:
                    sk["sme_criticality"] = sme_criticality
                if lightcast_skill_id and lightcast_skill_id != sk.get("lightcast_skill_id"):
                    sk["lightcast_skill_id"], sk["resolution_status"] = lightcast_skill_id, "set by reviewer"
                sk["sme_status"] = f"edited by {by or 'reviewer'}"
        r.data = d
    return _r(f"/p/{pid}/skills?msg=Skill {skill_id} updated")


@app.post("/p/{pid}/employers")
def employers(pid: int, e1: str = Form(""), e2: str = Form(""), e3: str = Form(""), by: str = Form("")):
    names = [e for e in (e1, e2, e3) if e.strip()]
    with session() as s:
        r = get_record(s, pid, "skills_map")
        d = dict(r.data)
        d["employers"] = names
        r.data = d
        g = s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key == "magic.employers").first()
        key = g.gate_key if g else None
    if len(names) < 3:
        return _r(f"/p/{pid}/skills?err=Record three validating employers")
    err = decide_gate(pid, key, "approve", by, "Validated by: " + "; ".join(names)) if key else "Gate not open"
    return _r(f"/p/{pid}/skills?" + (f"err={err}" if err else "msg=Employer validation recorded"))


@app.get("/p/{pid}/blueprint", response_class=HTMLResponse)
def blueprint(request: Request, pid: int):
    with session() as s:
        bp = get_record(s, pid, "blueprint")
        sm = get_record(s, pid, "skills_map")
    return T.TemplateResponse(request, "blueprint.html", ctx(request, pid, bp=bp.data if bp else None, sm=sm.data if sm else None))


@app.post("/p/{pid}/blueprint/regenerate")
def blueprint_regen(pid: int):
    with session() as s:
        n = s.query(Job).filter(Job.program_id == pid, Job.task == "design.blueprint").count()
        bp = get_record(s, pid, "blueprint")
        if bp:
            s.delete(bp)
        for j in s.query(Job).filter(Job.program_id == pid, Job.task == "design.blueprint"):
            j.dedupe_key = f"{j.dedupe_key}:old{n}"
    advance(pid)
    return _r(f"/p/{pid}/blueprint?msg=Blueprint regeneration queued")


@app.get("/p/{pid}/build", response_class=HTMLResponse)
def build_page(request: Request, pid: int, gate: int | None = None):
    with session() as s:
        assets = [r.data for r in list_records(s, pid, "asset")]
        vg = list(s.query(Gate).filter(Gate.program_id == pid, Gate.kind == "validation", Gate.status == "pending").order_by(Gate.id))
    return T.TemplateResponse(request, "build.html", ctx(request, pid, assets=assets, vg=vg, gate=gate))


@app.get("/p/{pid}/asset/{key}", response_class=HTMLResponse)
def asset_page(request: Request, pid: int, key: str, rev: int | None = None):
    with session() as s:
        a = get_record(s, pid, "asset", key).data
        sm = get_record(s, pid, "skills_map").data
        gates = list(s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key.like(f"val:{key}:%")).order_by(Gate.id))
    rev = rev or a["revision"]
    skills = {k["skill_id"]: k for k in sm["skills"]}
    return T.TemplateResponse(request, "asset.html", ctx(request, pid, a=a, rev=rev, content=a["revisions"].get(str(rev)), checks=a["checks"].get(str(rev)),
                                                prov=a["provenance"].get(str(rev)), gates=gates, skills=skills))


@app.get("/p/{pid}/trace", response_class=HTMLResponse)
def trace(request: Request, pid: int, skill: str | None = None, assessment: str | None = None):
    with session() as s:
        sm = get_record(s, pid, "skills_map")
        bp = get_record(s, pid, "blueprint")
        assets = {r.key: r.data for r in list_records(s, pid, "asset")}
    if not (sm and bp):
        return T.TemplateResponse(request, "trace.html", ctx(request, pid, ready=False))
    sm, bp = sm.data, bp.data
    skills = {k["skill_id"]: k for k in sm["skills"]}
    comps = {c["competency_id"]: c for c in sm["competencies"]}
    objs = {o["objective_id"]: o for o in bp["objectives"]}
    down = up = None
    if skill and skill in skills:
        sk = skills[skill]
        mods = [(c, m) for c in bp["courses"] for m in c["modules"] if skill in m.get("skill_ids", [])]
        asm = [a for a in bp["assessments"] if skill in a.get("measured", []) + a.get("practiced", [])]
        items, rubric = [], []
        for a in asm:
            ad = assets.get(f"A-{a['assessment_id']}")
            if ad and ad["revisions"].get(str(ad["revision"])):
                c = ad["revisions"][str(ad["revision"])]
                items += [(a["assessment_id"], it) for it in c.get("items", []) if it.get("skill_id") == skill]
                rubric += [(a["assessment_id"], r) for r in c.get("rubric", []) if r.get("skill_id") == skill]
        down = {"skill": sk, "competency": comps.get(sk.get("competency_id")), "objectives": [o for o in bp["objectives"] if skill in o.get("skill_ids", [])],
                "modules": mods, "assessments": asm, "items": items, "rubric": rubric,
                "credentials": [c for c in bp.get("credentials", [])], "occupation": sm["occupation"], "tm": bp["trace_matrix"].get(skill, {})}
    if assessment:
        a = next((x for x in bp["assessments"] if x["assessment_id"] == assessment), None)
        if a:
            ad = assets.get(f"A-{assessment}")
            up = {"assessment": a, "objectives": [objs[o] for o in a.get("objective_ids", []) if o in objs],
                  "skills": [skills[k] for k in a.get("measured", []) if k in skills],
                  "practiced": [skills[k] for k in a.get("practiced", []) if k in skills],
                  "competencies": sorted({skills[k]["competency_id"] for k in a.get("measured", []) if k in skills}),
                  "content": ad["revisions"].get(str(ad["revision"])) if ad else None, "occupation": sm["occupation"], "comps": comps}
    return T.TemplateResponse(request, "trace.html", ctx(request, pid, ready=True, sm=sm, bp=bp, down=down, up=up, skill=skill, assessment=assessment))


@app.get("/p/{pid}/quality", response_class=HTMLResponse)
def quality(request: Request, pid: int):
    with session() as s:
        qa = get_record(s, pid, "qa_report")
    return T.TemplateResponse(request, "quality.html", ctx(request, pid, qa=qa.data if qa else None))


@app.get("/p/{pid}/release", response_class=HTMLResponse)
def release_page(request: Request, pid: int):
    with session() as s:
        rels = sorted([(r.key, r.status, r.data) for r in list_records(s, pid, "release")], key=lambda t: [int(x) for x in t[0].split(".")])
    return T.TemplateResponse(request, "release.html", ctx(request, pid, rels=rels))


@app.get("/p/{pid}/learn", response_class=HTMLResponse)
def learn_page(request: Request, pid: int):
    with session() as s:
        cohorts = [r.data for r in list_records(s, pid, "cohort")]
        n_st = s.query(Statement).filter(Statement.program_id == pid).count()
        recent = list(s.query(Statement).filter(Statement.program_id == pid, Statement.verb == "asked").order_by(Statement.id.desc()).limit(12))
        mods = [(r.data["ref_id"], r.data["title"]) for r in list_records(s, pid, "asset") if r.data["asset_type"] == "module" and r.data.get("state") == "validated"]
    return T.TemplateResponse(request, "learn.html", ctx(request, pid, cohorts=cohorts, n_st=n_st, recent=recent, mods=mods))


@app.post("/p/{pid}/tutor")
async def tutor(pid: int, request: Request):
    body = await request.json()
    out = learn.tutor_answer(pid, body.get("q", ""), body.get("mode", "socratic"), body.get("module") or None)
    with session() as s:
        s.add(Statement(program_id=pid, learner="live-demo", verb="asked", object_id=f"M-{(out['sources'] or [{}])[0].get('module', 'NA')}",
                        extra={"topic": body.get("q", "")[:80], "category": out["category"]}))
    return JSONResponse(out)


@app.get("/p/{pid}/measure", response_class=HTMLResponse)
def measure_page(request: Request, pid: int):
    with session() as s:
        ms = sorted([r.data for r in list_records(s, pid, "metrics")], key=lambda d: [int(x) for x in d["release"].split(".")])
    return T.TemplateResponse(request, "measure.html", ctx(request, pid, ms=ms))


@app.get("/p/{pid}/improve", response_class=HTMLResponse)
def improve_page(request: Request, pid: int):
    with session() as s:
        tickets = [r.data for r in list_records(s, pid, "ticket")]
        sm = get_record(s, pid, "skills_map")
        assets = sorted(r.key for r in list_records(s, pid, "asset"))
    return T.TemplateResponse(request, "improve.html", ctx(request, pid, tickets=tickets, refresh=(sm.data.get("last_refresh") if sm else None), assets=assets))


@app.post("/p/{pid}/ticket")
def ticket_decide(pid: int, ticket_id: str = Form(...), decision: str = Form(...), by: str = Form(""), role: str = Form("Magic"),
                  change_class: str = Form("routine"), note: str = Form("")):
    err = decide_ticket(pid, ticket_id, decision, by, role, change_class, note)
    return _r(f"/p/{pid}/improve?" + (f"err={err}" if err else f"msg=Ticket {ticket_id} {decision}d"))


@app.post("/p/{pid}/event")
def raise_event(pid: int, title: str = Form(...), detail: str = Form(""), target: str = Form(""), change_class: str = Form("material")):
    tid = improve.raise_event(pid, title, detail, target, change_class)
    advance(pid)
    return _r(f"/p/{pid}/improve?msg=Ticket {tid} raised")


@app.post("/p/{pid}/refresh")
def refresh_now(pid: int):
    with session() as s:
        n = s.query(Job).filter(Job.program_id == pid, Job.task == "refresh.lightcast").count()
        enqueue(s, pid, "refresh", "refresh.lightcast", f"refresh.lightcast:manual{n + 1}", {})
    return _r(f"/p/{pid}/improve?msg=Lightcast refresh queued on the skills worker")


@app.get("/p/{pid}/api", response_class=HTMLResponse)
def api_log(request: Request, pid: int):
    with session() as s:
        calls = list(s.query(ApiCall).filter(ApiCall.program_id == pid).order_by(ApiCall.id.desc()).limit(400))
        by = Counter((c.service, c.mode, c.path.split("?")[0] if "/skills/" not in c.path or c.path.count("/") < 5 else "/skills/versions/latest/skills/{id}")
                     for c in s.query(ApiCall).filter(ApiCall.program_id == pid))
    return T.TemplateResponse(request, "api.html", ctx(request, pid, calls=calls, by=sorted(by.items(), key=lambda t: -t[1])))


# ------------------------------------------------------------------ downloads and JSON API
@app.get("/p/{pid}/download/workbook")
def dl_workbook(pid: int):
    return Response(exports.workbook(pid), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Traceability Workbook - Program {pid}.xlsx"'})


@app.get("/p/{pid}/download/trace.json")
def dl_trace(pid: int):
    return Response(exports.trace_json(pid), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="trace-program-{pid}.json"'})


@app.get("/p/{pid}/download/package/{version}")
def dl_package(pid: int, version: str):
    return Response(exports.package(pid, version), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="Program {pid} release {version}.zip"'})


@app.get("/api/programs/{pid}")
def api_program(pid: int):
    with session() as s:
        p = s.get(Program, pid)
        gates = [{"gate": g.gate_key, "label": g.label, "status": g.status, "owner": g.owner_role}
                 for g in s.query(Gate).filter(Gate.program_id == pid, Gate.status == "pending")]
        return {"id": p.id, "name": p.name, "stage": p.stage, "status": p.status, "release": p.release_version, "pending_gates": gates}


@app.get("/api/programs/{pid}/records/{kind}")
def api_records(pid: int, kind: str):
    with session() as s:
        return [{"key": r.key, "status": r.status, "version": r.version, "data": r.data} for r in list_records(s, pid, kind)]


# ------------------------------------------------------------------ identity
@app.post("/whoami")
def whoami(name: str = Form(""), role: str = Form(""), back: str = Form("/")):
    r = _r(back or "/")
    r.set_cookie("who", quote(f"{name.strip()}|{role}"), max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    return r


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request):
    m = me(request)
    rows = []
    with session() as s:
        progs = list(s.execute(select(Program)).scalars())
        for p in progs:
            for c in list_records(s, p.id, "comment"):
                d = c.data
                body = d["body"].lower()
                aliases = {m["name"].lower(), m["name"].split(" ")[0].lower(), m["role"].lower(),
                           m["role"].replace("Magic ", "").replace("UMGC ", "").lower(), m["role"].split(" (")[0].lower()}
                if "SME" in m["role"]:
                    aliases |= {"sme", "magic sme"}
                hit = any(al and "@" + al in body for al in aliases)
                if d["status"] == "open" and m["name"] and hit:
                    rows.append(("mention", p, d))
            for a in list_records(s, p.id, "asset"):
                o = a.data.get("owners") or {}
                if m["name"] and m["name"] in (o.get("author"), o.get("sme"), o.get("reviewer")) and a.data.get("state") != "validated":
                    rows.append(("assigned", p, a.data))
            gates = list(s.query(Gate).filter(Gate.program_id == p.id, Gate.status == "pending"))
            from ..orchestrator import role_allowed
            for g in gates:
                if m["role"] and role_allowed(g.kind, (g.evidence or {}).get("gate"), m["role"]):
                    rows.append(("gate", p, g))
    return T.TemplateResponse(request, "inbox.html", ctx(request, rows=rows))


# ------------------------------------------------------------------ authoring and collaboration zone
@app.get("/p/{pid}/studio", response_class=HTMLResponse)
def studio_home(request: Request, pid: int, zone: str = "ingest"):
    with session() as s:
        srcs = [r.data for r in list_records(s, pid, "source")]
        brief_srcs = s.get(Program, pid).brief.get("sources", [])
        pack = sources.source_pack(s, pid)
        assets = [r.data for r in list_records(s, pid, "asset")]
        comments = [r.data for r in list_records(s, pid, "comment")]
        tm = studio.team(s, pid)
    return T.TemplateResponse(request, "studio.html", ctx(request, pid, zone=zone, srcs=srcs, brief_srcs=brief_srcs, pack=pack,
                                                          pack_version=sources.pack_version(pack), assets=assets, comments=comments, team=tm))


@app.post("/p/{pid}/sources/upload")
async def source_upload(request: Request, pid: int, files: list[UploadFile] = File(...), source_type: str = Form("UMGC material"),
                        owner: str = Form("UMGC"), license_note: str = Form("")):
    m = me(request)
    keys, errs = [], []
    for f in files:
        data = await f.read()
        try:
            text, kind = sources.extract_text(f.filename or "upload.txt", data)
        except Exception as e:
            errs.append(f"{f.filename}: {type(e).__name__}")
            continue
        if not text.strip():
            errs.append(f"{f.filename}: no readable text (scanned PDF?)")
            continue
        keys.append(sources.add_source(pid, f.filename, source_type, text, f.filename, kind, owner, license_note, m["name"] or "unnamed"))
    q = f"msg=Uploaded {', '.join(keys)}; the ingest worker is tagging them to Lightcast skills" if keys else ""
    if errs:
        q += ("&" if q else "") + "err=" + "; ".join(errs)
    return _r(f"/p/{pid}/studio?zone=ingest&{q}")


@app.post("/p/{pid}/sources/paste")
def source_paste(request: Request, pid: int, title: str = Form(...), text: str = Form(...), source_type: str = Form("SME reference"),
                 owner: str = Form("Magic"), license_note: str = Form("")):
    k = sources.add_source(pid, title, source_type, text, title + ".txt", "text", owner, license_note, me(request)["name"] or "unnamed")
    return _r(f"/p/{pid}/studio?zone=ingest&msg=Source {k} queued for ingest")


@app.get("/p/{pid}/sources/{key}", response_class=HTMLResponse)
def source_view(request: Request, pid: int, key: str):
    with session() as s:
        d = get_record(s, pid, "source", key).data
    return T.TemplateResponse(request, "source.html", ctx(request, pid, src=d))


@app.post("/p/{pid}/sources/{key}/decide")
def source_decide(request: Request, pid: int, key: str, decision: str = Form(...), note: str = Form("")):
    m = me(request)
    err = sources.decide_source(pid, key, decision, m["name"], m["role"], note)
    return _r(f"/p/{pid}/sources/{key}?" + (f"err={err}" if err else "msg=Decision recorded. The source pack version changed."))


@app.get("/p/{pid}/studio/edit/{key}", response_class=HTMLResponse)
def studio_edit(request: Request, pid: int, key: str):
    with session() as s:
        a = get_record(s, pid, "asset", key).data
        comments = [r.data for r in list_records(s, pid, "comment") if r.data["asset"] == key]
        sm = get_record(s, pid, "skills_map")
        tm = studio.team(s, pid)
        pack = sources.source_pack(s, pid)
        p = s.get(Program, pid)
    working = (a.get("draft") or {}).get("content") or a["revisions"].get(str(a["revision"])) or {}
    ch = a["checks"].get(str(a["revision"]), {})
    return T.TemplateResponse(request, "editor.html", ctx(request, pid, a=a, c=working, comments=comments, findings=ch.get("findings", []),
                                                          skills={k["skill_id"]: k for k in (sm.data["skills"] if sm else [])}, team=tm,
                                                          pack_ids=[x["id"] for x in pack], live=bool(p.release_version)))


@app.post("/p/{pid}/studio/edit/{key}")
async def studio_save(request: Request, pid: int, key: str):
    form = dict((await request.form()).items())
    m = me(request)
    if not m["name"]:
        return _r(f"/p/{pid}/studio/edit/{key}?err=Tell us who you are (top right) before saving")
    with session() as s:
        a = get_record(s, pid, "asset", key).data
    content = studio.content_from_form(a, form)
    base = int(form.get("base_revision", a["revision"]))
    studio.save_draft(pid, key, content, m["name"], m["role"], base)
    if form.get("action") == "submit":
        res = studio.submit_draft(pid, key, m["name"], m["role"], form.get("change_class", "routine"), form.get("change_note", ""))
        if res.startswith("ticket:"):
            return _r(f"/p/{pid}/improve?msg=Your revision is ticket {res[7:]}. It publishes after the decision and the five gates.")
        if res:
            return _r(f"/p/{pid}/studio/edit/{key}?err={res}")
        return _r(f"/p/{pid}/asset/{key}?msg=Revision submitted. Automated checks are running; the five validation gates follow.")
    return _r(f"/p/{pid}/studio/edit/{key}?msg=Draft saved")


@app.post("/p/{pid}/studio/edit/{key}/discard")
def studio_discard(request: Request, pid: int, key: str):
    with session() as s:
        r = get_record(s, pid, "asset", key)
        d = dict(r.data)
        d["draft"] = None
        r.data = d
    return _r(f"/p/{pid}/studio/edit/{key}?msg=Draft discarded")


@app.post("/p/{pid}/studio/assist")
async def studio_assist(pid: int, request: Request):
    b = await request.json()
    return JSONResponse(sources.assist(pid, b.get("instruction", ""), b.get("heading", ""), b.get("current", ""), b.get("asset", "")))


@app.post("/p/{pid}/studio/comment")
def studio_comment(request: Request, pid: int, asset: str = Form(...), anchor: str = Form(""), body: str = Form(...), parent: str = Form("")):
    m = me(request)
    err = studio.add_comment(pid, asset, anchor, body, m["name"], m["role"], parent)
    return _r(f"/p/{pid}/studio/edit/{asset}?" + (f"err={err}" if err else "msg=Comment posted") + "#comments")


@app.post("/p/{pid}/studio/resolve")
def studio_resolve(request: Request, pid: int, cid: str = Form(...), asset: str = Form(...)):
    studio.resolve_comment(pid, cid, me(request)["name"])
    return _r(f"/p/{pid}/studio/edit/{asset}?msg=Comment resolved#comments")


@app.post("/p/{pid}/studio/owners")
def studio_owners(request: Request, pid: int, asset: str = Form(...), author: str = Form(""), sme: str = Form(""), reviewer: str = Form("")):
    studio.set_owners(pid, asset, author, sme, reviewer, me(request)["name"])
    return _r(f"/p/{pid}/studio/edit/{asset}?msg=Owners updated")


@app.post("/p/{pid}/team")
def team_save(request: Request, pid: int, roster: str = Form("")):
    members = []
    for ln in roster.splitlines():
        if not ln.strip():
            continue
        x = [c.strip() for c in ln.split("|")] + [""] * 6
        members.append({"name": x[0], "role": x[1], "org": "UMGC" if x[1].startswith("UMGC") else "Magic", "expertise": x[2], "credentials": x[3],
                        "courses": [c.strip() for c in x[4].split(",") if c.strip()], "location": x[5]})
    studio.save_team(pid, members, me(request)["name"])
    return _r(f"/p/{pid}/studio?zone=team&msg=Team saved")


@app.get("/p/{pid}/studio/tracker.csv")
def studio_tracker(pid: int):
    return Response(studio.tracker_csv(pid), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="Asset tracker - Program {pid} (Smartsheet import).csv"'})
