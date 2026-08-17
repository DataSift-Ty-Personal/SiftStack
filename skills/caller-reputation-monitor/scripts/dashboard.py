"""
dashboard.py - renders the self-contained Caller ID Reputation HTML dashboard.

Pure stdlib string templating (no jinja, no external CSS). Telnyx-only build:
two live signal layers (L1 CDR own-traffic, L2 Number Reputation carrier-grade),
the number lifecycle state machine, the recommended active dial pool, and the
remediation ledger.

One function: render(cfg, state, th, ren, meta, pool_plan=None, ledger=None) -> html.
"""
from __future__ import annotations

import datetime
import html


# ---- format helpers ----
def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _pretty(num10: str) -> str:
    num10 = "".join(ch for ch in str(num10) if ch.isdigit())
    if len(num10) == 11 and num10.startswith("1"):
        num10 = num10[1:]
    if len(num10) == 10:
        return f"({num10[:3]}) {num10[3:6]}-{num10[6:]}"
    return num10 or "?"


def _status_badge(status: str) -> str:
    cls = {"Clean": "b-clean", "Watch": "b-watch",
           "Flagged": "b-flag", "Pending": "b-pending"}.get(status, "b-pending")
    return f'<span class="badge {cls}">{_esc(status)}</span>'


def _state_badge(state: str) -> str:
    cls = {"ACTIVE": "s-active", "WARMING": "s-warm", "WATCH": "s-watch",
           "RESTING": "s-rest", "RETIRED": "s-retire"}.get(state, "s-new")
    return f'<span class="sbadge {cls}">{_esc(state or "NEW")}</span>'


def _metric(value, unit: str, bad: bool) -> str:
    if value is None:
        return '<span class="muted">-</span>'
    cls = "bad" if bad else "muted"
    return f'<span class="{cls}">{_esc(value)}{unit}</span>'


def _carrier_conf(label: str, value: str) -> str:
    v = (value or "pending").lower()
    mark = {"confirmed": '<span class="ok">Confirmed</span>',
            "rejected": '<span class="bad">Rejected</span>'}.get(
        v, '<span class="muted">pending</span>')
    return f"<b>{_esc(label)}:</b> {mark}"


def render(cfg: dict, state: dict, th: dict, ren: dict, meta: dict,
           pool_plan: dict | None = None, ledger: list | None = None) -> str:
    biz = cfg.get("business_name", "Caller ID Reputation")
    reg = cfg.get("registration", {}) or {}
    tx = cfg.get("telnyx", {}) or {}
    excluded = cfg.get("excluded", []) or []
    pool_plan = pool_plan or {}
    ledger = ledger or []
    l1cfg = th.get("l1_cdr", {})

    pool = []
    for e in cfg.get("numbers", []):
        d = "".join(ch for ch in str(e.get("number", "")) if ch.isdigit())
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        if len(d) == 10:
            pool.append(d)
    pool = sorted(set(pool))

    monitored = len(pool)
    active_ct = pool_plan.get("active_count", sum(
        1 for n in pool if state.get(n, {}).get("state") == "ACTIVE"))
    watch = sum(1 for n in pool if state.get(n, {}).get("status") == "Watch")
    flagged = sum(1 for n in pool if state.get(n, {}).get("status") == "Flagged")
    resting = sum(1 for n in pool if state.get(n, {}).get("state") == "RESTING")
    l2_on = bool(tx.get("enterprise_id"))

    updated = datetime.datetime.now().strftime("%B %d, %Y %H:%M")

    cards = [
        ("kpi-num", monitored, "NUMBERS<br>MONITORED"),
        ("kpi-clean", active_ct, "ACTIVE<br>DIAL POOL"),
        ("kpi-watch", watch, "WATCH"),
        ("kpi-flag", flagged, "FLAGGED"),
        ("kpi-num", resting, "RESTING"),
        ("kpi-num", "L2 ON" if l2_on else "L1", "SIGNAL<br>LAYERS"),
    ]
    kpi_html = "\n".join(
        f'<div class="card"><div class="kpi {cls}">{val}</div>'
        f'<div class="kpi-label">{label}</div></div>' for cls, val, label in cards)

    layers = [
        ("L1 &middot; Own traffic (CDR)",
         "Telnyx Detail Records: per-number ASR, average call length, and short-call rate "
         "from our OWN outbound calls. Ground truth, free, degrades before a carrier label lands. "
         f"Targets: ASR &ge; {l1cfg.get('asr_min_pct',30)}%, ALOC &ge; {l1cfg.get('aloc_min_sec',30)}s, "
         f"short &le; {l1cfg.get('short_call_max_pct',15)}%."),
        ("L2 &middot; Carrier reputation",
         "Telnyx Number Reputation: carrier-grade spam_risk + spam_category (Hiya-fed, all engines "
         "fused). Cached read free; a fresh check only fires on a number a cheaper layer already "
         "flagged. A high risk or any label = Flagged, regardless of L1."),
        ("Fusion &middot; worst-of",
         "Status = the worst of L1 and L2. Carrier-grade L2 can flag even when our call data looks "
         "clean; an L1 breach forces at least Watch. Neither layer can veto the other down."),
        ("Auto-remediation &middot; lifecycle",
         "A new Flagged auto-files a Telnyx remediation request (14-day cooldown), rests the number "
         "30 days, and drops it from the dial pool. 3 surviving flags retire it. See the ledger below."),
    ]
    layer_html = "\n".join(
        f'<div class="layer"><h4>{t}</h4><p>{b}</p></div>' for t, b in layers)

    # ---- number table ----
    rows = []
    for n in pool:
        r = state.get(n, {})
        srcs = r.get("sources", {}) or {}
        cdr = srcs.get("cdr", {}) or {}
        rep = srcs.get("telnyx_rep", {}) or {}
        status = r.get("status", "Pending")
        st = r.get("state", "NEW")
        m = meta.get(n, {})
        label = _esc(m.get("label", "") or r.get("label", ""))
        caller = _esc(m.get("caller", ""))

        # L1 cell
        asr, aloc, short = cdr.get("asr"), cdr.get("aloc"), cdr.get("short_pct")
        dials = cdr.get("dials")
        if dials is None and asr is None:
            l1_cell = '<span class="muted">-</span>'
        else:
            l1_cell = (
                f'{_metric(asr, "%", asr is not None and asr < l1cfg.get("asr_min_pct",30))} asr &middot; '
                f'{_metric(aloc, "s", aloc is not None and aloc < l1cfg.get("aloc_min_sec",30))} aloc &middot; '
                f'{_metric(short, "%", short is not None and short > l1cfg.get("short_call_max_pct",15))} short'
                f'<div class="sub2">{_esc(dials or 0)} dials</div>')

        # L2 cell
        risk = rep.get("spam_risk")
        cat = rep.get("spam_category")
        if risk is None and cat is None:
            l2_cell = '<span class="muted">-</span>'
        else:
            risk_cls = {"high": "bad", "medium": "warn"}.get((risk or "").lower(), "ok")
            l2_cell = f'<span class="{risk_cls}">{_esc(risk or "-")}</span>'
            if cat:
                l2_cell += f' <span class="bad">[{_esc(cat)}]</span>'
            scores = " ".join(str(rep.get(k)) for k in
                              ("maturity_score", "connection_score", "engagement_score", "sentiment_score")
                              if rep.get(k) is not None)
            if scores:
                l2_cell += f'<div class="sub2">m/c/e/s: {_esc(scores)}</div>'

        note_bits = []
        if r.get("rested_until"):
            note_bits.append(f"rest until {_esc(r['rested_until'])}")
        if r.get("retire_candidate"):
            note_bits.append('<span class="bad">RETIRE</span>')
        if r.get("reasons"):
            note_bits.append(_esc(", ".join(r["reasons"])))
        note = " &middot; ".join(note_bits)

        rows.append(f"""
          <tr>
            <td class="mono"><b>{_pretty(n)}</b>
                {(' <span class="lbl">'+label+'</span>') if label else ''}
                {(' <span class="lbl">/ '+caller+'</span>') if caller else ''}</td>
            <td>{_state_badge(st)}</td>
            <td>{_status_badge(status)}</td>
            <td>{l1_cell}</td>
            <td>{l2_cell}</td>
            <td>{_esc(r.get('last_checked','-'))}</td>
            <td class="note">{note}</td>
          </tr>""")
    table = "\n".join(rows) if rows else \
        '<tr><td colspan="7" class="muted">No numbers configured. Fill in config/numbers.json.</td></tr>'

    # ---- pool panel ----
    active = pool_plan.get("active", [])
    bench = pool_plan.get("bench", [])
    act_html = " &middot; ".join(
        f'{_pretty(a.get("number",""))}'
        + (f' <span class="lbl">({a["state"]})</span>' if a.get("state") else "")
        + f' <span class="muted">cap {a.get("cap","-")}</span>'
        for a in active) or '<span class="muted">none</span>'
    bench_html = " &middot; ".join(
        f'{_pretty(b.get("number",""))} <span class="lbl">{_esc(b.get("state",""))}</span>'
        + (f' <span class="muted">({_esc(b.get("reason",""))})</span>' if b.get("reason") else "")
        for b in bench) or '<span class="muted">none</span>'
    pool_panel = f"""
      <div class="panel">
        <p><b>Active dial pool ({len(active)}):</b> {act_html}</p>
        <p><b>Bench ({len(bench)}):</b> {bench_html}</p>
        <p class="muted">Generated {_esc(pool_plan.get('generated','-'))}. Feed the active set (with per-number caps) to the dialer; keep the bench out of rotation.</p>
      </div>
    """

    # ---- remediation ledger ----
    open_rows = [r for r in ledger if not r.get("closed")]
    led_rows = []
    for r in sorted(ledger, key=lambda x: x.get("opened", ""), reverse=True)[:20]:
        res = r.get("results") or {}
        buckets = " ".join(f"{k}:{len(v)}" for k, v in res.items() if v) if res else "-"
        led_rows.append(
            f'<tr><td class="mono">{_pretty(r.get("number",""))}</td>'
            f'<td>{_esc(r.get("opened","-"))}</td>'
            f'<td>{_esc(r.get("status","-"))}</td>'
            f'<td>{"closed" if r.get("closed") else "open"}</td>'
            f'<td class="note">{_esc(buckets)}</td></tr>')
    led_table = "\n".join(led_rows) if led_rows else \
        '<tr><td colspan="5" class="muted">No remediation requests filed yet.</td></tr>'

    # ---- registration panel ----
    renew_txt = _esc(ren.get("recheck_by") or "not set")
    if ren.get("days_left") is not None:
        cls = "bad" if ren.get("due") else "muted"
        renew_txt += f' <span class="{cls}">({ren["days_left"]} days)</span>'
    carriers = reg.get("carriers", {}) or {}
    conf_html = " &middot; ".join([
        _carrier_conf("Hiya (AT&T)", carriers.get("hiya_att")),
        _carrier_conf("TNS (Verizon)", carriers.get("tns_verizon")),
        _carrier_conf("First Orion (T-Mobile)", carriers.get("first_orion_tmobile")),
    ])
    ent_txt = _esc(tx.get("enterprise_id")) if tx.get("enterprise_id") else \
        '<span class="muted">not set - L2 idle until telnyx_setup.py is run</span>'
    reg_panel = f"""
      <div class="panel">
        <p><b>Telnyx enterprise_id:</b> {ent_txt}</p>
        <p><b>FCR confirmation:</b> {_esc(reg.get('feedback_id') or 'not submitted')}
           &nbsp;&middot;&nbsp; <b>Submitted:</b> {_esc(reg.get('submitted') or '-')}
           &nbsp;&middot;&nbsp; <b>Re-check by:</b> {renew_txt}</p>
        <p>{conf_html}</p>
      </div>
    """

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caller ID Reputation Monitor - {_esc(biz)}</title>
<style>
  :root {{ --navy:#1f3a5f; --navy2:#26527e; --ink:#1f2d3d; --line:#e3e8ef; --muted:#7c8aa0; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
          color:var(--ink); background:#f4f6fa; }}
  .head {{ background:linear-gradient(135deg,var(--navy),var(--navy2)); color:#fff; padding:26px 34px; }}
  .head h1 {{ margin:0; font-size:30px; }}
  .head .sub {{ opacity:.9; margin-top:4px; font-size:15px; }}
  .head .upd {{ opacity:.7; margin-top:8px; font-size:13px; }}
  .wrap {{ padding:22px 34px 60px; }}
  .cards {{ display:grid; grid-template-columns:repeat(6,1fr); gap:14px; margin-top:-44px; }}
  .card {{ background:#fff; border-radius:12px; padding:18px 16px; box-shadow:0 6px 20px rgba(31,58,95,.08); }}
  .kpi {{ font-size:34px; font-weight:800; line-height:1; }}
  .kpi-num {{ color:var(--navy); }} .kpi-clean {{ color:#1a9c5b; }}
  .kpi-watch {{ color:#c98a00; }} .kpi-flag {{ color:#c0392b; }}
  .kpi-label {{ margin-top:8px; font-size:12px; letter-spacing:.04em; color:var(--muted); font-weight:600; }}
  h2 {{ color:var(--navy); margin:34px 0 6px; font-size:19px; }}
  .rule {{ height:1px; background:var(--line); margin:4px 0 16px; }}
  .layers {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }}
  .layer {{ background:#fff; border-radius:10px; padding:16px; border-left:4px solid var(--navy2); }}
  .layer h4 {{ margin:0 0 6px; color:var(--navy); font-size:14px; }}
  .layer p {{ margin:0; font-size:12.5px; color:#48566b; line-height:1.45; }}
  .panel {{ background:#fff; border-radius:10px; padding:16px 20px; font-size:14px; line-height:1.7; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden;
           box-shadow:0 4px 14px rgba(31,58,95,.06); font-size:13.5px; }}
  thead th {{ background:var(--navy); color:#fff; text-align:left; padding:11px 12px; font-size:12.5px; }}
  tbody td {{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .mono {{ font-variant-numeric:tabular-nums; }}
  .lbl {{ color:var(--muted); font-weight:400; font-size:12px; }}
  .muted {{ color:var(--muted); }}
  .sub2 {{ color:var(--muted); font-size:11px; margin-top:2px; }}
  .note {{ font-size:12px; color:#48566b; }}
  .badge {{ padding:3px 10px; border-radius:999px; font-size:12px; font-weight:700; }}
  .b-clean {{ background:#e6f6ee; color:#1a9c5b; }}
  .b-watch {{ background:#fdf3dc; color:#a9760a; }}
  .b-flag {{ background:#fae6e3; color:#c0392b; }}
  .b-pending {{ background:#eef1f6; color:#6b7891; }}
  .sbadge {{ padding:2px 8px; border-radius:6px; font-size:11.5px; font-weight:700; }}
  .s-active {{ background:#e6f6ee; color:#158048; }}
  .s-warm {{ background:#e7f0fb; color:#2a63a8; }}
  .s-watch {{ background:#fdf3dc; color:#a9760a; }}
  .s-rest {{ background:#f3e9fb; color:#7b3fbf; }}
  .s-retire {{ background:#f0f2f5; color:#5a6577; text-decoration:line-through; }}
  .s-new {{ background:#eef1f6; color:#6b7891; }}
  .ok {{ color:#1a9c5b; font-weight:700; }}
  .warn {{ color:#a9760a; font-weight:700; }}
  .bad {{ color:#c0392b; font-weight:700; }}
</style></head>
<body>
  <div class="head">
    <h1>Caller ID Reputation Monitor</h1>
    <div class="sub">{_esc(biz)} &middot; Telnyx-native (L1 CDR + L2 Number Reputation)</div>
    <div class="upd">Last updated {updated}</div>
  </div>
  <div class="wrap">
    <div class="cards">{kpi_html}</div>

    <h2>How the system works</h2>
    <div class="rule"></div>
    <div class="layers">{layer_html}</div>

    <h2>Recommended dial pool</h2>
    <div class="rule"></div>
    {pool_panel}

    <h2>Number status ({monitored})</h2>
    <div class="rule"></div>
    <table>
      <thead><tr>
        <th>Number</th><th>State</th><th>Status</th>
        <th>L1 own traffic</th><th>L2 carrier</th><th>Last checked</th><th>Notes</th>
      </tr></thead>
      <tbody>{table}</tbody>
    </table>

    <h2>Remediation ledger ({len(open_rows)} open)</h2>
    <div class="rule"></div>
    <table>
      <thead><tr>
        <th>Number</th><th>Opened</th><th>Status</th><th>State</th><th>Result buckets</th>
      </tr></thead>
      <tbody>{led_table}</tbody>
    </table>

    <h2>Registration status</h2>
    <div class="rule"></div>
    {reg_panel}
  </div>
</body></html>"""
