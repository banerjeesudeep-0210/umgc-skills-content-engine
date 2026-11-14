"""The Skills Content Engine web app (Render web service).

Product structure (Product Spec v2): a workspace (Programs, My work, Governance, Admin) and, per Program, sections in
workflow order: Overview, Setup, Skills Studio, Design Studio, Development Studio, Quality and Release, Learner
Experience, Insights, Improvement, Traceability, Audit.

Security: every request needs a signed session; every action checks a named permission on the server (engine.rbac).
Live co-editing: shared documents and grids sync over /collab/<room> (Yjs); autosave writes the result to the
workflow records through the same permission checks.
"""
from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .. import auth, collab, config, content_html, docsgen, edits, exports, governance, outline as outlinemod, quality, rbac, sources, spec as specmod, studio
from ..board import BOARD, live_status
from ..db import ApiCall, Event, Gate, Job, Program, Statement, User, WorkerBeat, get_record, init_db, list_records, locked, log, now, put_record, session
from ..orchestrator import STAGES, UMGC_GATES, VALIDATION_GATES, advance, bulk_approve, decide_gate, decide_ticket
from ..programs import autopilot_step, create_program, new_brief, sample_brief
from ..queue import enqueue, retry_failed
from ..stages import improve, learn
from ..stages.qa import asset_text

HERE = Path(__file__).parent
app = FastAPI(title="UMGC Skills Content Engine")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
T = Jinja2Templates(directory=str(HERE / "templates"))
T.env.globals.update(config=config, STAGES=STAGES, VALIDATION_GATES=VALIDATION_GATES, UMGC_GATES=UMGC_GATES, enumerate=enumerate, sorted=sorted,
                     len=len, json=json, dict=dict, round=round, max=max, min=min, sum=sum, any=any, all=all, list=list, str=str, int=int,
                     ROLES=rbac.ROLES, LABELS=rbac.LABELS, PERMS=rbac.PERMS, CRITERIA=quality.CRITERIA, AT_TESTS=quality.AT_TESTS)

PUBLIC = ("/login", "/healthz", "/static", "/first-run", "/favicon.ico")

SECTIONS = [
    ("overview", "Overview", "", None), ("setup", "Setup", "/setup", None), ("skills", "Skills Studio", "/skills", 1),
    ("design", "Design Studio", "/design", 2), ("develop", "Development Studio", "/develop", 3), ("quality", "Quality and Release", "/quality", 4),
    ("learner", "Learner Experience", "/learner", 6), ("insights", "Insights", "/insights", 7), ("improve", "Improvement", "/improve", 8),
    ("trace", "Traceability", "/trace", None), ("audit", "Audit", "/audit", None),
]
TABS = {
    "setup": [("spec", "Program specification"), ("team", "Team and SMEs"), ("schedule", "Schedule and milestones")],
    "skills": [("sources", "Sources"), ("map", "Skills map"), ("competencies", "Competencies"), ("validation", "Validation and Gate 1")],
    "design": [("courses", "Courses"), ("modules", "Modules"), ("objectives", "Objectives"), ("assessments", "Assessments"),
               ("coverage", "Coverage and seat time"), ("gate", "Gate 2")],
    "develop": [("outline", "Module outline"), ("weekly", "Weekly map"), ("board", "Assets"), ("reviews", "Review gates"), ("comments", "Comments"), ("guides", "Guides")],
    "quality": [("qa", "QA and test plan"), ("a11y", "Accessibility record"), ("sampling", "Quality sampling"), ("deliverables", "Deliverables and acceptance"),
                ("releases", "Releases")],
    "learner": [("preview", "Learner preview"), ("tutor", "AI tutor"), ("integration", "Integration")],
    "insights": [("dashboard", "Dashboard"), ("reports", "Reports"), ("demographics", "Demographics")],
    "improve": [("tickets", "Tickets"), ("refresh", "Lightcast refresh"), ("recurring", "Recurring reports"), ("releases", "Maintenance releases")],
    "trace": [("explorer", "Trace explorer"), ("workbook", "Workbook"), ("attd", "Attachment D packet"), ("exports", "Exports")],
    "audit": [("activity", "Activity"), ("history", "Version history"), ("api", "API calls")],
}


@app.on_event("startup")
def _startup():
    init_db()
    if config.EMBEDDED_WORKERS:
        from ..worker import start_embedded
        start_embedded()


# ------------------------------------------------------------------ session and permissions
@app.middleware("http")
async def _session(request: Request, call_next):
    path = request.url.path
    u = auth.user_by_id(auth.read_cookie(request.cookies.get(auth.COOKIE)))
    request.state.user = u
    if path.startswith(PUBLIC):
        return await call_next(request)
    if not u:
        if path.startswith("/api/") or request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"error": "Sign in first"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    if u.must_change and path not in ("/account", "/logout"):
        return RedirectResponse("/account?err=Set a new password before you continue", status_code=303)
    return await call_next(request)


def U(request):
    return request.state.user


def can(request, perm):
    u = U(request)
    return bool(u and rbac.can(u.role, perm))


def who(request):
    u = U(request)
    return u.name if u else ""


def role(request):
    u = U(request)
    return u.role if u else ""


def _r(url):
    return RedirectResponse(url, status_code=303)


def back(url, msg="", err=""):
    sep = "&" if "?" in url else "?"
    if err:
        return _r(f"{url}{sep}err={quote(err)}")
    return _r(f"{url}{sep}msg={quote(msg)}" if msg else url)


def deny(request, perm, url):
    """Server-side permission check for a form post. Returns a redirect with the reason, or None."""
    if can(request, perm):
        return None
    return back(url, err=f"Your role ({role(request)}) cannot do this: {rbac.LABELS.get(perm, perm)}.")


def jdeny(request, perm):
    if can(request, perm):
        return None
    return JSONResponse({"error": f"Your role ({role(request)}) cannot do this: {rbac.LABELS.get(perm, perm)}."}, status_code=403)


def base_ctx(request, **kw):
    d = {"request": request, "flash": request.query_params.get("msg", ""), "err": request.query_params.get("err", ""), "user": U(request),
         "can": lambda perm: can(request, perm)}
    d.update(kw)
    return d


def page(request, tpl, **kw):
    return T.TemplateResponse(request, tpl, base_ctx(request, **kw))


def next_step(s, p, r):
    """The one thing that should happen next on this Program, and who does it."""
    gates = list(s.execute(select(Gate).where(Gate.program_id == p.id, Gate.status == "pending")).scalars())
    umgc = [g for g in gates if g.kind != "validation"]
    val = [g for g in gates if g.kind == "validation"]
    jobs = s.query(Job).filter(Job.program_id == p.id, Job.status.in_(["queued", "running"])).count()
    link = {"magic.employers": "/skills?tab=validation", "umgc.skills_map": "/skills?tab=validation", "umgc.blueprint": "/design?tab=gate",
            "magic.outline": "/develop?tab=outline", "umgc.prototype": "/develop?tab=reviews", "umgc.sprint1": "/develop?tab=reviews", "umgc.sprint2": "/develop?tab=reviews",
            "umgc.qa": "/quality?tab=qa", "umgc.launch": "/quality?tab=releases"}
    if p.status == "paused":
        return {"text": "The Program is paused. Resume it from the Overview.", "href": "", "owner": rbac.PM, "yours": rbac.can(r, "program.pause")}
    if umgc:
        g = umgc[0]
        perm = rbac.gate_perm(g.kind, None)
        return {"text": g.label, "href": link.get(g.gate_key, ""), "owner": g.owner_role, "yours": rbac.can(r, perm)}
    if val:
        n = Counter(g.evidence.get("gate") for g in val)
        gno = min(n)
        return {"text": f"{len(val)} validation decision(s) waiting; first: Gate {gno} {VALIDATION_GATES[gno][0]} on {n[gno]} asset(s)",
                "href": f"/develop?tab=reviews&gate={gno}", "owner": VALIDATION_GATES[gno][1], "yours": rbac.can(r, f"gate.val{gno}")}
    authors = [a for a in list_records(s, p.id, "asset") if a.data.get("state") == "author"]
    if authors:
        return {"text": f"{len(authors)} asset(s) returned to their author for revision", "href": "/develop?tab=board&state=author",
                "owner": "Instructional Designer or SME", "yours": rbac.can(r, "content.edit")}
    tickets = [t for t in list_records(s, p.id, "ticket") if t.data["status"] == "proposed"]
    if tickets:
        return {"text": f"{len(tickets)} improvement ticket(s) waiting for a decision", "href": "/improve?tab=tickets", "owner": "Magic PM, or UMGC for material changes",
                "yours": rbac.can(r, "ticket.routine") or rbac.can(r, "ticket.material")}
    if jobs:
        return {"text": f"Workers are on it: {jobs} job(s) running. This page updates as they finish.", "href": "", "owner": "Engine", "yours": False}
    if p.release_version:
        return {"text": f"Release {p.release_version} is live. Monitor Insights; the monthly scan raises tickets.", "href": "/insights", "owner": "Magic PM", "yours": True}
    return {"text": "Nothing waiting.", "href": "", "owner": "", "yours": False}


def pctx(request, pid, section, tab=None, **kw):
    with session() as s:
        p = s.get(Program, pid)
        if p is None:
            return None
        pend = list(s.execute(select(Gate).where(Gate.program_id == pid, Gate.status == "pending")).scalars())
        d = base_ctx(request, p=p, section=section, tab=tab, tabs=TABS.get(section, []), sections=SECTIONS,
                     pending_umgc=sum(1 for g in pend if g.kind != "validation"), pending_val=sum(1 for g in pend if g.kind == "validation"),
                     jobs_open=s.query(Job).filter(Job.program_id == pid, Job.status.in_(["queued", "running"])).count(),
                     jobs_failed=s.query(Job).filter(Job.program_id == pid, Job.status == "failed").count(),
                     tickets_open=sum(1 for t in list_records(s, pid, "ticket") if t.data["status"] == "proposed"),
                     nxt=next_step(s, p, role(request)), stage_idx=[k for k, _ in STAGES].index(p.stage) if p.stage in dict(STAGES) else 0)
    d.update(kw)
    return d


def ppage(request, pid, section, tpl, tab=None, **kw):
    d = pctx(request, pid, section, tab, **kw)
    if d is None:
        return Response("Program not found", status_code=404)
    return T.TemplateResponse(request, tpl, d)


def tab_of(request, section):
    t = request.query_params.get("tab")
    keys = [k for k, _ in TABS.get(section, [])]
    return t if t in keys else (keys[0] if keys else None)


# ------------------------------------------------------------------ sign in, account, first run
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    first = not auth.has_users()
    return page(request, "login.html", next=next, first=first, bootstrap=bool(config.APP_PASSWORD))


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    u = auth.login(username, password)
    if not u:
        return back(f"/login?next={quote(next)}", err="That username and password do not match an active account.")
    r = _r(next if next.startswith("/") else "/")
    r.set_cookie(auth.COOKIE, auth.make_cookie(u.id), max_age=auth.TTL, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return r


@app.post("/first-run")
def first_run(request: Request, username: str = Form(...), name: str = Form(...), password: str = Form(...)):
    """Local and demo installs without APP_PASSWORD: the first visitor creates the Administrator account."""
    if auth.has_users() or config.APP_PASSWORD:
        return back("/login", err="An administrator already exists.")
    if len(password) < 10:
        return back("/login", err="Use at least 10 characters.")
    uid, err = auth.create_user(username, name, "", rbac.ADMIN, password, must_change=False)
    if err:
        return back("/login", err=err)
    r = _r("/")
    r.set_cookie(auth.COOKIE, auth.make_cookie(uid), max_age=auth.TTL, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return r


@app.get("/logout")
def logout():
    r = _r("/login?msg=Signed out")
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    return page(request, "account.html", section="account")


@app.post("/account")
def account_post(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    u = U(request)
    with session() as s:
        row = s.get(User, u.id)
        if not auth.check_pw(current, row.password_hash or ""):
            return back("/account", err="Your current password is not right.")
        if len(new) < 10 or new != confirm:
            return back("/account", err="The new password needs at least 10 characters and both entries must match.")
        row.password_hash = auth.hash_pw(new)
        row.must_change = False
    return back("/", msg="Password changed.")


# ------------------------------------------------------------------ workspace: Programs, new Program
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    rows = []
    with session() as s:
        for p in s.execute(select(Program).order_by(Program.id.desc())).scalars():
            sp = specmod.get_spec(s, p.id)
            ms = [m for m in specmod.milestones(sp) if m["due"] >= now().date().isoformat()]
            rows.append({"p": p, "spec": sp, "next_ms": ms[0] if ms else None, "nxt": next_step(s, p, role(request)),
                         "umgc": s.query(Gate).filter(Gate.program_id == p.id, Gate.status == "pending", Gate.kind != "validation").count(),
                         "val": s.query(Gate).filter(Gate.program_id == p.id, Gate.status == "pending", Gate.kind == "validation").count()})
    return page(request, "home.html", section="programs", rows=rows)


@app.get("/programs/new", response_class=HTMLResponse)
def new_form(request: Request):
    if not can(request, "program.create"):
        return back("/", err="Your role cannot create Programs.")
    return page(request, "new.html", section="programs", sample=sample_brief())


@app.post("/programs/sample")
def start_sample(request: Request):
    if (r := deny(request, "program.create", "/")):
        return r
    pid = create_program(sample_brief())
    return back(f"/p/{pid}", msg="Sample Program started (8 weeks, 22.5 learner hours a week). Workers are drafting the skills map.")


@app.post("/programs/new")
async def new_submit(request: Request):
    if (r := deny(request, "program.create", "/programs/new")):
        return r
    f = dict((await request.form()).items())
    if not f.get("name", "").strip() or not f.get("occupation", "").strip():
        return back("/programs/new", err="Name the Program and the target occupation.")
    srcs = []
    for i in (1, 2, 3):
        txt = f.get(f"src{i}", "").strip()
        if txt:
            srcs.append({"id": ["SRC-EXAM", "SRC-UMGC", "SRC-JOBS"][i - 1], "type": f.get(f"src{i}_title", ""), "title": f.get(f"src{i}_title", ""), "text": txt})
    try:
        weeks = int(f.get("duration_weeks") or 5)
        hours = float(f.get("weekly_hours") or 10)
        courses = int(f.get("courses") or weeks)
    except ValueError:
        return back("/programs/new", err="Duration, weekly hours and Courses must be numbers.")
    b = new_brief(f["name"].strip(), f["occupation"].strip(), f.get("lot", ""), f.get("specialized", ""), f.get("credential", ""), srcs,
                  weeks, hours, courses, f.get("subject_area", ""), f.get("audience", ""), f.get("start_date", ""), f.get("task_order", ""),
                  f.get("required_modules", ""), f.get("assessment_requirements", ""))
    pid = create_program(b)
    with session() as s:
        log(s, pid, who(request), "setup", f"Program created by {who(request)}: {weeks} weeks at {hours} hours a week, {courses} Courses.")
    return back(f"/p/{pid}", msg="Program created. Add sources in Skills Studio; workers are drafting the skills map.")


# ------------------------------------------------------------------ workspace: My work
@app.get("/work", response_class=HTMLResponse)
def my_work(request: Request):
    u = U(request)
    rows = []
    first = u.name.split(" ")[0].lower()
    with session() as s:
        for p in s.execute(select(Program)).scalars():
            for g in s.query(Gate).filter(Gate.program_id == p.id, Gate.status == "pending"):
                if rbac.can(u.role, rbac.gate_perm(g.kind, (g.evidence or {}).get("gate"))) and u.role != rbac.ADMIN:
                    rows.append({"kind": "Decision", "p": p, "text": g.label, "href": f"/p/{p.id}/develop?tab=reviews" if g.kind == "validation" else f"/p/{p.id}"})
            for a in list_records(s, p.id, "asset"):
                o = a.data.get("owners") or {}
                if u.name in (o.get("author"), o.get("sme"), o.get("reviewer")) and a.data.get("state") != "validated":
                    rows.append({"kind": "Assigned", "p": p, "text": f"{a.key} {a.data['title']} ({a.data.get('state')})", "href": f"/p/{p.id}/edit/{a.key}"})
                if a.data.get("state") == "author" and (a.data.get("draft") or {}).get("edited_by") == u.name:
                    rows.append({"kind": "Returned to you", "p": p, "text": f"{a.key}: {(a.data.get('draft') or {}).get('returned_note', '')}", "href": f"/p/{p.id}/edit/{a.key}"})
            for c in list_records(s, p.id, "comment"):
                d = c.data
                body = d["body"].lower()
                if d["status"] == "open" and ("@" + first in body or "@" + u.name.lower() in body or "@" + u.role.lower() in body
                                              or (rbac.SME == u.role and "@sme" in body)):
                    rows.append({"kind": "Mention", "p": p, "text": f"{d['by']} on {d['asset']}: {d['body'][:140]}", "href": f"/p/{p.id}/edit/{d['asset']}#comments"})
            if rbac.can(u.role, "ticket.routine") or rbac.can(u.role, "ticket.material"):
                for t in list_records(s, p.id, "ticket"):
                    if t.data["status"] == "proposed" and (t.data["change_class"] != "material" or rbac.can(u.role, "ticket.material")):
                        rows.append({"kind": "Ticket", "p": p, "text": f"{t.key}: {t.data['signal']} ({t.data['change_class']})", "href": f"/p/{p.id}/improve?tab=tickets"})
            if rbac.can(u.role, "deliverable.accept") and u.role != rbac.ADMIN:
                for d in specmod.deliverables(s, p.id):
                    if d["rounds"] and not d["rounds"][-1].get("decision"):
                        rows.append({"kind": "Acceptance", "p": p, "text": f"{d['code']} {d['name']} (due {d['rounds'][-1]['due']})", "href": f"/p/{p.id}/quality?tab=deliverables"})
    return page(request, "work.html", section="work", rows=rows)


# ------------------------------------------------------------------ workspace: Governance
@app.get("/governance", response_class=HTMLResponse)
def gov_page(request: Request):
    with session() as s:
        progs = list(s.execute(select(Program)).scalars())
    return page(request, "governance.html", section="governance", models=governance.models(), prompts=governance.prompts(), eval_sets=governance.eval_sets(),
                eval_runs=governance.eval_runs(), incidents=governance.incidents(), changes=list(reversed(governance.changes()))[:60],
                subs=governance.SUBPROCESSORS, handling=governance.DATA_HANDLING, progs=progs)


@app.post("/governance/incident")
def gov_incident(request: Request, title: str = Form(...), detail: str = Form(""), severity: str = Form("moderate"), program: str = Form(""), asset: str = Form("")):
    if not (can(request, "incident.manage") or can(request, "event.raise")):
        return back("/governance", err="Your role cannot report incidents.")
    iid = governance.report_incident(title, detail, severity, program, asset, who(request))
    return back("/governance", msg=f"Incident {iid} reported. The AI Governance Lead triages it.")


@app.post("/governance/incident/resolve")
def gov_resolve(request: Request, iid: str = Form(...), resolution: str = Form("")):
    if (r := deny(request, "incident.manage", "/governance")):
        return r
    err = governance.resolve_incident(iid, resolution, who(request))
    return back("/governance", msg="" if err else f"{iid} resolved", err=err)


@app.post("/governance/prompt")
def gov_prompt(request: Request, prompt_id: str = Form(...), owner: str = Form(""), notes: str = Form("")):
    if (r := deny(request, "governance.edit", "/governance")):
        return r
    governance.save_prompt_note(prompt_id, owner, notes, who(request))
    return back("/governance", msg=f"Prompt {prompt_id} updated in the register.")


# ------------------------------------------------------------------ workspace: Admin
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if not can(request, "users.manage"):
        return back("/", err="Only the Administrator manages users and roles.")
    with session() as s:
        workers = list(s.execute(select(WorkerBeat).order_by(WorkerBeat.pool, WorkerBeat.name)).scalars())
        q = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        failed = list(s.execute(select(Job).where(Job.status == "failed").order_by(Job.id.desc()).limit(20)).scalars())
    fresh = lambda w: (now() - w.last_seen).total_seconds() < 90
    return page(request, "admin.html", section="admin", users=auth.all_users(), workers=workers, q=q, fresh=fresh, failed=failed,
                perms=list(rbac.PERMS.keys()))


@app.post("/admin/users")
def admin_create(request: Request, username: str = Form(...), name: str = Form(...), email: str = Form(""), role_: str = Form(..., alias="role"),
                 password: str = Form(...), expertise: str = Form("")):
    if (r := deny(request, "users.manage", "/admin")):
        return r
    if role_ not in rbac.ROLES:
        return back("/admin", err="Pick a role from the list.")
    if len(password) < 10:
        return back("/admin", err="Temporary passwords need at least 10 characters.")
    uid, err = auth.create_user(username, name, email, role_, password, expertise, must_change=True)
    if err:
        return back("/admin", err=err)
    return back("/admin", msg=f"Account {username} created as {role_}. They set their own password at first sign-in.")


@app.post("/admin/users/{uid}")
def admin_update(request: Request, uid: int, role_: str = Form("", alias="role"), active: str = Form(""), reset: str = Form(""), expertise: str = Form(None)):
    if (r := deny(request, "users.manage", "/admin")):
        return r
    with session() as s:
        u = s.get(User, uid)
        if not u:
            return back("/admin", err="User not found.")
        if u.id == U(request).id and (active == "no" or (role_ and role_ != rbac.ADMIN)):
            return back("/admin", err="You cannot deactivate or demote your own account.")
        if role_ in rbac.ROLES:
            u.role = role_
        if active in ("yes", "no"):
            u.active = active == "yes"
        if expertise is not None:
            u.expertise = expertise
        msg = f"{u.username} updated."
        if reset and len(reset) >= 10:
            u.password_hash, u.must_change = auth.hash_pw(reset), True
            msg += " Temporary password set; they must change it at sign-in."
    return back("/admin", msg=msg)


@app.post("/jobs/retry")
def jobs_retry(request: Request, program: int | None = Form(None)):
    if (r := deny(request, "jobs.retry", f"/p/{program}/audit" if program else "/admin")):
        return r
    n = retry_failed(program)
    if program:
        advance(program)
    return back(f"/p/{program}/audit" if program else "/admin", msg=f"Requeued {n} failed job(s)")


# ------------------------------------------------------------------ Program: Overview
@app.get("/p/{pid}", response_class=HTMLResponse)
def overview(request: Request, pid: int):
    with session() as s:
        p = s.get(Program, pid)
        if not p:
            return Response("Program not found", status_code=404)
        gates = list(s.query(Gate).filter(Gate.program_id == pid, Gate.kind != "validation").order_by(Gate.id))
        events = list(s.query(Event).filter(Event.program_id == pid).order_by(Event.id.desc()).limit(25))
        assets = [r.data for r in list_records(s, pid, "asset")]
        sp = specmod.get_spec(s, pid)
        ms = specmod.milestones(sp, (get_record(s, pid, "schedule").data.get("overrides") if get_record(s, pid, "schedule") else None))
        live = live_status(s, pid, p)
        dels = specmod.deliverables(s, pid)
    states = Counter(a.get("state") for a in assets)
    return ppage(request, pid, "overview", "overview.html", gates=gates, events=events, states=states, n_assets=len(assets), spec=sp, ms=ms,
                 BOARD=BOARD, live=live, dels=dels, total_hours=specmod.total_hours(sp))


@app.post("/p/{pid}/pause")
def pause(request: Request, pid: int):
    if (r := deny(request, "program.pause", f"/p/{pid}")):
        return r
    with session() as s:
        p = s.get(Program, pid)
        p.status = "running" if p.status == "paused" else "paused"
        log(s, pid, who(request), "setup", f"Program {p.status} by {who(request)}.")
    advance(pid)
    return back(f"/p/{pid}")


@app.post("/p/{pid}/autopilot")
def autopilot(request: Request, pid: int):
    if (r := deny(request, "autopilot", f"/p/{pid}")):
        return r
    n = autopilot_step(pid, f"{who(request)} (demo autopilot)")
    return back(f"/p/{pid}", msg=f"Autopilot recorded {n} decision(s). Gates with blocking findings were returned for revision.")


# ------------------------------------------------------------------ Program: Setup
@app.get("/p/{pid}/setup", response_class=HTMLResponse)
def setup(request: Request, pid: int):
    tab = tab_of(request, "setup")
    with session() as s:
        sp = specmod.get_spec(s, pid)
        tm = studio.team(s, pid)
        sched = get_record(s, pid, "schedule")
        ov = sched.data.get("overrides", {}) if sched else {}
        bp = get_record(s, pid, "blueprint")
        dels = specmod.deliverables(s, pid)
    users = [u for u in auth.all_users(active_only=True)]
    return ppage(request, pid, "setup", "setup.html", tab=tab, spec=sp, team=tm, ms=specmod.milestones(sp, ov), overrides=ov, users=users,
                 total_hours=specmod.total_hours(sp), completion=specmod.completion_definition(sp), has_bp=bool(bp), dels=dels)


@app.post("/p/{pid}/setup/spec")
async def setup_spec(request: Request, pid: int):
    url = f"/p/{pid}/setup?tab=spec"
    if (r := deny(request, "program.setup", url)):
        return r
    f = dict((await request.form()).items())
    with locked(pid) as s:
        sp = json.loads(json.dumps(specmod.get_spec(s, pid)))
        to, pr = sp["task_order"], sp["program"]
        for k in ("number", "start_date", "approval_authority", "sme_model", "background"):
            if k in f:
                to[k] = f[k].strip()
        for k in ("review_window_days", "acceptance_days", "recurring_months"):
            if f.get(k):
                to[k] = int(f[k])
        for k in ("subject_area", "occupation", "lot_occupation", "lot_specialized", "credential", "audience", "required_modules", "assessment_requirements"):
            if k in f:
                pr[k] = f[k].strip()
        try:
            pr["duration_weeks"] = max(1, min(52, int(f.get("duration_weeks") or pr["duration_weeks"])))
            pr["weekly_hours"] = max(1.0, min(60.0, float(f.get("weekly_hours") or pr["weekly_hours"])))
            pr["courses"] = max(1, min(30, int(f.get("courses") or pr["courses"])))
            pr["pass_mark"] = int(f.get("pass_mark") or pr["pass_mark"])
            pr["attempts"] = int(f.get("attempts") or pr["attempts"])
        except ValueError:
            return back(url, err="Duration, hours, Courses, pass mark and attempts must be numbers.")
        pr["capstone"] = f.get("capstone") == "yes"
        sp["attributes"] = [a.strip() for a in f.get("attributes", ", ".join(sp.get("attributes", []))).split(",") if a.strip()]
        put_record(s, pid, "spec", "main", sp, status="set", bump=True)
        p = s.get(Program, pid)
        b = dict(p.brief)
        b.update({"duration_weeks": pr["duration_weeks"], "weekly_hours": pr["weekly_hours"], "courses": pr["courses"], "credential": pr["credential"]})
        p.brief = b
        log(s, pid, who(request), "setup", f"Program specification saved: {pr['duration_weeks']} weeks, {pr['weekly_hours']} hours a week, {pr['courses']} Courses.")
        has_bp = bool(get_record(s, pid, "blueprint"))
    msg = "Specification saved."
    if has_bp:
        msg += " The Blueprint already exists: regenerate it in Design Studio > Gate 2 to apply a new duration."
    return back(url, msg=msg)


@app.post("/p/{pid}/setup/team")
async def setup_team(request: Request, pid: int):
    url = f"/p/{pid}/setup?tab=team"
    if (r := deny(request, "team.edit", url)):
        return r
    f = await request.form()
    members = []
    for i in range(0, 60):
        nm = (f.get(f"m{i}_name") or "").strip()
        if not nm or f.get(f"m{i}_remove"):
            continue
        rl = f.get(f"m{i}_role") or rbac.SME
        members.append({"name": nm, "role": rl, "org": rbac.org_of(rl), "expertise": (f.get(f"m{i}_expertise") or "").strip(),
                        "credentials": (f.get(f"m{i}_credentials") or "").strip(), "years": (f.get(f"m{i}_years") or "").strip(),
                        "courses": [c.strip() for c in (f.get(f"m{i}_courses") or "").split(",") if c.strip()], "location": (f.get(f"m{i}_location") or "").strip(),
                        "username": (f.get(f"m{i}_username") or "").strip()})
    studio.save_team(pid, members, who(request))
    return back(url, msg=f"Team saved ({len(members)} people).")


@app.post("/p/{pid}/setup/schedule")
async def setup_schedule(request: Request, pid: int):
    url = f"/p/{pid}/setup?tab=schedule"
    if (r := deny(request, "program.setup", url)):
        return r
    f = dict((await request.form()).items())
    ov = {k[3:]: v for k, v in f.items() if k.startswith("ms_") and v}
    with locked(pid) as s:
        put_record(s, pid, "schedule", "main", {"overrides": ov})
        log(s, pid, who(request), "setup", f"Milestone dates updated ({len(ov)} set by hand).")
    return back(url, msg="Schedule saved.")


@app.get("/p/{pid}/setup/milestones.csv")
def milestones_csv(pid: int):
    import csv
    import io
    with session() as s:
        sp = specmod.get_spec(s, pid)
        sched = get_record(s, pid, "schedule")
        ms = specmod.milestones(sp, sched.data.get("overrides") if sched else None)
        dels = specmod.deliverables(s, pid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Type", "Code", "Name", "Due", "Status", "Owner"])
    for m in ms:
        w.writerow(["Milestone", m["code"], m["name"], m["due"], "", "Magic Project Manager"])
    for d in dels:
        w.writerow(["Deliverable", d["code"], d["name"], d["due"], d["status"], "Magic Project Manager"])
    return Response(buf.getvalue().encode(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="Schedule - Program {pid} (Smartsheet import).csv"'})


# ------------------------------------------------------------------ shared grids: rooms, autosave
def grid_room(s, pid, kind, rec):
    """A room per record generation: regenerating the record from a worker starts a fresh room."""
    d = rec.data
    gen = d.get("_gen")
    if not gen:
        gen = int(now().timestamp())
        d = dict(d)
        d["_gen"] = gen
        rec.data = d
        s.flush()
    return f"p{pid}/{kind}/{rec.id}-{gen}"


def seeded(request, room):
    return collab.claim_seed(room, who(request))


def grid_spec(request, s, pid, kind, rec, columns, rows, perm, locked_):
    room = grid_room(s, pid, kind, rec)
    editable = can(request, perm) and not locked_
    return {"room": room, "columns": columns, "rows": rows, "editable": editable, "canSeed": False,
            "save": f"/p/{pid}/grid/{kind}", "user": {"name": who(request), "role": role(request)}}


def claim(request, grid):
    """Decide who seeds an empty room. Runs after the page's own session commits (SQLite allows one writer)."""
    if grid and grid["editable"]:
        grid["canSeed"] = seeded(request, grid["room"])
    return grid


@app.post("/p/{pid}/grid/{kind}")
async def grid_save(request: Request, pid: int, kind: str):
    body = await request.json()
    rows = body.get("rows", [])
    by = who(request)
    if kind in ("skills", "comps"):
        if (r := jdeny(request, "skills.edit")):
            return r
        res = edits.save_skills(pid, rows, by) if kind == "skills" else edits.save_competencies(pid, rows, by)
    elif kind == "outline":
        if (r := jdeny(request, "outline.edit")):
            return r
        res = outlinemod.save(pid, rows, by)
    elif kind.startswith("bp-"):
        if (r := jdeny(request, "blueprint.edit")):
            return r
        part = kind[3:]
        if part not in ("courses", "modules", "objectives", "assessments"):
            return JSONResponse({"error": "Unknown grid"}, status_code=400)
        res = edits.save_blueprint_part(pid, part, rows, by)
    else:
        return JSONResponse({"error": "Unknown grid"}, status_code=400)
    if res.get("error"):
        return JSONResponse(res, status_code=409)
    res["saved_at"] = now().strftime("%H:%M:%S")
    return JSONResponse(res)


@app.websocket("/collab/{room:path}")
async def collab_ws(ws: WebSocket, room: str):
    u = auth.user_by_id(auth.read_cookie(ws.cookies.get(auth.COOKIE)))
    if not u:
        await ws.close(code=4401)
        return
    room = room.split("?")[0]
    try:
        can_edit = room_can_edit(u, room)
    except Exception:
        await ws.close(code=4404)
        return
    await ws.accept()
    await collab.serve(ws, room, can_edit)


def room_can_edit(u, room) -> bool:
    """Edit rights per room, decided on the server for every connection."""
    parts = room.split("/")
    pid = int(parts[0][1:])
    kind = parts[1]
    with session() as s:
        if kind in ("skills", "comps"):
            return rbac.can(u.role, "skills.edit") and not edits.lock_state(s, pid, "skills")["locked"]
        if kind.startswith("bp-"):
            return rbac.can(u.role, "blueprint.edit") and not edits.lock_state(s, pid, "blueprint")["locked"]
        if kind == "outline":
            return rbac.can(u.role, "outline.edit") and not edits.lock_state(s, pid, "outline")["locked"]
        if kind == "asset":
            a = get_record(s, pid, "asset", parts[2])
            return bool(a) and rbac.can(u.role, "content.edit") and a.data.get("state") not in ("queued", "drafted")
    return False


# ------------------------------------------------------------------ Program: Skills Studio
@app.get("/p/{pid}/skills", response_class=HTMLResponse)
def skills(request: Request, pid: int):
    tab = tab_of(request, "skills")
    with session() as s:
        smr = get_record(s, pid, "skills_map")
        sm = smr.data if smr else None
        bp = get_record(s, pid, "blueprint")
        lock = edits.lock_state(s, pid, "skills")
        gates = {g.gate_key: g for g in s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key.in_(["magic.employers", "umgc.skills_map"]))}
        srcs = [r.data for r in list_records(s, pid, "source")]
        brief_srcs = s.get(Program, pid).brief.get("sources", [])
        pack = sources.source_pack(s, pid)
        grid = None
        if sm and tab == "map":
            comp_ids = [c["competency_id"] for c in sm.get("competencies", [])]
            grid = grid_spec(request, s, pid, "skills", smr, edits.skill_columns(comp_ids), edits.skills_rows(sm), "skills.edit", lock["locked"])
        elif sm and tab == "competencies":
            grid = grid_spec(request, s, pid, "comps", smr, edits.COMP_COLUMNS, edits.comp_rows(sm), "skills.edit", lock["locked"])
    claim(request, grid)
    return ppage(request, pid, "skills", "skills.html", tab=tab, sm=sm, tm=(bp.data.get("trace_matrix", {}) if bp else {}), lock=lock, gates=gates,
                 srcs=srcs, brief_srcs=brief_srcs, pack_version=sources.pack_version(pack), grid=grid)


@app.post("/p/{pid}/skills/reopen")
def skills_reopen(request: Request, pid: int, what: str = Form(...), note: str = Form("")):
    perm = {"skills": "skills.reopen", "outline": "outline.approve"}.get(what, "blueprint.reopen")
    url = {"skills": f"/p/{pid}/skills?tab=map", "outline": f"/p/{pid}/develop?tab=outline"}.get(what, f"/p/{pid}/design?tab=courses")
    if (r := deny(request, perm, url)):
        return r
    err = edits.reopen(pid, what, who(request), note)
    return back(url, msg="" if err else "Reopened for edits. The UMGC gate opens again for a fresh decision.", err=err)


@app.post("/p/{pid}/employers")
def employers(request: Request, pid: int, e1: str = Form(""), e2: str = Form(""), e3: str = Form(""), e1_who: str = Form(""), e2_who: str = Form(""),
              e3_who: str = Form(""), notes: str = Form("")):
    url = f"/p/{pid}/skills?tab=validation"
    if (r := deny(request, "employers.record", url)):
        return r
    names = [e for e in (e1, e2, e3) if e.strip()]
    if len(names) < 3:
        return back(url, err="Record three validating employers.")
    with locked(pid) as s:
        rec = get_record(s, pid, "skills_map")
        d = dict(rec.data)
        d["employers"] = names
        d["employer_detail"] = [{"employer": e, "reviewer": w} for e, w in zip((e1, e2, e3), (e1_who, e2_who, e3_who)) if e.strip()]
        d["employer_notes"] = notes
        rec.data = d
    err = decide_gate(pid, "magic.employers", "approve", who(request), "Validated by: " + "; ".join(names) + (f". {notes}" if notes else ""), role=role(request))
    return back(url, msg="" if err else "Employer validation recorded. Gate 1 goes to UMGC.", err=err)


@app.post("/p/{pid}/gate")
def gate_decide(request: Request, pid: int, gate_key: str = Form(...), decision: str = Form(...), note: str = Form(""), back_: str = Form("", alias="back")):
    url = back_ or f"/p/{pid}"
    err = decide_gate(pid, gate_key, decision, who(request), note, role=role(request) or "none")
    return back(url, msg="" if err else ("Approved." if decision == "approve" else "Returned with your note."), err=err)


# sources
@app.post("/p/{pid}/sources/upload")
async def source_upload(request: Request, pid: int, files: list[UploadFile] = File(...), source_type: str = Form("UMGC material"),
                        owner: str = Form("UMGC"), license_note: str = Form("")):
    url = f"/p/{pid}/skills?tab=sources"
    if (r := deny(request, "source.upload", url)):
        return r
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
        keys.append(sources.add_source(pid, f.filename, source_type, text, f.filename, kind, owner, license_note, who(request)))
    return back(url, msg=f"Uploaded {', '.join(keys)}; the ingest worker is tagging them to Lightcast skills." if keys else "", err="; ".join(errs))


@app.post("/p/{pid}/sources/paste")
def source_paste(request: Request, pid: int, title: str = Form(...), text: str = Form(...), source_type: str = Form("SME reference"),
                 owner: str = Form("Magic"), license_note: str = Form("")):
    url = f"/p/{pid}/skills?tab=sources"
    if (r := deny(request, "source.upload", url)):
        return r
    k = sources.add_source(pid, title, source_type, text, title + ".txt", "text", owner, license_note, who(request))
    return back(url, msg=f"Source {k} queued for ingest")


@app.get("/p/{pid}/sources/{key}", response_class=HTMLResponse)
def source_view(request: Request, pid: int, key: str):
    with session() as s:
        r = get_record(s, pid, "source", key)
        d = r.data if r else None
    if not d:
        return back(f"/p/{pid}/skills?tab=sources", err="Source not found")
    return ppage(request, pid, "skills", "source.html", tab="sources", src=d)


@app.post("/p/{pid}/sources/{key}/decide")
def source_decide(request: Request, pid: int, key: str, decision: str = Form(...), note: str = Form("")):
    url = f"/p/{pid}/sources/{key}"
    if (r := deny(request, "source.approve", url)):
        return r
    err = sources.decide_source(pid, key, decision, who(request), role(request), note)
    return back(url, msg="" if err else "Decision recorded. The source pack version changed.", err=err)


# ------------------------------------------------------------------ Program: Design Studio
@app.get("/p/{pid}/design", response_class=HTMLResponse)
def design(request: Request, pid: int):
    tab = tab_of(request, "design")
    with session() as s:
        bpr = get_record(s, pid, "blueprint")
        bp = bpr.data if bpr else None
        sm = get_record(s, pid, "skills_map")
        sm = sm.data if sm else None
        lock = edits.lock_state(s, pid, "blueprint")
        gate = s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key == "umgc.blueprint").first()
        sp = specmod.get_spec(s, pid)
        grid = None
        if bp:
            cids = [c["course_id"] for c in bp["courses"]]
            g = {"courses": (edits.COURSE_COLUMNS, edits.course_rows(bp)), "modules": (edits.module_columns(cids), edits.module_rows(bp)),
                 "objectives": (edits.OBJ_COLUMNS, edits.obj_rows(bp)), "assessments": (edits.assess_columns(cids), edits.assess_rows(bp))}.get(tab)
            if g:
                grid = grid_spec(request, s, pid, f"bp-{tab}", bpr, g[0], g[1], "blueprint.edit", lock["locked"])
    claim(request, grid)
    return ppage(request, pid, "design", "design.html", tab=tab, bp=bp, sm=sm, lock=lock, gate=gate, grid=grid, spec=sp,
                 total_hours=specmod.total_hours(sp), completion=specmod.completion_definition(sp))


@app.post("/p/{pid}/design/regenerate")
def design_regen(request: Request, pid: int):
    url = f"/p/{pid}/design?tab=gate"
    if (r := deny(request, "blueprint.edit", url)):
        return r
    with locked(pid) as s:
        if edits.lock_state(s, pid, "blueprint")["locked"]:
            return back(url, err="The Blueprint is approved. Reopen it before regenerating.")
        n = s.query(Job).filter(Job.program_id == pid, Job.task == "design.blueprint").count()
        bp = get_record(s, pid, "blueprint")
        if bp:
            edits.snapshot(s, pid, "blueprint", bp.data, who(request), note="Before regeneration")
            s.delete(bp)
        for j in s.query(Job).filter(Job.program_id == pid, Job.task.in_(["design.blueprint", "design.outline"])):
            j.dedupe_key = f"{j.dedupe_key}:old{n}"
        _clear_outline(s, pid, who(request))
        log(s, pid, who(request), "design", "Blueprint regeneration requested from the current specification and skills map.")
    advance(pid)
    return back(url, msg="Blueprint regeneration queued on the design worker.")


def _clear_outline(s, pid, by):
    ol = get_record(s, pid, "outline")
    if ol:
        edits.snapshot(s, pid, "outline", ol.data, by, note="Before redraft")
        s.delete(ol)
    for g in s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key == "magic.outline"):
        g.gate_key = f"magic.outline:superseded:{int(now().timestamp())}"
        if g.status == "pending":
            g.status = "returned"


@app.post("/p/{pid}/outline/redraft")
def outline_redraft(request: Request, pid: int):
    url = f"/p/{pid}/develop?tab=outline"
    if (r := deny(request, "outline.approve", url)):
        return r
    with locked(pid) as s:
        if edits.lock_state(s, pid, "outline")["locked"]:
            return back(url, err="The outline is approved. Reopen it before redrafting.")
        n = s.query(Job).filter(Job.program_id == pid, Job.task == "design.outline").count()
        for j in s.query(Job).filter(Job.program_id == pid, Job.task == "design.outline"):
            j.dedupe_key = f"{j.dedupe_key}:old{n}"
        _clear_outline(s, pid, who(request))
        log(s, pid, who(request), "build", "Module outline redraft requested from the current Blueprint.")
    advance(pid)
    return back(url, msg="Outline redraft queued on the design worker.")


# ------------------------------------------------------------------ Program: Development Studio
@app.get("/p/{pid}/develop", response_class=HTMLResponse)
def develop(request: Request, pid: int, gate: int | None = None, state: str = "", course: str = ""):
    tab = tab_of(request, "develop")
    if tab in ("outline", "weekly"):
        advance(pid)  # idempotent: makes sure an approved Blueprint has its outline queued
    with session() as s:
        assets = [r.data for r in list_records(s, pid, "asset")]
        vg = list(s.query(Gate).filter(Gate.program_id == pid, Gate.kind == "validation", Gate.status == "pending").order_by(Gate.id))
        ug = list(s.query(Gate).filter(Gate.program_id == pid, Gate.kind != "validation", Gate.gate_key.in_(["umgc.prototype", "umgc.sprint1", "umgc.sprint2"])).order_by(Gate.id))
        comments = [r.data for r in list_records(s, pid, "comment")]
        bp = get_record(s, pid, "blueprint")
        courses = [(c["course_id"], c["name"]) for c in bp.data["courses"]] if bp else []
        tm = studio.team(s, pid)
        olr = get_record(s, pid, "outline")
        ol_lock = edits.lock_state(s, pid, "outline")
        ol_gate = s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key == "magic.outline").first()
        grid = None
        if olr and tab == "outline":
            grid = grid_spec(request, s, pid, "outline", olr, outlinemod.COLUMNS, olr.data["rows"], "outline.edit", ol_lock["locked"])
        weeks = outlinemod.weekly_map(s, pid) if tab == "weekly" else None
        has_bp = bool(bp)
        sp = specmod.get_spec(s, pid)
    claim(request, grid)
    if state:
        assets = [a for a in assets if a.get("state") == state]
    if course:
        assets = [a for a in assets if a["course_id"] == course]
    guide = docsgen.build(pid, "guides") if tab == "guides" else None
    return ppage(request, pid, "develop", "develop.html", tab=tab, assets=assets, vg=vg, ug=ug, gate=gate, comments=comments, courses=courses,
                 state=state, course=course, team=tm, guide=guide, outline=olr.data if olr else None, ol_lock=ol_lock, ol_gate=ol_gate, grid=grid,
                 weeks=weeks, has_bp=has_bp, spec=sp)


@app.post("/p/{pid}/bulk")
def gate_bulk(request: Request, pid: int, gate_no: int = Form(...), note: str = Form("")):
    url = f"/p/{pid}/develop?tab=reviews&gate={gate_no}"
    if (r := deny(request, f"gate.val{gate_no}", url)):
        return r
    res = bulk_approve(pid, gate_no, who(request), note, role=role(request))
    msg = f"Gate {gate_no}: {res['approved']} approved"
    if res["skipped"]:
        msg += f"; {len(res['skipped'])} blocked by findings and need an individual decision"
    return back(url, msg=msg)


@app.get("/p/{pid}/asset/{key}", response_class=HTMLResponse)
def asset_page(request: Request, pid: int, key: str, rev: int | None = None, diff: int | None = None):
    with session() as s:
        r = get_record(s, pid, "asset", key)
        if not r:
            return back(f"/p/{pid}/develop", err="Asset not found")
        a = r.data
        sm = get_record(s, pid, "skills_map").data
        gates = list(s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key.like(f"val:{key}:%")).order_by(Gate.id))
        a11y = quality.a11y_records(s, pid).get(key)
    rev = rev or a["revision"]
    d_html = None
    if diff and str(diff) in a["revisions"] and str(rev) in a["revisions"]:
        old, new = asset_text(a["revisions"][str(diff)]).splitlines(), asset_text(a["revisions"][str(rev)]).splitlines()
        d_html = difflib.HtmlDiff(wrapcolumn=80).make_table(old, new, f"r{diff}", f"r{rev}", context=True, numlines=1)
    return ppage(request, pid, "develop", "asset.html", tab="board", a=a, rev=rev, content=a["revisions"].get(str(rev)), checks=a["checks"].get(str(rev)),
                 prov=a["provenance"].get(str(rev)), gates=gates, skills={k["skill_id"]: k for k in sm["skills"]}, diff=diff, d_html=d_html, a11y=a11y,
                 html=_content_html(a, a["revisions"].get(str(rev)) or {}))


def _kind(a, c):
    if a["asset_type"] == "module":
        return "module"
    if c.get("items") is not None or a.get("type") in ("knowledge check", "practice exam"):
        return "items"
    return "perf"


def _content_html(a, c):
    k = _kind(a, c)
    return content_html.module_to_html(c) if k == "module" else (content_html.perf_to_html(c) if k == "perf" else "")


@app.get("/p/{pid}/edit/{key}", response_class=HTMLResponse)
def editor(request: Request, pid: int, key: str):
    with session() as s:
        r = get_record(s, pid, "asset", key)
        if not r:
            return back(f"/p/{pid}/develop", err="Asset not found")
        a = r.data
        comments = [x.data for x in list_records(s, pid, "comment") if x.data["asset"] == key]
        sm = get_record(s, pid, "skills_map")
        tm = studio.team(s, pid)
        pack = sources.source_pack(s, pid)
        p = s.get(Program, pid)
    dr = a.get("draft") or {}
    base_rev = dr.get("base_revision", a["revision"])
    working = dr.get("content") or a["revisions"].get(str(a["revision"])) or {}
    kind = _kind(a, working)
    busy = a.get("state") in ("queued", "drafted")
    editable = can(request, "content.edit") and not busy
    room = f"p{pid}/asset/{key}/r{a['revision']}-{a.get('draft_gen', 0)}"
    seed_ok = collab.claim_seed(room, who(request)) if editable else False
    ch = a["checks"].get(str(a["revision"]), {})
    doc = {"room": room, "kind": kind, "editable": editable, "canSeed": seed_ok, "user": {"name": who(request), "role": role(request)},
           "seedHtml": content_html.module_to_html(working) if kind == "module" else (content_html.perf_to_html(working) if kind == "perf" else ""),
           "save": f"/p/{pid}/edit/{key}/autosave", "base": base_rev}
    if kind == "items":
        sk = [k["skill_id"] for k in (sm.data["skills"] if sm else [])]
        cols = [{"key": "item_id", "label": "Item", "width": 90, "readonly": True}, {"key": "skill_id", "label": "Skill", "type": "select", "options": [""] + sk, "width": 80},
                {"key": "objective_id", "label": "Objective", "width": 80}, {"key": "stem", "label": "Question stem", "type": "textarea", "width": 300}] + \
               [{"key": x, "label": f"Option {x}", "type": "textarea", "width": 180} for x in "ABCD"] + \
               [{"key": "key", "label": "Key", "type": "select", "options": list("ABCD"), "width": 60},
                {"key": "rationale", "label": "Rationales (A: ... || B: ...)", "type": "textarea", "width": 260}]
        doc.update({"columns": cols, "rows": content_html.items_to_rows(working)})
    return ppage(request, pid, "develop", "editor.html", tab="board", a=a, c=working, kind=kind, doc=doc, comments=comments, findings=ch.get("findings", []),
                 skills={k["skill_id"]: k for k in (sm.data["skills"] if sm else [])}, team=tm, pack_ids=[x["id"] for x in pack], live=bool(p.release_version),
                 busy=busy, editable=editable, draft=dr, screens=[sc.get("heading", "") for sc in working.get("screens", [])])


@app.post("/p/{pid}/edit/{key}/autosave")
async def editor_autosave(request: Request, pid: int, key: str):
    if (r := jdeny(request, "content.edit")):
        return r
    b = await request.json()
    with session() as s:
        a = get_record(s, pid, "asset", key).data
    if a.get("state") in ("queued", "drafted"):
        return JSONResponse({"error": "A worker is drafting or checking this asset; edits resume when it finishes."}, status_code=409)
    dr = a.get("draft") or {}
    base = dr.get("base_revision", a["revision"])
    cur = dr.get("content") or a["revisions"].get(str(a["revision"])) or {}
    kind = _kind(a, cur)
    if kind == "module":
        content = content_html.html_to_module(b.get("html", ""), cur)
    elif kind == "perf":
        content = content_html.html_to_perf(b.get("html", ""), cur)
    else:
        content = content_html.rows_to_items(b.get("rows", []), cur, a["ref_id"])
    if b.get("title"):
        content["title"] = b["title"].strip()
    studio.save_draft(pid, key, content, who(request), role(request), base)
    return JSONResponse({"ok": True, "saved_at": now().strftime("%H:%M:%S"),
                         "summary": f"{len(content.get('screens', []))} screens" if kind == "module" else (f"{len(content.get('items', []))} items" if kind == "items" else f"{len(content.get('rubric', []))} rubric rows")})


@app.post("/p/{pid}/edit/{key}/submit")
def editor_submit(request: Request, pid: int, key: str, change_class: str = Form("routine"), change_note: str = Form("")):
    url = f"/p/{pid}/edit/{key}"
    if (r := deny(request, "content.edit", url)):
        return r
    res = studio.submit_draft(pid, key, who(request), role(request), change_class, change_note)
    if res.startswith("ticket:"):
        return back(f"/p/{pid}/improve?tab=tickets", msg=f"Your revision is ticket {res[7:]}. It publishes after the decision and the five gates.")
    if res:
        return back(url, err=res)
    return back(f"/p/{pid}/asset/{key}", msg="Revision submitted. Automated checks are running; the five validation gates follow.")


@app.post("/p/{pid}/edit/{key}/discard")
def editor_discard(request: Request, pid: int, key: str):
    url = f"/p/{pid}/edit/{key}"
    if (r := deny(request, "content.edit", url)):
        return r
    with locked(pid) as s:
        rec = get_record(s, pid, "asset", key)
        d = dict(rec.data)
        d["draft"] = None
        d["draft_gen"] = d.get("draft_gen", 0) + 1
        rec.data = d
        log(s, pid, who(request), "author", f"Working draft of {key} discarded by {who(request)}.")
    return back(url, msg="Draft discarded. Everyone in the document now starts again from the current revision.")


@app.get("/p/{pid}/edit/{key}/history", response_class=HTMLResponse)
def editor_history(request: Request, pid: int, key: str, i: int | None = None):
    with session() as s:
        a = get_record(s, pid, "asset", key).data
    hist = (a.get("draft") or {}).get("history", [])
    base = a["revisions"].get(str((a.get("draft") or {}).get("base_revision", a["revision"])), {})
    d_html = None
    if i is not None and 0 <= i < len(hist):
        prev = hist[i - 1]["content"] if i > 0 else base
        d_html = difflib.HtmlDiff(wrapcolumn=80).make_table(asset_text(prev).splitlines(), asset_text(hist[i]["content"]).splitlines(),
                                                            "before", f"{hist[i]['by']} {hist[i]['at']}", context=True, numlines=1)
    return ppage(request, pid, "develop", "draft_history.html", tab="board", a=a, hist=hist, i=i, d_html=d_html)


@app.post("/p/{pid}/assist")
async def studio_assist(request: Request, pid: int):
    if (r := jdeny(request, "content.edit")):
        return r
    b = await request.json()
    return JSONResponse(sources.assist(pid, b.get("instruction", ""), b.get("heading", ""), b.get("current", ""), b.get("asset", "")))


@app.get("/p/{pid}/comments.json")
def comments_json(pid: int, asset: str):
    with session() as s:
        rows = [r.data for r in list_records(s, pid, "comment") if r.data["asset"] == asset]
    return JSONResponse(rows)


@app.post("/p/{pid}/comment")
async def add_comment(request: Request, pid: int):
    if (r := jdeny(request, "comment")):
        return r
    b = await request.json()
    err = studio.add_comment(pid, b.get("asset", ""), b.get("anchor", ""), b.get("body", ""), who(request), role(request), b.get("parent", ""))
    return JSONResponse({"error": err} if err else {"ok": True}, status_code=400 if err else 200)


@app.post("/p/{pid}/comment/resolve")
async def resolve_comment(request: Request, pid: int):
    if (r := jdeny(request, "comment")):
        return r
    b = await request.json()
    studio.resolve_comment(pid, b.get("cid", ""), who(request))
    return JSONResponse({"ok": True})


@app.post("/p/{pid}/owners")
def set_owners(request: Request, pid: int, asset: str = Form(...), author: str = Form(""), sme: str = Form(""), reviewer: str = Form("")):
    url = f"/p/{pid}/edit/{asset}"
    if (r := deny(request, "owners.assign", url)):
        return r
    studio.set_owners(pid, asset, author, sme, reviewer, who(request))
    return back(url, msg="Owners updated")


@app.get("/p/{pid}/develop/tracker.csv")
def tracker(pid: int):
    return Response(studio.tracker_csv(pid), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="Asset tracker - Program {pid} (Smartsheet import).csv"'})


# ------------------------------------------------------------------ Program: Quality and Release
@app.get("/p/{pid}/quality", response_class=HTMLResponse)
def quality_page(request: Request, pid: int):
    tab = tab_of(request, "quality")
    with session() as s:
        qa = get_record(s, pid, "qa_report")
        assets = [r.data for r in list_records(s, pid, "asset")]
        a11y = quality.a11y_records(s, pid)
        lims = quality.limitations(s, pid)
        queue, week = quality.sample_queue(s, pid)
        ssum = quality.sample_summary(s, pid)
        dels = specmod.deliverables(s, pid)
        rels = sorted([(r.key, r.status, r.data) for r in list_records(s, pid, "release")], key=lambda t: [int(x) for x in t[0].split(".")])
        gates = {g.gate_key: g for g in s.query(Gate).filter(Gate.program_id == pid, Gate.gate_key.in_(["umgc.qa", "umgc.launch"]))}
    tp = docsgen.build(pid, "testplan") if tab == "qa" else None
    return ppage(request, pid, "quality", "quality.html", tab=tab, qa=qa.data if qa else None, assets=assets, a11y=a11y, lims=lims, queue=queue, week=week,
                 ssum=ssum, dels=dels, rels=rels, gates=gates, tp=tp)


@app.post("/p/{pid}/a11y")
async def a11y_save(request: Request, pid: int):
    url = f"/p/{pid}/quality?tab=a11y"
    if (r := deny(request, "a11y.record", url)):
        return r
    f = dict((await request.form()).items())
    err = quality.save_a11y(pid, f.get("asset", ""), {k: f.get(k, "not tested") for k, _ in quality.AT_TESTS}, f.get("note", ""), who(request))
    return back(url, msg="" if err else "Assistive technology test recorded.", err=err)


@app.post("/p/{pid}/limitation")
def lim_save(request: Request, pid: int, asset: str = Form(""), issue: str = Form(""), criterion: str = Form(""), plan: str = Form(""), due: str = Form("")):
    url = f"/p/{pid}/quality?tab=a11y"
    if (r := deny(request, "a11y.record", url)):
        return r
    err = quality.save_limitation(pid, asset, issue, criterion, plan, due, who(request))
    return back(url, msg="" if err else "Limitation recorded with its remediation plan.", err=err)


@app.post("/p/{pid}/limitation/close")
def lim_close(request: Request, pid: int, lid: str = Form(...)):
    url = f"/p/{pid}/quality?tab=a11y"
    if (r := deny(request, "a11y.record", url)):
        return r
    quality.close_limitation(pid, lid, who(request))
    return back(url, msg=f"{lid} remediated.")


@app.post("/p/{pid}/sample")
async def sample_save(request: Request, pid: int):
    url = f"/p/{pid}/quality?tab=sampling"
    if (r := deny(request, "sample.score", url)):
        return r
    f = dict((await request.form()).items())
    err = quality.save_sample(pid, f.get("key", ""), {c: f.get(c) for c, _ in quality.CRITERIA}, f.get("note", ""), who(request))
    return back(url, msg="" if err else "Sample scored.", err=err)


@app.post("/p/{pid}/deliverable")
def deliverable_act(request: Request, pid: int, code: str = Form(...), action: str = Form(...), note: str = Form("")):
    url = f"/p/{pid}/quality?tab=deliverables"
    if action == "submit":
        if (r := deny(request, "deliverable.submit", url)):
            return r
        err = specmod.submit_deliverable(pid, code, who(request), note)
        return back(url, msg="" if err else f"{code} submitted to UMGC. The review window is 5 business days.", err=err)
    if (r := deny(request, "deliverable.accept", url)):
        return r
    err = specmod.decide_deliverable(pid, code, "accept" if action == "accept" else "return", who(request), note)
    return back(url, msg="" if err else f"{code} {'accepted' if action == 'accept' else 'returned'}.", err=err)


# ------------------------------------------------------------------ documents
@app.get("/p/{pid}/doc/{key}", response_class=HTMLResponse)
def doc_view(request: Request, pid: int, key: str):
    if key.endswith(".docx"):
        return doc_docx(request, pid, key[:-5])
    if key not in docsgen.DOCS:
        return back(f"/p/{pid}", err="Unknown document")
    d = docsgen.build(pid, key)
    sec = {"pmp": "setup", "schedule": "setup", "guides": "develop", "environment": "learner", "integration": "learner", "testplan": "quality",
           "training": "quality", "readiness": "quality", "monthly": "improve", "sla": "improve", "cip": "improve", "attd": "trace"}[key]
    return ppage(request, pid, sec, "doc.html", tab=None, doc=d, key=key)


@app.get("/p/{pid}/doc/{key}.docx")
def doc_docx(request: Request, pid: int, key: str):
    if key not in docsgen.DOCS:
        return back(f"/p/{pid}", err="Unknown document")
    d = docsgen.build(pid, key)
    with session() as s:
        log(s, pid, who(request), "delivery", f"{d['title']} generated as Word by {who(request)}.")
    return Response(docsgen.to_docx(d), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{d["title"]} - Program {pid}.docx"'})


# ------------------------------------------------------------------ Program: Learner Experience
@app.get("/p/{pid}/learner", response_class=HTMLResponse)
def learner(request: Request, pid: int, module: str = "", screen: int = 0):
    tab = tab_of(request, "learner")
    with session() as s:
        mods = [(r.data["ref_id"], r.data["title"], r.key) for r in list_records(s, pid, "asset") if r.data["asset_type"] == "module" and r.data.get("state") == "validated"]
        cur = None
        if mods:
            k = next((m[2] for m in mods if m[0] == module), mods[0][2])
            a = get_record(s, pid, "asset", k).data
            cur = {"ref": a["ref_id"], "title": a["title"], "content": a["revisions"][str(a["revision"])], "key": k}
        rels = sorted([r for r in list_records(s, pid, "release")], key=lambda r: [int(x) for x in r.key.split(".")])
        lti = rels[-1].data.get("lti_tool_config") if rels else None
        n_st = s.query(Statement).filter(Statement.program_id == pid).count()
        recent = list(s.query(Statement).filter(Statement.program_id == pid).order_by(Statement.id.desc()).limit(25))
        asked = list(s.query(Statement).filter(Statement.program_id == pid, Statement.verb == "asked").order_by(Statement.id.desc()).limit(10))
        bp = get_record(s, pid, "blueprint")
        sp = specmod.get_spec(s, pid)
    return ppage(request, pid, "learner", "learner.html", tab=tab, mods=mods, cur=cur, screen=screen, lti=lti, n_st=n_st, recent=recent, asked=asked,
                 bp=bp.data if bp else None, spec=sp)


@app.post("/p/{pid}/tutor")
async def tutor(request: Request, pid: int):
    body = await request.json()
    out = learn.tutor_answer(pid, body.get("q", ""), body.get("mode", "socratic"), body.get("module") or None)
    with session() as s:
        s.add(Statement(program_id=pid, learner="preview:" + (who(request) or "user")[:30], verb="asked",
                        object_id=f"M-{(out['sources'] or [{}])[0].get('module', 'NA')}", extra={"topic": body.get("q", "")[:80], "category": out["category"]}))
    return JSONResponse(out)


# ------------------------------------------------------------------ Program: Insights
@app.get("/p/{pid}/insights", response_class=HTMLResponse)
def insights(request: Request, pid: int):
    tab = tab_of(request, "insights")
    with session() as s:
        ms = sorted([r.data for r in list_records(s, pid, "metrics")], key=lambda d: [int(x) for x in d["release"].split(".")])
        sp = specmod.get_spec(s, pid)
        demo = quality.demographics(s, pid, sp.get("attributes", [])) if tab == "demographics" else None
        previews = {k: quality.report_rows(s, pid, k) for k, _, _ in quality.REPORTS} if tab == "reports" else {}
    return ppage(request, pid, "insights", "insights.html", tab=tab, ms=ms, m=ms[-1] if ms else None, demo=demo, spec=sp, reports=quality.REPORTS, previews=previews)


@app.get("/p/{pid}/reports/{key}.csv")
def report_csv(pid: int, key: str):
    with session() as s:
        data = quality.report_csv(s, pid, key)
    return Response(data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{key} report - Program {pid}.csv"'})


@app.post("/p/{pid}/demographics")
def demo_config(request: Request, pid: int, attributes: str = Form("")):
    url = f"/p/{pid}/insights?tab=demographics"
    if (r := deny(request, "demographics.config", url)):
        return r
    with locked(pid) as s:
        sp = json.loads(json.dumps(specmod.get_spec(s, pid)))
        sp["attributes"] = [a.strip() for a in attributes.split(",") if a.strip()]
        put_record(s, pid, "spec", "main", sp)
        log(s, pid, who(request), "measure", f"Learner attributes set: {', '.join(sp['attributes'])}.")
    return back(url, msg="Attributes saved.")


# ------------------------------------------------------------------ Program: Improvement
@app.get("/p/{pid}/improve", response_class=HTMLResponse)
def improve_page(request: Request, pid: int):
    tab = tab_of(request, "improve")
    with session() as s:
        tickets = [r.data for r in list_records(s, pid, "ticket")]
        sm = get_record(s, pid, "skills_map")
        assets = sorted(r.key for r in list_records(s, pid, "asset"))
        rels = sorted([(r.key, r.status, r.data) for r in list_records(s, pid, "release")], key=lambda t: [int(x) for x in t[0].split(".")])
        refresh_jobs = list(s.query(Job).filter(Job.program_id == pid, Job.task == "refresh.lightcast").order_by(Job.id.desc()).limit(10))
    return ppage(request, pid, "improve", "improve.html", tab=tab, tickets=tickets, refresh=(sm.data.get("last_refresh") if sm else None), assets=assets,
                 rels=rels, refresh_jobs=refresh_jobs)


@app.post("/p/{pid}/ticket")
def ticket_decide(request: Request, pid: int, ticket_id: str = Form(...), decision: str = Form(...), change_class: str = Form("routine"), note: str = Form("")):
    url = f"/p/{pid}/improve?tab=tickets"
    if not (can(request, "ticket.routine") or can(request, "ticket.material")):
        return back(url, err="Your role cannot decide change tickets.")
    err = decide_ticket(pid, ticket_id, decision, who(request), role(request), change_class, note)
    return back(url, msg="" if err else f"Ticket {ticket_id} {decision}d", err=err)


@app.post("/p/{pid}/event")
def raise_event(request: Request, pid: int, title: str = Form(...), detail: str = Form(""), target: str = Form(""), change_class: str = Form("material")):
    url = f"/p/{pid}/improve?tab=tickets"
    if (r := deny(request, "event.raise", url)):
        return r
    tid = improve.raise_event(pid, title, detail, target, change_class)
    advance(pid)
    return back(url, msg=f"Ticket {tid} raised")


@app.post("/p/{pid}/refresh")
def refresh_now(request: Request, pid: int):
    url = f"/p/{pid}/improve?tab=refresh"
    if (r := deny(request, "refresh.run", url)):
        return r
    with session() as s:
        n = s.query(Job).filter(Job.program_id == pid, Job.task == "refresh.lightcast").count()
        enqueue(s, pid, "refresh", "refresh.lightcast", f"refresh.lightcast:manual{n + 1}", {})
        log(s, pid, who(request), "improve", "Lightcast refresh requested.")
    return back(url, msg="Lightcast refresh queued on the skills worker")


# ------------------------------------------------------------------ Program: Traceability
@app.get("/p/{pid}/trace", response_class=HTMLResponse)
def trace(request: Request, pid: int, skill: str | None = None, assessment: str | None = None):
    tab = tab_of(request, "trace")
    with session() as s:
        sm = get_record(s, pid, "skills_map")
        bp = get_record(s, pid, "blueprint")
        assets = {r.key: r.data for r in list_records(s, pid, "asset")}
        rels = sorted([r.key for r in list_records(s, pid, "release")], key=lambda k: [int(x) for x in k.split(".")])
    attd = docsgen.build(pid, "attd") if tab == "attd" else None
    if not (sm and bp):
        return ppage(request, pid, "trace", "trace.html", tab=tab, ready=False, attd=attd, rels=rels)
    sm, bp = sm.data, bp.data
    skills_ = {k["skill_id"]: k for k in sm["skills"]}
    comps = {c["competency_id"]: c for c in sm["competencies"]}
    objs = {o["objective_id"]: o for o in bp["objectives"]}
    down = up = None
    if skill and skill in skills_:
        sk = skills_[skill]
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
                "modules": mods, "assessments": asm, "items": items, "rubric": rubric, "occupation": sm["occupation"], "tm": bp["trace_matrix"].get(skill, {})}
    if assessment:
        a = next((x for x in bp["assessments"] if x["assessment_id"] == assessment), None)
        if a:
            ad = assets.get(f"A-{assessment}")
            up = {"assessment": a, "objectives": [objs[o] for o in a.get("objective_ids", []) if o in objs],
                  "skills": [skills_[k] for k in a.get("measured", []) if k in skills_], "practiced": [skills_[k] for k in a.get("practiced", []) if k in skills_],
                  "content": ad["revisions"].get(str(ad["revision"])) if ad else None, "occupation": sm["occupation"], "comps": comps}
    return ppage(request, pid, "trace", "trace.html", tab=tab, ready=True, sm=sm, bp=bp, down=down, up=up, skill=skill, assessment=assessment, attd=attd, rels=rels)


@app.get("/p/{pid}/download/workbook")
def dl_workbook(pid: int):
    return Response(exports.workbook(pid), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Traceability Workbook - Program {pid}.xlsx"'})


@app.get("/p/{pid}/download/trace.json")
def dl_trace(pid: int):
    return Response(exports.trace_json(pid), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="trace-program-{pid}.json"'})


@app.get("/p/{pid}/download/package/{version}")
def dl_package(pid: int, version: str):
    return Response(exports.package(pid, version), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="Program {pid} release {version}.zip"'})


# ------------------------------------------------------------------ Program: Audit
@app.get("/p/{pid}/audit", response_class=HTMLResponse)
def audit(request: Request, pid: int, kind: str = "skills_map", i: int | None = None):
    tab = tab_of(request, "audit")
    with session() as s:
        events = list(s.query(Event).filter(Event.program_id == pid).order_by(Event.id.desc()).limit(400)) if tab == "activity" else []
        calls = list(s.query(ApiCall).filter(ApiCall.program_id == pid).order_by(ApiCall.id.desc()).limit(300)) if tab == "api" else []
        by = Counter((c.service, c.mode, c.path.split("?")[0]) for c in s.query(ApiCall).filter(ApiCall.program_id == pid)) if tab == "api" else {}
        hist = edits.history(s, pid, kind) if tab == "history" else []
        jobs = list(s.query(Job).filter(Job.program_id == pid).order_by(Job.id.desc()).limit(60)) if tab == "activity" else []
    d_html = None
    if tab == "history" and i is not None and 0 <= i < len(hist):
        cur = hist[i]["data"]
        prev = hist[i + 1]["data"] if i + 1 < len(hist) else {}
        dump = lambda d: json.dumps({k: v for k, v in d.items() if k in ("skills", "competencies", "courses", "objectives", "assessments")}, indent=1, sort_keys=True).splitlines()
        d_html = difflib.HtmlDiff(wrapcolumn=90).make_table(dump(prev), dump(cur), "previous", f"{hist[i]['by']} {hist[i]['at']}", context=True, numlines=1)
    return ppage(request, pid, "audit", "audit.html", tab=tab, events=events, calls=calls, by=sorted(by.items(), key=lambda t: -t[1]) if by else [],
                 hist=hist, kind=kind, i=i, d_html=d_html, jobs=jobs)


# ------------------------------------------------------------------ JSON API
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


@app.get("/api/programs/{pid}/pulse")
def api_pulse(pid: int):
    """Lightweight poll so pages can show progress without a reload."""
    with session() as s:
        p = s.get(Program, pid)
        last = s.query(Event).filter(Event.program_id == pid).order_by(Event.id.desc()).first()
        return {"stage": p.stage, "status": p.status, "jobs": s.query(Job).filter(Job.program_id == pid, Job.status.in_(["queued", "running"])).count(),
                "last_event": last.id if last else 0}
