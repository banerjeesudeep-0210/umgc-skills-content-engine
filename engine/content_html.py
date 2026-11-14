"""Two-way conversion between structured asset content and the shared document people edit.

Module document shape:
  <h2>Screen heading</h2> <p>Text with citations like [SRC-EXAM]</p> <img alt="..."> ...
  <h3>Practice</h3><ul><li>prompt</li></ul>
  <h3>Summary</h3><p>...</p>
Performance task document shape:
  <h3>Task</h3><p>prompt</p>  <h3>Steps</h3><ol><li>..</li></ol>  <h3>Rubric</h3><ul><li>criterion | level | level</li></ul>
Knowledge checks and practice exams are edited as a grid (one row per item), not as a document.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

CITE = re.compile(r"\[(SRC-[A-Z0-9-]+)\]")


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def module_to_html(c: dict) -> str:
    out = []
    for sc in c.get("screens", []) or []:
        out.append(f"<h2>{esc(sc.get('heading'))}</h2>")
        for b in sc.get("blocks", []) or []:
            cites = "".join(f" [{x}]" for x in b.get("citations", []) if f"[{x}]" not in (b.get("text") or ""))
            out.append(f"<p>{esc(b.get('text'))}{esc(cites)}</p>")
        md = sc.get("media")
        if md:
            alt = md.get("alt") or ""
            out.append(f'<img src="{_placeholder(alt)}" alt="{esc(alt)}">')
    if c.get("practice"):
        out.append("<h3>Practice</h3><ul>" + "".join(f"<li><p>{esc(p.get('prompt'))}</p></li>" for p in c["practice"]) + "</ul>")
    if c.get("reading"):
        out.append("<h3>Offline reading</h3><ul>" + "".join(f"<li><p>{esc(r)}</p></li>" for r in c["reading"]) + "</ul>")
    if c.get("summary"):
        out.append(f"<h3>Summary</h3><p>{esc(c['summary'])}</p>")
    return "".join(out) or "<h2>New screen</h2><p></p>"


def perf_to_html(c: dict) -> str:
    out = [f"<h3>Task</h3><p>{esc(c.get('prompt'))}</p>"]
    if c.get("objectives_text"):
        out.append(f"<p>{esc(c['objectives_text'])}</p>")
    out.append("<h3>Steps</h3><ol>" + "".join(f"<li><p>{esc(s)}</p></li>" for s in c.get("steps", []) or []) + "</ol>")
    out.append("<h3>Rubric</h3><ul>" + "".join(
        f"<li><p>{esc(r.get('criterion'))} | {esc(' | '.join(r.get('levels', [])))}</p></li>" for r in c.get("rubric", []) or []) + "</ul>")
    if c.get("auto_grade"):
        out.append(f"<h3>Grading</h3><p>{esc(c['auto_grade'])}</p>")
    return "".join(out)


def _placeholder(alt: str) -> str:
    import base64
    t = re.sub(r"[<&>\"]", "", alt)[:60]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="200"><rect width="640" height="200" fill="#EFE8FF"/>'
           f'<text x="20" y="105" font-family="Poppins,sans-serif" font-size="18" fill="#3E0AB7">Media: {t}</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


class _Walker(HTMLParser):
    """Flattens the editor HTML into a list of (kind, text, attrs) blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.stack, self.buf, self.list_kind = [], [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "p", "li", "blockquote"):
            self._flush()
            self.stack.append(tag)
        if tag in ("ul", "ol"):
            self._flush()
            self.list_kind = tag
        if tag == "img":
            self._flush()
            self.blocks.append(("img", "", {"alt": a.get("alt", ""), "src": a.get("src", "")}))
        if tag == "br":
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "p", "li", "blockquote"):
            kind = tag
            if tag == "p" and "li" in self.stack[:-1]:
                kind = "li"
            self._flush(kind if tag != "li" else None)
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
        if tag in ("ul", "ol"):
            self.list_kind = None

    def handle_data(self, d):
        self.buf.append(d)

    def _flush(self, kind=None):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if text and kind:
            k = "li" if kind == "li" or (kind == "p" and "li" in self.stack) else kind
            self.blocks.append((k, text, {"list": self.list_kind}))


def _blocks(h: str):
    w = _Walker()
    w.feed(h or "")
    w._flush("p")
    return w.blocks


def html_to_module(h: str, base: dict | None = None) -> dict:
    base = dict(base or {})
    screens, practice, summary, mode, reading = [], [], "", "screen", []
    cur = None
    for kind, text, attrs in _blocks(h):
        if kind == "h2":
            cur = {"heading": text, "blocks": [], "media": None}
            screens.append(cur)
            mode = "screen"
            continue
        if kind == "h3":
            low = text.lower()
            mode = "practice" if low.startswith("practice") else ("summary" if low.startswith("summary") else
                                                                  ("reading" if low.startswith(("offline", "reading")) else "screen"))
            if mode == "screen":
                if cur is None:
                    cur = {"heading": text, "blocks": [], "media": None}
                    screens.append(cur)
                else:
                    cur["blocks"].append({"text": text, "citations": [], "subheading": True})
            continue
        if mode == "practice" and kind in ("li", "p"):
            practice.append({"prompt": text, "skill_id": "", "feedback": ""})
            continue
        if mode == "reading" and kind in ("li", "p"):
            reading.append(text)
            continue
        if mode == "summary" and kind in ("p", "li"):
            summary = (summary + " " + text).strip()
            continue
        if cur is None:
            cur = {"heading": "Introduction", "blocks": [], "media": None}
            screens.append(cur)
        if kind == "img":
            cur["media"] = {"type": "diagram", "alt": attrs.get("alt", "")}
        else:
            cites = CITE.findall(text)
            clean = re.sub(r"\s+", " ", CITE.sub("", text)).strip()
            cur["blocks"].append({"text": clean, "citations": cites, "list": attrs.get("list")})
    base.update({"screens": screens, "practice": practice, "summary": summary, "reading": reading})
    return base


def html_to_perf(h: str, base: dict | None = None) -> dict:
    base = dict(base or {})
    section, prompt, steps, rubric, grading, extra = "task", [], [], [], "", []
    for kind, text, attrs in _blocks(h):
        if kind in ("h2", "h3"):
            low = text.lower()
            section = "steps" if "step" in low else "rubric" if "rubric" in low else "grading" if "grad" in low else "task"
            continue
        if section == "task":
            (prompt if not prompt else extra).append(text)
        elif section == "steps":
            steps.append(text)
        elif section == "rubric":
            parts = [x.strip() for x in text.split("|")]
            sid = re.search(r"\b(S\d+\.\d+)\b", parts[0])
            rubric.append({"criterion": parts[0], "levels": parts[1:] or ["Not demonstrated", "Developing", "Meets", "Exceeds"],
                           "skill_id": sid.group(1) if sid else ""})
        else:
            grading = (grading + " " + text).strip()
    base.update({"prompt": " ".join(prompt), "objectives_text": " ".join(extra) or base.get("objectives_text", ""), "steps": steps,
                 "rubric": rubric, "auto_grade": grading or base.get("auto_grade", ""), "kind": "performance"})
    return base


ITEM_COLS = ["item_id", "skill_id", "objective_id", "stem", "A", "B", "C", "D", "key", "rationale"]


def items_to_rows(c: dict) -> list[dict]:
    rows = []
    for it in c.get("items", []) or []:
        r = {"_id": it.get("item_id"), "item_id": it.get("item_id"), "skill_id": it.get("skill_id", ""), "objective_id": it.get("objective_id", ""),
             "stem": it.get("stem", ""), "key": it.get("key", "A")}
        why = []
        for o in it.get("options", []):
            r[o["id"]] = o.get("text", "")
            if o.get("rationale"):
                why.append(f"{o['id']}: {o['rationale']}")
        r["rationale"] = " || ".join(why)
        rows.append(r)
    return rows


def rows_to_items(rows: list[dict], base: dict | None, ref_id: str) -> dict:
    base = dict(base or {})
    items = []
    for i, r in enumerate(rows):
        if not str(r.get("stem", "")).strip():
            continue
        why = {}
        for part in str(r.get("rationale", "")).split("||"):
            m = re.match(r"\s*([ABCD])\s*:\s*(.*)", part)
            if m:
                why[m.group(1)] = m.group(2).strip()
        key = str(r.get("key") or "A").strip().upper()[:1] or "A"
        opts = [{"id": L, "text": str(r.get(L, "")).strip(), "correct": L == key, "rationale": why.get(L, "")} for L in "ABCD" if str(r.get(L, "")).strip()]
        items.append({"item_id": r.get("item_id") or f"{ref_id}-I{i + 1:02d}", "stem": str(r["stem"]).strip(), "options": opts, "key": key,
                      "skill_id": str(r.get("skill_id", "")).strip(), "objective_id": str(r.get("objective_id", "")).strip()})
    base.update({"items": items, "kind": "items"})
    return base
