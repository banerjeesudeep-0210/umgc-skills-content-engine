"""Database: one schema that runs on SQLite (laptop) and Postgres (Render).

Tables
  programs     one row per Program (Task Order)
  records      every artifact the workflow produces, keyed by (program, kind, key): skills map, blueprint,
               assets (Modules and assessments), QA report, releases, tickets, metrics ...
  jobs         the work queue the workers pull from
  gates        human decisions: UMGC approval gates and the five Magic validation gates per asset
  events       audit trail shown on every Program page
  api_calls    every Lightcast and Claude call (the API_Calls sheet in the workbook)
  statements   xAPI statements from the learner layer
  workers      worker heartbeats, so the dashboard shows which pools are alive
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

from sqlalchemy import (JSON, LargeBinary, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, event, select)
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

Base = declarative_base()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Program(Base):
    __tablename__ = "programs"
    id = Column(Integer, primary_key=True)
    name = Column(String(300), nullable=False)
    brief = Column(JSON, nullable=False)
    stage = Column(String(40), default="define")
    status = Column(String(40), default="running")
    release_version = Column(String(20), default="")
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("program_id", "kind", "key"),)
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("programs.id"), index=True)
    kind = Column(String(40), index=True)
    key = Column(String(120))
    status = Column(String(40), default="")
    version = Column(Integer, default=1)
    data = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=now, onupdate=now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("program_id", "dedupe_key"),)
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("programs.id"), index=True)
    queue = Column(String(30), index=True)
    task = Column(String(60))
    dedupe_key = Column(String(200))
    payload = Column(JSON, default=dict)
    status = Column(String(20), default="queued", index=True)  # queued, running, done, failed
    attempts = Column(Integer, default=0)
    worker = Column(String(80), default="")
    error = Column(Text, default="")
    created_at = Column(DateTime, default=now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class Gate(Base):
    __tablename__ = "gates"
    __table_args__ = (UniqueConstraint("program_id", "gate_key"),)
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("programs.id"), index=True)
    gate_key = Column(String(160))
    kind = Column(String(20))          # umgc or validation
    label = Column(String(200))
    owner_role = Column(String(120))
    status = Column(String(20), default="pending")  # pending, approved, returned
    decided_by = Column(String(120), default="")
    note = Column(Text, default="")
    evidence = Column(JSON, default=dict)
    opened_at = Column(DateTime, default=now)
    decided_at = Column(DateTime)


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, index=True)
    at = Column(DateTime, default=now, index=True)
    actor = Column(String(80))
    stage = Column(String(40))
    message = Column(Text)


class ApiCall(Base):
    __tablename__ = "api_calls"
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, index=True)
    at = Column(DateTime, default=now)
    service = Column(String(30))
    mode = Column(String(10))
    method = Column(String(10))
    path = Column(String(300))
    purpose = Column(String(200))
    status = Column(String(20))
    ms = Column(Integer, default=0)


class Statement(Base):
    __tablename__ = "statements"
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, index=True)
    learner = Column(String(40), index=True)
    verb = Column(String(30), index=True)
    object_id = Column(String(80), index=True)
    result_score = Column(Float)
    result_success = Column(Boolean)
    duration_min = Column(Float)
    extra = Column(JSON, default=dict)
    at = Column(DateTime, default=now)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, index=True)
    name = Column(String(160))
    email = Column(String(200), default="")
    role = Column(String(80))
    password_hash = Column(String(300))
    active = Column(Boolean, default=True)
    expertise = Column(Text, default="")
    must_change = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now)
    last_login = Column(DateTime)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(80), primary_key=True)
    value = Column(Text)


class CollabRoom(Base):
    __tablename__ = "collab_rooms"
    room = Column(String(200), primary_key=True)
    state = Column(LargeBinary)
    seeded_by = Column(String(120), default="")
    updated_at = Column(DateTime, default=now, onupdate=now)


class WorkerBeat(Base):
    __tablename__ = "workers"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True)
    pool = Column(String(30))
    queues = Column(String(200))
    last_seen = Column(DateTime, default=now)
    jobs_done = Column(Integer, default=0)


_kwargs = {"future": True, "pool_pre_ping": True}
if config.IS_SQLITE:
    _kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
engine = create_engine(config.DATABASE_URL, **_kwargs)

if config.IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db():
    """Create tables once. On Postgres several services start together, so creation runs under an advisory lock."""
    if config.IS_SQLITE:
        Base.metadata.create_all(engine)
        return
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(92303001)"))
        Base.metadata.create_all(conn)


@contextmanager
def session():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


import threading as _th

from sqlalchemy import text as _text

_prog_lock = _th.RLock()


@contextmanager
def locked(program_id: int):
    """A session that holds the Program's lock for its whole transaction, so read-modify-write on records
    (assets, tickets) never loses an update when several workers and reviewers act at once.
    Postgres: transaction-scoped advisory lock. SQLite (single process): a process-wide lock."""
    if config.IS_SQLITE:
        with _prog_lock:
            with session() as s:
                yield s
    else:
        with session() as s:
            s.execute(_text("SELECT pg_advisory_xact_lock(:k)"), {"k": 920300000 + int(program_id)})
            yield s


# ---------------------------------------------------------------- record helpers
def get_record(s, program_id: int, kind: str, key: str = "main"):
    return s.execute(select(Record).where(Record.program_id == program_id, Record.kind == kind,
                                          Record.key == key)).scalar_one_or_none()


def put_record(s, program_id: int, kind: str, key: str, data: dict, status: str = "", bump: bool = False):
    r = get_record(s, program_id, kind, key)
    if r is None:
        r = Record(program_id=program_id, kind=kind, key=key, data=data, status=status, version=1)
        s.add(r)
    else:
        r.data = data
        if status:
            r.status = status
        if bump:
            r.version = (r.version or 1) + 1
    s.flush()
    return r


def list_records(s, program_id: int, kind: str):
    return list(s.execute(select(Record).where(Record.program_id == program_id, Record.kind == kind)
                          .order_by(Record.id)).scalars())


def log(s, program_id: int, actor: str, stage: str, message: str):
    s.add(Event(program_id=program_id, actor=actor, stage=stage, message=message))


def log_api(program_id, service, mode, method, path, purpose, status="200", ms=0):
    try:
        with session() as s:
            s.add(ApiCall(program_id=program_id, service=service, mode=mode, method=method, path=path[:300],
                          purpose=purpose[:200], status=str(status), ms=ms))
    except Exception:
        pass
