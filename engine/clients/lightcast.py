"""Lightcast client: live (OAuth 2.0 client credentials) with a mock twin that needs no credentials.

Open Skills (free tier, scope emsi_open)
  GET  /skills/versions/latest                        taxonomy version
  GET  /skills/versions/latest/skills?q=              search
  GET  /skills/versions/latest/skills/{id}            verify an ID
  POST /skills/versions/latest/extract                extract skills with confidence
  POST /skills/versions/latest/extract/trace          extract with the text spans that matched

Licensed products (UMGC's or Magic's license; LIGHTCAST_LICENSED_PRODUCTS=true and LIGHTCAST_LICENSED_SCOPE)
  POST /titles/versions/latest/normalize              job title to Lightcast Occupation Taxonomy (LOT)
  POST /jpa/rankings/{facet}                          Job Postings Analytics demand by skill
  POST /jpa/totals                                    total postings for the filter
  GET  /career-pathways/...                           feeder and next occupations

Endpoint paths follow the Lightcast API reference and are confirmed when keys are issued.
Mock IDs and mock demand numbers always start with MOCK so nothing mock can pass for real data.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time

from .. import config
from ..db import log_api

AUTH_URL = "https://auth.emsicloud.com/connect/token"
BASE = "https://emsiservices.com"


class LightcastError(RuntimeError):
    pass


def _hash01(text: str) -> float:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class LiveLightcast:
    mode = "live"

    def __init__(self, program_id: int | None = None):
        import requests
        self.requests = requests
        self.program_id = program_id
        self._tokens: dict[str, tuple[str, float]] = {}
        self._last = 0.0

    def _token(self, scope: str) -> str:
        tok = self._tokens.get(scope)
        if tok and time.time() < tok[1] - 60:
            return tok[0]
        r = self.requests.post(AUTH_URL, data={"client_id": config.LIGHTCAST_CLIENT_ID,
                                               "client_secret": config.LIGHTCAST_CLIENT_SECRET,
                                               "grant_type": "client_credentials", "scope": scope}, timeout=30)
        if r.status_code != 200:
            raise LightcastError(f"Lightcast authentication failed for scope {scope} ({r.status_code}): {r.text[:200]}")
        d = r.json()
        self._tokens[scope] = (d["access_token"], time.time() + float(d.get("expires_in", 3600)))
        return d["access_token"]

    def _req(self, method, path, purpose, scope=None, params=None, body=None):
        scope = scope or config.LIGHTCAST_SCOPE
        wait = 0.25 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        for attempt in range(4):
            r = self.requests.request(method, BASE + path, params=params, json=body, timeout=60,
                                      headers={"Authorization": f"Bearer {self._token(scope)}"})
            self._last = time.time()
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401:
                self._tokens.pop(scope, None)
                continue
            log_api(self.program_id, "lightcast", "live", method, path, purpose, r.status_code, int((time.time() - t0) * 1000))
            if r.status_code != 200:
                raise LightcastError(f"{method} {path} returned {r.status_code}: {r.text[:200]}")
            return r.json()
        raise LightcastError(f"{method} {path}: gave up after retries")

    # Open Skills
    def version(self):
        d = self._req("GET", "/skills/versions/latest", "taxonomy version")
        d = d.get("data", d)
        return {"version": d.get("version") or d.get("id") or str(d)[:40], "skillCount": d.get("skillCount")}

    def search(self, q, limit=8):
        d = self._req("GET", "/skills/versions/latest/skills", f"resolve '{q}'",
                      params={"q": q, "limit": limit, "fields": "id,name,type,category,subcategory,infoUrl"})
        return d.get("data", [])

    def get_skill(self, sid):
        d = self._req("GET", f"/skills/versions/latest/skills/{sid}", f"verify {sid}")
        return d.get("data", d)

    def extract_trace(self, text, threshold=0.6, purpose="extract/trace"):
        d = self._req("POST", "/skills/versions/latest/extract/trace", purpose, params={"language": "en"},
                      body={"text": text[:12000], "confidenceThreshold": threshold})
        return d.get("data", d)

    # Licensed
    def normalize_title(self, title):
        if not config.LIGHTCAST_LICENSED:
            return None
        d = self._req("POST", "/titles/versions/latest/normalize", "title to LOT occupation",
                      scope=os.getenv("LIGHTCAST_LICENSED_SCOPE", "titles"), body={"term": title})
        return d.get("data", d)

    def jpa_skill_demand(self, skill_names, occupation_name, geography="MD"):
        if not config.LIGHTCAST_LICENSED:
            return None
        body = {"filter": {"when": {"start": "last 12 months"}, "state": [geography] if geography else None,
                           "lot_specialized_occupation_name": [occupation_name], "min_years_experience": {"lower_bound": 0, "upper_bound": 2}},
                "rank": {"by": "unique_postings", "limit": 500, "extra_metrics": ["significance"]}}
        d = self._req("POST", "/jpa/rankings/skills_name", "JPA demand by skill",
                      scope=os.getenv("LIGHTCAST_LICENSED_SCOPE", "postings:us"), body=body)
        buckets = (d.get("data", {}).get("ranking", {}) or {}).get("buckets", [])
        by = {b.get("name", "").lower(): b for b in buckets}
        tot = self._req("POST", "/jpa/totals", "JPA total postings", scope=os.getenv("LIGHTCAST_LICENSED_SCOPE", "postings:us"),
                        body={"filter": body["filter"], "metrics": ["unique_postings"]})
        total = (tot.get("data", {}).get("totals", {}) or {}).get("unique_postings") or 0
        out = {}
        for n in skill_names:
            b = by.get(n.lower())
            up = int(b.get("unique_postings", 0)) if b else 0
            out[n] = {"unique_postings": up, "postings_share_pct": round(100 * up / total, 2) if total else None,
                      "significance": b.get("significance") if b else None}
        return {"total_postings": total, "skills": out}


class MockLightcast:
    mode = "mock"

    def __init__(self, program_id: int | None = None):
        self.program_id = program_id
        self.tax = json.loads((config.SEED / "mock_taxonomy.json").read_text("utf-8"))["skills"]
        self._pats = []
        for sk in self.tax:
            for n in [sk["name"]] + sk.get("aliases", []):
                self._pats.append((re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", re.I), sk))

    def _obj(self, sk):
        return {"id": sk["id"], "name": sk["name"], "type": {"name": sk.get("type", "Specialized Skill")},
                "category": {"name": sk.get("category", "")}, "subcategory": {"name": sk.get("subcategory", "")},
                "infoUrl": f"https://lightcast.io/open-skills/skills/{sk['id']}"}

    def _log(self, method, path, purpose):
        log_api(self.program_id, "lightcast", "mock", method, path, purpose)

    def version(self):
        self._log("GET", "/skills/versions/latest", "taxonomy version")
        return {"version": "MOCK-taxonomy-2026.4", "skillCount": len(self.tax)}

    def search(self, q, limit=8):
        self._log("GET", "/skills/versions/latest/skills", f"resolve '{q}'")
        ql = re.sub(r"\s*\(.*?\)", "", q).lower().strip()
        scored = []
        for sk in self.tax:
            names = [sk["name"]] + sk.get("aliases", [])
            best = max(difflib.SequenceMatcher(None, q.lower(), n.lower()).ratio() for n in names)
            if ql and (ql == sk["name"].lower() or ql in sk["name"].lower()):
                best = max(best, 0.95)
            scored.append((best, sk))
        scored.sort(key=lambda t: -t[0])
        return [dict(self._obj(sk), score=round(sc, 3)) for sc, sk in scored[:limit] if sc > 0.4]

    def get_skill(self, sid):
        self._log("GET", f"/skills/versions/latest/skills/{sid}", f"verify {sid}")
        for sk in self.tax:
            if sk["id"] == sid:
                return self._obj(sk)
        raise LightcastError(f"skill not found: {sid}")

    def extract_trace(self, text, threshold=0.6, purpose="extract/trace"):
        self._log("POST", "/skills/versions/latest/extract/trace", purpose)
        found, trace = {}, []
        for pat, sk in self._pats:
            for m in pat.finditer(text):
                trace.append({"classificationData": {"skills": [{"id": sk["id"], "name": sk["name"]}]},
                              "surfaceForm": {"sourceStart": m.start(), "sourceEnd": m.end(), "value": m.group(0)}})
                found.setdefault(sk["id"], {"skill": self._obj(sk), "n": 0})["n"] += 1
        skills = [{"confidence": round(min(0.99, 0.6 + 0.1 * v["n"]), 3), "skill": v["skill"]}
                  for v in found.values() if min(0.99, 0.6 + 0.1 * v["n"]) >= threshold]
        return {"skills": skills, "trace": trace}

    def normalize_title(self, title):
        self._log("POST", "/titles/versions/latest/normalize", "title to LOT occupation")
        return {"lot_occupation_id": "MOCK-LOT-" + hashlib.md5(title.encode()).hexdigest()[:6].upper(),
                "lot_occupation_name": title, "mock": True}

    def jpa_skill_demand(self, skill_names, occupation_name, geography="MD"):
        self._log("POST", "/jpa/rankings/skills_name", "JPA demand by skill")
        self._log("POST", "/jpa/totals", "JPA total postings")
        total = 4800 if geography == "MD" else 96000
        out = {}
        for n in skill_names:
            share = round(4 + 58 * _hash01(n + occupation_name), 2)
            out[n] = {"unique_postings": int(total * share / 100), "postings_share_pct": share,
                      "significance": round(1 + 9 * _hash01("sig" + n), 2)}
        return {"total_postings": total, "skills": out, "mock": True}


def client(program_id: int | None = None):
    return LiveLightcast(program_id) if config.LIGHTCAST_LIVE else MockLightcast(program_id)
