#!/usr/bin/env python3
"""Generate docs/AGENT-MAP.md from docs/agents.json.

docs/agents.json is the single description of the agent system. This script
renders it to markdown; tools/../docs/agent-org-chart.html renders the same
file to an interactive chart. Two renderers, one source, so the published
chart and the committed doc cannot disagree.

    python tools/agent_map.py            # write docs/AGENT-MAP.md
    python tools/agent_map.py --check    # fail if the doc is stale (CI)
    python tools/agent_map.py --audit    # report agents whose source is gone
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "agents.json"
OUT = ROOT / "docs" / "AGENT-MAP.md"

STATUS_LABEL = {
    "live": "live",
    "gated": "gated",
    "planned": "planned",
    "retired": "retired",
}


def _all_agents(doc: dict):
    for a in doc.get("exec", []):
        yield None, a
    for dept in doc["departments"]:
        if dept.get("lead"):
            yield dept, dept["lead"]
        for a in dept.get("agents", []):
            yield dept, a


def audit(doc: dict) -> int:
    """Report any agent pointing at a source file that no longer exists.

    An org chart is only worth the accuracy of its file references. This is
    the cheap check that stops it becoming a museum piece.
    """
    missing = []
    for dept, a in _all_agents(doc):
        for ref in (a.get("source") or "").split(","):
            ref = ref.strip()
            if not ref or ref.startswith(("http", "N/A")):
                continue
            if not (ROOT / ref).exists():
                missing.append(f"{a['id']:16s} {a['name']:32s} -> {ref}")
    for m in missing:
        print(f"MISSING SOURCE  {m}")
    total = sum(1 for _ in _all_agents(doc))
    print(f"{total} agents; {len(missing)} with a missing source reference")
    return 1 if missing else 0


def render(doc: dict) -> str:
    m = doc["meta"]
    L: list[str] = []
    add = L.append

    add(f"# {m['title']}")
    add("")
    add(f"*{m['subtitle']}*")
    add("")
    total = sum(len(d.get("agents", [])) + (1 if d.get("lead") else 0) for d in doc["departments"])
    add(f"{total} agents across {len(doc['departments'])} divisions. "
        f"Generated from [`docs/agents.json`](agents.json) by `tools/agent_map.py`. "
        f"Build {m.get('version')}, {m.get('generated')}.")
    add("")
    add("Do not hand-edit this file. Edit `docs/agents.json` and re-run the generator.")
    add("")

    add("## How to read this")
    add("")
    add("Every entry carries the same five things, because the fifth is the one that matters:")
    add("")
    add("- **Trigger**: what starts it.")
    add("- **Does**: the steps it actually runs.")
    add("- **Human**: where a person still has to sign off. An empty list means it runs unattended.")
    add("- **Outputs**: what comes out.")
    add("- **The trap**: the specific failure this agent exists to prevent. Most were found in "
        "production, not in review, and several of them failed silently for weeks before anyone "
        "noticed. That field is the real documentation.")
    add("")

    add("## Rollout phases")
    add("")
    for k in sorted(doc["phases"]):
        p = doc["phases"][k]
        add(f"- **{p['label']}**: {p['desc']}")
    add("")

    if doc.get("exec"):
        add("## Top of the org")
        add("")
        for a in doc["exec"]:
            add(_agent_md(a, level=3))

    add("## Divisions")
    add("")
    for dept in doc["departments"]:
        n = len(dept.get("agents", [])) + (1 if dept.get("lead") else 0)
        add(f"### {dept['name']}")
        add("")
        if dept.get("tagline"):
            add(f"*{dept['tagline']}*")
            add("")
        add(f"{n} agents.")
        add("")
        if dept.get("lead"):
            add(_agent_md(dept["lead"], level=4, lead=True))
        for a in dept.get("agents", []):
            add(_agent_md(a, level=4))

    if doc.get("infra"):
        add("## What the system depends on")
        add("")
        for g in doc["infra"]:
            add(f"**{g['group']}**")
            add("")
            for it in g["items"]:
                add(f"- {it}")
            add("")

    add("---")
    add("")
    add("Install the skills that drive these agents: see the "
        "[README](../README.md#install-the-skill-library).")
    add("")
    return "\n".join(L)


def _agent_md(a: dict, level: int, lead: bool = False) -> str:
    h = "#" * level
    tag = " (division lead)" if lead else ""
    status = STATUS_LABEL.get(a.get("status", ""), a.get("status", ""))
    L = [f"{h} {a['name']}{tag}", ""]
    L.append(f"`{a.get('source', 'n/a')}` &middot; phase {a.get('phase', '?')} &middot; {status}")
    L.append("")
    L.append(a.get("role", ""))
    L.append("")
    if a.get("trigger"):
        L.append(f"**Trigger.** {a['trigger']}")
        L.append("")
    if a.get("steps"):
        L.append("**Does.**")
        L.append("")
        for s in a["steps"]:
            L.append(f"- {s}")
        L.append("")
    if a.get("human"):
        L.append("**Human checkpoint.**")
        L.append("")
        for s in a["human"]:
            L.append(f"- {s}")
        L.append("")
    else:
        L.append("**Human checkpoint.** None. Runs unattended.")
        L.append("")
    if a.get("outputs"):
        L.append(f"**Outputs.** {', '.join(a['outputs'])}")
        L.append("")
    if a.get("tools"):
        L.append(f"**Touches.** {', '.join(a['tools'])}")
        L.append("")
    if a.get("gotcha"):
        L.append(f"> **The trap this exists to avoid.** {a['gotcha']}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if AGENT-MAP.md is stale")
    ap.add_argument("--audit", action="store_true", help="report missing source references")
    args = ap.parse_args()

    doc = json.loads(DATA.read_text(encoding="utf-8"))

    if args.audit:
        return audit(doc)

    text = render(doc)
    if args.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print("AGENT-MAP.md is stale. Run: python tools/agent_map.py", file=sys.stderr)
            return 1
        print("AGENT-MAP.md is current")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    n = sum(1 for _ in _all_agents(doc))
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}  ({n} agents, {len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
