"""Background worker. One process serves one pool of queues.

    python -m engine.worker --pool build          # one Render background worker per pool
    python -m engine.worker --pool all            # every queue in one process (laptop)
"""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import traceback

from sqlalchemy import select

from . import config
from .db import WorkerBeat, init_db, log, now, session
from .orchestrator import advance
from .queue import claim, finish, reset_stuck
from . import sources
from .stages import build, define, deliver, design, improve, learn, measure, qa

TASKS = {
    "skills.define": define.run,
    "refresh.lightcast": improve.run_refresh,
    "design.blueprint": design.run,
    "build.asset": build.run,
    "qa.asset_checks": qa.run_asset_checks,
    "qa.program": qa.run_program_qa,
    "deliver.release": deliver.run,
    "learn.cohort": learn.run_cohort,
    "measure.compute": measure.run,
    "improve.scan": improve.run_scan,
    "ingest.source": sources.run_ingest,
}


def _beat(name, pool, queues, inc=0):
    try:
        with session() as s:
            w = s.execute(select(WorkerBeat).where(WorkerBeat.name == name)).scalar_one_or_none()
            if not w:
                w = WorkerBeat(name=name, pool=pool, queues=",".join(queues), jobs_done=0)
                s.add(w)
            w.last_seen = now()
            w.jobs_done = (w.jobs_done or 0) + inc
    except Exception:
        pass


def work_loop(pool: str, queues: list[str], name: str, stop: threading.Event | None = None):
    _beat(name, pool, queues)
    last_beat = time.time()
    while not (stop and stop.is_set()):
        job = claim(queues, name)
        if job is None:
            if time.time() - last_beat > 20:
                _beat(name, pool, queues)
                last_beat = time.time()
            time.sleep(config.WORKER_POLL_SECONDS)
            continue
        fn = TASKS.get(job.task)
        try:
            if fn is None:
                raise RuntimeError(f"Unknown task {job.task}")
            payload = dict(job.payload or {})
            payload["_worker"] = f"worker:{pool}"
            fn(job.program_id, payload)
            finish(job.id, True)
            _beat(name, pool, queues, 1)
        except Exception as e:
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            finish(job.id, False, err)
            try:
                with session() as s:
                    log(s, job.program_id, name, "error", f"{job.task} failed (attempt {job.attempts}): {type(e).__name__}: {e}")
            except Exception:
                pass
        try:
            advance(job.program_id)
        except Exception:
            traceback.print_exc()


def start_embedded(threads_per_pool: dict | None = None) -> list[threading.Thread]:
    """Run every pool as threads in this process (EMBEDDED_WORKERS=true or local dev)."""
    threads_per_pool = threads_per_pool or {"build": 3, "qa": 2}
    out = []
    host = socket.gethostname()[:20]
    for pool, queues in config.POOLS.items():
        for i in range(threads_per_pool.get(pool, 1)):
            t = threading.Thread(target=work_loop, args=(pool, queues, f"{pool}-{host}-t{i + 1}"), daemon=True)
            t.start()
            out.append(t)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=os.getenv("WORKER_POOL", "all"))
    ap.add_argument("--threads", type=int, default=int(os.getenv("WORKER_THREADS", "1")))
    a = ap.parse_args()
    init_db()
    reset_stuck()
    queues = config.ALL_QUEUES if a.pool == "all" else config.POOLS[a.pool]
    host = os.getenv("RENDER_INSTANCE_ID", socket.gethostname())[-12:] + f"-{os.getpid()}"
    print(f"Worker pool '{a.pool}' serving queues {queues} with {a.threads} thread(s). Lightcast "
          f"{'live' if config.LIGHTCAST_LIVE else 'mock'}, Claude {'live' if config.LLM_LIVE else 'mock'}.", flush=True)
    ths = [threading.Thread(target=work_loop, args=(a.pool, queues, f"{a.pool}-{host}-t{i + 1}"), daemon=True) for i in range(a.threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()


if __name__ == "__main__":
    main()
