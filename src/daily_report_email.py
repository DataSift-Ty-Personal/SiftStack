"""Daily Report email — the full SiftStack daily digest emailed to Aaron + Mike
with all deep-prospecting PDFs attached (zipped).

Same data as the Slack summary (scrape breakdown, deep-prospecting, auctions),
but rendered as a boxed, card/table HTML layout and with the actual DP PDFs
attached (Slack never attached them). Runs in the daily Apify push; no-ops
until SMTP creds are set (see auction_watch.send_email).
"""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from datetime import date

from auction_watch import send_email

logger = logging.getLogger(__name__)

MAX_ATTACH_BYTES = 24 * 1024 * 1024  # Gmail ~25MB cap

# ── HTML building blocks (email-safe inline styles) ──────────────────

def _card(title, inner, color="#2c3e50"):
    return (
        '<div style="border:1px solid #dcdcdc;border-radius:6px;margin:14px 0;overflow:hidden">'
        f'<div style="background:{color};color:#fff;padding:9px 14px;font-weight:bold;font-size:14px">{title}</div>'
        f'<div style="padding:14px">{inner}</div></div>'
    )

def _table(headers, rows):
    head = "".join(
        f'<th style="padding:6px 10px;text-align:left;background:#f0f2f5;'
        f'border-bottom:2px solid #dcdcdc;font-size:12px;color:#555">{h}</th>'
        for h in headers
    )
    return (f'<table style="border-collapse:collapse;width:100%;font-size:13px">'
            f'<tr>{head}</tr>{"".join(rows)}</table>')

def _row(cells, color=None):
    tds = "".join(
        f'<td style="padding:6px 10px;border-bottom:1px solid #eee;'
        f'{("color:"+color+";font-weight:bold;") if color else ""}">{c}</td>'
        for c in cells
    )
    return f"<tr>{tds}</tr>"

def _badges(pairs):
    """Inline count badges: [(label, count), ...]."""
    return " ".join(
        f'<span style="display:inline-block;background:#eef2f7;border:1px solid #dce3ec;'
        f'border-radius:12px;padding:3px 10px;margin:2px;font-size:12px">'
        f'{k} <b>{v}</b></span>' for k, v in pairs
    )


def build_report_html(notices, cost_breakdown=None, elapsed_min=0, pdf_note=""):
    from slack_notifier import _count_by_field, _upcoming_auctions
    total = len(notices)
    by_county = sorted(_count_by_field(notices, "county").items())
    by_type = sorted(_count_by_field(notices, "notice_type").items())
    deceased = [n for n in notices if getattr(n, "owner_deceased", "") == "yes"]
    high = [n for n in deceased if getattr(n, "dm_confidence", "") == "high"]
    heirs = [n for n in deceased if (getattr(n, "heir_map_json", "") or "").strip()]
    # Wide lookahead so the 30+-day-out "early signal" auctions appear (the
    # 7-day default hid exactly the ones Mike wants to see first).
    upcoming = _upcoming_auctions(notices, days=90)
    pct = round(len(deceased) / total * 100) if total else 0

    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px;margin:0 auto;color:#222">',
        f'<h2 style="margin:0 0 2px">SiftStack — Daily Report</h2>',
        f'<div style="color:#888;font-size:13px">{date.today():%A, %B %d, %Y}</div>',
        f'<div style="font-size:32px;font-weight:bold;color:#2c3e50;margin:10px 0 2px">{total}</div>'
        '<div style="color:#666;font-size:13px;margin-bottom:6px">new notices scraped</div>',
    ]

    # Scrape breakdown card
    breakdown = (
        '<div style="font-size:12px;color:#888;margin-bottom:4px">BY COUNTY</div>'
        + _badges([(c.title(), n) for c, n in by_county])
        + '<div style="font-size:12px;color:#888;margin:12px 0 4px">BY NOTICE TYPE</div>'
        + _badges([(t, n) for t, n in by_type])
    )
    parts.append(_card("Scrape Breakdown", breakdown))

    # Deep prospecting card — show ALL, not a capped selection (Mike: "show
    # them all vs selected one").
    dp_inner = [f'<b>{len(deceased)}</b> deceased owners <span style="color:#888">({pct}% of today)</span>']
    if high:
        rows = [_row([f"{n.address}, {n.city}",
                      (n.decision_maker_name or "?").strip(),
                      (n.decision_maker_relationship or "DM").strip()]) for n in high]
        dp_inner.append('<div style="margin:10px 0 4px;font-weight:bold;color:#c0392b">'
                        f'🔥 {len(high)} HIGH confidence — handwritten letter + ISA dial within 24h</div>')
        dp_inner.append(_table(["Address", "Decision Maker", "Relationship"], rows))
    if heirs:
        rows = [_row([f"{n.address}, {n.city}", (n.decision_maker_name or "").strip() or "—"]) for n in heirs]
        dp_inner.append('<div style="margin:12px 0 4px;font-weight:bold;color:#27713a">'
                        f'🌳 {len(heirs)} with heir maps — split-test multiple DMs</div>')
        dp_inner.append(_table(["Address", "Primary DM"], rows))
    parts.append(_card("Deep Prospecting", "".join(dp_inner), color="#6b3fa0"))

    # Auctions card — grouped near-vs-far. Mike wants the 30+-day-out ones
    # surfaced first (early signal = most runway to work the lead), THEN the
    # urgent next-7-day window. Widen the lookahead so the far ones actually
    # appear, and show ALL of them (no 7-day cutoff, no selection).
    def _auction_rows(items):
        out = []
        for a in items:
            d = a["days_out"]
            color = "#c0392b" if d <= 14 else ("#e67e22" if d <= 25 else None)
            out.append(_row([a["date"], f"{d}d", f'{a["address"]}, {a["city"]}',
                             a.get("type", "")], color=color))
        return out

    far = [a for a in upcoming if a["days_out"] > 7]     # early signal (incl. 30+)
    near = [a for a in upcoming if a["days_out"] <= 7]    # urgent
    if far or near:
        sections = []
        if far:
            sections.append(
                '<div style="font-weight:bold;color:#27713a;margin:2px 0 6px">'
                f'📅 Early signal — {len(far)} auctions 8+ days out (most runway)</div>'
                + _table(["Auction Date", "Out", "Address", "Type"], _auction_rows(far)))
        if near:
            sections.append(
                '<div style="font-weight:bold;color:#c0392b;margin:14px 0 6px">'
                f'⏰ Urgent — {len(near)} auctions in the next 7 days</div>'
                + _table(["Auction Date", "Out", "Address", "Type"], _auction_rows(near)))
        parts.append(_card(f"Upcoming Auctions ({len(upcoming)} total)",
                           "".join(sections), color="#b5651d"))

    # Footer: run stats + attachment note
    foot = []
    if elapsed_min:
        foot.append(f"Run time: <b>{elapsed_min:.0f} min</b>")
    if cost_breakdown:
        foot.append("Cost: " + ", ".join(f"{k} ${v:.2f}" for k, v in cost_breakdown.items()))
    if pdf_note:
        foot.append(pdf_note)
    if foot:
        parts.append('<div style="color:#888;font-size:12px;margin-top:16px;'
                     'border-top:1px solid #eee;padding-top:10px">' + " &nbsp;·&nbsp; ".join(foot) + "</div>")
    parts.append("</div>")
    return "".join(parts)


def _zip_attachments(pdf_urls, csv_paths):
    """Zip the daily DP PDFs (under reports/) AND the full data-pull CSVs
    (under data/) into one attachment. Mike used to pull the data CSVs out of
    Apify storage by hand every day — they now ride in the same zip as the PDFs.
    Returns (zip_path | None, n_pdfs, n_csvs)."""
    pdfs = [p["path"] for p in (pdf_urls or [])
            if p.get("path") and os.path.exists(p["path"])]
    csvs = [c for c in (csv_paths or []) if c and os.path.exists(str(c))]
    if not pdfs and not csvs:
        return None, 0, 0
    fd, zpath = tempfile.mkstemp(suffix=".zip", prefix="siftstack_daily_")
    os.close(fd)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pdfs:
            z.write(p, arcname=f"reports/{os.path.basename(p)}")
        for c in csvs:
            z.write(str(c), arcname=f"data/{os.path.basename(str(c))}")
    return zpath, len(pdfs), len(csvs)


def run_daily_report_email(notices, pdf_urls=None, csv_paths=None,
                           cost_breakdown=None, elapsed_min=0, recipients=None):
    """Build + send the boxed daily report email with DP PDFs + the full
    data-pull CSVs attached (zipped). No-ops if email unconfigured."""
    zpath, npdf, ncsv = _zip_attachments(pdf_urls, csv_paths)
    attachments, pdf_note = [], ""
    if zpath:
        size = os.path.getsize(zpath)
        if size <= MAX_ATTACH_BYTES:
            attachments = [zpath]
            bits = []
            if npdf:
                bits.append(f"{npdf} deep-prospecting PDFs")
            if ncsv:
                bits.append(f"{ncsv} data-pull CSVs")
            pdf_note = f"📎 {' + '.join(bits)} attached ({size // 1024} KB zip)"
        else:
            pdf_note = (f"⚠ Daily zip ({size // (1024*1024)} MB) too large to attach "
                        "— see Drive/KVS links")

    html = build_report_html(notices, cost_breakdown=cost_breakdown,
                             elapsed_min=elapsed_min, pdf_note=pdf_note)
    subject = f"SiftStack Daily Report — {len(notices)} notices ({date.today():%b %d})"
    try:
        res = send_email(subject, html, recipients=recipients, attachments=attachments)
    finally:
        if zpath:
            try:
                os.remove(zpath)
            except OSError:
                pass
    res["pdfs_attached"] = npdf if attachments else 0
    res["csvs_attached"] = ncsv if attachments else 0
    logger.info("Daily report email: sent=%s, %d PDFs + %d CSVs attached",
                res.get("sent"), res["pdfs_attached"], res["csvs_attached"])
    return res
