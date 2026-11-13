"""Claude client. Live when ANTHROPIC_API_KEY is set; otherwise returns None and the caller uses its
template (mock) path. Every call is logged with a prompt identifier and version for the provenance record."""
from __future__ import annotations

import json
import re
import time

from .. import config
from ..db import log_api

PROMPTS = {
    # prompt_id: version. The version is stamped on every asset's provenance record.
    "skills.cluster": "1.2",
    "design.blueprint": "1.1",
    "build.module": "2.0",
    "build.assessment": "1.4",
    "improve.proposal": "1.0",
    "tutor.answer": "1.3",
}

GROUNDING_RULE = ("Use ONLY the source pack provided. Do not use outside knowledge for factual claims. "
                  "Cite the source item id in square brackets after each claim, for example [SRC-EXAM]. "
                  "US English. Never use em dashes; use hyphens, colons or commas.")


def available() -> bool:
    return config.LLM_LIVE


def model_name() -> str:
    return config.ANTHROPIC_MODEL if config.LLM_LIVE else "template-mock"


def complete(prompt_id: str, system: str, user: str, program_id=None, max_tokens=2500, want_json=False):
    if not config.LLM_LIVE:
        return None
    import anthropic
    cl = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    t0 = time.time()
    sys = system + "\n\n" + GROUNDING_RULE
    if want_json:
        sys += "\nReturn only valid JSON, no prose before or after."
    msg = cl.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=max_tokens, system=sys,
                             messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    log_api(program_id, "claude", "live", "POST", "/v1/messages", f"{prompt_id} v{PROMPTS.get(prompt_id, '1.0')}",
            200, int((time.time() - t0) * 1000))
    text = text.replace("\u2014", " - ")
    if want_json:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None
    return text
