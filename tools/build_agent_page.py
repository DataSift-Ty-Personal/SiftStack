#!/usr/bin/env python3
"""Render docs/agents.json to the published agent system page.

Same source as tools/agent_map.py, different renderer. Two renderers over one
JSON file so the interactive page and the committed markdown cannot disagree
about what the system does.

    python tools/build_agent_page.py            # -> docs/agent-system.html
    python tools/build_agent_page.py --out X    # somewhere else
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "agents.json"
OUT = ROOT / "docs" / "agent-system.html"

REPO = "DataSift-Ty-Personal/SiftStack"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
INSTALL = f"curl -fsSL {RAW}/install.py | python3 -"

STATUS_TEXT = {
    "live": "Live",
    "gated": "Gated",
    "planned": "Planned",
    "retired": "Retired",
}


def e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def agent_card(a: dict, dept_id: str, lead: bool = False) -> str:
    status = a.get("status", "live")
    phase = a.get("phase", 1)
    human = a.get("human") or []
    steps = a.get("steps") or []
    outs = a.get("outputs") or []
    tools = a.get("tools") or []

    blocks = []
    if a.get("trigger"):
        blocks.append(
            f'<div class="fact"><div class="fact-k">Trigger</div>'
            f'<div class="fact-v">{e(a["trigger"])}</div></div>')
    if steps:
        li = "".join(f"<li>{e(s)}</li>" for s in steps)
        blocks.append(
            f'<div class="fact"><div class="fact-k">What it does</div>'
            f'<ul class="fact-v steps">{li}</ul></div>')
    if human:
        li = "".join(f"<li>{e(s)}</li>" for s in human)
        blocks.append(
            f'<div class="fact"><div class="fact-k">Human signs off</div>'
            f'<ul class="fact-v steps">{li}</ul></div>')
    else:
        blocks.append(
            f'<div class="fact"><div class="fact-k">Human signs off</div>'
            f'<div class="fact-v muted">Nobody. Runs unattended.</div></div>')
    if outs:
        blocks.append(
            f'<div class="fact"><div class="fact-k">Outputs</div>'
            f'<div class="fact-v">{e(", ".join(outs))}</div></div>')
    if tools:
        chips = "".join(f'<span class="chip">{e(t)}</span>' for t in tools)
        blocks.append(
            f'<div class="fact"><div class="fact-k">Touches</div>'
            f'<div class="fact-v chips">{chips}</div></div>')

    trap = ""
    if a.get("gotcha"):
        trap = (f'<div class="trap"><div class="trap-k">The trap this exists to avoid</div>'
                f'<p>{e(a["gotcha"])}</p></div>')

    search = " ".join([a.get("name", ""), a.get("role", ""), a.get("source", ""),
                       a.get("gotcha", ""), " ".join(tools)]).lower()

    return f"""<article class="agent" data-dept="{e(dept_id)}" data-status="{e(status)}" data-phase="{e(phase)}" data-human="{'yes' if human else 'no'}" data-search="{e(search)}">
  <details>
    <summary>
      <span class="dot dot-{e(status)}" aria-hidden="true"></span>
      <span class="agent-name">{e(a.get('name'))}{' <span class="lead-tag">lead</span>' if lead else ''}</span>
      <span class="agent-meta">
        <span class="badge badge-{e(status)}">{e(STATUS_TEXT.get(status, status))}</span>
        {'<span class="badge badge-gate">Human gate</span>' if human else ''}
      </span>
      <span class="agent-role">{e(a.get('role'))}</span>
      <code class="src">{e(a.get('source'))}</code>
    </summary>
    <div class="agent-body">
      {''.join(blocks)}
      {trap}
    </div>
  </details>
</article>"""


def _skill_count() -> int:
    """Current, non-superseded packages, read from the manifest.

    Hardcoding this drifts the moment a skill is added, and a page that
    advertises the wrong number is the kind of small wrongness that makes
    people distrust the rest of it.
    """
    mf = ROOT / "skills" / "manifest.json"
    if mf.is_file():
        return json.loads(mf.read_text(encoding="utf-8"))["counts"]["current"]
    return 0


def build(doc: dict) -> str:
    meta = doc["meta"]
    depts = doc["departments"]

    n_agents = sum(len(d.get("agents", [])) + (1 if d.get("lead") else 0) for d in depts)
    n_live = 0
    n_gate = 0
    for d in depts:
        for a in [d.get("lead")] + d.get("agents", []):
            if not a:
                continue
            if a.get("status") == "live":
                n_live += 1
            if a.get("human"):
                n_gate += 1

    # Filter bar
    dept_btns = "".join(
        f'<button class="filt" data-filter="dept" data-value="{e(d["id"])}">{e(d["name"])}</button>'
        for d in depts)

    sections = []
    for d in depts:
        cards = []
        if d.get("lead"):
            cards.append(agent_card(d["lead"], d["id"], lead=True))
        for a in d.get("agents", []):
            cards.append(agent_card(a, d["id"]))
        n = len(cards)
        sections.append(f"""<section class="dept" id="dept-{e(d['id'])}" data-dept="{e(d['id'])}">
  <header class="dept-head">
    <h3>{e(d['name'])}</h3>
    <p class="dept-tag">{e(d.get('tagline'))}</p>
    <span class="dept-count"><b>{n}</b> agents</span>
  </header>
  <div class="grid">{''.join(cards)}</div>
</section>""")

    exec_cards = "".join(agent_card(a, "exec") for a in doc.get("exec", []))

    infra = ""
    if doc.get("infra"):
        groups = "".join(
            f'<div class="infra-g"><h4>{e(g["group"])}</h4><ul>'
            + "".join(f"<li>{e(i)}</li>" for i in g["items"]) + "</ul></div>"
            for g in doc["infra"])
        infra = f'<div class="infra">{groups}</div>'

    return Template(PAGE).substitute(
        n_skills=_skill_count(),
        title=e(meta["title"]),
        subtitle=e(meta["subtitle"]),
        version=e(meta.get("version")),
        generated=e(meta.get("generated")),
        n_agents=n_agents,
        n_depts=len(depts),
        n_live=n_live,
        n_gate=n_gate,
        install=e(INSTALL),
        install_raw=e(INSTALL),
        repo=e(REPO),
        raw=e(RAW),
        dept_btns=dept_btns,
        exec_cards=exec_cards,
        sections="".join(sections),
        infra=infra,
    )


PAGE = """<title>SiftStack Agent Org Chart</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
/* ---------------------------------------------------------------- tokens
   DataSift house palette. Navy #0A1130, blue #316AFF, green #1B9E5A for
   status, gold for caveats, system font stack. Neutrals are biased toward
   the blue accent rather than pure grey so they read as chosen. */
:root {
  --navy:#0A1130; --blue:#316AFF; --green:#1B9E5A; --gold:#9A6B12;
  --bg:#F6F7FB; --surface:#FFFFFF; --surface-2:#F0F2F8; --sunk:#FBFCFE;
  --ink:#0A1130; --ink-2:#3A4364; --ink-3:#646E8C;
  --line:#DDE2EE; --line-2:#C9D0E2;
  --accent:#316AFF; --accent-ink:#1D4ED8; --accent-wash:#EAF0FF;
  --ok:#12784A; --ok-wash:#E6F4EC;
  --warn:#8A5D0B; --warn-wash:#FBF2DF;
  --off:#646E8C; --off-wash:#EEF0F6;
  --shadow:0 1px 2px rgba(10,17,48,.05);
  --r-lg:12px; --r-md:8px; --r-sm:6px; --r-xs:4px;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#070B1C; --surface:#0E1430; --surface-2:#141B3C; --sunk:#0A1027;
    --ink:#E9EDF9; --ink-2:#AFB9D6; --ink-3:#8590AF;
    --line:#222C52; --line-2:#2E3A66;
    --accent:#7A9EFF; --accent-ink:#A8C0FF; --accent-wash:#16204A;
    --ok:#5CD79B; --ok-wash:#0F2C20;
    --warn:#E5B65A; --warn-wash:#2E2311;
    --off:#8590AF; --off-wash:#171E3E;
    --shadow:0 1px 2px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"] {
  --bg:#070B1C; --surface:#0E1430; --surface-2:#141B3C; --sunk:#0A1027;
  --ink:#E9EDF9; --ink-2:#AFB9D6; --ink-3:#8590AF;
  --line:#222C52; --line-2:#2E3A66;
  --accent:#7A9EFF; --accent-ink:#A8C0FF; --accent-wash:#16204A;
  --ok:#5CD79B; --ok-wash:#0F2C20;
  --warn:#E5B65A; --warn-wash:#2E2311;
  --off:#8590AF; --off-wash:#171E3E;
  --shadow:0 1px 2px rgba(0,0,0,.3);
}

*,*::before,*::after { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink);
  font-family:var(--sans); font-size:16px; line-height:1.55;
  -webkit-text-size-adjust:100%;
}
.wrap { max-width:1120px; margin:0 auto; padding:0 20px; }
@media (max-width:520px){ .wrap { padding:0 14px; } }

a { color:var(--accent-ink); text-underline-offset:2px; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:var(--r-xs); }

/* ---------------------------------------------------------------- masthead */
.mast { border-bottom:1px solid var(--line); background:var(--surface); }
/* padding-top/bottom, NOT the shorthand. This element carries both .wrap and
   .mast-in; a `padding: 34px 0 26px` here has equal specificity and comes
   later, so it silently reset .wrap's horizontal padding to 0 and ran the
   headline edge to edge on mobile. */
.mast-in { padding-top:34px; padding-bottom:26px; }
.eyebrow {
  font-size:13px; color:var(--ink-3); margin:0 0 10px;
  display:flex; gap:8px; align-items:center; flex-wrap:wrap;
}
.eyebrow .sep { color:var(--line-2); }
h1 {
  margin:0 0 10px; font-size:clamp(28px,5.2vw,42px); line-height:1.12;
  letter-spacing:-.022em; font-weight:680; text-wrap:balance;
}
.lede { margin:0; max-width:64ch; color:var(--ink-2); font-size:clamp(15px,2.4vw,17px); }

.stats { display:flex; flex-wrap:wrap; gap:10px; margin-top:22px; }
.stat {
  background:var(--sunk); border:1px solid var(--line); border-radius:var(--r-md);
  padding:10px 14px; min-width:104px;
}
.stat b { display:block; font-size:22px; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
.stat span { font-size:12.5px; color:var(--ink-3); }

/* ---------------------------------------------------------------- install */
.install { margin:26px 0 0; }
.install h2 { margin:0 0 4px; font-size:17px; font-weight:660; letter-spacing:-.01em; }
.install p { margin:0 0 12px; color:var(--ink-2); font-size:14.5px; max-width:62ch; }
.cmd {
  display:flex; align-items:stretch; gap:0; border:1px solid var(--line-2);
  border-radius:var(--r-md); overflow:hidden; background:var(--sunk);
}
.cmd code {
  flex:1; min-width:0; font-family:var(--mono); font-size:13.5px; padding:13px 14px;
  overflow-x:auto; white-space:nowrap; color:var(--ink);
}
.cmd button {
  flex:none; border:0; border-left:1px solid var(--line-2); background:var(--surface-2);
  color:var(--ink); font:inherit; font-size:13.5px; font-weight:560;
  padding:0 16px; cursor:pointer; transition:background .12s ease;
}
.cmd button:hover { background:var(--accent-wash); color:var(--accent-ink); }
.cmd button:active { transform:translateY(1px); }
.install-note { margin-top:9px; font-size:13px; color:var(--ink-3); }

/* ---------------------------------------------------------------- controls */
.controls {
  position:sticky; top:0; z-index:20; background:var(--surface);
  border-bottom:1px solid var(--line); padding:12px 0;
}
.controls-in { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.search {
  flex:1 1 210px; min-width:0; font:inherit; font-size:14.5px; color:var(--ink);
  background:var(--sunk); border:1px solid var(--line-2); border-radius:var(--r-md);
  padding:9px 12px;
}
.search::placeholder { color:var(--ink-3); }
.filts { display:flex; gap:6px; flex-wrap:wrap; }
.filt {
  font:inherit; font-size:13px; font-weight:520; color:var(--ink-2);
  background:var(--surface-2); border:1px solid var(--line);
  border-radius:var(--r-sm); padding:6px 11px; cursor:pointer;
  transition:background .12s ease,color .12s ease,border-color .12s ease;
}
.filt:hover { border-color:var(--line-2); }
.filt:active { transform:translateY(1px); }
.filt[aria-pressed="true"] {
  background:var(--accent-wash); border-color:var(--accent); color:var(--accent-ink);
}
.count-live { font-size:13px; color:var(--ink-3); font-variant-numeric:tabular-nums; margin-left:auto; }

/* ---------------------------------------------------------------- sections */
main { padding:30px 0 60px; }
.band { margin:0 0 34px; }
.band > h2 {
  margin:0 0 4px; font-size:20px; font-weight:660; letter-spacing:-.015em;
}
.band > p { margin:0 0 18px; color:var(--ink-2); font-size:14.5px; max-width:66ch; }

.dept { margin:0 0 34px; scroll-margin-top:70px; }
.dept-head {
  display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;
  padding-bottom:10px; border-bottom:1px solid var(--line); margin-bottom:14px;
}
.dept-head h3 { margin:0; font-size:18px; font-weight:660; letter-spacing:-.012em; }
.dept-tag { margin:0; flex:1 1 240px; color:var(--ink-3); font-size:13.5px; }
.dept-count { font-size:13px; color:var(--ink-3); font-variant-numeric:tabular-nums; }
.dept-count b { color:var(--ink-2); }

.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:12px; }
@media (max-width:560px){ .grid { grid-template-columns:1fr; } }

/* ---------------------------------------------------------------- agent */
.agent {
  background:var(--surface); border:1px solid var(--line);
  border-radius:var(--r-lg); box-shadow:var(--shadow); overflow:hidden;
}
.agent details { }
.agent summary {
  list-style:none; cursor:pointer; padding:14px 15px;
  display:grid; grid-template-columns:auto 1fr auto; gap:6px 9px; align-items:start;
}
.agent summary::-webkit-details-marker { display:none; }
.agent summary:hover { background:var(--surface-2); }
.dot { width:8px; height:8px; border-radius:50%; margin-top:7px; }
.dot-live { background:var(--ok); }
.dot-gated { background:var(--warn); }
.dot-planned { background:var(--off); }
.dot-retired { background:var(--line-2); }
.agent-name { font-weight:620; font-size:15px; letter-spacing:-.008em; }
.lead-tag {
  font-size:11px; font-weight:560; color:var(--accent-ink);
  background:var(--accent-wash); border-radius:var(--r-xs); padding:1px 5px; margin-left:5px;
  vertical-align:1px;
}
.agent-meta { display:flex; gap:5px; flex-wrap:wrap; justify-content:flex-end; }
.badge {
  font-size:11.5px; font-weight:560; border-radius:var(--r-xs);
  padding:2px 7px; white-space:nowrap; border:1px solid transparent;
}
.badge-live { background:var(--ok-wash); color:var(--ok); }
.badge-gated { background:var(--warn-wash); color:var(--warn); }
.badge-planned { background:var(--off-wash); color:var(--off); }
.badge-retired { background:var(--off-wash); color:var(--off); }
.badge-gate { background:var(--accent-wash); color:var(--accent-ink); }
.agent-role { grid-column:2 / -1; color:var(--ink-2); font-size:13.8px; line-height:1.5; }
.src {
  grid-column:2 / -1; font-family:var(--mono); font-size:11.8px; color:var(--ink-3);
  overflow-wrap:anywhere;
}

.agent-body { padding:2px 15px 15px; border-top:1px solid var(--line); margin-top:2px; }
.fact { padding:12px 0 0; }
.fact-k {
  font-size:12px; font-weight:600; color:var(--ink-3); margin-bottom:3px;
}
.fact-v { font-size:14px; color:var(--ink-2); margin:0; }
.fact-v.muted { color:var(--ink-3); }
ul.steps { padding-left:17px; margin:0; }
ul.steps li { margin:2px 0; }
.chips { display:flex; gap:5px; flex-wrap:wrap; }
.chip {
  font-family:var(--mono); font-size:11.5px; color:var(--ink-2);
  background:var(--surface-2); border:1px solid var(--line);
  border-radius:var(--r-xs); padding:2px 6px;
}

/* The trap is the most valuable field on the page: every one is a real
   production failure, most of which failed silently. It gets its own
   recessed treatment so it reads as evidence rather than commentary. */
.trap {
  margin-top:14px; background:var(--warn-wash); border:1px solid transparent;
  border-left:3px solid var(--warn); border-radius:0 var(--r-sm) var(--r-sm) 0;
  padding:10px 12px;
}
.trap-k { font-size:11.5px; font-weight:640; color:var(--warn); margin-bottom:3px; }
.trap p { margin:0; font-size:13.5px; line-height:1.52; color:var(--ink-2); }

/* ---------------------------------------------------------------- infra */
.infra { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:12px; }
.infra-g {
  background:var(--surface); border:1px solid var(--line);
  border-radius:var(--r-md); padding:13px 15px;
}
.infra-g h4 { margin:0 0 7px; font-size:14px; font-weight:620; }
.infra-g ul { margin:0; padding-left:16px; color:var(--ink-2); font-size:13.5px; }
.infra-g li { margin:2px 0; }

/* ---------------------------------------------------------------- legend */
.legend {
  display:flex; gap:16px; flex-wrap:wrap; font-size:13px; color:var(--ink-3);
  padding:12px 0 0; border-top:1px solid var(--line); margin-top:6px;
}
.legend span { display:flex; align-items:center; gap:6px; }
.legend .dot { margin-top:0; }

.empty { display:none; padding:34px 0; color:var(--ink-3); font-size:15px; }
.empty.on { display:block; }

footer {
  border-top:1px solid var(--line); background:var(--surface);
  padding:22px 0 30px; color:var(--ink-3); font-size:13.5px;
}
footer p { margin:0 0 6px; }

@media (prefers-reduced-motion: reduce) {
  * { transition:none !important; animation:none !important; }
}
</style>

<div class="mast">
  <div class="wrap mast-in">
    <p class="eyebrow">
      <span>SiftStack</span><span class="sep">/</span><span>Build $version</span>
      <span class="sep">/</span><span>$generated</span>
    </p>
    <h1>$title</h1>
    <p class="lede">$subtitle.</p>

    <div class="stats">
      <div class="stat"><b>$n_agents</b><span>agents</span></div>
      <div class="stat"><b>$n_depts</b><span>divisions</span></div>
      <div class="stat"><b>$n_live</b><span>live in production</span></div>
      <div class="stat"><b>$n_gate</b><span>with a human gate</span></div>
    </div>

    <div class="install">
      <h2>Install the skill library</h2>
      <p>$n_skills Claude skills that drive the agents below. One command, into Claude Code.
         No clone, no pip, no virtualenv.</p>
      <div class="cmd">
        <code id="cmd">$install</code>
        <button type="button" id="copy" aria-label="Copy the install command">Copy</button>
      </div>
      <p class="install-note">
        On Claude Co-Work or claude.ai, download a package from
        <a href="https://github.com/$repo/tree/main/dist">dist/</a> and upload it instead.
        Full instructions in the <a href="https://github.com/$repo#install-the-skill-library">README</a>.
      </p>
    </div>
  </div>
</div>

<div class="controls">
  <div class="wrap controls-in">
    <input id="q" class="search" type="search" placeholder="Search agents, files, traps" aria-label="Search agents">
    <div class="filts">
      <button class="filt" data-filter="status" data-value="live">Live</button>
      <button class="filt" data-filter="status" data-value="gated">Gated</button>
      <button class="filt" data-filter="human" data-value="yes">Human gate</button>
      $dept_btns
    </div>
    <span class="count-live" id="count"></span>
  </div>
</div>

<main class="wrap">
  <section class="band">
    <h2>How to read this</h2>
    <p>Every agent carries a trigger, the steps it runs, where a person still signs off, what
       comes out, and the trap it exists to avoid. That last field is the one worth reading:
       each is a real production failure, and most of them failed silently for days or weeks
       before anyone noticed. Click any agent to open it.</p>
    <div class="legend">
      <span><i class="dot dot-live"></i>Live in production</span>
      <span><i class="dot dot-gated"></i>Built, held behind a human go/no-go</span>
      <span><i class="dot dot-planned"></i>Designed, not yet wired</span>
      <span><i class="dot dot-retired"></i>Retired on purpose</span>
    </div>
  </section>

  <section class="band">
    <h2>Top of the org</h2>
    <p>Every gated action routes to a person. Outreach identity is anchored to the assigned
       human rather than a company name, and no agent puts a dollar figure in front of a seller
       or a buyer without someone approving it.</p>
    <div class="grid">$exec_cards</div>
  </section>

  <section class="band">
    <h2>Divisions</h2>
    <p>Nine divisions, from pulling the county record through to handing a private lender a
       funded deal package.</p>
  </section>

  $sections

  <p class="empty" id="empty">No agent matches that. Clear the filters or try another word.</p>

  <section class="band">
    <h2>What the system depends on</h2>
    $infra
  </section>
</main>

<footer>
  <div class="wrap">
    <p>Generated from <code>docs/agents.json</code> by <code>tools/build_agent_page.py</code>.
       The same file renders <code>docs/AGENT-MAP.md</code>, so this page and the repo docs
       cannot disagree.</p>
    <p><a href="https://github.com/$repo">github.com/$repo</a></p>
  </div>
</footer>

<script>
(function () {
  var agents = Array.prototype.slice.call(document.querySelectorAll('.agent'));
  var depts  = Array.prototype.slice.call(document.querySelectorAll('.dept'));
  var q = document.getElementById('q');
  var countEl = document.getElementById('count');
  var empty = document.getElementById('empty');
  var active = { status: null, human: null, dept: null };

  function apply() {
    var term = (q.value || '').trim().toLowerCase();
    var shown = 0;
    agents.forEach(function (el) {
      var ok = true;
      if (active.status && el.dataset.status !== active.status) ok = false;
      if (active.human && el.dataset.human !== active.human) ok = false;
      if (active.dept && el.dataset.dept !== active.dept) ok = false;
      if (ok && term) ok = el.dataset.search.indexOf(term) !== -1;
      el.hidden = !ok;
      if (ok) shown++;
    });
    // Hide a division whose agents have all been filtered out, so the page
    // does not leave a row of empty headers behind.
    depts.forEach(function (d) {
      var any = d.querySelector('.agent:not([hidden])');
      d.hidden = !any;
    });
    countEl.textContent = shown + ' of ' + agents.length + ' agents';
    empty.classList.toggle('on', shown === 0);
  }

  document.querySelectorAll('.filt').forEach(function (b) {
    b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', function () {
      var k = b.dataset.filter, v = b.dataset.value;
      var on = active[k] === v;
      // One value per filter key. Re-clicking the active one clears it.
      document.querySelectorAll('.filt[data-filter="' + k + '"]').forEach(function (o) {
        o.setAttribute('aria-pressed', 'false');
      });
      active[k] = on ? null : v;
      b.setAttribute('aria-pressed', on ? 'false' : 'true');
      apply();
    });
  });

  q.addEventListener('input', apply);

  var copy = document.getElementById('copy');
  copy.addEventListener('click', function () {
    var text = document.getElementById('cmd').textContent;
    var done = function () {
      copy.textContent = 'Copied';
      setTimeout(function () { copy.textContent = 'Copy'; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { copy.textContent = 'Press Ctrl C'; });
    } else {
      // execCommand fallback: clipboard API needs a secure context and this
      // page may be opened from a file:// path.
      var t = document.createElement('textarea');
      t.value = text; document.body.appendChild(t); t.select();
      try { document.execCommand('copy'); done(); } catch (err) { copy.textContent = 'Press Ctrl C'; }
      document.body.removeChild(t);
    }
  });

  apply();
})();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
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
