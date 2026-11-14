"""Accounts and sessions. Passwords are stored as PBKDF2-SHA256 hashes; sessions are HMAC-signed cookies whose
signing key is generated once and kept in the database, so every web instance shares it."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from sqlalchemy import select

from . import config
from .db import Setting, User, now, session
from .rbac import ADMIN

COOKIE = "sce_session"
TTL = 60 * 60 * 12


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex()
    return f"pbkdf2${salt}${h}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except Exception:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 240_000).hex(), h)


def _secret() -> bytes:
    with session() as s:
        row = s.get(Setting, "session_key")
        if row is None:
            row = Setting(key="session_key", value=secrets.token_hex(32))
            s.add(row)
        return row.value.encode()


def make_cookie(uid: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "e": int(time.time()) + TTL}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_cookie(val: str | None) -> int | None:
    if not val or "." not in val:
        return None
    body, sig = val.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest(), sig):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        return None
    return d["u"] if d.get("e", 0) > time.time() else None


def user_by_id(uid: int | None):
    if not uid:
        return None
    with session() as s:
        u = s.get(User, uid)
        if u and u.active:
            s.expunge(u)
            return u
    return None


def has_users() -> bool:
    with session() as s:
        return s.execute(select(User.id).limit(1)).first() is not None


def login(username: str, password: str):
    username = username.strip().lower()
    with session() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if u is None and not has_users_in(s) and username == "admin" and config.APP_PASSWORD and \
                hmac.compare_digest(password, config.APP_PASSWORD):
            u = User(username="admin", name="Administrator", role=ADMIN, password_hash=hash_pw(password), active=True)
            s.add(u)
            s.flush()
        if u is None or not u.active or not check_pw(password, u.password_hash or ""):
            return None
        u.last_login = now()
        s.flush()
        s.expunge(u)
        return u


def has_users_in(s) -> bool:
    return s.execute(select(User.id).limit(1)).first() is not None


def create_user(username, name, email, role, password, expertise="", must_change=True):
    username = username.strip().lower()
    with session() as s:
        if s.execute(select(User.id).where(User.username == username)).first():
            return None, "That username is taken"
        u = User(username=username, name=name.strip() or username, email=email.strip(), role=role,
                 password_hash=hash_pw(password), active=True, expertise=expertise, must_change=must_change)
        s.add(u)
        s.flush()
        return u.id, ""


def all_users(active_only=False):
    with session() as s:
        q = select(User).order_by(User.role, User.name)
        if active_only:
            q = q.where(User.active == True)  # noqa: E712
        rows = list(s.execute(q).scalars())
        for r in rows:
            s.expunge(r)
        return rows
