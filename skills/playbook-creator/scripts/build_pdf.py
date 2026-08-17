#!/usr/bin/env python3
"""
build_pdf.py — Converts a playbook/SOP markdown file into a polished PDF.

Extracts Mermaid code blocks, renders them as PNG images via mmdc (Mermaid CLI),
then builds a formatted PDF using ReportLab with the images embedded inline.

Usage:
    python build_pdf.py input.md output.pdf [--title "My Playbook"]
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether, ListFlowable, ListItem
)
from reportlab.lib import colors


# ---------------------------------------------------------------------------
# Mermaid rendering
# ---------------------------------------------------------------------------

def find_mmdc():
    """Find the mmdc binary."""
    # Check local node_modules first
    local = os.path.join(os.getcwd(), "node_modules", ".bin", "mmdc")
    if os.path.exists(local):
        return local
    # Check common install locations
    for path in [
        "/sessions/confident-gallant-pasteur/node_modules/.bin/mmdc",
        os.path.expanduser("~/.npm-global/bin/mmdc"),
    ]:
        if os.path.exists(path):
            return path
    # Fall back to PATH
    result = subprocess.run(["which", "mmdc"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def render_mermaid(code: str, output_path: str, mmdc_path: str) -> bool:
    """Render a Mermaid code block to a PNG file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mmd", delete=False) as f:
        f.write(code)
        mmd_path = f.name

    try:
        result = subprocess.run(
            [mmdc_path, "-i", mmd_path, "-o", output_path,
             "-b", "white", "-w", "1200", "-s", "2"],
            capture_output=True, text=True, timeout=30
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"  Warning: Mermaid render failed: {e}", file=sys.stderr)
        return False
    finally:
        os.unlink(mmd_path)


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def parse_markdown(md_text: str):
    """
    Parse markdown into a list of content blocks.
    Each block is a dict with 'type' and content fields.
    """
    blocks = []
    lines = md_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Mermaid code block
        if line.strip().startswith("```mermaid"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({"type": "mermaid", "code": "\n".join(code_lines)})
            i += 1
            continue

        # Generic code block
        if line.strip().startswith("```"):
            lang = line.strip().lstrip("`").strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append({"type": "code", "lang": lang, "code": "\n".join(code_lines)})
            i += 1
            continue

        # Horizontal rule
        if line.strip() in ("---", "***", "___"):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # Headers
        header_match = re.match(r"^(#{1,4})\s+(.*)", line)
        if header_match:
            level = len(header_match.group(1))
            text = header_match.group(2).strip()
            blocks.append({"type": "heading", "level": level, "text": text})
            i += 1
            continue

        # Table
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
            table_lines = []
            while i < len(lines) and "|" in lines[i]:
                stripped = lines[i].strip()
                if re.match(r"^\|[\s\-:|]+\|$", stripped):
                    i += 1
                    continue
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                table_lines.append(cells)
                i += 1
            blocks.append({"type": "table", "rows": table_lines})
            continue

        # Blockquote
        if line.strip().startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "blockquote", "text": "\n".join(quote_lines)})
            continue

        # Checkbox list
        if re.match(r"^\s*[-*]\s*\[[ x]\]", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s*\[[ x]\]", lines[i]):
                checked = "[x]" in lines[i].lower()
                text = re.sub(r"^\s*[-*]\s*\[[ x]\]\s*", "", lines[i])
                items.append({"checked": checked, "text": text})
                i += 1
            blocks.append({"type": "checklist", "items": items})
            continue

        # Numbered list
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                text = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                items.append(text)
                i += 1
            blocks.append({"type": "numbered_list", "items": items})
            continue

        # Bullet list
        if re.match(r"^\s*[-*]\s+", line) and not re.match(r"^\s*[-*]\s*\[", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                text = re.sub(r"^\s*[-*]\s+", "", lines[i])
                items.append(text)
                i += 1
            blocks.append({"type": "bullet_list", "items": items})
            continue

        # Regular paragraph
        if line.strip():
            para_lines = []
            while i < len(lines) and lines[i].strip() and not any([
                lines[i].strip().startswith("#"),
                lines[i].strip().startswith("```"),
                lines[i].strip() in ("---", "***", "___"),
                lines[i].strip().startswith(">"),
                re.match(r"^\s*\d+\.\s+", lines[i]),
                re.match(r"^\s*[-*]\s+", lines[i]),
                "|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1],
            ]):
                para_lines.append(lines[i].strip())
                i += 1
            blocks.append({"type": "paragraph", "text": " ".join(para_lines)})
            continue

        i += 1

    return blocks


# ---------------------------------------------------------------------------
# Markdown inline formatting to ReportLab XML
# ---------------------------------------------------------------------------

def md_inline(text: str) -> str:
    """Convert inline markdown (bold, italic, code) to ReportLab XML."""
    # Escape XML entities first
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    # Inline code
    text = re.sub(r"`(.+?)`", r'<font face="Courier" size="9">\1</font>', text)
    # Arrow →
    text = text.replace("→", "->")
    return text


# ---------------------------------------------------------------------------
# PDF building
# ---------------------------------------------------------------------------

def build_pdf(md_text: str, output_path: str, title: str = None):
    """Build a PDF from parsed markdown content."""

    mmdc = find_mmdc()
    if not mmdc:
        print("Warning: mmdc not found. Mermaid diagrams will be shown as code.", file=sys.stderr)

    blocks = parse_markdown(md_text)

    # Set up styles
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "PlaybookTitle", parent=styles["Title"],
        fontSize=24, spaceAfter=6, textColor=HexColor("#1a1a2e"),
        fontName="Helvetica-Bold"
    ))
    styles.add(ParagraphStyle(
        "H2Custom", parent=styles["Heading2"],
        fontSize=16, spaceBefore=18, spaceAfter=8,
        textColor=HexColor("#1a1a2e"), fontName="Helvetica-Bold",
        borderWidth=0, borderPadding=0, borderColor=None,
    ))
    styles.add(ParagraphStyle(
        "H3Custom", parent=styles["Heading3"],
        fontSize=13, spaceBefore=12, spaceAfter=6,
        textColor=HexColor("#2d3436"), fontName="Helvetica-Bold"
    ))
    styles.add(ParagraphStyle(
        "H4Custom", parent=styles["Heading4"],
        fontSize=11, spaceBefore=8, spaceAfter=4,
        textColor=HexColor("#2d3436"), fontName="Helvetica-BoldOblique"
    ))
    styles.add(ParagraphStyle(
        "BodyCustom", parent=styles["Normal"],
        fontSize=10, leading=14, spaceAfter=6,
        fontName="Helvetica"
    ))
    styles.add(ParagraphStyle(
        "BlockquoteStyle",
        parent=styles["Normal"],
        fontSize=10, leading=14, leftIndent=20, spaceAfter=8, spaceBefore=4,
        textColor=HexColor("#2d3436"), fontName="Helvetica-Oblique",
        borderWidth=0, borderPadding=6,
        backColor=HexColor("#f0f7ff"),
    ))
    styles.add(ParagraphStyle(
        "CodeStyle", parent=styles["Code"],
        fontSize=8, leading=10, leftIndent=12, spaceAfter=8,
        backColor=HexColor("#f5f5f5"), fontName="Courier",
        borderWidth=0, borderPadding=6
    ))
    styles.add(ParagraphStyle(
        "ProTipStyle", parent=styles["Normal"],
        fontSize=10, leading=14, leftIndent=20, spaceAfter=8, spaceBefore=4,
        textColor=HexColor("#2d6a4f"), fontName="Helvetica-Oblique",
        backColor=HexColor("#e8f5e9"), borderPadding=6
    ))
    styles.add(ParagraphStyle(
        "ScreenshotStyle", parent=styles["Normal"],
        fontSize=9, leading=12, leftIndent=20, spaceAfter=8, spaceBefore=4,
        textColor=HexColor("#555555"), fontName="Helvetica-Oblique",
        backColor=HexColor("#fff8e1"), borderPadding=6
    ))
    styles.add(ParagraphStyle(
        "Motto", parent=styles["Normal"],
        fontSize=12, leading=16, spaceAfter=12,
        textColor=HexColor("#636e72"), fontName="Helvetica-Oblique",
        alignment=TA_CENTER
    ))

    # Build the document
    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.75*inch, bottomMargin=0.75*inch,
        title=title or "Playbook"
    )

    story = []
    mermaid_count = 0
    tmp_dir = tempfile.mkdtemp()

    for block in blocks:
        btype = block["type"]

        if btype == "heading":
            level = block["level"]
            text = md_inline(block["text"])
            if level == 1:
                story.append(Spacer(1, 20))
                story.append(Paragraph(text, styles["PlaybookTitle"]))
                story.append(HRFlowable(
                    width="100%", thickness=2,
                    color=HexColor("#1a1a2e"), spaceAfter=12
                ))
            elif level == 2:
                story.append(Spacer(1, 6))
                story.append(Paragraph(text, styles["H2Custom"]))
                story.append(HRFlowable(
                    width="100%", thickness=0.5,
                    color=HexColor("#dfe6e9"), spaceAfter=6
                ))
            elif level == 3:
                story.append(Paragraph(text, styles["H3Custom"]))
            else:
                story.append(Paragraph(text, styles["H4Custom"]))

        elif btype == "paragraph":
            text = md_inline(block["text"])
            # Detect motto lines (italic wrapped in *)
            if block["text"].startswith("*") and block["text"].endswith("*") and len(block["text"]) < 200:
                clean = block["text"].strip("*")
                story.append(Paragraph(md_inline(clean), styles["Motto"]))
            else:
                story.append(Paragraph(text, styles["BodyCustom"]))

        elif btype == "blockquote":
            text = block["text"]
            # Convert inline markdown FIRST, then add line breaks
            # (md_inline escapes < and > which would break <br/> tags)
            lines_list = text.split("\n")
            converted_lines = [md_inline(l) for l in lines_list]
            formatted = "<br/>".join(converted_lines)
            # Detect screenshot placeholders
            if "SCREENSHOT:" in text:
                story.append(Paragraph(formatted, styles["ScreenshotStyle"]))
            elif "Pro Tip:" in text:
                story.append(Paragraph(formatted, styles["ProTipStyle"]))
            else:
                story.append(Paragraph(formatted, styles["BlockquoteStyle"]))

        elif btype == "mermaid":
            mermaid_count += 1
            png_path = os.path.join(tmp_dir, f"mermaid_{mermaid_count}.png")
            if mmdc and render_mermaid(block["code"], png_path, mmdc):
                try:
                    img = Image(png_path)
                    # Scale to fit page width
                    max_w = 6.5 * inch
                    max_h = 4.5 * inch
                    w, h = img.imageWidth, img.imageHeight
                    if w > 0 and h > 0:
                        ratio = min(max_w / w, max_h / h)
                        img._restrictSize(w * ratio, h * ratio)
                    story.append(Spacer(1, 6))
                    story.append(img)
                    story.append(Spacer(1, 6))
                except Exception as e:
                    print(f"  Warning: Could not embed image: {e}", file=sys.stderr)
                    story.append(Paragraph(
                        f'<font face="Courier" size="8">{block["code"][:500]}</font>',
                        styles["CodeStyle"]
                    ))
            else:
                # Fallback: show as code
                code_text = block["code"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(
                    f'<font face="Courier" size="8">[Flowchart — renders in Mermaid-compatible viewers]<br/>{code_text[:500]}</font>',
                    styles["CodeStyle"]
                ))

        elif btype == "code":
            code_text = block["code"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            code_text = code_text.replace("\n", "<br/>")
            story.append(Paragraph(
                f'<font face="Courier" size="8">{code_text}</font>',
                styles["CodeStyle"]
            ))

        elif btype == "table":
            rows = block["rows"]
            if not rows:
                continue
            # Convert markdown in cells
            table_data = []
            for row in rows:
                table_data.append([
                    Paragraph(md_inline(cell), styles["BodyCustom"]) for cell in row
                ])

            col_count = len(table_data[0]) if table_data else 1
            avail_width = 7.0 * inch
            col_width = avail_width / col_count

            t = Table(table_data, colWidths=[col_width] * col_count)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1a1a2e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
                ("TOPPADDING", (0, 1), (-1, -1), 5),
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#dfe6e9")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, HexColor("#f8f9fa")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(Spacer(1, 4))
            story.append(t)
            story.append(Spacer(1, 4))

        elif btype == "numbered_list":
            for idx, item in enumerate(block["items"], 1):
                text = md_inline(item)
                story.append(Paragraph(
                    f"<b>{idx}.</b>  {text}",
                    ParagraphStyle("ListItem", parent=styles["BodyCustom"], leftIndent=20)
                ))

        elif btype == "bullet_list":
            for item in block["items"]:
                text = md_inline(item)
                story.append(Paragraph(
                    f"&#8226;  {text}",
                    ParagraphStyle("BulletItem", parent=styles["BodyCustom"], leftIndent=20)
                ))

        elif btype == "checklist":
            for item in block["items"]:
                check = "&#9745;" if item["checked"] else "&#9744;"
                text = md_inline(item["text"])
                story.append(Paragraph(
                    f"{check}  {text}",
                    ParagraphStyle("CheckItem", parent=styles["BodyCustom"], leftIndent=20)
                ))

        elif btype == "hr":
            story.append(Spacer(1, 6))
            story.append(HRFlowable(
                width="100%", thickness=0.5,
                color=HexColor("#dfe6e9"), spaceAfter=6, spaceBefore=6
            ))

    # Build
    doc.build(story)
    print(f"PDF created: {output_path}")

    # Cleanup temp files
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert playbook/SOP markdown to PDF")
    parser.add_argument("input", help="Input markdown file")
    parser.add_argument("output", help="Output PDF file")
    parser.add_argument("--title", help="Document title", default=None)
    args = parser.parse_args()

    with open(args.input, "r") as f:
        md_text = f.read()

    build_pdf(md_text, args.output, title=args.title)
