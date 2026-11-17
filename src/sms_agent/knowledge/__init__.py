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


@functools.lru_cache(maxsize=4)
def playbook(program: str = "seller") -> str:
    """The assembled system prompt for one program.

    "seller" is the acquisition agent and reads this folder, unchanged. Any
    other program name is a SUBFOLDER, so the dispo agent gets its own prompt
    without the seller's rules leaking in: those two documents disagree on
    purpose about whether you may say a price.

    An empty prompt is refused rather than returned. A model handed no identity
    invents one, which is exactly how the responder once introduced itself as
    "Alex", and a silently missing playbook would do it again.
    """
    root = HERE if program == "seller" else HERE / program
    parts = []
    if root.is_dir():
        for path in sorted(root.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
    out = "\n\n---\n\n".join(parts)
    if not out:
        raise RuntimeError(
            "no playbook found for program '" + program + "' in " + str(root)
            + "; refusing to run the model with an empty system prompt")
    return out
