"""The KPI tracker's Google Sheet, laid out like the DataSift KPI tracker. Layout only: no network.

Ty, 2026-11-23: "a Google Sheet that we essentially set up and create, mirroring the style of the DataSift
KPI tracker. It has everything in one place that we can then use to update the KPI, as opposed to the HTML
template." The DataSift tracker (hubspot-ops/kpi/sheet.py, the sheet "DataSift KPI tracker") is the model:

  * Every tab opens with its name as the title and one gray "Refreshed Mon Nov 23 at 3:33 PM ET" line, then
    its tables. No comma lists at the top (Ty, 2026-11-23: they read clunky). A metric table carries its
    sections as bold rows, the measures down the side and the periods across, newest first. Notes sit at
    the bottom in gray.
  * The design pass of 2026-11-23 (Ty: "all of the standard Google Sheet/Excel design upgrades"): gridlines
    off, a quiet header band (light fill, a medium rule under it), faint zebra rows on a long table, a rule
    over a total, rows with room to breathe, the grid trimmed to what it holds, a tab color by area, a
    benchmark cell holding the figure only. Nothing is frozen: Google refuses a merge across a frozen
    column, and the notes are merged across every table.
  * Numbers stay numbers: counts as 1,234, dollars as $1,234, shares as 12.5%. A mark is a word (on track,
    at risk, behind) in its color, never a color alone. A number nothing can read yet is gray, never 0.
    A record link reads "Open in DataSift".

New beside the DataSift tracker is the Goals tab (Kendrick Eid and Ty, 2026-11-23: "Combine inputs and goals",
"add pace to goals"): the numbers only the owner knows (the year's goal, the targets for a calling day, the
costs the ledger cannot see, each deal's fee), typed in yellow cells, each goal beside what it needs a month,
the month so far, the pace and the mark. The writer makes that tab once and never clears what a person
typed. Every run reads it first (parse_inputs here, the call in kpisinks.read_inputs), kpi.load() lays what
was typed over team/kpi.yaml, and the gray columns the brain writes there say back what every report used.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------- colors (DataSift's, as the page uses them)
INK = {"red": 0.039, "green": 0.067, "blue": 0.188}       # #0A1130 navy
MUTED = {"red": 0.29, "green": 0.333, "blue": 0.408}      # #4A5568
RULE = {"red": 0.82, "green": 0.85, "blue": 0.9}          # a 1px rule under a header
BLUE = {"red": 0.192, "green": 0.416, "blue": 1.0}        # #316AFF, links
GREEN = {"red": 0.106, "green": 0.62, "blue": 0.353}      # #1B9E5A, on track
GOLD = {"red": 0.718, "green": 0.475, "blue": 0.122}      # #B7791F, at risk and caveats
RED = {"red": 0.753, "green": 0.188, "blue": 0.188}       # #C03030, behind
YELLOW = {"red": 1.0, "green": 0.949, "blue": 0.8}        # #FFF2CC, a cell a person types in
CAVEAT = {"red": 0.549, "green": 0.353, "blue": 0.078}    # #8C5A14, a tab still showing old numbers
# 2026-11-23 design pass (Ty: "all of the standard Google Sheet/Excel design upgrades"): a quiet header band,
# faint zebra rows on long tables, a medium rule under each header, gridlines off.
INK2 = {"red": 0.176, "green": 0.216, "blue": 0.282}      # #2D3748, header text
HEAD_BG = {"red": 0.961, "green": 0.969, "blue": 0.984}   # #F5F7FB, DataSift's light background
ZEBRA = {"red": 0.98, "green": 0.984, "blue": 0.992}      # #FAFBFD, every other row of a long table
RULE_DARK = {"red": 0.627, "green": 0.682, "blue": 0.753}  # #A0AEC0, the rule under a header
NAVY_TAB = {"red": 0.039, "green": 0.067, "blue": 0.188}
GRAY_TAB = {"red": 0.627, "green": 0.682, "blue": 0.753}

FORMATS = {"int": "#,##0", "dec1": "#,##0.0", "dec2": "#,##0.00", "money": "$#,##0", "money2": "$#,##0.00",
           "pct": '0.0"%"'}
# Row heights in pixels: room to breathe without the Sheets default's cramped 21.
TITLE_PX, LINE_PX, HEAD_PX, SECTION_PX, ROW_PX = 36, 20, 30, 28, 23
ZEBRA_MIN = 6                    # a table of this many rows or more gets zebra rows

# The words a cell holds when there is no number to show: gray, so a gap never reads as a figure.
GAP_WORDS = ("not tracked yet", "not read this run", "-", "could not be read", "no figure in the catalog",
             "not judged yet", "not judged", "nothing yet", "not tracked")
MARKS = {"on track": GREEN, "at risk": GOLD, "behind": RED}
# A figure's settledness in brackets, set gray so the number reads first:
# "150 dials a day per caller (recommended, not yet confirmed, waits on D30)".
TAG_RX = re.compile(r"\s\((?:recommended|a placeholder|measured|typed)[^()]*\)\s*$")
URL_RX = re.compile(r"https?://\S+$")
SENTENCE_RX = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
# A first column whose cells repeat in runs is a section column: it becomes gray bold rows above its runs,
# the DataSift tracker's metric_table. "Section" is the Scoreboard's; the others group the same way.
FOLD_COLUMNS = ("Section", "Channel", "Pipeline", "Campaign")
TOTAL_LABELS = ("Team", "Total known ($)", "On the board", "Total")

# A tab's title is its name (Ty, 2026-11-23: no comma lists at the top, they read clunky). The scoreboard's
# comes from the stage's options (scoreboard_title).
TITLES: dict = {}
# Tab colors group the tabs by what they are for: the daily numbers navy, the pipeline blue, the money green,
# the one tab a person types in yellow, the reference tabs gray.
TAB_COLORS = {"Scoreboard": NAVY_TAB, "Today": NAVY_TAB, "Callers": NAVY_TAB, "Texts": NAVY_TAB,
              "Leads": BLUE, "Deals": BLUE, "First to Market": BLUE,
              "Direct Mail": GRAY_TAB, "Channels": GRAY_TAB, "Lists": GRAY_TAB, "How it is counted": GRAY_TAB,
              "Costs": GREEN, "Goals": YELLOW}
# Kendrick Eid and Ty, 2026-11-23: "Combine inputs and goals". The tab a person types in IS the Goals tab now:
# each goal beside what it needs a month, the month so far, the pace and the mark. An install made before
# that has the owner's tab under the old name; the writer renames it once (kpisinks, layout version 3).
INPUTS_TAB = "Goals"
LEGACY_INPUTS_TAB = "Inputs"
# Label, your number, needed (or target), so far, pace, mark, default, in use now. The deal table shares
# the columns: property, signed, type, price, expected fee, closed, fee collected, notes.
INPUTS_WIDTHS = (300, 120, 135, 135, 135, 110, 290, 200)
COMBINED_VERSION = 3             # 3: one Goals tab, the pace beside each goal (2026-11-23)
INPUTS_VERSION = 4               # 4: the Default column wide enough for "(recommended)" (widths only)


# ---------------------------------------------------------------------- the grid a tab becomes
@dataclass
class Grid:
    """One tab, ready for the Sheets API: the values as written (RAW), each styled cell's whole format,
    rich-text runs, merges, the row heights Google cannot work out itself, column widths."""
    name: str
    values: list = field(default_factory=list)
    fmt: dict = field(default_factory=dict)            # (row, col) -> userEnteredFormat
    runs: dict = field(default_factory=dict)           # (row, col) -> textFormatRuns
    merges: list = field(default_factory=list)         # (row0, row1, col0, col1)
    heights: dict = field(default_factory=dict)        # row -> pixels
    widths: list = field(default_factory=list)         # pixels a column
    validations: list = field(default_factory=list)    # (row0, row1, col0, col1, rule)
    tab_color: dict | None = None
    measure: list = field(default_factory=list)        # (row, col, text, numeric) the column widths read
    frozen_cols: int = 0                               # the label column stays put on a wide tab
    notes: dict = field(default_factory=dict)          # (row, col) -> a cell's hover note
    plain_rows: set = field(default_factory=set)       # rows of one line: they take ROW_PX, never Google's guess
    head_rows: set = field(default_factory=set)        # header rows: their height follows their longest label
    rows_at: dict = field(default_factory=dict)        # the Goals tab: key -> the row its label sits on

    @property
    def width(self) -> int:
        return max([len(r) for r in self.values] + [len(self.widths), 1])


def _fmt(size: int = 10, color: dict | None = None, bold: bool = False, wrap: bool = False, valign: str = "MIDDLE",
         number: str = "", halign: str = "", bg: dict | None = None, border=False, link: str = "",
         underline: bool = False, italic: bool = False, text: bool = False) -> dict:
    """A cell's whole format. Every styled cell carries the full set, so one write leaves nothing behind
    from an earlier run's layout. `border`: True or "rule" a 1px rule under the cell, "head" the medium rule
    under a header, "top" a 1px rule over a total, "box" a 1px box around a cell a person types in."""
    tf = {"fontSize": size, "foregroundColor": color or INK}
    if bold:
        tf["bold"] = True
    if italic:
        tf["italic"] = True
    if underline:
        tf["underline"] = True
    if link:
        tf["link"] = {"uri": link}
    f = {"textFormat": tf, "verticalAlignment": valign, "wrapStrategy": "WRAP" if wrap else "OVERFLOW_CELL"}
    if number:
        f["numberFormat"] = {"type": "NUMBER", "pattern": FORMATS[number]}
    elif text:
        f["numberFormat"] = {"type": "TEXT"}
    if halign:
        f["horizontalAlignment"] = halign
    if bg:
        f["backgroundColor"] = bg
    if border in (True, "rule"):
        f["borders"] = {"bottom": {"style": "SOLID", "color": RULE}}
    elif border == "head":
        f["borders"] = {"bottom": {"style": "SOLID_MEDIUM", "color": RULE_DARK}}
    elif border == "top":
        f["borders"] = {"top": {"style": "SOLID", "color": RULE}}
    elif border == "box":
        side = {"style": "SOLID", "color": RULE}
        f["borders"] = {"top": side, "bottom": side, "left": side, "right": side}
    return f


DEFAULT = _fmt()


# ---------------------------------------------------------------------- reading the snapshot's tab contract
@dataclass
class Block:
    title: str = ""
    header: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    lines: list = field(default_factory=list)          # a block of sentences (a heading over one-cell rows)


def _trim(row) -> list:
    r = ["" if v is None else v for v in (row or [])]
    while r and (r[-1] == "" or (isinstance(r[-1], str) and not r[-1].strip())):
        r.pop()
    return r


def blocks(tab: dict) -> list[Block]:
    """The tab's blocks, read the way the tracker writes them: a blank row ends a block; a block that
    starts with a one-cell row has that row as its heading, and the row after a heading names its columns
    when it has two cells or more (one-cell rows under a heading are sentences). The first block's header
    is the tab's own columns. A row whose first cell is "Section" is always a header."""
    out: list[Block] = []
    cur = Block(header=[str(c) for c in tab.get("columns") or []])
    after_heading = False
    for raw in tab.get("rows") or []:
        r = _trim(raw)
        if not r:
            if cur is not None and (cur.title or cur.header or cur.rows or cur.lines):
                out.append(cur)
            cur, after_heading = None, False
            continue
        if cur is None:
            if len(r) == 1 and isinstance(r[0], str):
                cur, after_heading = Block(title=r[0].strip()), True
                continue
            cur = Block()
            if r[0] == "Section":
                cur.header = [str(c) for c in r]
                continue
        if after_heading:
            after_heading = False
            if len(r) >= 2:
                cur.header = [str(c) for c in r]
                continue
            cur.lines.append(str(r[0]))
            continue
        if cur.lines and len(r) == 1:
            cur.lines.append(str(r[0]))
            continue
        if not cur.rows and not cur.header and r[0] == "Section":
            cur.header = [str(c) for c in r]
            continue
        cur.rows.append(r)
    if cur is not None and (cur.title or cur.header or cur.rows or cur.lines):
        out.append(cur)
    return out


def fold(block: Block, first_label: str = "") -> tuple[list, list, bool]:
    """(header, rows, absorbed) with a section column folded into gray bold rows above its runs: the
    DataSift tracker's metric table. A row in the result is ("section", text) or ("row", cells). A header
    whose first label is the plain word Measure takes the block's heading instead ("Last 6 weeks (Monday
    to Sunday)" over the weeks, as the DataSift tracker's "Weekly (Monday to Sunday)"), and `absorbed`
    says the heading went there. A block without a section column comes back as it is."""
    head = list(block.header)
    rows = [list(r) for r in block.rows]
    if not head or head[0] not in FOLD_COLUMNS or not rows:
        return head, [("row", r) for r in rows], False
    if head[0] != "Section" and len({str(r[0]) for r in rows}) == len(rows):
        return head, [("row", r) for r in rows], False   # nothing repeats: a plain column, not sections
    new_head, absorbed = head[1:], False
    if new_head:
        if first_label:
            new_head[0] = first_label
        elif block.title and new_head[0] in ("Measure", ""):
            new_head[0], absorbed = block.title, True
    out, last = [], object()
    for r in rows:
        sec = r[0] if r else ""
        if sec != last:
            out.append(("section", str(sec)))
            last = sec
        out.append(("row", r[1:]))
    return new_head, out, absorbed


# ---------------------------------------------------------------------- one cell
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


def number_kind(v, *hints: str) -> str:
    """The number format a value takes, from the column's header and the row's label: ($) is dollars,
    (%) is a share already in percent, a whole number gets commas, anything else one or two decimals."""
    h = " ".join(str(x or "") for x in hints).lower()
    money = "($)" in h or "($ a month" in h
    if "(%)" in h or h.rstrip().endswith("%"):
        return "pct"
    if money:
        return "money" if float(v).is_integer() else "money2"
    if isinstance(v, int):
        return "int"
    # a float is a ratio or a rate: one decimal even when whole, so 13.0 sits beside 23.5 as 13.0, not 13
    return "dec2" if abs(round(v, 1) - v) > 1e-9 else "dec1"


def link_text(url: str) -> str:
    return "Open in DataSift" if "reisift.io" in url or "datasift" in url else "Open"


def _shown(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if _is_num(v):
        return "{:,}".format(v) if isinstance(v, int) or float(v).is_integer() else "{:,.2f}".format(v)
    return str(v or "")


class Builder:
    """Rows in order, each cell with its format, the way hubspot-ops/kpi/sheet.py's Tab builds a tab."""

    def __init__(self, name: str):
        self.g = Grid(name=name)
        self.tags_cut = False            # a benchmark's "(recommended, ...)" was cut from a cell on this tab
        self.no_figure = False           # a benchmark with no figure in the catalog was left blank

    @property
    def n(self) -> int:
        return len(self.g.values)

    def put(self, row: list, fmts: list | None = None) -> int:
        r = self.n
        self.g.values.append(list(row))
        for c, f in enumerate(fmts or []):
            if f is not None and f != DEFAULT:
                self.g.fmt[(r, c)] = f
        return r

    def blank(self):
        if self.g.values and self.g.values[-1]:
            self.put([])

    def title(self, text: str, size: int = 16):
        r = self.put([text], [_fmt(size=size, bold=True, valign="BOTTOM")])
        self.g.heights[r] = TITLE_PX if size >= 14 else SECTION_PX + 4
        return r

    def gray(self, text: str, size: int = 9, color: dict | None = None, bold: bool = False):
        r = self.put([text], [_fmt(size=size, color=color or MUTED, bold=bold)])
        self.g.heights[r] = LINE_PX if size <= 9 else ROW_PX
        return r

    def paragraph(self, text: str, width_px: int, cols: int, size: int = 9, color: dict | None = None):
        """A sentence too long for one line: one cell merged across the table and wrapped, its height set
        here because Google does not size a merged row to its text."""
        r = self.put([text], [_fmt(size=size, color=color or MUTED, wrap=True, valign="TOP")])
        if cols > 1:
            self.g.merges.append((r, r + 1, 0, cols))
        self.g.heights[r] = row_height(text, width_px, size)
        return r

    def cell(self, v, head: str = "", label: str = "", bold: bool = False, numeric_col: bool = False):
        """(the value to write, its format, its rich-text runs or None)."""
        color = INK
        if isinstance(v, str):
            s = v.strip()
            low = s.lower()
            if URL_RX.fullmatch(s):
                return link_text(s), _fmt(color=BLUE, underline=True, link=s, bold=bold), None
            if low in MARKS:
                return v, _fmt(color=MARKS[low], bold=True, halign="LEFT"), None
            if low == "no figure in the catalog":
                self.no_figure = True
                return "", _fmt(), None
            if low in GAP_WORDS or low.startswith("not tracked yet") or low.startswith("not read"):
                return v, _fmt(color=MUTED, bold=bold, halign="RIGHT" if numeric_col else ""), None
            m = TAG_RX.search(v)
            if m and m.start() > 0:
                # The figure only: "150 dials a day per caller". Whether it is recommended or a placeholder is
                # said once in the tab's notes and on How it is counted, not in every cell (Ty, 2026-11-23).
                self.tags_cut = True
                return v[:m.start()].rstrip(" ;,"), _fmt(color=INK2, bold=bold), None
            # words in a column of numbers ("47 of 894 (5.3%)") line up with the numbers
            return v, _fmt(color=color, bold=bold, halign="RIGHT" if numeric_col else ""), None
        if _is_num(v):
            return v, _fmt(bold=bold, number=number_kind(v, head, label)), None
        return _shown(v), _fmt(bold=bold), None

    def table(self, header: list, rows: list, width_px_of, table_cols: int):
        """A header over a 1px rule, then the rows. `rows` holds ("section", text) and ("row", cells).
        A column that is mostly numbers sets its header right, over its numbers."""
        cells_rows = [c for kind, c in rows if kind == "row"]
        width = max([len(header)] + [len(r) for r in cells_rows] + [1])
        numeric = []
        for ci in range(width):
            # a gap word ("-", "not tracked yet") takes no side: a column of counts with a few dashes is numbers
            vals = [r[ci] for r in cells_rows if ci < len(r) and r[ci] not in ("", None)
                    and not (isinstance(r[ci], str) and r[ci].strip().lower() in GAP_WORDS)]
            numeric.append(bool(vals) and ci > 0 and sum(1 for v in vals if _is_num(v)) * 2 > len(vals))
        if header:
            # A quiet header band: light fill, bold secondary ink, a medium rule under it (the design rules'
            # table header), across the whole table so the band does not stop short of the last column.
            head = list(header) + [""] * (width - len(header))
            r0 = self.put(head, [_fmt(bold=True, wrap=True, color=INK2, valign="BOTTOM", border="head", bg=HEAD_BG,
                                      halign="RIGHT" if numeric[ci] else "") for ci in range(width)])
            self.g.heights[r0] = HEAD_PX
            self.g.head_rows.add(r0)
            for ci, h in enumerate(header):
                self.g.measure.append((r0, ci, str(h)[:16], False))
        zebra = len(cells_rows) >= ZEBRA_MIN
        stripe = 0
        for kind, item in rows:
            if kind == "section":
                r = self.put([item], [_fmt(bold=True, color=INK, valign="BOTTOM")])
                self.g.heights[r] = SECTION_PX
                stripe = 0
                continue
            r = item
            label = str(r[0]) if r else ""
            # a total names itself in the first column, or the second under a day column (the Callers tab)
            bold = label in TOTAL_LABELS or (len(r) > 1 and str(r[1]) in TOTAL_LABELS)
            if len(r) == 1 and width > 1 and isinstance(r[0], str) and not URL_RX.fullmatch(r[0].strip()):
                # one cell in a wider table: an empty-state line ("no contract signed in the window")
                self.paragraph(r[0], width_px_of(0, width), width, size=10)
                continue
            # every other row of a long table carries a faint fill; a total row carries a rule over it
            bg = ZEBRA if zebra and stripe % 2 == 1 else None
            stripe += 1
            out, fmts, row_idx = [], [], self.n
            for ci in range(width):
                if ci < len(r):
                    head = header[ci] if ci < len(header) else ""
                    val, f, runs = self.cell(r[ci], head=head, label=label, bold=bold,
                                             numeric_col=numeric[ci] if ci < len(numeric) else False)
                    self.g.measure.append((row_idx, ci, _shown(val) if not isinstance(val, str) else val, _is_num(val)))
                    if runs:
                        self.g.runs[(row_idx, ci)] = runs
                else:
                    val, f = "", _fmt()
                if bg or bold:
                    f = dict(f)
                    if bg:
                        f["backgroundColor"] = bg
                    if bold:
                        f["borders"] = {"top": {"style": "SOLID", "color": RULE}}
                out.append(val)
                fmts.append(f)
            self.put(out, fmts)
            self.g.plain_rows.add(row_idx)
        return width


def row_height(text: str, width_px: int, size: int = 9) -> int:
    per_char = 6.0 if size <= 9 else 6.6
    lines = max(1, math.ceil(len(str(text)) * per_char / max(60, width_px - 12)))
    return max(21, lines * (14 if size <= 9 else 16) + 7)


def _pct_len(lengths: list[int], share: float) -> int:
    if not lengths:
        return 0
    s = sorted(lengths)
    return s[min(len(s) - 1, int(share * (len(s) - 1) + 0.5))]


def column_widths(g: Grid, cols: int) -> list[int]:
    """Column A wide for the labels (the DataSift tracker's 260px), numbers narrow, text as wide as most
    of its cells need: a column follows its typical cell, and the rare long one wraps (wrap_long) rather
    than widening the column for every block that shares it. Read from the table cells only: a title or a
    note row runs past its cell on purpose and must not widen column A."""
    nums = [[] for _ in range(cols)]
    texts = [[] for _ in range(cols)]
    for _r, c, text, isnum in g.measure:
        if c >= cols:
            continue
        (nums if isnum else texts)[c].append(len(str(text)))
    out = []
    for c in range(cols):
        if c == 0:
            out.append(min(max(_pct_len(texts[c] + nums[c], 0.9) * 7 + 24, 200), 300))
        elif len(nums[c]) >= len(texts[c]):
            # 100px at least (the DataSift tracker's columns are 104): "not tracked yet" fits on one line
            out.append(min(max(max(nums[c] + [_pct_len(texts[c], 0.8)]) * 8 + 24, 100), 140))
        else:
            out.append(min(max(_pct_len(texts[c], 0.8) * 7 + 24, 110), 260))
    return out


def wrap_long(g: Grid, widths: list[int]) -> None:
    """A text cell longer than its column wraps (Google then sizes the row), and keeps its color."""
    for r, c, text, isnum in g.measure:
        if isnum or c >= len(widths) or r in g.head_rows:
            continue
        if len(str(text)) * 6.8 > widths[c] - 10:
            f = dict(g.fmt.get((r, c)) or DEFAULT)
            f["wrapStrategy"] = "WRAP"
            f["verticalAlignment"] = "TOP"
            g.fmt[(r, c)] = f
            g.plain_rows.discard(r)          # a wrapped row is as tall as Google measures it


def sentences(text: str) -> list[str]:
    out = []
    for s in SENTENCE_RX.split(" ".join(str(text or "").split())):
        s = s.strip()
        if s:
            out.append(s)
    return out


def render(tab: dict, line: str, title: str = "", first_labels: dict | None = None) -> Grid:
    """One tab of the snapshot as a Grid: the title row, the Refreshed line, each block (its heading, its
    header over a rule, its rows), then the tab's note as sentences at the bottom."""
    name = str(tab.get("name") or "")
    b = Builder(name)
    b.title(title or TITLES.get(name) or name)
    b.gray(line)
    b.put([])
    blks = blocks(tab)
    tables = []
    for i, blk in enumerate(blks):
        first = (first_labels or {}).get(name, "") if i == 0 else ""
        head, rows, absorbed = fold(blk, first)
        tables.append((blk, head, rows, absorbed))
    width = max([len(h) for _b, h, _r, _f in tables] + [max([len(c) for _k, c in r if _k == "row"] + [0])
                                                         for _b, _h, r, _f in tables] + [4])

    def span_px(c0, c1):
        return sum(g_widths[c0:c1]) if g_widths else 104 * (c1 - c0)

    g_widths: list[int] = []
    for i, (blk, head, rows, absorbed) in enumerate(tables):
        if i:
            b.blank()
        if blk.title and not absorbed:
            b.title(blk.title, size=12)
        if blk.lines:
            for s in blk.lines:
                b.paragraph(s, 0, width, size=10, color=INK)
        if head or rows:
            b.table(head, rows, span_px, width)
    note = [s for s in sentences(tab.get("note") or "") if not s.lower().startswith("refreshed ")]
    if b.no_figure:
        note.insert(0, "A blank benchmark means team/kpi.yaml holds no figure for that measure yet.")
    if b.tags_cut:
        note.insert(0, "A benchmark is the recommended figure until you type your own on the Goals tab; How it is "
                       "counted says which figures are still recommended or a placeholder.")
    if note:
        b.blank()
        b.gray("Notes", size=10, bold=True)
        for s in note:
            b.paragraph(s, 0, width)
    g = b.g
    g_widths[:] = column_widths(g, width)
    g.widths = list(g_widths)
    wrap_long(g, g.widths)
    # merged rows were sized before the widths existed: size them now against the real span
    full = sum(g.widths[:width])
    for (r0, _r1, c0, c1) in g.merges:
        text = g.values[r0][0] if g.values[r0] else ""
        size = (g.fmt.get((r0, 0)) or DEFAULT)["textFormat"].get("fontSize", 10)
        g.heights[r0] = row_height(text, sum(g.widths[c0:c1]) or full, size)
    finish(g, width, name)
    return g


def finish(g: Grid, width: int, name: str) -> None:
    """The row heights Google cannot pick well (a header's lines, one-line rows, the spacers), and the tab's
    color. Nothing is frozen (see below)."""
    for r in g.head_rows:
        lines = 1
        for c, text in enumerate(g.values[r]):
            if c < len(g.widths) and str(text).strip():
                lines = max(lines, math.ceil(len(str(text)) * 6.9 / max(40, g.widths[c] - 12)))
        g.heights[r] = max(HEAD_PX, lines * 15 + 12)
    for r in g.plain_rows:
        g.heights.setdefault(r, ROW_PX)
    for r, row in enumerate(g.values):
        if not row:
            g.heights.setdefault(r, 14)                 # a spacer between blocks
    # No frozen column: Google refuses a merge across a frozen boundary ("You can't merge frozen and non-frozen
    # columns"), and every tab's notes are merged across the table. Measured live 2026-11-23: eight tabs lost
    # their whole layout batch to it.
    g.frozen_cols = 0
    g.tab_color = TAB_COLORS.get(name, g.tab_color)


def stale_note(text: str) -> tuple[str, dict]:
    """A tab that could not be rebuilt keeps its numbers; its Refreshed line says from when, in gold."""
    return text, _fmt(size=10, bold=True, color=CAVEAT)


# ====================================================================== the Goals tab (the owner's own)
# Each row a person types in: (label, key, kind, what it changes). The label is how a row is found when
# it is read back, so a row may be moved; a renamed label is a row the tracker no longer reads.
GOAL_ROWS = (
    ("Revenue goal a year ($)", "goal.revenue_per_year", "money", "Deals, contracts, leads and dials needed a month"),
    ("Average fee a deal ($)", "goal.average_deal", "money", "Deals needed a month"),
    ("Contracts that close (%)", "funnel.contracts_that_close", "pct", "Contracts needed a month"),
    ("Leads a deal", "funnel.leads_per_deal", "range", "Leads needed a month; the leads per contract mark"),
    ("Dials a lead", "funnel.dials_per_lead", "range", "Dials needed a month; the dials per lead mark"),
)
TARGET_ROWS = (
    ("Dials a day", "caller.dials_per_day", "number", "Each caller's mark on the Callers tab; the pace in Slack"),
    ("Conversations a day", "caller.conversations_per_day", "range", "One number, or a range such as 20-25"),
    ("Leads a day", "caller.opportunities_per_day", "range", "One number, or a range such as 1-5"),
    ("Dials a correct number", "funnel.dials_per_correct_number", "number", "Lower is better"),
)
COST_ROWS = (
    ("Dialer", "dialer"),
    ("Texting", "texting"),
    ("Salaries and commissions", "salaries"),
    ("Subscriptions (DataSift and other software)", "subscriptions"),
    ("Calling agency", "agency"),
    ("Other", "other"),
)
BUSINESS_ROWS = (
    ("Day marketing began", "marketing_started", "date", "Days to a first deal, in the quarterly owner review"),
    ("Marketing budget a month ($)", "monthly_marketing_budget", "money", "The runway check"),
    ("Months of runway in the bank", "runway_months", "number", "The runway check"),
    ("County investor deals, last 6 months", "market_investor_deals_6mo", "number",
     "Market share, in the quarterly owner review"),
)
DEAL_HEAD = ("Property (address or DataSift link)", "Contract signed", "Deal type", "Contract price ($)",
             "Expected fee ($)", "Closed", "Fee collected ($)", "Notes")
DEAL_TYPES = ("Assignment", "Double close", "Wholetail", "Flip", "Rental", "Other")
DEAL_ROWS = 25
# Each section's header: the label, the yellow cell, the four columns the tracker fills from the month's
# numbers, then the default and what the last refresh used. Costs and the business rows have no pace.
SECTION_HEADS = {
    "goals": ("Your number", "Needed a month", "This month so far", "Pace by month end", "Mark", "Default", "In use now"),
    "targets": ("Your number", "Target a day", "This month a day", "", "Mark", "Default", "In use now"),
    "plain": ("Your number", "", "", "", "", "Default", "In use now"),
}
SECTION_HEAD = SECTION_HEADS["plain"]
GOALS_TITLE = "Goals"
TARGETS_TITLE = "Targets for one caller a day"
COSTS_TITLE = "Monthly costs the autopilot cannot see ($)"
BUSINESS_TITLE = "Your business"
DEALS_TITLE = "Deals: one row a contract"
INPUTS_NOTE = ("Type your numbers in the yellow cells. A blank cell keeps the default. The gray columns fill in at every "
               "refresh. Hover a label to see what it changes.")
# The number format of each goal's Needed, So far and Pace cells.
GOAL_CELL_FORMATS = {
    "goal.revenue_per_year": ("money", "money", "money"),
    "goal.average_deal": ("dec1", "int", "dec1"),
    "funnel.contracts_that_close": ("dec1", "int", "dec1"),
    "funnel.leads_per_deal": ("int", "int", "int"),
    "funnel.dials_per_lead": ("int", "int", "int"),
    "caller.dials_per_day": ("int", "dec1", ""),
    "caller.conversations_per_day": ("dec1", "dec1", ""),
    "caller.opportunities_per_day": ("dec1", "dec1", ""),
    "funnel.dials_per_correct_number": ("dec1", "dec1", ""),
}


def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def goal_cell_formats(key: str, mark: str = "") -> list[dict]:
    """The four gray columns' formats for one row: Needed (or the target), So far and Pace in the key's
    number format, and the mark in its color. Shared by the tab's first build and every refresh."""
    kinds = GOAL_CELL_FORMATS.get(key, ("", "", ""))
    out = [_fmt(number=k, halign="RIGHT") if k else _fmt(halign="RIGHT") for k in kinds]
    low = str(mark or "").strip().lower()
    out.append(_fmt(color=MARKS[low], bold=True) if low in MARKS else _fmt(color=MUTED))
    return out


def goal_cell_values(cells: dict | None, key: str) -> list:
    """The four gray columns' values for one row, from the snapshot's goal_cells, else blank."""
    got = (cells or {}).get(key)
    if not isinstance(got, (list, tuple)):
        return ["", "", "", ""]
    return (list(got) + ["", "", "", ""])[:4]


def inputs_grid(defaults: dict, in_use_now: dict | None = None, cells: dict | None = None, carry: dict | None = None,
                line: str = "") -> Grid:
    """The Goals tab as it is made, once: the owner's yellow cells, and beside each goal what it needs a
    month, the month so far, the pace and the mark (cells, the snapshot's goal_cells). `defaults` maps a key
    to its default's words (team/kpi.yaml alone); `in_use_now` to what the run used (in_use); `carry` holds
    what was already typed when the tab is rebuilt from an older layout: {"keys": {key: raw}, "deals":
    [[eight cells], ...]}, written back as it was typed."""
    in_use_now, carry = in_use_now or {}, carry or {}
    typed, deal_rows = dict(carry.get("keys") or {}), list(carry.get("deals") or [])
    b = Builder(INPUTS_TAB)
    g = b.g
    b.title(GOALS_TITLE)
    b.gray(line or "Refreshed at the next run")
    b.gray(INPUTS_NOTE, size=10, color=INK)
    b.put([])
    width = len(INPUTS_WIDTHS)
    yellow = _fmt(bg=YELLOW, text=True, halign="RIGHT", border="box")
    gray = _fmt(color=MUTED)
    rows_at: dict = {}

    def head(title, labels, right=(2, 3, 4)):
        r = b.put([title] + list(labels), [_fmt(bold=True, wrap=True, color=INK2, valign="BOTTOM", border="head",
                                              bg=HEAD_BG, halign="RIGHT" if i in right else "")
                                         for i in range(1 + len(labels))])
        g.heights[r] = HEAD_PX + 8
        g.head_rows.add(r)

    def section(title, rows, kind):
        head(title, SECTION_HEADS[kind], right=(2, 3, 4) if kind != "plain" else ())
        for row in rows:
            label, key = row[0], row[1]
            full = key if len(row) > 2 else "cost:" + key
            what = row[3] if len(row) > 3 else "Cost per lead, contract and closed deal; net margin"
            vals = goal_cell_values(cells, full) if kind != "plain" else ["", "", "", ""]
            fmts = goal_cell_formats(full, vals[3]) if kind != "plain" else [_fmt()] * 4
            r = b.put([label, typed.get(full, "")] + vals + [defaults.get(full, ""), in_use_now.get(full, "")],
                      [_fmt(), yellow] + fmts + [gray, gray])
            rows_at[full] = r
            g.notes[(r, 0)] = what
            g.plain_rows.add(r)
        b.put([])

    section(GOALS_TITLE, GOAL_ROWS, "goals")
    section(TARGETS_TITLE, TARGET_ROWS, "targets")
    section(COSTS_TITLE, COST_ROWS, "plain")
    section(BUSINESS_TITLE, BUSINESS_ROWS, "plain")
    b.title(DEALS_TITLE, size=12)
    head(DEAL_HEAD[0], DEAL_HEAD[1:], right=(3, 4, 6))
    first = b.n
    for i in range(max(DEAL_ROWS, len(deal_rows))):
        cells_ = (list(deal_rows[i]) + [""] * len(DEAL_HEAD))[:len(DEAL_HEAD)] if i < len(deal_rows) else [""] * len(DEAL_HEAD)
        r = b.put(cells_, [_fmt(bg=YELLOW, text=True, border="box")] * len(DEAL_HEAD))
        g.plain_rows.add(r)
    last = b.n
    g.validations.append((first, last, 2, 3, {
        "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": t} for t in DEAL_TYPES]},
        "strict": False, "showCustomUi": True,
        "inputMessage": "Pick one, or type your own"}))
    g.widths = list(INPUTS_WIDTHS)
    for r in g.plain_rows:
        g.heights.setdefault(r, ROW_PX)
    for r, row in enumerate(g.values):
        if not row:
            g.heights.setdefault(r, 14)
    g.tab_color = YELLOW
    g.rows_at = rows_at
    return g


# ---------------------------------------------------------------------- reading what was typed
MONEY_RX = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)$")
RANGE_RX = re.compile(r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:-|to|%s|%s)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*$" % (chr(0x2013), chr(0x2014)))   # a typed range: 20-25, 20 to 25, or a dash a phone types
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%d %b %Y", "%m-%d-%Y")


def _num(v):
    """A typed number, or None. "$1,200", "1200", "12k", "1.5m", 1200 all read; anything else is None."""
    if _is_num(v):
        return float(v)
    s = str(v or "").strip().replace(" ", "")
    if not s:
        return None
    neg = s.startswith("-")
    s = s.lstrip("-").rstrip("%")
    m = MONEY_RX.match(s)
    if not m:
        return None
    x = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1.0)
    return -x * mult if neg else x * mult


def _date(v):
    """A typed day, as YYYY-MM-DD, or None. A Sheets day number (days since 1899-12-30) reads too."""
    if _is_num(v):
        n = float(v)
        if 20000 < n < 80000:
            return (date(1899, 12, 30) + timedelta(days=int(n))).isoformat()
        return None
    s = " ".join(str(v or "").replace(",", ", ").split()).replace(" ,", ",")
    if not s:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def _value(kind: str, raw):
    """(value or None, problem or ""). A value is a float, (low, high), or a YYYY-MM-DD string."""
    s = str(raw).strip() if not _is_num(raw) else raw
    if s in ("", None):
        return None, ""
    if kind == "date":
        d = _date(raw)
        return (d, "") if d else (None, "is not a day the tracker can read (write it as 2026-11-23)")
    if kind == "range":
        m = RANGE_RX.match(str(raw))
        if m:
            lo, hi = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
            if lo > hi:
                return None, "starts above where it ends"
            return (lo, hi), ""
    x = _num(raw)
    if x is None:
        return None, "is not a number"
    if x < 0:
        return None, "is below zero"
    if kind == "pct":
        if 0 < x <= 1 and "%" not in str(raw):
            x *= 100.0                    # 0.75 typed as a share
        if x > 100:
            return None, "is over 100%"
    return x, ""


def parse_inputs(values: list) -> dict:
    """What the Goals tab holds, from its rows as Google returns them (FORMATTED_VALUE). Rows are found
    by their label, so a moved row still reads; a cell that cannot be read keeps the default and says
    why. Returns {"figures": {key: {"value"} | {"low","high"}}, "inputs": {...}, "deals": [...],
    "problems": [str], "rows": {key: row index}, "typed": int}."""
    out = {"figures": {}, "inputs": {}, "deals": [], "problems": [], "rows": {}, "typed": 0, "bad": {}}
    by_label = {}
    for kind_rows in (GOAL_ROWS, TARGET_ROWS, BUSINESS_ROWS):
        for row in kind_rows:
            by_label[_norm(row[0])] = (row[1], row[2], row[0])
    for label, key in COST_ROWS:
        by_label[_norm(label)] = ("cost:" + key, "money", label)
    deal_at = None
    rows = [list(r) for r in values or []]
    for i, r in enumerate(rows):
        if not r:
            continue
        lab = _norm(r[0])
        if lab == _norm(DEAL_HEAD[0]) and deal_at is None:
            deal_at = i
            continue
        hit = by_label.get(lab)
        if hit is None or deal_at is not None:
            continue
        key, kind, label = hit
        out["rows"][key] = i
        raw = r[1] if len(r) > 1 else ""
        v, why = _value(kind, raw)
        if why:
            out["problems"].append("Goals tab, %s: %r %s, so the default is used" % (label, str(raw)[:40], why))
            out["bad"][key] = str(raw)[:40]
            continue
        if v is None:
            continue
        out["typed"] += 1
        if key.startswith("cost:"):
            out["inputs"].setdefault("monthly_costs", {})[key[5:]] = float(v)
        elif key in ("marketing_started", "monthly_marketing_budget", "runway_months", "market_investor_deals_6mo"):
            out["inputs"][key] = v if kind == "date" else float(v)
        elif isinstance(v, tuple):
            if kind != "range":
                out["problems"].append("Goals tab, %s: type one number, not a range, so the default is used" % label)
                out["typed"] -= 1
                continue
            out["figures"][key] = {"low": v[0], "high": v[1]}
        else:
            out["figures"][key] = {"value": float(v)}
    if deal_at is not None:
        for j, r in enumerate(rows[deal_at + 1:], start=deal_at + 1):
            cells = [str(x).strip() if not _is_num(x) else x for x in (r + [""] * len(DEAL_HEAD))[:len(DEAL_HEAD)]]
            if not any(str(c).strip() for c in cells[:7]):
                continue
            deal = {"row": j + 1, "property": str(cells[0]), "type": str(cells[2])}
            for idx, name in ((1, "signed"), (5, "closed")):
                if str(cells[idx]).strip():
                    d = _date(cells[idx])
                    if d is None:
                        out["problems"].append("Goals tab, deal on row %d: %s %r is not a day the tracker can read"
                                               % (j + 1, DEAL_HEAD[idx].lower(), str(cells[idx])[:30]))
                    deal[name] = d
            for idx, name in ((3, "price"), (4, "expected"), (6, "fee")):
                if str(cells[idx]).strip():
                    x = _num(cells[idx])
                    if x is None or x < 0:
                        out["problems"].append("Goals tab, deal on row %d: %s %r is not a number"
                                               % (j + 1, DEAL_HEAD[idx].lower(), str(cells[idx])[:30]))
                    deal[name] = x
            out["deals"].append(deal)
            out["typed"] += 1
    out["found_tab"] = True
    return out


def carry_over(values: list) -> dict:
    """What a person typed on an older Goals or Inputs tab, cell by cell as it was typed: {"keys": {key:
    raw}, "deals": [[eight cells], ...]}. The tab is rebuilt in the new layout with these written back, so
    nothing typed is lost to a layout change. A blank is not carried."""
    by_label = {}
    for kind_rows in (GOAL_ROWS, TARGET_ROWS, BUSINESS_ROWS):
        for row in kind_rows:
            by_label[_norm(row[0])] = row[1]
    for label, key in COST_ROWS:
        by_label[_norm(label)] = "cost:" + key
    keys, deals, deal_at = {}, [], None
    for i, r in enumerate([list(x) for x in values or []]):
        if not r:
            continue
        if deal_at is None and _norm(r[0]) == _norm(DEAL_HEAD[0]):
            deal_at = i
            continue
        if deal_at is not None:
            cells = (r + [""] * len(DEAL_HEAD))[:len(DEAL_HEAD)]
            if any(str(c).strip() for c in cells):
                deals.append(cells)
            continue
        key = by_label.get(_norm(r[0]))
        if key and len(r) > 1 and str(r[1]).strip():
            keys[key] = r[1]
    return {"keys": keys, "deals": deals}


def in_use_words(kind: str, typed, default_words: str) -> str:
    """What the In use now column says for one row."""
    if typed is None:
        return ("Using the default (%s)" % default_words) if default_words else "Blank"
    if isinstance(typed, dict):
        if "value" in typed:
            typed = typed["value"]
        else:
            return "Using yours: %s to %s" % (_plain(typed["low"], kind), _plain(typed["high"], kind))
    return "Using yours: %s" % _plain(typed, kind)


def _plain(x, kind: str) -> str:
    if isinstance(x, str):
        return x
    if kind == "money":
        return "$" + ("{:,}".format(int(round(x))))
    if kind == "pct":
        return ("%s%%" % ("{:,}".format(int(x)) if float(x).is_integer() else "{:,.1f}".format(x)))
    return "{:,}".format(int(x)) if float(x).is_integer() else "{:,.1f}".format(x)


def _short_tag(f) -> str:
    return {"placeholder": " (a placeholder)", "recommended": " (recommended)", "measured": " (measured)"}.get(
        getattr(f, "status", ""), "")


def default_words(plain) -> dict:
    """Each Inputs row's default, from team/kpi.yaml alone (kpi.load(kpi.path()), nothing laid over it)."""
    out = {}
    for rows in (GOAL_ROWS, TARGET_ROWS):
        for _label, key, _kind, _what in rows:
            try:
                f = plain.fig(key)
            except Exception:  # noqa: BLE001  (a figure the file lacks shows no default)
                continue
            out[key] = f.text() + _short_tag(f)
    costs = plain.monthly_costs()
    for _label, key in COST_ROWS:
        v = costs.get(key)
        out["cost:" + key] = ("$%s a month" % "{:,}".format(int(round(v)))) if v is not None else ""
    for _label, key, kind, _what in BUSINESS_ROWS:
        v = (plain.inputs or {}).get(key)
        out[key] = "" if v is None else (str(v) if kind == "date" else _plain(float(v), kind))
    return out


def in_use(cat, plain) -> dict:
    """What the In use now column says for each row: the owner's number, the default, or why a typed
    cell could not be used. Read from the catalog every report used this run."""
    info = getattr(cat, "typed", None) or {}
    used, bad = set(info.get("keys") or []), info.get("bad") or {}
    d = default_words(plain)
    out = {}
    for key in all_keys():
        dflt = d.get(key) or ""
        if key in bad:
            out[key] = "Could not read %r, so the default" % bad[key] if dflt else "Could not read %r: blank" % bad[key]
        elif key in used:
            out[key] = "Yours: " + _yours(cat, key)
        else:
            out[key] = "Default" if dflt else "Blank, not counted"
    return out


def _yours(cat, key: str) -> str:
    if key.startswith("cost:"):
        v = cat.monthly_costs().get(key[5:])
        return "$%s a month" % "{:,}".format(int(round(v))) if v is not None else ""
    if key in [r[1] for r in BUSINESS_ROWS]:
        v = (cat.inputs or {}).get(key)
        return str(v) if kind_of(key) == "date" else _plain(float(v), kind_of(key))
    try:
        return cat.fig(key).text()
    except Exception:  # noqa: BLE001
        return ""


def kind_of(key: str) -> str:
    for rows in (GOAL_ROWS, TARGET_ROWS, BUSINESS_ROWS):
        for row in rows:
            if row[1] == key:
                return row[2]
    return "money"


def all_keys() -> list[str]:
    return ([r[1] for r in GOAL_ROWS] + [r[1] for r in TARGET_ROWS] + ["cost:" + k for _l, k in COST_ROWS]
            + [r[1] for r in BUSINESS_ROWS])
