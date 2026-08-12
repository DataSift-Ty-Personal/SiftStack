"""Knowledge base for the SMS responder.

`playbook.md` is the editable system prompt: the DataSift Call Playbook, the
4 Pillars, and the hard rules, adapted to SMS. Drop additional `.md` files in
this folder and they are appended in filename order, so a market-specific or
seasonal addendum does not require touching code.

Files are loaded once per process and the assembled prompt is cached, which
also keeps the Anthropic prompt-cache prefix byte-stable across requests.
"""
from __future__ import annotations

import functools
from pathlib import Path

HERE = Path(__file__).resolve().parent


@functools.lru_cache(maxsize=1)
def playbook() -> str:
    parts = []
    for path in sorted(HERE.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            parts.append(text)
    return "\n\n---\n\n".join(parts)
