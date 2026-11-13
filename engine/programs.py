"""Create Programs and drive demo runs."""
from __future__ import annotations

import json

from sqlalchemy import select

from . import config
from .db import Gate, Program, log, session
from .orchestrator import advance, bulk_approve, decide_gate


def sample_brief() -> dict:
    return json.loads((config.SEED / "sample_program.json").read_text("utf-8"))


def create_program(brief: dict) -> int:
    with session() as s:
        p = Program(name=brief["name"], brief=brief, stage="define", status="running")
        s.add(p)
        s.flush()
        pid = p.id
        log(s, pid, "UMGC Task Order", "define", f"Program created: {brief['name']}. Lightcast {'live' if config.LIGHTCAST_LIVE else 'mock'}, "
            f"Claude {'live' if config.LLM_LIVE else 'mock'}.")
    advance(pid)
    return pid


def new_brief(name, occupation_title, lot_name, specialized, credential, sources: list[dict]) -> dict:
    return {"name": name, "sample": False,
            "occupation": {"title": occupation_title, "lot_occupation_name": lot_name or occupation_title,
                           "lot_specialized_occupation_name": specialized or "", "geography": "Maryland, with national comparison",
                           "experience_band": "0-2 years"},
            "credential": credential, "sources": sources, "reference": {}}


def autopilot_step(program_id: int, who: str = "Demo reviewer") -> int:
    """Approve every pending gate that exit criteria allow, and return blocked validation gates for revision.
    For demos and tests only; real runs use named reviewers in the dashboard."""
    n = 0
    with session() as s:
        gates = [(g.gate_key, g.kind, g.evidence) for g in s.execute(select(Gate).where(Gate.program_id == program_id, Gate.status == "pending")).scalars()]
    for key, kind, ev in gates:
        if kind == "validation":
            blocking = [f for f in ev.get("findings", []) if f["severity"] in ("critical", "high")]
            if blocking:
                err = decide_gate(program_id, key, "return", who, "Fix: " + "; ".join(f["message"] for f in blocking))
            else:
                err = decide_gate(program_id, key, "approve", who, "Findings reviewed; documented decision", force=True)
        else:
            if key == "magic.employers":
                from .db import get_record
                with session() as s:
                    r = get_record(s, program_id, "skills_map")
                    d = dict(r.data)
                    d["employers"] = d.get("employers") or ["[Demo employer 1, Maryland]", "[Demo employer 2, Maryland]", "[Demo employer 3, UMGC nominee]"]
                    r.data = d
            err = decide_gate(program_id, key, "approve", who, "Approved (demo autopilot)")
        n += not err
    return n
