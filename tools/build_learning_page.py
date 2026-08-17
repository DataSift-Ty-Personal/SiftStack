#!/usr/bin/env python3
"""Render docs/agents.json to the learn.datasift.ai org chart page.

Third renderer over the same JSON as tools/agent_map.py (markdown) and
tools/build_agent_page.py (the standalone page). This one targets the
Datasift-learning-pages repo, which deploys to learn.datasift.ai through AWS
Amplify, and follows that project's contract: single file, all CSS and JS
inline, ds- prefixed classes, one IIFE with 'use strict', DM Sans only, brand
palette only, no em or en dashes, reduced-motion and print safe.

    python tools/build_learning_page.py --out <learning-pages>/pages/agent-org-chart.html

Layout is computed HERE, in Python, rather than in the browser: node positions
and connector geometry are baked into the markup, so the chart cannot reflow
differently on someone else's machine and the page has no layout work to do on
load. The browser only handles zoom, pan, filtering and the detail drawer.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "agents.json"

REPO = "DataSift-Ty-Personal/SiftStack"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
INSTALL = f"curl -fsSL {RAW}/install.py | python3 -"
SLUG = "agent-org-chart"
SITE = "https://learn.datasift.ai"

# ---- layout constants (CSS px in chart space) ----------------------------
COL_W, COL_GAP = 214, 26
COL_PITCH = COL_W + COL_GAP
NODE_H, NODE_GAP = 34, 7
HEAD_Y, HEAD_H = 196, 50
FIRST_NODE_Y = HEAD_Y + HEAD_H + 16
EXEC_Y, EXEC_H, EXEC_W = 24, 40, 210
CORE_Y, CORE_H, CORE_W = 96, 38, 190
BUS_Y = 166

# One hue per division, all drawn from the brand ramp. These are the only
# per-division colors on the page and they exist to make a column scannable
# at zoomed-out sizes, where text is unreadable.
DIV_COLOR = {
    "acq": "#62D4FF", "enrich": "#70A0FF", "deal": "#316AFF",
    "dispo": "#99BEFF", "outreach": "#FFBD2E", "crm": "#33FFD6",
    "market": "#D6E1FF", "coach": "#5CBFA2", "soi": "#F4A980",
}
STATUS_TEXT = {"live": "Live", "gated": "Gated", "planned": "Planned", "retired": "Retired"}


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def agents_of(d: dict) -> list:
    out = []
    if d.get("lead"):
        out.append((d["lead"], True))
    for a in d.get("agents", []):
        out.append((a, False))
    return out


def detail_payload(a: dict, dept: dict | None) -> dict:
    return {
        "id": a["id"],
        "name": a.get("name", ""),
        "dept": (dept or {}).get("name", "Top of the org"),
        "role": a.get("role", ""),
        "trigger": a.get("trigger", ""),
        "steps": a.get("steps", []),
        "human": a.get("human", []),
        "outputs": a.get("outputs", []),
        "tools": a.get("tools", []),
        "gotcha": a.get("gotcha", ""),
        "source": a.get("source", ""),
        "status": a.get("status", "live"),
        "phase": a.get("phase", 1),
    }


def _skill_count() -> int:
    """Current, non-superseded packages, read from the manifest."""
    mf = ROOT / "skills" / "manifest.json"
    if mf.is_file():
        return json.loads(mf.read_text(encoding="utf-8"))["counts"]["current"]
    return 0


def build(doc: dict) -> str:
    depts = doc["departments"]
    execs = doc.get("exec", [])
    n_cols = len(depts)
    chart_w = n_cols * COL_W + (n_cols - 1) * COL_GAP
    max_agents = max(len(agents_of(d)) for d in depts)
    chart_h = FIRST_NODE_Y + max_agents * (NODE_H + NODE_GAP) + 46

    nodes: list[str] = []
    details: dict[str, dict] = {}
    lines: list[str] = []
    cx = chart_w / 2

    # ---- exec row ----
    ex_total = len(execs) * EXEC_W + (len(execs) - 1) * 30
    ex_x = cx - ex_total / 2
    for i, a in enumerate(execs):
        x = ex_x + i * (EXEC_W + 30)
        details[a["id"]] = detail_payload(a, None)
        nodes.append(
            f'<button class="ds-node ds-node-exec" data-id="{e(a["id"])}" '
            f'style="left:{x:.0f}px;top:{EXEC_Y}px;width:{EXEC_W}px;height:{EXEC_H}px">'
            f'<span class="ds-node-dot" data-s="{e(a.get("status","live"))}"></span>'
            f'<span class="ds-node-name">{e(a.get("name"))}</span></button>')
        lines.append(f'<path d="M {x + EXEC_W/2:.0f} {EXEC_Y + EXEC_H} '
                     f'V {CORE_Y - 10} H {cx:.0f} V {CORE_Y}" />')

    # ---- core hub ----
    core_x = cx - CORE_W / 2
    nodes.append(
        f'<div class="ds-node ds-node-core" '
        f'style="left:{core_x:.0f}px;top:{CORE_Y}px;width:{CORE_W}px;height:{CORE_H}px">'
        f'<span class="ds-node-name">SiftStack core</span></div>')

    # ---- bus from core down to each division ----
    first_c = COL_W / 2
    last_c = (n_cols - 1) * COL_PITCH + COL_W / 2
    lines.append(f'<path d="M {cx:.0f} {CORE_Y + CORE_H} V {BUS_Y}" />')
    lines.append(f'<path d="M {first_c:.0f} {BUS_Y} H {last_c:.0f}" />')

    # ---- division columns ----
    for ci, d in enumerate(depts):
        x = ci * COL_PITCH
        col = DIV_COLOR.get(d["id"], "#316AFF")
        items = agents_of(d)
        lines.append(f'<path d="M {x + COL_W/2:.0f} {BUS_Y} V {HEAD_Y}" />')

        nodes.append(
            f'<div class="ds-col-head" data-dept="{e(d["id"])}" '
            f'style="left:{x}px;top:{HEAD_Y}px;width:{COL_W}px;height:{HEAD_H}px;'
            f'--ds-div:{col}">'
            f'<span class="ds-col-name">{e(d["name"])}</span>'
            f'<span class="ds-col-count">{len(items)} agents</span></div>')

        for ai, (a, is_lead) in enumerate(items):
            y = FIRST_NODE_Y + ai * (NODE_H + NODE_GAP)
            details[a["id"]] = detail_payload(a, d)
            search = " ".join([a.get("name", ""), a.get("role", ""), a.get("source", ""),
                               a.get("gotcha", ""), " ".join(a.get("tools", []))]).lower()
            gate = " ds-has-gate" if a.get("human") else ""
            nodes.append(
                f'<button class="ds-node ds-node-agent{gate}{" ds-node-lead" if is_lead else ""}" '
                f'data-id="{e(a["id"])}" data-dept="{e(d["id"])}" '
                f'data-status="{e(a.get("status","live"))}" '
                f'data-human="{"yes" if a.get("human") else "no"}" '
                f'data-search="{e(search)}" '
                f'style="left:{x}px;top:{y}px;width:{COL_W}px;height:{NODE_H}px;--ds-div:{col}">'
                f'<span class="ds-node-dot" data-s="{e(a.get("status","live"))}"></span>'
                f'<span class="ds-node-name">{e(a.get("name"))}</span>'
                + ('<span class="ds-node-gate" aria-hidden="true"></span>' if a.get("human") else "")
                + '</button>')

    # ---- toolbar chips ----
    chips = "".join(
        f'<button class="ds-chip" data-filter="dept" data-value="{e(d["id"])}" '
        f'style="--ds-div:{DIV_COLOR.get(d["id"], "#316AFF")}">'
        f'<span class="ds-chip-dot" aria-hidden="true"></span>{e(d["name"])}</button>'
        for d in depts)

    # ---- mobile list fallback ----
    # A pan-and-zoom canvas is unusable on a phone, so narrow viewports get the
    # same data as a plain list rather than a shrunken chart nobody can read.
    mob = []
    for d in depts:
        rows = "".join(
            f'<button class="ds-m-row" data-id="{e(a["id"])}">'
            f'<span class="ds-node-dot" data-s="{e(a.get("status","live"))}"></span>'
            f'<span class="ds-m-name">{e(a.get("name"))}</span>'
            f'<span class="ds-m-role">{e(a.get("role"))}</span></button>'
            for a, _ in agents_of(d))
        mob.append(
            f'<section class="ds-m-dept" style="--ds-div:{DIV_COLOR.get(d["id"], "#316AFF")}">'
            f'<h3>{e(d["name"])}</h3><p class="ds-m-tag">{e(d.get("tagline"))}</p>{rows}</section>')

    stats = {
        "agents": sum(len(agents_of(d)) for d in depts),
        "divisions": n_cols,
        "live": sum(1 for d in depts for a, _ in agents_of(d) if a.get("status") == "live"),
        "gates": sum(1 for d in depts for a, _ in agents_of(d) if a.get("human")),
    }

    return Template(PAGE).substitute(
        n_skills=_skill_count(),
        slug=SLUG, site=SITE, install=e(INSTALL), repo=REPO,
        version=e(doc["meta"].get("version")), generated=e(doc["meta"].get("generated")),
        chart_w=f"{chart_w:.0f}", chart_h=f"{chart_h:.0f}",
        nodes="".join(nodes), lines="".join(lines), chips=chips,
        mobile="".join(mob),
        details=json.dumps(details, separators=(",", ":")),
        n_agents=stats["agents"], n_div=stats["divisions"],
        n_live=stats["live"], n_gates=stats["gates"],
    )


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <!-- ds:engagement -->
  <meta name="ds:guide_slug" content="$slug">
  <meta name="ds:guide_category" content="other">
  <script src="/assets/ds-engagement.js" defer></script>
  <script src="/assets/gtm-boot.js" defer></script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SiftStack Agent Org Chart | DataSift Interactive Learning</title>
<meta name="description" content="Every agent in the SiftStack AI operation: what triggers it, what it touches, where a human still signs off, and the trap it exists to avoid. Zoom, filter and search the full map.">
<!-- OG META START -->
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="DataSift Interactive Learning">
<meta property="og:title" content="SiftStack Agent Org Chart | DataSift Interactive Learning">
<meta property="og:description" content="Every agent in the SiftStack AI operation: what triggers it, what it touches, where a human still signs off, and the trap it exists to avoid.">
<meta property="og:url" content="$site/$slug">
<meta property="og:image" content="$site/og-images/$slug.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="SiftStack Agent Org Chart | DataSift Interactive Learning">
<meta name="twitter:description" content="Every agent in the SiftStack AI operation: what triggers it, what it touches, and where a human still signs off.">
<meta name="twitter:image" content="$site/og-images/$slug.png">
<!-- OG META END -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,100..1000;1,9..40,100..1000&display=swap" rel="stylesheet">
<style>
/* PAGE IDENTITY: agent-org-chart
   Mood: Control room. A dark canvas you fly over, not a document you scroll.
   Hero: none. The chart IS the page, so the toolbar sits at the top and the
         canvas fills the viewport.
   Signature motion: none beyond the drawer slide. Movement here is the user's
         own pan and zoom, so decorative motion would fight it. */

:root {
  --brand-navy: #131C34;
  --brand-navy-app: #181F48;
  --brand-primary: #316AFF;
  --brand-primary-dark: #1959D8;
  --brand-sky: #62D4FF;
  --brand-pale: #F4F3F0;
  --color-error: #EE2C54;
  --color-muted: #6A707E;
  --color-border: #D0D5DD;
  --ds-transition: 0.28s ease;
  --brand-accent: #62D4FF;
  --neutral-900: #0a0b3d;
  --neutral-400: #9ca3af;
  --white: #FCFBF9;
  --status-live: #1B9E5A;
  --status-gated: #FFBD2E;
  --status-off: #808798;
  --xs: 0.5rem; --sm: 0.707rem; --rg: 1rem; --md: 1.414rem; --lg: 1.999rem;
  --font-mono: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  font-family: "DM Sans", sans-serif;
}

*, *::before, *::after { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; background: var(--brand-navy); color: var(--white);
  font-optical-sizing: auto; font-size: 16px; line-height: 1.5;
  overflow: hidden;
}
h1, h2, h3, h4 { text-wrap: balance; }
p { text-wrap: pretty; }
.ds-label { font-family: var(--font-mono); }
:focus-visible { outline: 2px solid var(--brand-sky); outline-offset: 2px; }

.ds-app { display: flex; flex-direction: column; height: 100dvh; }

/* ===== TOOLBAR ===== */
/* Two explicit rows, both wrapping. An overflow-x toolbar would hide half the
   divisions behind a scrollbar nobody sees, and the layout law here is that
   navs wrap rather than scroll sideways. */
.ds-bar {
  flex: none; display: flex; flex-direction: column; gap: 9px;
  padding: 10px 14px; background: var(--brand-navy-app);
  border-bottom: 1px solid rgba(255,255,255,0.10);
}
.ds-bar-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ds-chips { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ds-brand { display: flex; align-items: baseline; gap: 8px; flex: none; }
.ds-brand b { font-size: 15px; font-weight: 700; letter-spacing: -0.01em; }
.ds-brand span { font-family: var(--font-mono); font-size: 11px; color: rgba(255,255,255,0.55); }

.ds-search {
  flex: none; width: 210px; min-width: 150px; font-family: inherit; font-size: 13px;
  color: var(--white); background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.16); border-radius: 6px; padding: 7px 10px;
}
.ds-search::placeholder { color: rgba(255,255,255,0.45); }
.ds-search:focus-visible { border-color: var(--brand-sky); }

.ds-chip {
  flex: none; display: inline-flex; align-items: center; gap: 6px;
  font-family: inherit; font-size: 12.5px; font-weight: 600; white-space: nowrap;
  color: rgba(255,255,255,0.72); background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.14); border-radius: 6px;
  padding: 6px 11px; cursor: pointer; min-height: 32px;
  transition: background var(--ds-transition), color var(--ds-transition), border-color var(--ds-transition);
}
.ds-chip:hover { color: var(--white); border-color: rgba(255,255,255,0.34); }
.ds-chip:active { transform: translateY(1px); }
.ds-chip[aria-pressed="true"] {
  background: rgba(49,106,255,0.24); border-color: var(--brand-primary); color: var(--white);
}
.ds-chip-dot { width: 7px; height: 7px; border-radius: 2px; background: var(--ds-div, var(--brand-primary)); }

.ds-zoom { flex: none; display: flex; align-items: center; gap: 4px; margin-left: auto; }
.ds-zoom button {
  font-family: inherit; font-size: 13px; font-weight: 600; color: rgba(255,255,255,0.8);
  background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.16);
  border-radius: 6px; min-width: 34px; min-height: 32px; padding: 0 9px; cursor: pointer;
  transition: background var(--ds-transition), color var(--ds-transition);
}
.ds-zoom button:hover { background: rgba(255,255,255,0.13); color: var(--white); }
.ds-zoom button:active { transform: translateY(1px); }
.ds-zoom output {
  font-family: var(--font-mono); font-size: 12px; color: rgba(255,255,255,0.6);
  min-width: 44px; text-align: center; font-variant-numeric: tabular-nums;
}

/* ===== CANVAS ===== */
.ds-stage {
  position: relative; flex: 1 1 auto; overflow: hidden; cursor: grab;
  background:
    radial-gradient(circle at 1px 1px, rgba(255,255,255,0.055) 1px, transparent 0) 0 0 / 26px 26px,
    var(--brand-navy);
  touch-action: none;
}
.ds-stage.ds-grabbing { cursor: grabbing; }
.ds-canvas { position: absolute; top: 0; left: 0; transform-origin: 0 0; will-change: transform; }
.ds-wires { position: absolute; top: 0; left: 0; pointer-events: none; overflow: visible; }
.ds-wires path { fill: none; stroke: rgba(255,255,255,0.19); stroke-width: 1.5; }

.ds-col-head {
  position: absolute; display: flex; flex-direction: column; justify-content: center;
  gap: 2px; padding: 0 12px; border-radius: 8px;
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.12);
  border-top: 3px solid var(--ds-div, var(--brand-primary));
}
.ds-col-name { font-size: 13.5px; font-weight: 700; letter-spacing: -0.01em; }
.ds-col-count { font-family: var(--font-mono); font-size: 10.5px; color: rgba(255,255,255,0.5); }

.ds-node {
  position: absolute; display: flex; align-items: center; gap: 8px;
  padding: 0 10px; text-align: left; font-family: inherit;
  border-radius: 6px; cursor: pointer; color: var(--white);
  background: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.13);
  transition: background var(--ds-transition), border-color var(--ds-transition), opacity var(--ds-transition);
}
.ds-node:hover { background: rgba(255,255,255,0.13); border-color: rgba(255,255,255,0.34); }
.ds-node:active { transform: translateY(1px); }
.ds-node-agent { border-left: 3px solid var(--ds-div, var(--brand-primary)); }
.ds-node-lead { background: rgba(255,255,255,0.10); font-weight: 600; }
.ds-node-exec { background: rgba(49,106,255,0.20); border-color: rgba(49,106,255,0.55); justify-content: center; }
.ds-node-core {
  background: var(--brand-primary); border-color: var(--brand-primary);
  justify-content: center; font-weight: 700; cursor: default;
}
.ds-node-name {
  font-size: 12.5px; line-height: 1.2; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; flex: 1 1 auto;
}
.ds-node-exec .ds-node-name, .ds-node-core .ds-node-name { flex: 0 0 auto; }
.ds-node-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--status-off); }
.ds-node-dot[data-s="live"] { background: var(--status-live); }
.ds-node-dot[data-s="gated"] { background: var(--status-gated); }
.ds-node-gate {
  flex: none; width: 5px; height: 5px; border-radius: 50%;
  background: var(--brand-sky); box-shadow: 0 0 0 2px rgba(98,212,255,0.22);
}
.ds-node.ds-dim { opacity: 0.16; pointer-events: none; }
.ds-node.ds-hit { border-color: var(--brand-sky); background: rgba(98,212,255,0.18); }
.ds-node.ds-open { border-color: var(--brand-sky); background: rgba(98,212,255,0.24); }

/* ===== DRAWER ===== */
.ds-drawer {
  position: absolute; top: 0; right: 0; bottom: 0; width: min(430px, 92vw);
  background: var(--brand-navy-app); border-left: 1px solid rgba(255,255,255,0.13);
  overflow-y: auto; padding: 20px 22px 40px;
  transform: translateX(100%); transition: transform var(--ds-transition);
  z-index: 5;
}
.ds-drawer.ds-open { transform: translateX(0); }
.ds-drawer-top { display: flex; align-items: flex-start; gap: 12px; }
.ds-drawer-top .ds-label { font-size: 11px; color: var(--brand-sky); letter-spacing: 0.02em; }
.ds-close {
  margin-left: auto; flex: none; font-size: 20px; line-height: 1; color: rgba(255,255,255,0.6);
  background: none; border: 0; cursor: pointer; padding: 4px 8px; border-radius: 4px;
  min-height: 32px; min-width: 32px;
}
.ds-close:hover { color: var(--white); background: rgba(255,255,255,0.08); }
.ds-drawer h2 { margin: 6px 0 8px; font-size: 22px; font-weight: 700; letter-spacing: -0.02em; color: #fff; }
.ds-drawer h3 { margin: 20px 0 6px; font-family: var(--font-mono); font-size: 11px;
  font-weight: 700; color: rgba(255,255,255,0.52); }
.ds-drawer p { margin: 0 0 10px; font-size: 14.5px; color: rgba(255,255,255,0.82); }
.ds-drawer ol, .ds-drawer ul { margin: 0; padding-left: 18px; font-size: 14px; color: rgba(255,255,255,0.82); }
.ds-drawer li { margin: 4px 0; }
.ds-meta { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 4px; }
.ds-pill {
  font-family: var(--font-mono); font-size: 10.5px; font-weight: 700; padding: 3px 8px;
  border-radius: 4px; border: 1px solid rgba(255,255,255,0.18); color: rgba(255,255,255,0.75);
}
.ds-pill[data-s="live"] { color: var(--status-live); border-color: rgba(27,158,90,0.5); }
.ds-pill[data-s="gated"] { color: var(--status-gated); border-color: rgba(255,189,46,0.5); }
.ds-pill-gate { color: var(--brand-sky); border-color: rgba(98,212,255,0.5); }
.ds-src {
  display: block; font-family: var(--font-mono); font-size: 11.5px;
  color: rgba(255,255,255,0.5); margin-top: 10px; overflow-wrap: anywhere;
}
.ds-tools { display: flex; gap: 5px; flex-wrap: wrap; }
.ds-tool {
  font-family: var(--font-mono); font-size: 11px; padding: 3px 7px; border-radius: 4px;
  background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.13);
  color: rgba(255,255,255,0.8);
}
/* The trap reads as a bold-lead paragraph rather than a boxed aside, per the
   visual contract. It is the most valuable line in the record and it does not
   need a tinted panel to prove it. */
.ds-trap { margin-top: 20px; padding-top: 14px; border-top: 1px solid rgba(255,255,255,0.13); }
.ds-trap p { font-size: 14px; color: rgba(255,255,255,0.86); }
.ds-trap b { color: #fff; }

/* ===== INSTALL BAR ===== */
.ds-install {
  flex: none; display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 14px; background: var(--brand-navy-app);
  border-top: 1px solid rgba(255,255,255,0.10);
}
.ds-install p { margin: 0; font-size: 13px; color: rgba(255,255,255,0.72); }
.ds-install p b { color: #fff; }
.ds-cmd { display: flex; align-items: stretch; margin-left: auto; max-width: 100%;
  border: 1px solid rgba(255,255,255,0.18); border-radius: 6px; overflow: hidden; }
.ds-cmd code {
  font-family: var(--font-mono); font-size: 12px; padding: 8px 10px; color: rgba(255,255,255,0.9);
  background: rgba(255,255,255,0.05); white-space: nowrap; overflow-x: auto; scrollbar-width: none;
}
.ds-cmd code::-webkit-scrollbar { display: none; }
.ds-cmd button {
  flex: none; font-family: inherit; font-size: 12.5px; font-weight: 600; color: var(--white);
  background: var(--brand-primary); border: 0; padding: 0 14px; cursor: pointer; min-height: 34px;
}
.ds-cmd button:hover { background: var(--brand-primary-dark); }
.ds-cmd button:active { transform: translateY(1px); }

/* ===== LEGEND ===== */
.ds-legend {
  position: absolute; left: 14px; bottom: 14px; display: flex; gap: 14px; flex-wrap: wrap;
  padding: 9px 12px; border-radius: 8px; background: rgba(19,28,52,0.9);
  border: 1px solid rgba(255,255,255,0.13); font-size: 11.5px; color: rgba(255,255,255,0.66);
  pointer-events: none;
}
.ds-legend span { display: flex; align-items: center; gap: 6px; }

.ds-hint {
  position: absolute; right: 14px; bottom: 14px; font-family: var(--font-mono);
  font-size: 11px; color: rgba(255,255,255,0.42); pointer-events: none;
}

/* ===== MOBILE LIST ===== */
.ds-mobile { display: none; }
@media (max-width: 860px) {
  body { overflow: auto; }
  .ds-app { height: auto; }
  .ds-stage, .ds-zoom, .ds-legend, .ds-hint { display: none; }
  .ds-mobile { display: block; padding: 18px 16px 60px; }
  .ds-mobile > p { margin: 0 0 var(--md); font-size: 14.5px; color: rgba(255,255,255,0.72); }
  .ds-m-dept { margin-bottom: var(--lg); }
  .ds-m-dept h3 {
    margin: 0 0 2px; font-size: 16px; font-weight: 700; color: #fff;
    padding-left: 10px; border-left: 3px solid var(--ds-div, var(--brand-primary));
  }
  .ds-m-tag { margin: 0 0 10px 13px; font-size: 13px; color: rgba(255,255,255,0.6); }
  .ds-m-row {
    display: grid; grid-template-columns: auto 1fr; gap: 3px 9px; width: 100%;
    text-align: left; font-family: inherit; color: var(--white); cursor: pointer;
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 6px; padding: 11px 12px; margin-bottom: 7px; min-height: 44px;
  }
  .ds-m-row:active { background: rgba(255,255,255,0.12); }
  .ds-m-name { font-size: 14.5px; font-weight: 600; }
  .ds-m-role { grid-column: 2; font-size: 13px; color: rgba(255,255,255,0.66); }
  .ds-drawer { position: fixed; width: 100%; }
  .ds-install { flex-direction: column; align-items: stretch; }
  .ds-cmd { margin-left: 0; }

}

@media (max-width: 768px) {
  .ds-bar { gap: 8px; padding: 9px 12px; }
  .ds-brand span { display: none; }
  .ds-search { width: 160px; }
  .ds-mobile { padding: 16px 13px 52px; }
  .ds-drawer { padding: 18px 16px 40px; }
  .ds-drawer h2 { font-size: 20px; }
  .ds-install { padding: 12px; gap: 9px; }
  .ds-install p { font-size: 13.5px; }
  .ds-cmd code { font-size: 11.5px; }
}

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
  .ds-drawer { transition: none !important; }
}
@media print {
  body { overflow: visible; background: #fff; color: #000; }
  .ds-bar, .ds-stage, .ds-install, .ds-drawer, .ds-legend, .ds-hint { display: none !important; }
  .ds-mobile { display: block !important; }
  .ds-mobile h3, .ds-m-name, .ds-m-role, .ds-m-tag { color: #000 !important; }
  .ds-m-row { border: 1px solid #999; }
}
</style>
</head>
<body>
<div class="ds-app">

  <div class="ds-bar">
    <div class="ds-bar-row">
      <div class="ds-brand"><b>SiftStack</b><span>Agent org chart</span></div>
      <input id="ds-q" class="ds-search" type="search" placeholder="Search agents, files, traps" aria-label="Search agents">
      <div class="ds-zoom">
        <button type="button" id="ds-out" aria-label="Zoom out">&minus;</button>
        <output id="ds-pct" aria-live="off">100%</output>
        <button type="button" id="ds-in" aria-label="Zoom in">+</button>
        <button type="button" id="ds-fit">Fit</button>
      </div>
    </div>
    <div class="ds-chips">
      <button class="ds-chip" data-filter="all" data-value="all" aria-pressed="true">All divisions</button>
      $chips
      <button class="ds-chip" data-filter="human" data-value="yes">Human gate</button>
    </div>
  </div>

  <div class="ds-stage" id="ds-stage">
    <div class="ds-canvas" id="ds-canvas" style="width:${chart_w}px;height:${chart_h}px">
      <svg class="ds-wires" width="$chart_w" height="$chart_h" viewBox="0 0 $chart_w $chart_h" aria-hidden="true">$lines</svg>
      $nodes
    </div>
    <div class="ds-legend">
      <span><i class="ds-node-dot" data-s="live"></i>Live</span>
      <span><i class="ds-node-dot" data-s="gated"></i>Gated</span>
      <span><i class="ds-node-dot" data-s="planned"></i>Planned or retired</span>
      <span><i class="ds-node-gate"></i>Human signs off</span>
    </div>
    <p class="ds-hint">Drag to pan, scroll to zoom, click any agent</p>
  </div>

  <div class="ds-mobile">
    <p><b>$n_agents agents</b> across $n_div divisions. $n_live live in production, $n_gates with a human gate. Tap any agent for what triggers it and the trap it exists to avoid.</p>
    $mobile
  </div>

  <div class="ds-install">
    <p><b>$n_agents agents, $n_div divisions.</b> The $n_skills Claude skills that drive them install in one command.</p>
    <div class="ds-cmd">
      <code id="ds-cmd">$install</code>
      <button type="button" id="ds-copy">Copy</button>
    </div>
  </div>

  <aside class="ds-drawer" id="ds-drawer" role="dialog" aria-modal="false" aria-labelledby="ds-d-name" hidden>
    <div class="ds-drawer-top">
      <span class="ds-label" id="ds-d-dept"></span>
      <button type="button" class="ds-close" id="ds-d-close" aria-label="Close details">&times;</button>
    </div>
    <h2 id="ds-d-name"></h2>
    <div class="ds-meta" id="ds-d-meta"></div>
    <p id="ds-d-role"></p>
    <div id="ds-d-body"></div>
    <code class="ds-src" id="ds-d-src"></code>
  </aside>

</div>

<script>
(function () {
  'use strict';

  var DETAIL = $details;

  // ===== STORAGE =====
  var Storage = (function () {
    var KEY = 'datasift_orgchart_';
    function get(k, dflt) {
      try { var v = localStorage.getItem(KEY + k); return v === null ? dflt : JSON.parse(v); }
      catch (err) { return dflt; }
    }
    function set(k, v) {
      try { localStorage.setItem(KEY + k, JSON.stringify(v)); } catch (err) { /* private mode */ }
    }
    return { get: get, set: set };
  })();

  // ===== VIEWPORT (zoom and pan) =====
  var Viewport = (function () {
    var stage, canvas, pct, scale = 1, tx = 0, ty = 0;
    var MIN = 0.2, MAX = 1.6;
    var dragging = false, moved = false, sx = 0, sy = 0, ox = 0, oy = 0;

    function apply() {
      canvas.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
      pct.textContent = Math.round(scale * 100) + '%';
    }
    function clampScale(s) { return Math.min(MAX, Math.max(MIN, s)); }

    function zoomAt(cx, cy, next) {
      var s = clampScale(next);
      if (s === scale) return;
      // Keep the point under the cursor fixed while the scale changes.
      tx = cx - (cx - tx) * (s / scale);
      ty = cy - (cy - ty) * (s / scale);
      scale = s;
      apply();
    }

    function fit() {
      var r = stage.getBoundingClientRect();
      var w = canvas.offsetWidth, h = canvas.offsetHeight;
      if (!w || !h || !r.width) return;
      scale = clampScale(Math.min((r.width - 40) / w, (r.height - 40) / h));
      tx = (r.width - w * scale) / 2;
      ty = 20;
      apply();
    }

    function init() {
      stage = document.getElementById('ds-stage');
      canvas = document.getElementById('ds-canvas');
      pct = document.getElementById('ds-pct');
      if (!stage || !canvas || !pct) return;

      document.getElementById('ds-in').addEventListener('click', function () {
        var r = stage.getBoundingClientRect();
        zoomAt(r.width / 2, r.height / 2, scale * 1.25);
      });
      document.getElementById('ds-out').addEventListener('click', function () {
        var r = stage.getBoundingClientRect();
        zoomAt(r.width / 2, r.height / 2, scale / 1.25);
      });
      document.getElementById('ds-fit').addEventListener('click', fit);

      stage.addEventListener('wheel', function (ev) {
        ev.preventDefault();
        var r = stage.getBoundingClientRect();
        zoomAt(ev.clientX - r.left, ev.clientY - r.top, scale * (ev.deltaY < 0 ? 1.1 : 1 / 1.1));
      }, { passive: false });

      stage.addEventListener('pointerdown', function (ev) {
        if (ev.button !== 0) return;
        dragging = true; moved = false;
        sx = ev.clientX; sy = ev.clientY; ox = tx; oy = ty;
        stage.classList.add('ds-grabbing');
        // Deliberately NOT calling setPointerCapture here. Capturing on
        // pointerdown retargets the whole gesture to the stage, so the
        // pointerup never lands on the node and no click event is ever
        // dispatched to it. That silently broke every node on the chart while
        // panning still worked, which is the worst possible way for it to
        // fail. Capture is taken below, only once a real drag begins.
      });
      stage.addEventListener('pointermove', function (ev) {
        if (!dragging) return;
        var dx = ev.clientX - sx, dy = ev.clientY - sy;
        // A few pixels of slop so a click with a shaky hand still opens a node.
        if (!moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
          moved = true;
          try { stage.setPointerCapture(ev.pointerId); } catch (err) { /* already gone */ }
        }
        if (!moved) return;
        tx = ox + dx; ty = oy + dy;
        apply();
      });
      function endDrag(ev) {
        if (!dragging) return;
        dragging = false;
        stage.classList.remove('ds-grabbing');
        if (ev && ev.pointerId !== undefined && stage.hasPointerCapture(ev.pointerId)) {
          stage.releasePointerCapture(ev.pointerId);
        }
      }
      stage.addEventListener('pointerup', endDrag);
      stage.addEventListener('pointercancel', endDrag);
      stage.addEventListener('pointerleave', endDrag);

      fit();
      window.addEventListener('resize', function () { if (scale <= 0.999) fit(); });
    }
    return { init: init, didDrag: function () { return moved; } };
  })();

  // ===== DRAWER =====
  var Drawer = (function () {
    var el, nameEl, deptEl, roleEl, metaEl, bodyEl, srcEl, current = null, lastFocus = null;

    function list(title, items, ordered) {
      if (!items || !items.length) return '';
      var tag = ordered ? 'ol' : 'ul';
      var out = '<h3>' + title + '</h3><' + tag + '>';
      for (var i = 0; i < items.length; i++) out += '<li>' + esc(items[i]) + '</li>';
      return out + '</' + tag + '>';
    }
    function esc(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function open(id, node) {
      var d = DETAIL[id];
      if (!d) return;
      current = id;
      lastFocus = node || null;

      var nodes = document.querySelectorAll('.ds-node.ds-open');
      for (var i = 0; i < nodes.length; i++) nodes[i].classList.remove('ds-open');
      if (node) node.classList.add('ds-open');

      deptEl.textContent = d.dept;
      nameEl.textContent = d.name;
      roleEl.textContent = d.role;

      var meta = '<span class="ds-pill" data-s="' + esc(d.status) + '">' +
        esc(d.status.charAt(0).toUpperCase() + d.status.slice(1)) + '</span>' +
        '<span class="ds-pill">Phase ' + esc(d.phase) + '</span>';
      if (d.human && d.human.length) {
        meta += '<span class="ds-pill ds-pill-gate">' + d.human.length + ' human gate</span>';
      }
      metaEl.innerHTML = meta;

      var body = '';
      if (d.trigger) body += '<h3>Trigger</h3><p>' + esc(d.trigger) + '</p>';
      body += list('What it does', d.steps, true);
      if (d.human && d.human.length) body += list('Human signs off', d.human, false);
      else body += '<h3>Human signs off</h3><p>Nobody. Runs unattended.</p>';
      body += list('Outputs', d.outputs, false);
      if (d.tools && d.tools.length) {
        body += '<h3>Touches</h3><div class="ds-tools">';
        for (var t = 0; t < d.tools.length; t++) body += '<span class="ds-tool">' + esc(d.tools[t]) + '</span>';
        body += '</div>';
      }
      if (d.gotcha) {
        body += '<div class="ds-trap"><p><b>The trap this exists to avoid.</b> ' + esc(d.gotcha) + '</p></div>';
      }
      bodyEl.innerHTML = body;
      srcEl.textContent = d.source || '';

      el.hidden = false;
      // Next frame, so the transform transition actually runs from the
      // off-screen position rather than being collapsed with the unhide.
      requestAnimationFrame(function () { el.classList.add('ds-open'); });
      el.scrollTop = 0;
    }

    function close() {
      el.classList.remove('ds-open');
      var nodes = document.querySelectorAll('.ds-node.ds-open');
      for (var i = 0; i < nodes.length; i++) nodes[i].classList.remove('ds-open');
      current = null;
      window.setTimeout(function () { if (!el.classList.contains('ds-open')) el.hidden = true; }, 300);
      if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
    }

    function init() {
      el = document.getElementById('ds-drawer');
      nameEl = document.getElementById('ds-d-name');
      deptEl = document.getElementById('ds-d-dept');
      roleEl = document.getElementById('ds-d-role');
      metaEl = document.getElementById('ds-d-meta');
      bodyEl = document.getElementById('ds-d-body');
      srcEl = document.getElementById('ds-d-src');
      if (!el) return;
      document.getElementById('ds-d-close').addEventListener('click', close);
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && current) close();
      });
    }
    return { init: init, open: open, close: close };
  })();

  // ===== FILTERS =====
  var Filters = (function () {
    var nodes, chips, q, active = { dept: null, human: null };

    function apply() {
      var term = (q.value || '').trim().toLowerCase();
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i], ok = true;
        if (active.dept && n.dataset.dept !== active.dept) ok = false;
        if (active.human && n.dataset.human !== active.human) ok = false;
        n.classList.toggle('ds-dim', !ok);
        var hit = ok && term.length > 1 && (n.dataset.search || '').indexOf(term) !== -1;
        n.classList.toggle('ds-hit', hit);
      }
    }

    function press(chip, on) { chip.setAttribute('aria-pressed', on ? 'true' : 'false'); }

    function init() {
      nodes = document.querySelectorAll('.ds-node-agent');
      chips = document.querySelectorAll('.ds-chip');
      q = document.getElementById('ds-q');
      if (!q) return;

      for (var i = 0; i < chips.length; i++) {
        (function (chip) {
          chip.addEventListener('click', function () {
            var k = chip.dataset.filter, v = chip.dataset.value;
            if (k === 'all') { active.dept = null; active.human = null; }
            else { active[k] = active[k] === v ? null : v; }
            for (var j = 0; j < chips.length; j++) {
              var c = chips[j];
              if (c.dataset.filter === 'all') press(c, !active.dept && !active.human);
              else press(c, active[c.dataset.filter] === c.dataset.value);
            }
            apply();
          });
        })(chips[i]);
      }
      q.addEventListener('input', apply);
      apply();
    }
    return { init: init };
  })();

  // ===== NODE CLICKS =====
  var Nodes = (function () {
    function bind(sel) {
      var els = document.querySelectorAll(sel);
      for (var i = 0; i < els.length; i++) {
        (function (el) {
          el.addEventListener('click', function () {
            // A pan that ends on top of a node must not also open it.
            if (Viewport.didDrag()) return;
            Drawer.open(el.dataset.id, el);
          });
        })(els[i]);
      }
    }
    function init() { bind('.ds-node[data-id]'); bind('.ds-m-row[data-id]'); }
    return { init: init };
  })();

  // ===== COPY =====
  var Copy = (function () {
    function init() {
      var btn = document.getElementById('ds-copy');
      var cmd = document.getElementById('ds-cmd');
      if (!btn || !cmd) return;
      btn.addEventListener('click', function () {
        var text = cmd.textContent;
        function done() {
          btn.textContent = 'Copied';
          Storage.set('copied', true);
          window.setTimeout(function () { btn.textContent = 'Copy'; }, 1800);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, fallback);
        } else { fallback(); }
        function fallback() {
          var t = document.createElement('textarea');
          t.value = text; document.body.appendChild(t); t.select();
          try { document.execCommand('copy'); done(); } catch (err) { btn.textContent = 'Select and copy'; }
          document.body.removeChild(t);
        }
      });
    }
    return { init: init };
  })();

  document.addEventListener('DOMContentLoaded', function () {
    Viewport.init();
    Drawer.init();
    Filters.init();
    Nodes.init();
    Copy.init();
  });
})();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="destination .html path")
    args = ap.parse_args()
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    text = build(doc)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out}  ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
