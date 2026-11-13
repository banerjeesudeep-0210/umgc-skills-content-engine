"""Authoring and collaboration zone, ingest side: the source library.

Anyone on the Program can upload or paste source material (UMGC syllabi and outcomes, style guides, the
credential body's exam objectives, SME references, existing course content). The ingest worker chunks it by
heading and runs Lightcast extract/trace on every chunk, so each source arrives tagged to skills.
A Magic SME decides whether a source is approved for grounding. Only approved sources join the source pack the
AI may draft from (Technical Proposal 2.3.1 Grounding); every change to the pack bumps its version, which is
stamped on every asset drafted from it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import re

from .clients import lightcast, llm
from .db import Program, get_record, list_records, locked, log, put_record, session

MAX_CHARS = 400_000


# ------------------------------------------------------------------ text extraction (runs in the web request)
def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Returns (text, kind). Supports PDF, Word, PowerPoint, Markdown, HTML, plain text and CSV."""
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return "\n\n".join(f"# Page {i + 1}\n" + (p.extract_text() or "") for i, p in enumerate(r.pages)), "pdf"
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        out = []
        for p in d.paragraphs:
            if not p.text.strip():
                continue
            st = (p.style.name or "") if p.style is not None else ""
            lvl = re.match(r"Heading (\d)", st)
            out.append(("#" * int(lvl.group(1)) + " " if lvl else "") + p.text)
        for t in d.tables:
            for row in t.rows:
                out.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(out), "docx"
    if name.endswith(".pptx"):
        from pptx import Presentation
        pr = Presentation(io.BytesIO(data))
        out = []
        for i, sl in enumerate(pr.slides, 1):
            out.append(f"# Slide {i}")
            for sh in sl.shapes:
                if sh.has_text_frame and sh.text_frame.text.strip():
                    out.append(sh.text_frame.text)
        return "\n".join(out), "pptx"
    text = data.decode("utf-8", errors="replace")
    if name.endswith((".html", ".htm")):
        text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<h([1-4])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"[ \t]+", " ", text), "html"
    return text, ("markdown" if name.endswith(".md") else "csv" if name.endswith(".csv") else "text")


def chunk(text: str, size: int = 2400) -> list[dict]:
    """Split on headings, then on size, keeping the character offset of every chunk."""
    parts, pos = [], 0
    blocks = re.split(r"(?m)^(?=#{1,4} )", text)
    for b in blocks:
        if not b.strip():
            pos += len(b)
            continue
        head = b.splitlines()[0].lstrip("# ").strip()[:120] if b.startswith("#") else "Untitled section"
        for i in range(0, len(b), size):
            seg = b[i:i + size]
            if seg.strip():
                parts.append({"chunk_id": f"C{len(parts) + 1:03d}", "heading": head, "start": pos + i, "text": seg})
        pos += len(b)
    return parts


# ------------------------------------------------------------------ the source pack the AI may use
def source_pack(s, program_id: int) -> list[dict]:
    p = s.get(Program, program_id)
    pack = [dict(x, origin="Task Order brief", status="approved") for x in p.brief.get("sources", [])]
    for r in list_records(s, program_id, "source"):
        if r.data.get("status") == "approved":
            pack.append({"id": r.key, "type": r.data.get("source_type"), "title": r.data.get("title"), "text": r.data.get("text", ""),
                         "origin": f"Uploaded by {r.data.get('uploaded_by')}", "status": "approved"})
    return pack


def pack_version(pack: list[dict]) -> str:
    raw = "".join(x.get("id", "") + x.get("text", "") for x in pack)
    return "SP-" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def add_source(program_id: int, title: str, source_type: str, text: str, filename: str, kind: str, owner: str, license_note: str, uploaded_by: str) -> str:
    from .queue import enqueue
    with locked(program_id) as s:
        n = len(list_records(s, program_id, "source")) + 1
        key = f"SRC-U{n:02d}"
        put_record(s, program_id, "source", key, {
            "source_id": key, "title": title or filename, "source_type": source_type, "filename": filename, "format": kind,
            "owner": owner, "license": license_note, "uploaded_by": uploaded_by, "text": text[:MAX_CHARS], "truncated": len(text) > MAX_CHARS,
            "chars": min(len(text), MAX_CHARS), "status": "ingesting", "chunks": [], "skills": [],
            "uploaded_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "decision": None}, status="ingesting")
        enqueue(s, program_id, "ingest", "ingest.source", f"ingest:{key}", {"source": key})
        log(s, program_id, uploaded_by, "author", f"Source {key} '{title or filename}' uploaded ({kind}, {len(text)} characters); ingest queued.")
    return key


def run_ingest(program_id: int, payload: dict):
    key = payload["source"]
    with session() as s:
        d = dict(get_record(s, program_id, "source", key).data)
        sm = get_record(s, program_id, "skills_map")
        catalog = {k.get("lightcast_skill_id"): k["skill_id"] for k in (sm.data["skills"] if sm else []) if k.get("lightcast_skill_id")}
    lc = lightcast.client(program_id)
    chunks = chunk(d["text"])
    found: dict[str, dict] = {}
    for c in chunks[:120]:
        res = lc.extract_trace(c["text"], 0.6, purpose=f"ingest {key} {c['chunk_id']}")
        c["skills"] = []
        for t in res.get("trace", []):
            for sk in (t.get("classificationData", {}) or {}).get("skills", []):
                f = found.setdefault(sk["id"], {"lightcast_skill_id": sk["id"], "name": sk["name"], "mentions": 0,
                                                "catalog_skill": catalog.get(sk["id"]), "chunks": set()})
                f["mentions"] += 1
                f["chunks"].add(c["chunk_id"])
                if sk["name"] not in c["skills"]:
                    c["skills"].append(sk["name"])
    skills = sorted(({**v, "chunks": sorted(v["chunks"])} for v in found.values()), key=lambda x: -x["mentions"])
    with locked(program_id) as s:
        r = get_record(s, program_id, "source", key)
        d = dict(r.data)
        d["chunks"] = [{k: v for k, v in c.items()} for c in chunks]
        d["skills"] = skills
        d["status"] = "ready for SME review"
        d["lightcast_mode"] = lc.mode
        put_record(s, program_id, "source", key, d, status=d["status"])
        log(s, program_id, payload.get("_worker", "worker:skills"), "author",
            f"Source {key} ingested: {len(chunks)} sections, {len(skills)} Lightcast skills ({sum(1 for x in skills if x['catalog_skill'])} in the catalog). Awaiting SME decision on grounding.")


def decide_source(program_id: int, key: str, decision: str, by: str, role: str, note: str = "") -> str:
    if not by.strip():
        return "Enter your name (top right) before deciding"
    if decision == "approve" and "SME" not in role and "Learning Experience Architect" not in role:
        return "A Magic SME or the Learning Experience Architect approves sources for grounding"
    with locked(program_id) as s:
        r = get_record(s, program_id, "source", key)
        d = dict(r.data)
        d["status"] = "approved" if decision == "approve" else "excluded"
        d["decision"] = {"by": by, "role": role, "note": note, "at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"}
        put_record(s, program_id, "source", key, d, status=d["status"])
        log(s, program_id, by, "author", f"Source {key} {d['status']} for AI grounding" + (f" ({note})" if note else "") + ".")
    return ""


# ------------------------------------------------------------------ AI assist inside the editor
def _tok(t):
    return set(w for w in re.findall(r"[a-z]{4,}", t.lower()))


def assist(program_id: int, instruction: str, heading: str, current: str, asset_key: str) -> dict:
    """Draft or rewrite one block from the approved source pack only, with citations."""
    with session() as s:
        pack = source_pack(s, program_id)
        a = get_record(s, program_id, "asset", asset_key).data
    qa_ = _tok(instruction + " " + heading)
    qt = _tok(a.get("title", ""))
    sents = []
    for src in pack:
        for sn in re.split(r"(?<=[.!?])\s+|\n+", src.get("text", "")):
            sn = sn.strip().lstrip("# ").strip()
            if 6 <= len(sn.split()) <= 60:
                sents.append((src["id"], sn, _tok(sn)))
    df = {}
    for _, _, tk in sents:
        for w in tk:
            df[w] = df.get(w, 0) + 1
    import math
    idf = lambda w: math.log((len(sents) + 1) / (df.get(w, 0) + 0.5))
    scored = sorted(((sum(2 * idf(w) for w in qa_ & tk) + sum(0.5 * idf(w) for w in qt & tk), sid, sn) for sid, sn, tk in sents), key=lambda x: -x[0])
    top = [(sc, sid, sn) for sc, sid, sn in scored[:4] if sc > 2.0]
    if not top:
        return {"text": "", "citations": [], "note": "Nothing in the approved source pack supports this. Add or approve a source first."}
    if llm.available():
        ctx = "\n\n".join(f"[{sid}] {txt}" for _, sid, txt in top)
        out = llm.complete("build.module", "You help a Magic instructional designer write one block of a UMGC noncredit Module for working adults.",
                           f"SOURCE PACK EXCERPTS\n{ctx}\n\nSCREEN HEADING: {heading}\nCURRENT TEXT: {current}\nINSTRUCTION: {instruction}\n"
                           "Write the block (60 to 140 words), citing source ids in square brackets.", program_id=program_id, max_tokens=500)
        if out:
            cites = sorted(set(re.findall(r"\[(SRC-[A-Z0-9]+)\]", out))) or [top[0][1]]
            return {"text": re.sub(r"\s*\[SRC-[A-Z0-9]+\]", "", out).strip(), "citations": cites, "note": "AI-assisted draft. Edit before saving."}
    lead = f"{heading}: " if heading else ""
    body = " ".join(sn.rstrip(".") + "." for _, _, sn in top[:3])
    return {"text": lead + body, "citations": sorted({sid for _, sid, _ in top[:3]}),
            "note": "Template assist (mock mode): the best-matching sentences from approved sources. Rewrite in your own words before saving."}
