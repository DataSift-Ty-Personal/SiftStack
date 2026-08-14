"""add_property_column.py - backfill a PROPERTY column (address linked to the
reisift record) into every coaching report table + per-call header, so a row is
identifiable without opening the call.

For every markdown table whose first column is Call ID, inserts a second column
"Property (opens record)" holding [<address or owner>](<reisift record url>).
Per-call report headers get a "- property:" line above the reisift line.
Idempotent: skips files that already have the Property column / property line.

Sources: output/call_coaching/record_map.json (address+owner+url per graded call),
with call_log.json fallback for any other call id (gate-table wrong numbers, VMs).

USAGE (from SiftStack root, venv python):
  python src/call_coaching/add_property_column.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "call_coaching"
REPORTS = OUT / "reports"
SKIP = {"docx", "_archive_wrong_rubric"}
PROP_HEADER = "Property (opens record)"

CALL_CELL = re.compile(r"\[(\d{9,})\]\(([^)\s]+)\)")


def build_lookup() -> dict:
    rec = json.loads((OUT / "record_map.json").read_text(encoding="utf-8"))
    log = {str(c["call_id"]): c for c in
           json.loads((OUT / "call_log.json").read_text(encoding="utf-8"))}
    out = {}
    all_ids = set(rec) | set(log)
    for cid in all_ids:
        r = rec.get(cid, {})
        c = log.get(cid, {})
        url = r.get("reisift_url") or c.get("reisift_record_url")
        label = (r.get("address") or r.get("owner") or r.get("contact")
                 or c.get("contact_name") or "record")
        out[cid] = {"label": str(label), "url": url}
    return out


def prop_cell(cid: str, look: dict) -> str:
    info = look.get(cid)
    if not info:
        return "-"
    label = info["label"].replace("|", "/").strip()
    url = info["url"]
    return f"[{label}]({url})" if url else label


def patch_tables(text: str, look: dict) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        # a table starts with a row that begins '|' and contains 'Call ID'
        if ln.lstrip().startswith("|") and "call id" in ln.lower():
            # collect the full table block
            block = []
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                block.append(lines[j])
                j += 1
            if PROP_HEADER in block[0]:
                out.extend(block)  # already patched
            else:
                out.extend(inject_column(block, look))
            i = j
            continue
        out.append(ln)
        i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def split_row(row: str) -> list[str]:
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def inject_column(block: list[str], look: dict) -> list[str]:
    result = []
    for idx, row in enumerate(block):
        cells = split_row(row)
        if idx == 0:  # header
            cells.insert(1, f" {PROP_HEADER} ")
        elif idx == 1 and re.match(r"^[\s:|-]+$", "|".join(cells)):  # separator
            cells.insert(1, "---")
        else:  # data row
            m = CALL_CELL.search(cells[0])
            cells.insert(1, f" {prop_cell(m.group(1), look) if m else '-'} ")
        result.append("|" + "|".join(cells) + "|")
    return result


HEADER_REISIFT = re.compile(r"^- reisift record: (\S+)")


def patch_header(text: str, look: dict, cid: str) -> str:
    if "\n- property:" in text or "\n- property " in text:
        return text
    lines = text.splitlines()
    info = look.get(cid)
    if not info or not info.get("url"):
        return text
    prop_line = f"- property: [{info['label']}]({info['url']})"
    out = []
    inserted = False
    for ln in lines:
        if not inserted and (ln.startswith("- reisift record:") or ln.startswith("- Recording:")):
            out.append(prop_line)
            inserted = True
        out.append(ln)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def main() -> int:
    look = build_lookup()
    patched = 0
    for md in sorted(REPORTS.rglob("*.md")):
        rel = md.relative_to(REPORTS)
        if any(p in SKIP for p in rel.parts):
            continue
        text = md.read_text(encoding="utf-8")
        new = patch_tables(text, look)
        m = re.match(r"^(\d{9,})_", md.name)
        if m:
            new = patch_header(new, look, m.group(1))
        if new != text:
            md.write_text(new, encoding="utf-8")
            patched += 1
    print(f"Patched {patched} report files with the Property column / header line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
