"""Render a deep-prospecting research pack (markdown) to a branded PDF.

Deep-prospecting deliverables ship as PDF so they upload cleanly into DataSift /
Sift (and read well as an attachment) instead of a raw .md file. This is a
dependency-light markdown -> PDF renderer built on reportlab (already a project
dep); no markdown/weasyprint/wkhtmltopdf needed.

Supports the subset the packs use: # / ## / ### headings, **bold**, `code`,
- bullets (nested by indent), | pipe tables |, ``` fenced code blocks ``` (kept
monospace -- critical for the heir map + master dial sheet), and --- rules.

Two house rules baked in:
  - Zero em/en dashes in the output (project style). All dash characters and a
    handful of non-WinAnsi glyphs are transliterated to ASCII the core PDF fonts
    can actually render, so the heir map and dial sheet don't show tofu boxes.

CLI:
    python src/deep_prospect_pdf.py output/deep_prospect/<pack>.md [out.pdf]
"""

import argparse
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── Brand palette (matches report_generator.py) ──────────────────────
BRAND_DARK = colors.HexColor("#1a1a2e")
BRAND_ACCENT = colors.HexColor("#0f3460")
BRAND_HIGHLIGHT = colors.HexColor("#e94560")
BRAND_MUTED = colors.HexColor("#7f8c8d")
CODE_BG = colors.HexColor("#f5f6fa")
ROW_ALT = colors.HexColor("#f8f9fa")
BORDER = colors.HexColor("#dcdde1")

_ss = getSampleStyleSheet()
TITLE = ParagraphStyle("dp_title", parent=_ss["Title"], fontName="Helvetica-Bold",
                       fontSize=18, leading=22, textColor=BRAND_DARK, spaceAfter=4)
H2 = ParagraphStyle("dp_h2", parent=_ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=13, leading=16, textColor=BRAND_ACCENT, spaceBefore=12, spaceAfter=4)
H3 = ParagraphStyle("dp_h3", parent=_ss["Heading3"], fontName="Helvetica-Bold",
                    fontSize=11, leading=14, textColor=BRAND_DARK, spaceBefore=8, spaceAfter=3)
BODY = ParagraphStyle("dp_body", parent=_ss["Normal"], fontName="Helvetica",
                      fontSize=9.5, leading=13, textColor=colors.black, alignment=TA_LEFT, spaceAfter=3)
BULLET = ParagraphStyle("dp_bullet", parent=BODY, leftIndent=14, bulletIndent=4, spaceAfter=2)
BULLET2 = ParagraphStyle("dp_bullet2", parent=BODY, leftIndent=28, bulletIndent=18, spaceAfter=1)
CODE = ParagraphStyle("dp_code", parent=_ss["Code"], fontName="Courier", fontSize=7.2,
                      leading=8.4, textColor=BRAND_DARK, backColor=CODE_BG,
                      borderPadding=6, spaceBefore=4, spaceAfter=6)
CELL = ParagraphStyle("dp_cell", parent=BODY, fontSize=8, leading=10, spaceAfter=0)
CELL_H = ParagraphStyle("dp_cell_h", parent=CELL, fontName="Helvetica-Bold", textColor=colors.white)
FOOT = ParagraphStyle("dp_foot", parent=BODY, fontSize=7.5, textColor=BRAND_MUTED, spaceBefore=8)

# Transliterate dashes + non-WinAnsi glyphs the core PDF fonts can't render.
_TRANS = {
    "—": "-", "–": "-",           # em/en dash -> hyphen (project style)
    "†": "X", "✓": "v", "✔": "v",  # dagger, checkmarks
    "▸": ">", "►": ">", "‣": ">",   # triangle/pointer -> recommended DM
    "●": "o", "★": "*", "⚠": "!",    # filled circle, star, warning
    "→": "->", "≥": ">=", "≤": "<=",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ",
}

def _sanitize(s: str) -> str:
    for k, v in _TRANS.items():
        s = s.replace(k, v)
    return s

def _inline(text: str) -> str:
    """Escape XML then apply **bold** and `code` as reportlab markup."""
    t = _sanitize(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\*(.+?)\*", r"<i>\1</i>", t)
    t = re.sub(r"`(.+?)`", r'<font face="Courier" size="8">\1</font>', t)
    return t


def _split_row(row: str):
    cells = row.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _make_table(rows, avail_width):
    parsed = [_split_row(r) for r in rows if r.strip()]
    # Drop the |---|---| separator row.
    parsed = [r for r in parsed if not all(set(c) <= set("-: ") for c in r)]
    if not parsed:
        return None
    ncol = max(len(r) for r in parsed)
    parsed = [r + [""] * (ncol - len(r)) for r in parsed]
    # Column widths proportional to header length, clamped to a minimum.
    header = parsed[0]
    weights = [max(6, len(h)) for h in header]
    total = sum(weights)
    widths = [max(0.7 * inch, avail_width * w / total) for w in weights]
    # Rescale to fit exactly.
    scale = avail_width / sum(widths)
    widths = [w * scale for w in widths]

    data = [[Paragraph(_inline(c), CELL_H if ri == 0 else CELL) for c in row]
            for ri, row in enumerate(parsed)]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for ri in range(1, len(data)):
        if ri % 2 == 0:
            style.append(("BACKGROUND", (0, ri), (-1, ri), ROW_ALT))
    t.setStyle(TableStyle(style))
    return t


def md_to_story(md: str, avail_width: float):
    lines = md.split("\n")
    story = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block -> Preformatted (monospace, spacing preserved).
        if stripped.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(_sanitize(lines[i]))
                i += 1
            i += 1
            story.append(Preformatted("\n".join(block) or " ", CODE))
            continue

        # Pipe table.
        if stripped.startswith("|") and stripped.count("|") >= 2:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            tbl = _make_table(rows, avail_width)
            if tbl is not None:
                story.append(Spacer(1, 2))
                story.append(tbl)
                story.append(Spacer(1, 4))
            continue

        if not stripped:
            story.append(Spacer(1, 4))
            i += 1
            continue

        if stripped.startswith("# "):
            story.append(Paragraph(_inline(stripped[2:]), TITLE))
        elif stripped.startswith("## "):
            story.append(Paragraph(_inline(stripped[3:]), H2))
        elif stripped.startswith("### "):
            story.append(Paragraph(_inline(stripped[4:]), H3))
        elif set(stripped) <= set("-*_") and len(stripped) >= 3:
            story.append(HRFlowable(width="100%", thickness=0.6, color=BORDER,
                                    spaceBefore=4, spaceAfter=6))
        elif re.match(r"^\s*[-*] ", line):
            indent = len(line) - len(line.lstrip())
            body = re.sub(r"^\s*[-*] ", "", line)
            story.append(Paragraph(_inline(body), BULLET2 if indent >= 2 else BULLET,
                                   bulletText="•"))
        elif stripped.startswith("*") and stripped.endswith("*") and stripped.count("*") == 2:
            story.append(Paragraph(_inline(stripped), FOOT))
        else:
            story.append(Paragraph(_inline(stripped), BODY))
        i += 1
    return story


def generate_pdf(md_path, pdf_path=None) -> str:
    md_path = Path(md_path)
    md = md_path.read_text(encoding="utf-8")
    if pdf_path is None:
        pdf_path = md_path.with_suffix(".pdf")
    pdf_path = Path(pdf_path)

    margin = 0.6 * inch
    avail = letter[0] - 2 * margin
    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=letter,
        leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=0.55 * inch,
        title=md_path.stem, author="SiftStack Deep Prospecting",
    )

    def _footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(BRAND_MUTED)
        canvas.drawString(margin, 0.35 * inch,
                          "SiftStack Deep Prospecting (V4) - grounded in API / county-record / obituary sources; confidential")
        canvas.drawRightString(letter[0] - margin, 0.35 * inch, f"Page {d.page}")
        canvas.restoreState()

    doc.build(md_to_story(md, avail), onFirstPage=_footer, onLaterPages=_footer)
    return str(pdf_path)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Render a deep-prospecting markdown pack to PDF")
    ap.add_argument("md", help="path to the research-pack .md")
    ap.add_argument("pdf", nargs="?", default=None, help="output .pdf (default: same name)")
    args = ap.parse_args(argv)
    out = generate_pdf(args.md, args.pdf)
    print(f"Wrote {out}")


if __name__ == "__main__":
    _main()
