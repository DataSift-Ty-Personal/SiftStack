"""The --full-report workbook for validate_phones.py: validation_report.xlsx with the tier and score charts.

    from generate_report import create_xlsx_report
    path = create_xlsx_report(results, errors, tiers, output_dir)

Sheets: Summary (counts, tier and line-type tables, two bar charts), Results (every number scored), Errors (when
any number failed). Needs openpyxl (pip install openpyxl); nothing else, and no network.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime

NAVY, BLUE, LINE, SOFT = "0A1130", "316AFF", "D9DEE8", "F4F6FA"
RESULT_COLUMNS = [("Phone Number", "phone_number"), ("Activity Score", "activity_score"), ("Dial Tier", "assigned_tag"),
                  ("Line Type", "line_type"), ("Carrier", "carrier"), ("Valid", "is_valid"), ("Prepaid", "is_prepaid"),
                  ("Litigator Risk", "is_litigator_risk")]


def _rows(results: list, tiers: dict) -> tuple[list, list, list, int, float]:
    total = len(results)
    scores = [r["activity_score"] for r in results if isinstance(r.get("activity_score"), (int, float))]
    tags = Counter(r.get("assigned_tag") for r in results)
    order = list(tiers.keys()) + sorted(t for t in tags if t not in tiers and t)
    tier_rows = [(t, tags.get(t, 0), (tags.get(t, 0) / total) if total else 0.0) for t in order]
    lines = Counter((r.get("line_type") or "Unknown") for r in results)
    line_rows = [(k, n, (n / total) if total else 0.0) for k, n in lines.most_common()]
    buckets = Counter(min(int(s) // 10 * 10, 90) for s in scores)
    score_rows = [("%d-%d" % (b, b + 9 if b < 90 else 100), buckets.get(b, 0)) for b in range(0, 100, 10)]
    avg = (sum(scores) / len(scores)) if scores else 0.0
    return tier_rows, line_rows, score_rows, total, avg


def create_xlsx_report(results: list, errors: list, tiers: dict, output_dir: str) -> str:
    """Write output_dir/validation_report.xlsx and return its path."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    head = Font(bold=True, color=NAVY)
    fill = PatternFill("solid", start_color=SOFT, end_color=SOFT)
    rule = Border(bottom=Side(style="thin", color=LINE))
    tier_rows, line_rows, score_rows, total, avg = _rows(results, tiers)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Phone validation report"])
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws.append(["Built %s" % datetime.now().strftime("%Y-%m-%d %H:%M")])
    ws.append([])
    for label, value in (("Numbers scored", total), ("Numbers that failed", len(errors)),
                         ("Average activity score", round(avg, 1))):
        ws.append([label, value])
    ws.append([])

    def table(title: str, header: list, rows: list, pct_col: int | None) -> tuple[int, int]:
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True, color=NAVY)
        ws.append(header)
        for c in ws[ws.max_row]:
            c.font, c.fill, c.border = head, fill, rule
        first = ws.max_row + 1
        for r in rows:
            ws.append(list(r))
            if pct_col is not None:
                ws.cell(ws.max_row, pct_col).number_format = "0.0%"
        last = ws.max_row
        ws.append([])
        return first, last

    t0, t1 = table("Dial tiers", ["Tier", "Numbers", "Share"], tier_rows, 3)
    table("Line types", ["Line type", "Numbers", "Share"], line_rows, 3)
    s0, s1 = table("Activity scores", ["Score", "Numbers"], score_rows, None)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 10

    def chart(title: str, r0: int, r1: int, anchor: str) -> None:
        if r1 < r0:
            return
        ch = BarChart()
        ch.type, ch.title, ch.legend = "bar", title, None
        ch.height, ch.width = 7, 14
        ch.add_data(Reference(ws, min_col=2, min_row=r0 - 1, max_row=r1), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=r0, max_row=r1))
        ch.series[0].graphicalProperties.solidFill = BLUE
        ch.series[0].graphicalProperties.line.solidFill = BLUE
        ws.add_chart(ch, anchor)

    chart("Numbers by dial tier", t0, t1, "E3")
    chart("Numbers by activity score", s0, s1, "E19")

    rs = wb.create_sheet("Results")
    rs.append([h for h, _k in RESULT_COLUMNS])
    for c in rs[1]:
        c.font, c.fill, c.border = head, fill, rule
    for r in results:
        rs.append([r.get(k) for _h, k in RESULT_COLUMNS])
    rs.freeze_panes = "A2"
    for i, (h, _k) in enumerate(RESULT_COLUMNS, 1):
        rs.column_dimensions[rs.cell(1, i).column_letter].width = max(12, len(h) + 4)
    if errors:
        es = wb.create_sheet("Errors")
        es.append(["Phone Number", "Error", "Detail"])
        for c in es[1]:
            c.font, c.fill, c.border = head, fill, rule
        for e in errors:
            es.append([e.get("phone_number", ""), e.get("error", ""), str(e.get("detail", ""))[:500]])
        es.column_dimensions["A"].width, es.column_dimensions["B"].width, es.column_dimensions["C"].width = 16, 30, 60
        for row in es.iter_rows(min_row=2):
            row[2].alignment = Alignment(wrap_text=True, vertical="top")

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "validation_report.xlsx")
    tmp = path + ".tmp.xlsx"
    wb.save(tmp)
    os.replace(tmp, path)
    return path
