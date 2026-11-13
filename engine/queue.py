"""Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED). No Redis needed.

Jobs are idempotent: each has a dedupe_key unique per Program, so the orchestrator can call
enqueue() as often as it likes and a job is created once.
"""
from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import config
from .db import Job, now, session

_sqlite_lock = threading.Lock()


def enqueue(s, program_id: int, queue: str, task: str, dedupe_key: str, payload: dict | None = None) -> bool:
    """Create the job if it does not exist yet. Returns True when a new job was created."""
    existing = s.execute(select(Job.id).where(Job.program_id == program_id, Job.dedupe_key == dedupe_key)).first()
    if existing:
        return False
    sp = s.begin_nested()
    try:
        s.add(Job(program_id=program_id, queue=queue, task=task, dedupe_key=dedupe_key, payload=payload or {}))
        s.flush()
        sp.commit()
        return True
    except IntegrityError:
        sp.rollback()
        return False


def claim(queues: list[str], worker: str) -> Job | None:
    def _claim():
        with session() as s:
            q = (select(Job).where(Job.status == "queued", Job.queue.in_(queues)).order_by(Job.id).limit(1))
            if not config.IS_SQLITE:
                q = q.with_for_update(skip_locked=True)
            job = s.execute(q).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.worker = worker
            job.started_at = now()
            s.flush()
            s.expunge(job)
            return job
    if config.IS_SQLITE:
        with _sqlite_lock:
            return _claim()
    return _claim()


def finish(job_id: int, ok: bool, error: str = ""):
    with session() as s:
        job = s.get(Job, job_id)
        if ok:
            job.status = "done"
            job.error = ""
        elif job.attempts < config.JOB_MAX_ATTEMPTS:
            job.status = "queued"
            job.error = error[-4000:]
        else:
            job.status = "failed"
            job.error = error[-4000:]
        job.finished_at = now()


def retry_failed(program_id: int | None = None) -> int:
    with session() as s:
        q = select(Job).where(Job.status == "failed")
        if program_id:
            q = q.where(Job.program_id == program_id)
        n = 0
        for j in s.execute(q).scalars():
            j.status, j.attempts, n = "queued", 0, n + 1
        return n


def reset_stuck(minutes: int = 15) -> int:
    """A job left 'running' by a worker that restarted goes back to the queue."""
    import datetime as dt
    with session() as s:
        cutoff = now() - dt.timedelta(minutes=minutes)
        n = 0
        for j in s.execute(select(Job).where(Job.status == "running", Job.started_at < cutoff)).scalars():
            j.status, n = "queued", n + 1
        return n
