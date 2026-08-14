"""export_html.py - render a coaching report .md as a styled standalone HTML
fragment for Google Drive upload (text/html converts to a styled Google Doc).
Mirrors the export_docs.py design system: navy headings, band-colored score
banner, shaded table headers, right-aligned numbers, unwrapped code fences.

USAGE: python src/call_coaching/export_html.py <report.md> [<out.html>]
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "call_coaching"

NAVY = "#1F3864"
LIGHT = "#EEF3FA"
TOTAL_FILL = "#DCE6F4"
BAND_COLORS = {"ELITE": "#1E7B34", "STRONG": "#2E7D32", "DEVELOPING": "#B26A00",
               "NEEDS WORK": "#C05000", "RETRAIN": "#B71C1C", "FAIL": "#B71C1C"}
NUM_CELL = re.compile(r"^-?\$?\d[\d,.]*\s*(%|/100|/5|s|pts?|min)?$|^N/A.*$|^--$", re.I)
CAPS_LABEL = re.compile(r"^([A-Z][A-Z0-9 ,#&/'-]{5,})(\s*\(.*\))?\s*:?\s*$")


def norm(t: str) -> str:
    t = re.sub(r"(\d(?:\.\d+)?)\s*/\s*100\b", r"\1/100", t)
    t = re.sub(r"(\d(?:\.\d+)?)\s*/\s*5\b", r"\1/5", t)
    return t


def band_of(t: str) -> str | None:
    for k in BAND_COLORS:
        if k in t.upper():
            return k
    return None


def inline(t: str) -> str:
    t = html.escape(norm(t), quote=False)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2" style="color:#0563C1">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r'<span style="font-family:Consolas,monospace">\1</span>', t)
    return t


def convert(md_path: Path) -> str:
    raw = md_path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
    full = "\n".join(lines)
    score = re.search(r"TOTAL[^\n]*?(\d+(?:\.\d+)?)\s*/\s*100", full)
    band = re.search(r"Grade band:\s*\[?([A-Za-z .#;()\d/-]+?)\]?\s*$", full, re.M)
    autofail = re.search(r"Auto-fail check:\s*(\S+)", full)
    call_id = re.match(r"^(\d{9,})_", md_path.name)
    call = None
    if call_id:
        log = json.loads((OUT / "call_log.json").read_text(encoding="utf-8"))
        call = next((c for c in log if str(c.get("call_id")) == call_id.group(1)), None)

    out: list[str] = [f'<div style="font-family:Calibri,Arial,sans-serif;font-size:10.5pt;color:#222">']
    table: list[str] = []
    meta: list[tuple[str, str]] = []
    title_done = banner_done = False
    in_list: str | None = None

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    def flush_table():
        nonlocal table
        rows = [r for r in table if not re.match(r"^\s*\|[\s:|-]+\|?\s*$", r)]
        table = []
        if not rows:
            return
        out.append('<table style="border-collapse:collapse;width:100%;font-size:9pt" border="1" cellpadding="4">')
        for ri, r in enumerate(rows):
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            is_total = any("TOTAL" in c.upper() for c in cells[:2])
            out.append("<tr>")
            for ci, c in enumerate(cells):
                c = norm(c)
                tag = "th" if ri == 0 else "td"
                style = "border:1px solid #999;padding:3px 6px;"
                if ri == 0:
                    style += f"background:{NAVY};color:#fff;text-align:left;"
                elif is_total:
                    style += f"background:{TOTAL_FILL};font-weight:bold;"
                if ri > 0 and ci > 0 and NUM_CELL.match(c):
                    style += "text-align:right;"
                b = band_of(c) if ci > 0 and len(c) <= 24 else None
                content = inline(c)
                if ri > 0 and b:
                    content = f'<b style="color:{BAND_COLORS[b]}">{content}</b>'
                out.append(f'<{tag} style="{style}">{content}</{tag}>')
            out.append("</tr>")
        out.append("</table><br>")

    def flush_meta():
        nonlocal meta, banner_done
        if meta:
            out.append('<table style="border-collapse:collapse;font-size:9pt" border="1" cellpadding="3">')
            for k, v in meta:
                out.append(f'<tr><td style="border:1px solid #999;background:{LIGHT};font-weight:bold;'
                           f'padding:2px 8px;white-space:nowrap">{html.escape(k)}</td>'
                           f'<td style="border:1px solid #999;padding:2px 8px">{inline(v)}</td></tr>')
            out.append("</table><br>")
            meta = []
        if not banner_done:
            parts = []
            if score:
                parts.append(f"Score: {score.group(1)}/100")
            if band:
                parts.append(f"Band: {band.group(1).strip()}")
            if autofail:
                parts.append(f"Auto-fail check: {autofail.group(1)}")
            if parts:
                fill = BAND_COLORS.get(band_of(band.group(1)) or "", NAVY) if band else NAVY
                out.append(f'<p style="background:{fill};color:#fff;font-weight:bold;font-size:13pt;'
                           f'padding:8px 12px">{" &nbsp;|&nbsp; ".join(parts)}</p>')
            banner_done = True

    grade_line = re.compile(r"^(Grade band|Auto-fail check|Outcome|Call length|Caller):", re.I)

    for ln in lines:
        s = ln.strip()
        if s.startswith("|"):
            close_list()
            table.append(s)
            continue
        if table:
            flush_table()
        if title_done and not banner_done and s.startswith("- "):
            m = re.match(r"^-\s+\**([^:*]+)\**:?\**\s*:?\s*(.*)$", s)
            if m:
                key = m.group(1).strip().rstrip(":")
                if key.lower() == "recording" and call and call.get("recording_url"):
                    continue
                meta.append((key[0].upper() + key[1:] if key.islower() else key, m.group(2).strip()))
                continue
        if title_done and meta and s:
            close_list()
            flush_meta()
        if not s:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            close_list()
            flush_meta()
            lvl = min(len(m.group(1)) + 1, 4)
            size = {2: "16pt", 3: "12.5pt", 4: "11pt"}[lvl]
            out.append(f'<h{lvl} style="color:{NAVY};font-size:{size};margin:14px 0 4px">{inline(re.sub(r"[*]{2}", "", m.group(2)))}</h{lvl}>')
            if not title_done:
                title_done = True
                if call and call.get("recording_url"):
                    local = OUT / "recordings" / f"{call['call_id']}.mp3"
                    out.append(f'<p style="background:{LIGHT};padding:6px 10px">&#9654; '
                               f'<a href="{call["recording_url"]}" style="color:#0563C1;font-weight:bold">'
                               f'Listen to this call (streaming MP3)</a><br>'
                               f'<span style="font-size:8pt;color:#6B7078">Local file: {local}</span></p>')
            continue
        cm = CAPS_LABEL.match(s)
        if cm and not s.startswith("- "):
            close_list()
            flush_meta()
            out.append(f'<h4 style="color:#404652;font-size:11pt;margin:12px 0 4px">{inline(cm.group(1).title() + (cm.group(2) or ""))}</h4>')
            continue
        if grade_line.match(s):
            close_list()
            key, _, val = s.partition(":")
            val = norm(val.strip())
            b = band_of(val)
            v = f'<b style="color:{BAND_COLORS[b]}">{inline(val)}</b>' if b else (
                f'<b style="color:{BAND_COLORS["STRONG"]}">{inline(val)}</b>' if "PASS" in val else inline(val))
            out.append(f"<p><b>{html.escape(key)}:</b> {v}</p>")
            continue
        if s.startswith("> "):
            close_list()
            out.append(f'<p style="margin-left:24px;color:#404652"><i>{inline(s[2:])}</i></p>')
            continue
        m = re.match(r"^\s*[-*]\s+(.*)", ln)
        if m:
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{inline(m.group(1))}</li>")
            continue
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)", ln)
        if m:
            close_list()
            out.append(f'<p style="margin:3px 0 3px 12px"><b>{m.group(1)}.</b> {inline(m.group(2))}</p>')
            continue
        close_list()
        out.append(f"<p>{inline(s)}</p>")

    close_list()
    if table:
        flush_table()
    flush_meta()
    out.append("</div>")
    return "\n".join(out)


if __name__ == "__main__":
    src = Path(sys.argv[1])
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".html")
    dest.write_text(convert(src), encoding="utf-8")
    print(f"{dest} ({dest.stat().st_size // 1024}KB)")
