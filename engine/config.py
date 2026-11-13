"""Runtime settings. Every value comes from an environment variable so Render (or a laptop) configures it."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed"


PLACEHOLDERS = {"", "set-me", "changeme", "none"}


def _secret(name: str) -> str:
    v = os.getenv(name, "").strip()
    return "" if v.lower() in PLACEHOLDERS else v


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{(Path.cwd() / 'engine.db').as_posix()}"
    # Render hands out postgres:// or postgresql://; SQLAlchemy with psycopg 3 wants postgresql+psycopg://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Lightcast. Open Skills is free; LOT, Job Postings Analytics and Career Pathways need UMGC's or Magic's license.
LIGHTCAST_CLIENT_ID = _secret("LIGHTCAST_CLIENT_ID")
LIGHTCAST_CLIENT_SECRET = _secret("LIGHTCAST_CLIENT_SECRET")
LIGHTCAST_SCOPE = os.getenv("LIGHTCAST_SCOPE", "emsi_open").strip()
LIGHTCAST_LICENSED = _bool("LIGHTCAST_LICENSED_PRODUCTS", False)  # LOT, JPA, Career Pathways
LIGHTCAST_LIVE = bool(LIGHTCAST_CLIENT_ID and LIGHTCAST_CLIENT_SECRET) and not _bool("FORCE_MOCK_LIGHTCAST")

# Claude (Anthropic API)
ANTHROPIC_API_KEY = _secret("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
LLM_LIVE = bool(ANTHROPIC_API_KEY) and not _bool("FORCE_MOCK_LLM")

# Web
APP_PASSWORD = _secret("APP_PASSWORD")  # empty = no login (local only)
EMBEDDED_WORKERS = _bool("EMBEDDED_WORKERS", False)   # run every worker inside the web process (cheap demo mode)
WORKER_POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "1.5"))
JOB_MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", "3"))

# Queues and the worker pool that serves each. One Render background worker per pool.
POOLS = {
    "skills": ["skills", "refresh", "ingest"],  # Stage 1 Define skills, quarterly Lightcast refresh, source ingest
    "design": ["design"],                     # Stage 2 Blueprint
    "build": ["build"],                       # Stage 3 Build (fan-out, scale this pool horizontally)
    "qa": ["qa", "deliver"],                  # Stage 4 Assure quality, Stage 5 Deliver
    "learn": ["learn", "measure", "improve"], # Stages 6 Learn, 7 Measure, 8 Improve
}
ALL_QUEUES = sorted({q for qs in POOLS.values() for q in qs})

# Data-informed refresh thresholds (Technical Proposal 2.3.3.2)
THRESHOLDS = {
    "module_dropoff_pct": float(os.getenv("T_DROPOFF", "12")),
    "item_p_low": 0.25,
    "item_p_high": 0.97,
    "item_discrimination_min": 0.10,
    "time_on_task_ratio_high": 1.8,
    "time_on_task_ratio_low": 0.4,
    "tutor_cluster_min": int(os.getenv("T_TUTOR_CLUSTER", "40")),
    "completion_target_pct_150": 70.0,  # Workforce Pell: 70 percent complete within 150 percent of expected time
}

RELEASE_THRESHOLD = 85  # 100-point Course release standard (Technical Proposal 2.2.1.1)
