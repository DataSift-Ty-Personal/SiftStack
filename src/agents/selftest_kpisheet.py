"""The KPI tracker's Google Sheet layout (autopilot/kpisheet.py), the Inputs tab it reads back, and the catalog
those typed numbers are laid over (kpi.apply_typed, kpi.load). Zero network, no Google: the layout is plain data
and is checked as data. Every name, number and street here is invented.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from . import config


def run(check, tmp: Path) -> None:
    from .autopilot import kpi, kpisheet as ks
    keep = (config.DATA_DIR,)
    try:
        config.DATA_DIR = Path(tmp) / "kpisheet"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        kpi.override(None)
        _layout(check, ks)
        _inputs(check, ks)
        _overlay(check, kpi, ks)
        _connect(check, Path(tmp))
        _hygiene(check)
    finally:
        (config.DATA_DIR,) = keep
        kpi.override(None)


def _layout(check, ks) -> None:
    print("  the layout: the DataSift tracker's")
    tab = {"name": "Scoreboard", "note": "Refreshed Monday, November 23 at 12:34 PM. Every call is counted once. "
                                         "Marks judge the pace.",
           "columns": ["Section", "Monthly", "Nov (to date)", "Pace this month", "Mark", "Oct", "Goal or benchmark"],
           "rows": [["Activity", "Dials", 3370, 4597, "on track", 7940, "150 dials a day per caller (recommended, not yet confirmed, waits on D30)"],
                    ["Activity", "Texts sent", "not tracked yet", "", "", "not tracked yet", ""],
                    ["Spend", "Paid vendor spend ($)", 89.12, 121.56, "", 0.0, ""],
                    ["Ratios", "Share reached (%)", 76.2, "", "behind", 70.0, ""],
                    [],
                    ["Right now, and the last 90 days"],
                    ["Measure", "Value", "Goal or benchmark", "Mark"],
                    ["Leads per contract, last 90 days", "-", "5 to 15 leads a deal", ""],
                    [],
                    ["Last 6 weeks (Monday to Sunday)"],
                    ["Section", "Measure", "Nov 23 (to date)", "Nov 16", "Weekly goal"],
                    ["Activity", "Dials", 49, 679, 692],
                    [],
                    ["Lead ledger"],
                    ["Record", "Became a lead"],
                    ["https://app.reisift.io/records/properties/00000000-0000-4000-8000-0000000000b1", "Mon Nov 2"],
                    [],
                    ["Contract ledger"],
                    ["Record", "Contract signed", "Fee"],
                    ["no contract signed in the window"],
                    [],
                    ["Quarterly owner review"],
                    ["Days to a first deal: not read."],
                    ["Net margin: not read."]],
           "failed": False}
    bl = ks.blocks(tab)
    check([b.title for b in bl] == ["", "Right now, and the last 90 days", "Last 6 weeks (Monday to Sunday)", "Lead ledger",
                                    "Contract ledger", "Quarterly owner review"],
          "a tab reads as blocks: a blank row ends one, a one-cell row after it is the next one's heading")
    check(bl[1].header == ["Measure", "Value", "Goal or benchmark", "Mark"] and bl[5].lines == ["Days to a first deal: not read.",
                                                                                              "Net margin: not read."],
          "the row after a heading names its columns; one-cell rows under a heading are sentences")
    head, rows, absorbed = ks.fold(bl[0])
    check(head[:3] == ["Monthly", "Nov (to date)", "Pace this month"] and rows[0] == ("section", "Activity")
          and rows[1] == ("row", ["Dials", 3370, 4597, "on track", 7940,
                                  "150 dials a day per caller (recommended, not yet confirmed, waits on D30)"])
          and ("section", "Spend") in rows and not absorbed,
          "a Section column folds into gray bold rows above its runs, the DataSift tracker's metric table")
    head2, _rows2, absorbed2 = ks.fold(bl[2])
    check(head2[0] == "Last 6 weeks (Monday to Sunday)" and absorbed2,
          "a heading over a folded table becomes the header's first cell, as the tracker's 'Weekly (Monday to Sunday)'")
    plain = ks.Block(header=["Channel", "Measure"], rows=[["Calls", "Dials"], ["Mail", "Pieces"]])
    check(ks.fold(plain)[0] == ["Channel", "Measure"], "a grouping column whose cells never repeat is left as a column")
    check(ks.number_kind(89.12, "", "Paid vendor spend ($)") == "money2" and ks.number_kind(0.0, "Cost ($)") == "money"
          and ks.number_kind(76.2, "Share reached (%)") == "pct" and ks.number_kind(3370) == "int"
          and ks.number_kind(13.5) == "dec1" and ks.number_kind(2.86) == "dec2" and ks.number_kind(13.0) == "dec1",
          "dollars, shares, counts and ratios each take their own number format; a whole ratio keeps its decimal")

    g = ks.render(tab, "Refreshed Monday, November 23 at 12:34 PM Eastern", title="the business KPI scoreboard")
    v = g.values
    check(v[0] == ["the business KPI scoreboard"] and v[1] == ["Refreshed Monday, November 23 at 12:34 PM Eastern"] and v[2] == []
          and v[3][:3] == ["Monthly", "Nov (to date)", "Pace this month"] and v[4] == ["Activity"],
          "the title, the plain Refreshed line, then the table with its first section: %r" % v[:5])
    f_title, f_line = g.fmt[(0, 0)], g.fmt[(1, 0)]
    check(f_title["textFormat"].get("bold") and f_title["textFormat"]["fontSize"] == 16 and g.heights.get(0) == ks.TITLE_PX
          and f_line["textFormat"]["foregroundColor"] == ks.MUTED and f_line["textFormat"]["fontSize"] == 9,
          "the title is bold and large, the Refreshed line small and gray")
    hdr = g.fmt[(3, 0)]
    check(hdr["textFormat"].get("bold") and hdr.get("borders", {}).get("bottom", {}).get("style") == "SOLID_MEDIUM"
          and hdr.get("backgroundColor") == ks.HEAD_BG and hdr["textFormat"]["foregroundColor"] == ks.INK2
          and all(g.fmt[(3, c)].get("backgroundColor") == ks.HEAD_BG for c in range(len(v[3])))
          and g.fmt[(4, 0)]["textFormat"]["foregroundColor"] == ks.INK and g.fmt[(4, 0)]["textFormat"].get("bold")
          and g.heights.get(4) == ks.SECTION_PX,
          "a header is a quiet band across the table over a medium rule; a section row is bold with room above it")
    dials_r = v.index(["Dials", 3370, 4597, "on track", 7940, "150 dials a day per caller"])
    check(g.fmt[(dials_r, 1)]["numberFormat"]["pattern"] == "#,##0" and g.fmt[(dials_r, 3)]["textFormat"]["foregroundColor"] == ks.GREEN
          and g.fmt[(dials_r, 3)]["textFormat"].get("bold"),
          "a count has commas; a mark is its word in its color")
    check(not g.runs.get((dials_r, 5)) and g.heights.get(dials_r) == ks.ROW_PX,
          "a benchmark cell holds the figure only, its settledness said once in the notes; a one-line row takes its height")
    gap_r = next(i for i, r in enumerate(v) if r and r[0] == "Texts sent")
    check(g.fmt[(gap_r, 1)]["textFormat"]["foregroundColor"] == ks.MUTED and v[gap_r][1] == "not tracked yet",
          "a number nothing can read yet is gray words, never a zero")
    money_r = next(i for i, r in enumerate(v) if r and r[0] == "Paid vendor spend ($)")
    pct_r = next(i for i, r in enumerate(v) if r and r[0] == "Share reached (%)")
    check(g.fmt[(money_r, 1)]["numberFormat"]["pattern"] == "$#,##0.00" and g.fmt[(pct_r, 1)]["numberFormat"]["pattern"] == '0.0"%"'
          and g.fmt[(pct_r, 3)]["textFormat"]["foregroundColor"] == ks.RED,
          "a ($) row is dollars, a (%) row a share, and behind is red")
    link_r = next(i for i, r in enumerate(v) if r and r[0] == "Open in DataSift")
    lf = g.fmt[(link_r, 0)]["textFormat"]
    check(lf.get("link", {}).get("uri", "").startswith("https://app.reisift.io/records/properties/") and lf.get("underline"),
          "a record link reads Open in DataSift and opens the record")
    empty_r = next(i for i, r in enumerate(v) if r and r[0] == "no contract signed in the window")
    check(any(m[0] == empty_r and m[3] - m[2] > 1 for m in g.merges) and g.heights.get(empty_r, 0) >= 21,
          "an empty table's one line runs across the table, its height set")
    notes_r = v.index(["Notes"])
    check(str(v[notes_r + 1][0]).startswith("A benchmark is the recommended figure")
          and v[notes_r + 2:notes_r + 4] == [["Every call is counted once."], ["Marks judge the pace."]]
          and all(any(m[0] == r for m in g.merges) for r in (notes_r + 2, notes_r + 3))
          and not any(r and str(r[0]).startswith("Refreshed Monday, November 23 at 12:34 PM.") for r in v[notes_r:]),
          "the note sits at the bottom as one line a sentence, merged across, and never repeats the Refreshed line")
    check(len(g.widths) == g.width and 200 <= g.widths[0] <= 300 and all(80 <= w <= 300 for w in g.widths[1:]),
          "every column gets a width: the labels wide, numbers narrow, nothing past the cap: %r" % g.widths)
    fills = {json.dumps(f.get("backgroundColor"), sort_keys=True) for f in g.fmt.values() if f.get("backgroundColor")}
    check(fills <= {json.dumps(ks.HEAD_BG, sort_keys=True), json.dumps(ks.ZEBRA, sort_keys=True)},
          "the only fills are the quiet header band and the faint zebra rows: %r" % fills)
    check(g.frozen_cols == 0 and g.tab_color == ks.TAB_COLORS["Scoreboard"]
          and all(g.heights.get(r) == 14 for r, row in enumerate(v) if not row),
          "nothing frozen (Google refuses a merge across a frozen column), the tab takes its color, a spacer is short")
    long_tab = {"name": "Callers", "note": "", "columns": ["Day", "Caller", "Dials"],
                "rows": [["Mon Nov %d" % d, "Emerson", d * 10] for d in range(1, 9)] + [["Mon Nov 9", "Team", 90]],
                "failed": False}
    lg = ks.render(long_tab, "Refreshed Mon Nov 23 at 3:33 PM ET")
    body = [r for r, row in enumerate(lg.values) if row and row[1:2] == ["Emerson"]]
    team_r = next(r for r, row in enumerate(lg.values) if row and row[1:2] == ["Team"])
    check(lg.fmt.get((body[1], 0), {}).get("backgroundColor") == ks.ZEBRA and "backgroundColor" not in lg.fmt.get((body[0], 0), {})
          and lg.fmt[(team_r, 0)].get("borders", {}).get("top") and lg.fmt[(team_r, 0)]["textFormat"].get("bold"),
          "a long table stripes every other row faintly; a total row is bold under a rule")
    check(lg.values[0] == ["Callers"] and "," not in lg.values[0][0],
          "a tab's title is its name: no comma list at the top (Ty, 2026-11-23)")


def _inputs(check, ks) -> None:
    print("  the Goals tab (the Inputs tab, combined)")
    g = ks.inputs_grid({"goal.revenue_per_year": "$360,000 a year (a placeholder)"})
    labels = [r[0] for r in g.values if r]
    check(ks.INPUTS_TAB == "Goals" and labels[0] == "Goals" and labels[1].startswith("Refreshed")
          and labels[2].startswith("Type your numbers in the yellow cells")
          and all(x in labels for x in (ks.TARGETS_TITLE, ks.COSTS_TITLE, ks.BUSINESS_TITLE, ks.DEALS_TITLE,
                                        "Calling agency", "Property (address or DataSift link)"))
          and not any("," in str(x) for x in labels[:3]),
          "the Goals tab: its name, the Refreshed line, how to use it (no comma at the top), the goals, a day's "
          "targets, the costs, the business, the deals")
    yellow = [k for k, f in g.fmt.items() if f.get("backgroundColor") == ks.YELLOW]
    check(len(yellow) >= len(ks.GOAL_ROWS) + len(ks.TARGET_ROWS) + len(ks.COST_ROWS) + len(ks.BUSINESS_ROWS) + 25 * 8
          and all(g.fmt[k].get("numberFormat", {}).get("type") == "TEXT" for k in yellow),
          "every cell a person types in is yellow, and plain text, so Google never turns 1-5 into a date")
    check(g.validations and g.validations[0][4]["condition"]["type"] == "ONE_OF_LIST" and g.tab_color == ks.YELLOW,
          "a deal's type is a pick list, and the tab itself is yellow")
    goal_r = next(i for i, r in enumerate(g.values) if r and r[0] == "Revenue goal a year ($)")
    check(g.values[goal_r][6] == "$360,000 a year (a placeholder)" and g.rows_at["goal.revenue_per_year"] == goal_r
          and g.notes.get((goal_r, 0)),
          "each row shows its default in the gray columns, its label carries what it changes as a hover note")
    head = g.values[goal_r - 1]
    check(head == ["Goals", "Your number", "Needed a month", "This month so far", "Pace by month end", "Mark", "Default",
                   "In use now"],
          "Kendrick: the goals beside what each needs a month, the month so far and the pace: %r" % head)
    cells = {"funnel.dials_per_lead": [3000, 3419, 4565.5, "on track"], "caller.dials_per_day": [150, 206.6, "", "behind"]}
    g2 = ks.inputs_grid({}, cells=cells, carry={"keys": {"goal.revenue_per_year": "$600k"},
                                                "deals": [["100 Sample St", "11/02/2026", "Assignment", "", "", "", "", ""]]})
    dl = g2.rows_at["funnel.dials_per_lead"]
    dd = g2.rows_at["caller.dials_per_day"]
    check(g2.values[dl][2:6] == [3000, 3419, 4565.5, "on track"] and g2.fmt[(dl, 5)]["textFormat"]["foregroundColor"] == ks.GREEN
          and g2.fmt[(dl, 2)]["numberFormat"]["pattern"] == "#,##0" and g2.values[dd][5] == "behind"
          and g2.fmt[(dd, 5)]["textFormat"]["foregroundColor"] == ks.RED,
          "the gray columns carry the month's numbers in their formats and the mark in its color")
    rv = g2.rows_at["goal.revenue_per_year"]
    deal_head = next(i for i, r in enumerate(g2.values) if r and r[0] == ks.DEAL_HEAD[0])
    check(g2.values[rv][1] == "$600k" and g2.values[deal_head + 1][0] == "100 Sample St"
          and g2.values[deal_head + 2] == [""] * len(ks.DEAL_HEAD),
          "a tab rebuilt for a new layout keeps what was typed, as it was typed: the goal and the deal row")
    old = [["Inputs: the numbers only you know"], ["Type in the yellow cells."], [],
           ["Goals", "Your number", "Default", "In use now", "What it changes"],
           ["Revenue goal a year ($)", "$600k", "$360,000 a year", "Default", "Deals"], ["Leads a deal", ""],
           ["Calling agency", "1,800"], ["Deals: one row a contract"], list(ks.DEAL_HEAD),
           ["100 Sample St", "11/02/2026", "Assignment", "$90,000"], ["", "", ""]]
    co = ks.carry_over(old)
    check(co["keys"] == {"goal.revenue_per_year": "$600k", "cost:agency": "1,800"}
          and co["deals"] == [["100 Sample St", "11/02/2026", "Assignment", "$90,000", "", "", "", ""]],
          "what a person typed on the old Inputs tab is read back cell by cell, blanks left behind: %r" % co)

    # what was typed, as Google hands it back (formatted text), in any order
    rows = [["Inputs: the numbers only you know"], [], ["Goals", "Your number", "Default", "In use now"],
            ["Average fee a deal ($)", "$12k"], ["Revenue goal a year ($)", "$600,000"], ["Contracts that close (%)", "0.6"],
            ["Leads a deal", "8-12"], ["Dials a lead", "abc"], [], ["Dials a day", "180"], ["Conversations a day", "6 to 8"],
            ["Leads a day", ""], ["Dials a correct number", "-3"], ["Dialer", "$300"], ["Calling agency", "1,800"],
            ["Day marketing began", "01/15/2026"], ["Months of runway in the bank", "6"],
            ["County investor deals, last 6 months", "166"], ["Some row a person added", "42"], [],
            ["Deals: one row a contract (the fees give revenue and net margin)"], list(ks.DEAL_HEAD),
            ["100 Sample St", "11/02/2026", "Assignment", "$90,000", "12,000", "11/12/2026", "12000", "first"],
            ["", "", "", "", "", "", "", ""],
            ["200 Example Ave", "2026-11-10", "Flip", "", "9k", "", "", ""],
            ["300 Demo Rd", "not a day", "", "", "", "", "", ""]]
    p = ks.parse_inputs(rows)
    f = p["figures"]
    check(f["goal.average_deal"] == {"value": 12000.0} and f["goal.revenue_per_year"] == {"value": 600000.0}
          and f["funnel.contracts_that_close"] == {"value": 60.0} and f["funnel.leads_per_deal"] == {"low": 8.0, "high": 12.0}
          and f["caller.dials_per_day"] == {"value": 180.0} and f["caller.conversations_per_day"] == {"low": 6.0, "high": 8.0},
          "typed numbers read the way people type them: $12k, $600,000, 0.6 for 60%%, 8-12 and 6 to 8 as ranges: %r" % f)
    check("caller.opportunities_per_day" not in f and "funnel.dials_per_lead" not in f
          and "funnel.dials_per_correct_number" not in f and p["bad"].get("funnel.dials_per_lead") == "abc",
          "a blank cell keeps the default; a cell that is not a number, or below zero, keeps it too and is named")
    check(any("Dials a lead" in x and "'abc'" in x for x in p["problems"])
          and any("Dials a correct number" in x and "below zero" in x for x in p["problems"]),
          "each cell that could not be read is said, with the row and what was typed")
    check(p["inputs"] == {"monthly_costs": {"dialer": 300.0, "agency": 1800.0}, "marketing_started": "2026-01-15",
                          "runway_months": 6.0, "market_investor_deals_6mo": 166.0},
          "the costs and the business numbers are read, the day as YYYY-MM-DD: %r" % p["inputs"])
    d = p["deals"]
    check(len(d) == 3 and d[0]["fee"] == 12000.0 and d[0]["price"] == 90000.0 and d[0]["closed"] == "2026-11-12"
          and d[1]["expected"] == 9000.0 and d[1]["signed"] == "2026-11-10" and "closed" not in d[1]
          and d[2].get("signed") is None and any("row 26" in x for x in p["problems"]),
          "each deal row is read, blank rows skipped, and a day that cannot be read is said with its row")
    check(p["rows"]["goal.average_deal"] == 3 and p["rows"]["cost:agency"] == 14 and p["typed"] == 14,
          "a row is found by its label wherever it was moved, and every typed cell is counted")
    check(ks._date(46000) == "2025-12-09" and ks._num("1.5m") == 1500000.0 and ks._value("pct", "75%")[0] == 75.0
          and ks._value("pct", "150")[1] == "is over 100%",
          "a Sheets day number, 1.5m and 75% all read; a share over 100% is refused")


def _overlay(check, kpi, ks) -> None:
    print("  the typed numbers laid over the catalog")
    plain = kpi.load(kpi.path())
    data = {"read_at": "2026-11-23T17:30:00+00:00",
            "figures": {"goal.revenue_per_year": {"value": 600000}, "goal.average_deal": {"value": 0},
                        "funnel.contracts_that_close": {"value": 150}, "caller.conversations_per_day": {"low": 6, "high": 8},
                        "caller.dials_per_day": {"low": 150, "high": 180}, "funnel.leads_per_deal": {"value": 12},
                        "alarm.dials_without_lead": {"value": 1}},
            "inputs": {"monthly_costs": {"dialer": 300, "agency": 1800}, "marketing_started": "2026-01-15",
                       "runway_months": 6},
            "deals": [{"property": "100 Sample St", "closed": "2026-11-12", "fee": 12000}],
            "problems": ["Inputs tab, Dials a lead: 'abc' is not a number, so the default is used"],
            "bad": {"funnel.dials_per_lead": "abc"}, "rows": {"goal.revenue_per_year": 3}}
    cat = kpi.apply_typed(plain, data)
    rev = cat.fig("goal.revenue_per_year")
    check(rev.value == 600000 and rev.status == "confirmed" and rev.source == (kpi.TYPED_SOURCE,) and rev.tag() == "",
          "a typed goal replaces the file's, confirmed: the owner typed it")
    check(cat.fig("goal.average_deal").value == plain.fig("goal.average_deal").value
          and cat.fig("funnel.contracts_that_close").value == plain.fig("funnel.contracts_that_close").value,
          "a zero that would divide the goal sheet, or a share over 100%, is passed over")
    check(cat.fig("caller.conversations_per_day").low == 6 and cat.fig("caller.dials_per_day").value == plain.fig("caller.dials_per_day").value
          and cat.fig("funnel.leads_per_deal").value == 12 and cat.high("funnel.leads_per_deal") == 12,
          "a range fits a range figure, one number fits any; a range typed where one number belongs is passed over")
    check(cat.fig("alarm.dials_without_lead").value == plain.fig("alarm.dials_without_lead").value,
          "only the figures the Inputs tab offers can be typed over: an alarm's threshold stays the file's")
    check(cat.monthly_costs() == {"dialer": 300.0, "agency": 1800.0} and cat.input("marketing_started") == "2026-01-15"
          and cat.input("runway_months") == 6.0 and cat.typed["deals"][0]["fee"] == 12000,
          "the typed costs, the business numbers and the deals ride on the catalog")
    check(plain.fig("goal.revenue_per_year").value != 600000 and not plain.typed,
          "the plain catalog is untouched")
    check(kpi.apply_typed(plain, {"found_tab": False}) is plain and kpi.apply_typed(plain, {}) is plain
          and kpi.apply_typed(plain, "junk") is plain,
          "a deleted Inputs tab, an empty read or a bad file leaves the catalog as team/kpi.yaml has it")
    words = ks.in_use(cat, plain)
    check(words["goal.revenue_per_year"] == "Yours: $600,000 a year"
          and words["funnel.dials_per_lead"] == "Could not read 'abc', so the default"
          and words["cost:texting"] == "Blank, not counted" and words["cost:dialer"] == "Yours: $300 a month"
          and words["caller.dials_per_day"] == "Default",
          "In use now says what every report used: yours, the default, or why a typed cell could not be used")

    path = kpi.typed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        loaded = kpi.load()
        check(loaded.fig("goal.revenue_per_year").value == 600000 and kpi.load(kpi.path()).fig("goal.revenue_per_year").value
              == plain.fig("goal.revenue_per_year").value,
              "kpi.load() lays the last read over the file; a path given by name is the file alone")
        data["figures"]["goal.revenue_per_year"] = {"value": 720000}
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        check(kpi.load().fig("goal.revenue_per_year").value == 720000, "a new read is picked up the next time anything loads")
        path.write_text("{not json", encoding="utf-8")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
        check(kpi.load().fig("goal.revenue_per_year").value == plain.fig("goal.revenue_per_year").value,
              "an unreadable read is the file's numbers, never a crash")
    finally:
        path.unlink(missing_ok=True)


def _connect(check, tmp: Path) -> None:
    print("  the one-run sign-in")
    src = Path(__file__).resolve().parents[2] / "deploy" / "kpi_sheet_connect.py"
    spec = importlib.util.spec_from_file_location("kpi_sheet_connect_selftest", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = tmp / "connect.env"
    env.write_bytes(b"# keep me\r\nSLACK_BOT_TOKEN=xoxb-keep\r\nAGENTS_KPI_SHEET_ID=old-id\r\n")
    mod.save_env(env, {"AGENTS_KPI_SHEET_ID": "new-id", "AGENTS_GOOGLE_REFRESH_TOKEN": "rt-test"})
    raw = env.read_bytes().decode("utf-8")
    check(raw.startswith("# keep me\r\nSLACK_BOT_TOKEN=xoxb-keep\r\nAGENTS_KPI_SHEET_ID=new-id\r\n")
          and "AGENTS_GOOGLE_REFRESH_TOKEN=rt-test\r\n" in raw and raw.count("AGENTS_KPI_SHEET_ID") == 1,
          "the sign-in is saved into .env in place: a key's line replaced where it stands, every other line kept")
    check(mod.sheet_id("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit?usp=sharing")
          == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345" and mod.sheet_id("plain-id") == "plain-id",
          "the sheet is named by its link or its id")
    fenv = mod.fly_env()
    check("FLY_API_TOKEN" not in fenv and fenv.get("MSYS_NO_PATHCONV") == "1",
          "flyctl runs on the person's own login, never the repo's one-app token")


def _hygiene(check) -> None:
    root = Path(__file__).resolve().parents[2]
    bad = []
    for rel in ("src/agents/autopilot/kpisheet.py", "src/agents/selftest_kpisheet.py", "deploy/kpi_sheet_connect.py"):
        text = (root / rel).read_text(encoding="utf-8")
        if chr(0x2014) in text or chr(0x2013) in text or "\x08" in text:
            bad.append(rel)
    check(not bad, "no long dash and no backspace byte in the sheet's code: %r" % bad)
