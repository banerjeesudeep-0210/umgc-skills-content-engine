# UMGC Skills Content Engine

The working version of the end-to-end AI workflow in Magic EdTech's response to UMGC RFP 92303 (Technical Proposal 2.3.4, Figure 19). Eight stages, four lanes, every gate in the proposal, and an authoring and collaboration zone where source material comes in and people write.

| Stage | What the AI layer does | What people decide |
|---|---|---|
| 1 Define skills | Lightcast LOT occupation, Job Postings Analytics demand, extract/trace on every source, one Lightcast skill ID per skill, 40/30/20/10 priority score, tier | Magic SMEs approve statements; three employers validate; UMGC Gate 1 |
| 2 Design Blueprint | Objectives, Courses and Modules, assessments, prerequisites; five coverage rules, prerequisite DAG, seat-time map | ID and SME edit; UMGC Gate 2 |
| 3 Build | One job per Module or assessment, grounded in the approved source pack, provenance on every asset | Five validation gates per asset; prototype Module; Sprint 1 and Sprint 2 review windows |
| 4 Assure quality | WCAG lint, readability, terminology, bias screen, grounding, Lightcast trace of the built text, 100-point Course release score | UMGC accepts the QA report |
| 5 Deliver | Numbered release, content package (HTML5, QTI 2.1, manifest), LTI 1.3 config, provenance log, tutor evaluation set | UMGC launch sign-off |
| 6 Learn | AI Study Assistant (grounded, Socratic, declines graded work); xAPI statements | Guardrails set at design time |
| 7 Measure | Completion at 100 and 150 percent of expected time, drop-off, item p-value and discrimination, time on task, tutor clusters | Monthly review |
| 8 Improve | Quarterly Lightcast refresh, event triggers, data thresholds draft tickets | Routine under standing approval; material changes need a UMGC approver; changes re-enter Build and pass the same gates |

**Authoring and collaboration zone.** Ingest (upload PDF, Word, PowerPoint, HTML or text; the ingest worker tags every section to Lightcast skills; a Magic SME approves a source before the AI may ground on it), Write (shared drafts for every Module and assessment, AI assist from the approved sources only, submit into checks and the five gates; after launch a submission becomes a change ticket), Collaborate (comments anchored to a screen or item, @mentions, resolve, owners per asset, My inbox), Team (Magic SMEs are a vendor responsibility; roster with expertise and Course ownership). A returned human revision goes back to its author, never to the AI. The asset tracker exports as CSV for UMGC's Smartsheet.

## Publish on Render

1. Put this folder in a GitHub repository (on github.com: New repository, then "uploading an existing file" and drag the folder contents in; `render.yaml` must sit at the top level).
2. In Render: New, then Blueprint, pick the repository, and Apply. Render creates the web service, five background workers, two cron jobs and the Postgres database from `render.yaml`.
3. Open the web service's Environment tab and copy `APP_PASSWORD` (Render generated it). Sign in to the dashboard with any user name and that password.
4. Open the environment group `skills-engine-keys` and replace `set-me` with your keys:
   - `ANTHROPIC_API_KEY` turns on Claude drafting, AI assist, ticket proposals and the tutor.
   - `LIGHTCAST_CLIENT_ID` and `LIGHTCAST_CLIENT_SECRET` turn on live Lightcast Open Skills (free: lightcast.io/open-skills/access).
   - `LIGHTCAST_LICENSED_PRODUCTS=true` plus `LIGHTCAST_LICENSED_SCOPE` turns on LOT, Job Postings Analytics and Career Pathways under UMGC's or Magic's license.
   Leave any key as `set-me` and that part runs in mock mode: every mock ID and demand figure starts with MOCK, and every provenance record says template-drafted.
5. Click "Start sample Program". Use "Demo autopilot" to walk the gates quickly, or sign in (top right) with a name and role and decide each gate yourself.

**Demo option (two paid items).** In Render choose New, then Blueprint, and set the Blueprint file path to `demo/render.yaml`. It creates one web service that runs every worker inside itself, plus Postgres, and asks for your keys during setup.

Plans in `render.yaml` are the smallest paid instances (background workers and crons have no free plan). Scale the build pool with `numInstances` when several Programs run at once. For a one-service demo, run only the web service with `EMBEDDED_WORKERS=true`; it then runs every worker pool inside itself.

## Run it on a Mac or PC

Double-click `Run Skills Content Engine.command` (Mac) or `run_local.bat` (Windows). It installs what it needs and opens http://localhost:8000. Copy `.env.example` to `.env` to add keys.

## How it is built

- `engine/orchestrator.py` the workflow: stages, UMGC gates, five validation gates, ticket loop. Idempotent; called after every job and every decision.
- `engine/queue.py` Postgres job queue (FOR UPDATE SKIP LOCKED); `engine/worker.py` one process per pool: skills (and ingest, refresh), design, build, qa (and deliver), learn (and measure, improve).
- `engine/stages/` one module per stage. `engine/sources.py` ingest and the grounding pack. `engine/studio.py` drafts, comments, owners, team.
- `engine/clients/lightcast.py` live and mock Lightcast; `engine/clients/llm.py` Claude with prompt IDs and versions for provenance.
- `engine/exports.py` traceability workbook (Exhibit I shape plus build, QA, provenance, gates, tickets, API calls), trace JSON, release packages.
- `engine/web/` dashboard (FastAPI and Jinja, Magic brand).

The synthetic learner cohort in Stage 6 stands in for the delivery platform's LRS so Measure and Improve have signals before UMGC learners arrive; it plants four signals in revision 1 (a drop-off Module, a slow lab, an ambiguous item, a tutor question cluster) that the Improve stage must find and the gates must fix.
