"""Scheduled triggers (Render cron jobs).

    python -m engine.cron refresh     quarterly Lightcast refresh for every live Program (periodic trigger)
    python -m engine.cron housekeep   requeue jobs stuck by a restarted worker; nudge every Program forward
"""
from __future__ import annotations

import datetime as dt
import sys

from sqlalchemy import select

from .db import Program, init_db, session
from .orchestrator import advance
from .queue import enqueue, reset_stuck


def refresh():
    q = f"{dt.date.today().year}Q{(dt.date.today().month - 1) // 3 + 1}"
    with session() as s:
        for p in s.execute(select(Program)).scalars():
            if p.release_version:
                enqueue(s, p.id, "refresh", "refresh.lightcast", f"refresh.lightcast:{q}", {"quarter": q})
                print(f"Queued Lightcast refresh {q} for Program {p.id} {p.name}")


def housekeep():
    print(f"Requeued {reset_stuck()} stuck job(s)")
    with session() as s:
        ids = [p.id for p in s.execute(select(Program)).scalars()]
    for i in ids:
        advance(i)


if __name__ == "__main__":
    init_db()
    {"refresh": refresh, "housekeep": housekeep}[sys.argv[1] if len(sys.argv) > 1 else "housekeep"]()
