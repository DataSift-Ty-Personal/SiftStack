"""Three-pipelines selftest: the First to Market board (placement for each kind of county record, one
column a business day, a missed day holds, Research to Day 1, Exhausted after Day 5, a lead leaves,
foreclosure urgency), what the other stages ask of the board (the text lane, the mail, the attempt
keeper, the afternoon text), plan mode and undo, the account tool (narrow the Top 1,000, restore the
county folders, make the board) and the playbook blocks handed over, checked so that no stage mixes
the two pipelines.

Zero network: the autopilot's fake DataSift with a fake SiftLine board, and the restructure
selftest's fake account with a fake board and a fake login for the columns. Every name, street,
number and id here is invented.
"""
from __future__ import annotations

import base64
import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, PRESETS, _rec
from .selftest_phase0 import status_ev, tag_ev
from .selftest_textlane import Crm, _wipe, at, call_ev, mob

TZ = "America/New_York"
# The line this track hands the orchestrator for autopilot/registry.py. Until it is in that file the
# section adds it for its own run and takes it out again, so the checks run through the same playbook
# parser and stage runner as every other stage.
REGISTRY = {"ftmpipeline": ("ftmpipeline", "run_ftmpipeline", "free", True, False)}
FTM_PRESETS = ["FTM - 00 Needs Skipped", "FTM - 01 Skipped No Numbers", "FTM - 02 Ready to Call", "FTM - 03 Call Attempt 1",
               "FTM - 04 Call Attempt 2", "FTM - 05 Call Attempt 3", "FTM - 06 Call Attempt 4"]
EXTRA_PRESETS = {
    "FTM - 03 Call Attempt 1": lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 1,
    "FTM - 04 Call Attempt 2": lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 2,
    "FTM - 05 Call Attempt 3": lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 3,
    "FTM - 06 Call Attempt 4": lambda r: "FTM" in r["tags"] and r["owner"]["phones"] and r["predictivecall_attempts"] == 4,
    "Obituary - All": lambda r: "Obituary" in (r.get("lists") or []),
}


def _register(policy, registry):
    added = [k for k in REGISTRY if k not in registry.EXTRA]
    saved = (policy.KINDS, policy.NO_PRESETS)
    spend_added = [k for k in REGISTRY if k not in policy.SPEND_BY_KIND]
    for k in added:
        registry.EXTRA[k] = REGISTRY[k]
    policy.KINDS = tuple(dict.fromkeys(tuple(policy.KINDS) + tuple(REGISTRY)))
    policy.SPEND_BY_KIND.update({k: v[2] for k, v in REGISTRY.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(tuple(policy.NO_PRESETS) + tuple(k for k, v in REGISTRY.items() if not v[3])))

    def restore():
        for k in added:
            registry.EXTRA.pop(k, None)
        for k in spend_added:
            policy.SPEND_BY_KIND.pop(k, None)
        policy.KINDS, policy.NO_PRESETS = saved
    return restore


class Missing(RuntimeError):
    def __init__(self, text):
        super().__init__(text)
        self.code = 404


class BoardCrm(Crm):
    """The fake DataSift plus one SiftLine board. A card listing carries no record id, as the
    reference does not promise one, so the stage has to know its own cards."""
    BOARD_ID = "board-ftm"

    def __init__(self, records, board=True, **kw):
        from .autopilot import ftmpipeline
        super().__init__(records, **kw)
        self.board = board
        self.cols = {t: "col-%d" % i for i, t in enumerate(ftmpipeline.COLUMNS)}
        self.cards: dict = {}            # card id -> {"column", "prop", "since"}
        self.seq = 0
        self.card_writes: list = []
        self.now = None

    def in_column(self, title: str) -> set:
        return {c["prop"] for c in self.cards.values() if c["column"] == self.cols[title]}

    def card_of(self, u: str):
        return next(((k, c) for k, c in self.cards.items() if c["prop"] == u), (None, None))

    def call(self, path, method="GET", body=None, headers=None):
        import urllib.parse
        parts = [p for p in path.split("?")[0].split("/") if p]
        if parts[:3] != ["api", "internal", "siftline"]:
            return super().call(path, method, body, headers)
        q = urllib.parse.parse_qs(path.split("?", 1)[1]) if "?" in path else {}
        rest = parts[3:]
        if rest == ["board"]:
            return {"results": [{"uuid": self.BOARD_ID, "title": "First to Market"}] if self.board else []}
        if len(rest) == 2 and rest[0] == "board":
            return {"uuid": rest[1], "title": "First to Market"}
        if len(rest) >= 4 and rest[:2] == ["board", "column"] and rest[3] == "card":
            col = rest[2]
            if len(rest) == 4:
                if method == "GET":
                    rows = [{"uuid": k, "updated": ""} for k, c in sorted(self.cards.items()) if c["column"] == col]
                    off, lim = int((q.get("offset") or ["0"])[0]), int((q.get("limit") or ["100"])[0])
                    return {"results": rows[off:off + lim]}
                if method == "POST":
                    self.seq += 1
                    k = "card-%d" % self.seq
                    self.cards[k] = {"column": col, "prop": body["prop"], "since": self.now}
                    self.card_writes.append(("create", body["prop"], col))
                    self.writes.append(("card_create", body["prop"], col))
                    return {"uuid": k}
            card = rest[4]
            c = self.cards.get(card)
            if c is None or c["column"] != col:
                raise Missing("HTTP 404 on %s %s" % (method, path))
            if len(rest) == 6 and rest[5] == "timeline":
                return {cu: {"title": t, "order": i, "time": 3600 if cu == col else 0, "current": cu == col}
                        for i, (t, cu) in enumerate(self.cols.items())}
            if method == "GET":
                return {"uuid": card}
            if method == "PATCH":
                c["column"] = body["column"]
                self.card_writes.append(("move", c["prop"], body["column"]))
                self.writes.append(("card_move", c["prop"], body["column"]))
                return {"uuid": card}
            if method == "DELETE":
                del self.cards[card]
                self.card_writes.append(("remove", c["prop"], col))
                self.writes.append(("card_remove", c["prop"], col))
                return {}
        raise AssertionError("the fake board has no route for %s %s" % (method, path))


def world() -> list:
    """Nine county records, one of each kind the board has to place."""
    return [
        _rec("f1", "101 Board St", phones=[mob(6101)]),                                     # a foreclosure, owner known
        _rec("p1", "102 Board St", lists=["Probate"]),                                      # a probate, nobody researched yet
        _rec("p2", "103 Board St", phones=[mob(6103)], lists=["Probate"],
             tags=["FTM", "Courthouse Data", "Deep Prospected"]),                           # a probate research has finished
        _rec("o1", "104 Board St", lists=["Obituary"], tags=["Courthouse Data"]),           # an obituary
        _rec("h1", "105 Board St", phones=[mob(6105)], tags=["FTM", "Courthouse Data", "Foreclosure", "Auction Soon"]),
        _rec("b1", "106 Board St", phones=[mob(6106)], predictivecall_attempts=3),          # called three days before the board
        _rec("l1", "107 Board St", phones=[mob(6107)], status="Warm Lead"),                 # already a lead
        _rec("s1", "108 Board St", phones=[mob(6108)], tags=["FTM", "Courthouse Data", "recently sold"]),
        _rec("n1", "109 Board St"),                                                          # traced, no number yet
    ]


def playbook(board_mode="live", text_mode="live", **board_over) -> dict:
    from .autopilot import ftmpipeline
    board = {"kind": "ftmpipeline", "owner": "ops", "mode": board_mode,
             "schedule": ["02:50 Mon-Fri", "08:35 Mon-Fri", "17:00 Mon-Fri"], "presets": list(FTM_PRESETS),
             "column_ids": {t: "col-%d" % i for i, t in enumerate(ftmpipeline.COLUMNS)}, "text_stage": "p_text",
             "obituary_presets": ["Obituary - All"]}
    board.update(board_over)
    return {
        "business": {"tz": TZ, "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
        "caps": {"daily_usd": 25, "record_usd": 1.0},
        "people": {"callers": ["Adriana"]},
        "lanes": {"ftm": {"tags": ["FTM"]}, "hot_auction": {"tags": ["Auction Soon"], "timeline": True},
                  "top1000": {"tags": ["Top 1000"], "timeline": True}},
        "stages": {
            "p_board": board,
            "p_text": {"kind": "textlane", "owner": "ops", "mode": text_mode, "schedule": "08:45 Mon-Fri", "lane": "ftm",
                       "series": "opening", "pipeline": "p_board",
                       "presets": ["FTM - 02 Ready to Call", "FTM - 03 Call Attempt 1", "FTM - 04 Call Attempt 2"]},
            "p_follow": {"kind": "textlane", "owner": "ops", "mode": text_mode, "schedule": "10:15 Mon-Fri", "lane": "ftm",
                         "series": "followup", "pipeline": "p_board"},
            "p_attempts": {"kind": "attempts", "owner": "ops", "mode": "live", "schedule": "every 60 min 09:00-19:00 Mon-Fri",
                           "lane": "ftm", "pipeline": "p_board", "first_run": "auto", "presets": FTM_PRESETS[2:],
                           "counters": {t: i for i, t in enumerate(FTM_PRESETS[2:])}},
            "p_mail": {"kind": "mail", "owner": "ops", "mode": "plan", "schedule": "10:00 Mon-Fri", "lane": "ftm",
                       "pipeline": "p_board", "vendor": "csv",
                       "pieces": [{"preset": "FTM - Mail 01 Handwritten Letter", "piece": "handwritten_letter"}]},
            "p_full": {"kind": "fullattempt", "owner": "sales", "mode": "plan", "schedule": "16:30 Mon-Fri", "lane": "ftm",
                       "pipeline": "p_board"},
            "p_limits": {"kind": "limits", "owner": "ops", "mode": "plan", "schedule": "19:20 Mon-Fri"},
        },
    }


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import policy, registry
    saved = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.MODE)
    config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = tmp, 100000.0, 5.0
    config.MODE = "live"
    store.init()
    saved_presets = dict(PRESETS)
    PRESETS.update(EXTRA_PRESETS)
    restore = _register(policy, registry)
    try:
        _pure_checks(check)
        _block_checks(check)
        _week_checks(check)
        _plan_checks(check)
        _board_problem_checks(check)
        _person_checks(check)
        _undo_checks(check)
        _more_rules(check)
        _hook_checks(check)
        _textlane_checks(check)
        _attempt_checks(check)
        _afternoon_lane_checks(check)
        _account_checks(check, Path(tmp) / "pipelines")
        _playbook_checks(check)
    finally:
        restore()
        PRESETS.clear()
        PRESETS.update(saved_presets)
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD, config.MODE = saved


def _clean() -> None:
    from . import store
    from .autopilot import activity, limits, scrub
    _wipe(store, activity, limits, scrub)


def _sweep(sid, crm, when, pol):
    from .autopilot import sweep
    crm.now = when
    return sweep.run(sid, crm=crm, now=when, pol=pol)


def _texted(u: str, owner: str, when: datetime, number: str, step: int = 1, batch: int = 0) -> None:
    """A text to this owner handed over on `when`'s day, the way a released batch leaves it."""
    from .autopilot import limits
    bid = batch or (7000 + int(when.strftime("%d")) * 10 + step)
    limits.stage_text(stage="p_text", batch_id=bid, owner_uuid=owner, record_uuid=u,
                      numbers=[{"number": number, "thread": "smarter_contact:FTM 1116 Named East",
                                "send_at": when.astimezone(timezone.utc).isoformat(timespec="seconds")}],
                      system="smarter_contact", series="opening", step=step, touch="p:%s:%d" % (u, step), tz=TZ, when=when)
    limits.release_text(bid, u, [number])


# ---------------------------------------------------------------------- the pieces
def _pure_checks(check) -> None:
    from .autopilot import ftmpipeline as fp
    print("  the pieces")
    days = frozenset(range(5))
    check([fp.column_no(t) for t in fp.COLUMNS] == [0, 1, 2, 3, 4, 5, 6] and fp.column_no("Somewhere") == -1
          and [fp.column_title(n) for n in range(7)] == list(fp.COLUMNS),
          "the board's seven columns read Research 0, Day 1 to Day 5, Exhausted 6, and back")
    from zoneinfo import ZoneInfo
    z = ZoneInfo(TZ)
    check(fp.due_day(at(16, 8, 35), z, days, 540) == date(2026, 11, 16)
          and fp.due_day(at(16, 17, 0), z, days, 540) == date(2026, 11, 17)
          and fp.due_day(at(20, 17, 0), z, days, 540) == date(2026, 11, 23)
          and fp.due_day(at(21, 8, 0), z, days, 540) == date(2026, 11, 23),
          "a card placed before calls start is due that day; one placed later, or on a weekend, the next business day")
    mon, tue, wed = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
    always = (lambda n: True)
    check(fp.judge(1, mon, tue, {mon, tue}, {mon, tue}, always, days)[0] == 3,
          "each business day with its call and its text earns one column")
    check(fp.judge(1, mon, mon, {mon}, {mon}, always, days)[0] == 2,
          "one day earns one column at most, however many calls it carried")
    n, misses = fp.judge(1, mon, tue, {tue}, {mon}, always, days)
    check(n == 2 and misses == [(mon, "no call")],
          "a text sent on a held day still counts once the call comes: the owner is not texted twice for one column")
    n, misses = fp.judge(2, mon, tue, {mon}, set(), always, days)
    check(n == 2 and [m[1] for m in misses] == ["no text", "no call"], "a day with no text, then a day with no call: the card holds")
    check(fp.judge(1, mon, mon, {mon}, set(), lambda k: False, days)[0] == 2, "a day that needs no text moves on the call alone")
    check(fp.judge(5, mon, mon, {mon}, {mon}, always, days)[0] == 6, "the fifth full attempt earns Exhausted")
    check(fp.next_business_day(date(2026, 11, 20), days) == date(2026, 11, 23)
          and fp.back_business_days(date(2026, 11, 23), 1, days) == date(2026, 11, 20),
          "business days skip the weekend both ways")


def _block_checks(check) -> None:
    from .autopilot import policy
    print("  the playbook block")
    pol = policy.parse(copy.deepcopy(playbook()), EMPLOYEES)
    st = pol.stage("p_board")
    check(st.spend == "free" and st.kind == "ftmpipeline", "the board stage is free: it moves cards, it pays nobody")
    for bad, why in (({"column_ids": {"Day 6": "x"}}, "a column the board does not have"),
                     ({"text_days": [0, 7]}, "a text day outside the round"),
                     ({"require_text": "sometimes"}, "a text rule that is not auto, always or never"),
                     ({"call_alone_lanes": ["hotauction"]}, "a lane that does not exist"),
                     ({"late_after": "5pm"}, "a time that does not read like 17:00"),
                     ({"lead_column": "Leads"}, "a lead column the board does not have")):
        try:
            policy.parse(playbook(**bad), EMPLOYEES)
            check(False, "a block with %s is refused when the playbook loads" % why)
        except policy.PolicyError:
            check(True, "a block with %s is refused when the playbook loads" % why)
    try:
        policy.parse(playbook(presets=[]), EMPLOYEES)
        check(False, "a board stage with no preset is refused: it reads the county presets")
    except policy.PolicyError:
        check(True, "a board stage with no preset is refused: it reads the county presets")


# ---------------------------------------------------------------------- a week on the board
def _week_checks(check) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import lanes, ledger, policy
    print("  a week on the board")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    crm = BoardCrm(world())

    s = _sweep("p_board", crm, at(16, 8, 35), pol)
    where = {u: (ledger.fact(u, fp.FACT) or {}).get("column") for u in ("f1", "p1", "p2", "o1", "h1", "b1", "l1", "s1", "n1")}
    check(where == {"f1": "Day 1", "p1": "Research", "p2": "Day 1", "o1": "Research", "h1": "Day 1", "b1": "Day 4",
                    "l1": None, "s1": None, "n1": "Day 1"},
          "each county record is placed where it belongs: %s" % where)
    check(crm.in_column("Day 1") == {"f1", "p2", "h1", "n1"} and crm.in_column("Research") == {"p1", "o1"}
          and crm.in_column("Day 4") == {"b1"}, "and the cards are on the board in those columns")
    check(s.live and "Placed 7" in s.head and s.tally.get("not placed: %s" % fp.DONE_LEAD) == 1
          and s.tally.get("not placed: the house sold") == 1,
          "a lead and a sold house are not placed, and the run says why: %s" % s.head)
    made = [c for c in ledger.changes() if c["what"] == "card_create"]
    check(len(made) == 7 and all((c["after"] or {}).get("column") for c in made),
          "every card it placed is in the change log, with its column")
    check(not any(ledger.fact(u, fp.PLAN_FACT) for u in ("f1", "p1")), "a live run never writes the plan's simulated board")

    # Monday: two calls and a text to the foreclosure, a call to the hot auction and to the researched
    # probate, and a 9pm dial to the record called before the board.
    crm.log_rows += [call_ev("w1", "o-f1", at(16, 10, 0), "8655556101"), call_ev("w2", "o-f1", at(16, 14, 0), "8655556101"),
                     call_ev("w3", "o-h1", at(16, 11, 0), "8655556105"), call_ev("w4", "o-p2", at(16, 11, 30), "8655556103"),
                     call_ev("w5", "o-b1", at(16, 20, 30), "8655556106")]
    _texted("f1", "o-f1", at(16, 10, 5), "8655556101")
    s = _sweep("p_board", crm, at(17, 8, 35), pol)
    where = {u: (ledger.fact(u, fp.FACT) or {}).get("column") for u in ("f1", "h1", "p2", "b1", "n1")}
    check(where["f1"] == "Day 2", "a day with its call and its text moves the card one column, even with two calls in it")
    check(where["h1"] == "Day 2", "foreclosure urgency: a sale inside the window moves on its call alone, as its lane always counted")
    check(where["p2"] == "Day 1" and "no text on Mon Nov 16" in s.head,
          "a day with a call and no text holds the card, and the run says so: %s" % s.head)
    check(where["b1"] == "Day 4" and "no call on Mon Nov 16" in s.head,
          "a dial outside calling hours does not count: the card holds on its day")
    check(where["n1"] == "Day 1" and "no number a caller may dial yet" in s.head, "a record with no number holds on Day 1 until one lands")
    check(s.tally.get("in Research: no decision maker named yet") == 1
          and any(k.startswith("in Research: a possible death") for k in s.tally),
          "a probate waits in Research for a decision maker, an obituary for research on the death")
    n0 = len(crm.card_writes)
    s2 = _sweep("p_board", crm, at(17, 8, 40), pol)
    check(len(crm.card_writes) == n0 and (ledger.fact("f1", fp.FACT) or {}).get("column") == "Day 2",
          "a second pass the same morning moves nothing: one column a business day")

    # Tuesday: the foreclosure gets its call and text 2, the hot auction its call, and the held
    # probate gets its text but no call.
    crm.log_rows += [call_ev("w6", "o-f1", at(17, 10, 0), "8655556101"), call_ev("w7", "o-h1", at(17, 10, 30), "8655556105")]
    _texted("f1", "o-f1", at(17, 10, 5), "8655556101", step=2)
    _texted("p2", "o-p2", at(17, 10, 10), "8655556103", step=1)
    s = _sweep("p_board", crm, at(18, 8, 35), pol)
    check((ledger.fact("f1", fp.FACT) or {}).get("column") == "Day 3" and (ledger.fact("h1", fp.FACT) or {}).get("column") == "Day 3",
          "the next full attempt moves each card on again")
    check((ledger.fact("p2", fp.FACT) or {}).get("column") == "Day 1" and "no call on Tue Nov 17" in s.head,
          "the held probate had its text but no call, so it holds again")

    # Wednesday: the held probate gets its call; the foreclosure's owner becomes a lead; research
    # names a decision maker on the first probate and a number lands.
    crm.log_rows += [call_ev("w8", "o-p2", at(18, 10, 0), "8655556103"), call_ev("w9", "o-h1", at(18, 10, 30), "8655556105"),
                     status_ev("w10", "f1", at(18, 12, 0), None, "Warm Lead"),
                     tag_ev("w11", "p1", at(18, 12, 30), ["Deep Prospected"])]
    crm.recs["f1"]["status"] = "Warm Lead"
    crm.recs["p1"]["tags"].append("Deep Prospected")
    crm.recs["p1"]["owner"]["phones"] = [mob(6102)]
    s = _sweep("p_board", crm, at(19, 8, 35), pol)
    check((ledger.fact("p2", fp.FACT) or {}).get("column") == "Day 2",
          "a call the day after its text completes the held day: the probate moves to Day 2")
    check(ledger.fact("f1", fp.FACT) is None and "f1" not in {c["prop"] for c in crm.cards.values()}
          and any(c["what"] == "card_remove" and c["record_uuid"] == "f1" for c in ledger.changes()),
          "a lead status takes the card off the board, with the removal in the change log")
    p1 = ledger.fact("p1", fp.FACT) or {}
    check(p1.get("column") == "Day 1" and p1.get("round_start") == "2026-11-19",
          "research naming a decision maker with a number moves the probate from Research to Day 1, and its round starts")
    check((ledger.fact("h1", fp.FACT) or {}).get("column") == "Day 4", "the hot auction keeps one column a day")

    # The late pass on Thursday: the hot auction was called this morning; nothing on a day moves,
    # and what today still lacks is said.
    crm.log_rows += [call_ev("w12", "o-h1", at(19, 10, 0), "8655556105")]
    n0 = len(crm.card_writes)
    s = _sweep("p_board", crm, at(19, 17, 0), pol)
    check(len(crm.card_writes) == n0 and (ledger.fact("h1", fp.FACT) or {}).get("column") == "Day 4",
          "the late pass never moves a day column, even for a card whose attempt is already in")
    check("Short today" in s.head and "no call yet today on Day 2" in s.head and s.acted >= 1
          and s.tally.get("today's full attempt is in") == 1,
          "the late pass names the cards still short of today's call, and counts the ones that are done: %s" % s.head)

    # Friday the hot auction is called again; the next Monday it reaches Exhausted.
    _sweep("p_board", crm, at(20, 8, 35), pol)
    crm.log_rows += [call_ev("w13", "o-h1", at(20, 10, 0), "8655556105")]
    s = _sweep("p_board", crm, at(23, 8, 35), pol)
    check((ledger.fact("h1", fp.FACT) or {}).get("column") == "Exhausted" and "h1" in crm.in_column("Exhausted"),
          "five full attempts take the card to Exhausted")
    check(any("hot auctions" in ln for ln in s.lines),
          "the run says a hot auction in Exhausted keeps its lane's call tasks until the sale")
    ok, why = lanes.channel_open(pol.lanes, "hot_auction", "call", 9, 5)
    check(ok and "every 2 days" in why, "foreclosure urgency: past the round the hot auction lane still calls (%s)" % why)
    check(not any(chr(0x2014) in x or chr(0x2013) in x for x in [s.head] + s.lines + s.problems),
          "nothing the board says carries a dash")


def _plan_checks(check) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import ledger, policy
    print("  plan mode")
    _clean()
    pol = policy.parse(playbook(board_mode="plan", text_mode="plan"), EMPLOYEES)
    crm = BoardCrm(world())
    s = _sweep("p_board", crm, at(16, 8, 35), pol)
    check(not s.live and s.head.startswith("Would place 7") and not crm.card_writes and not crm.writes,
          "a plan run says what it would place and writes nothing to DataSift: %s" % s.head)
    check(all(ledger.fact(u, fp.FACT) is None for u in ("f1", "p1", "h1")) and (ledger.fact("f1", fp.PLAN_FACT) or {}).get("column") == "Day 1",
          "the plan's board lives in its own memory, never where the other stages look")
    check(not ledger.changes(), "a plan run records no change to put back")
    crm.log_rows += [call_ev("pl1", "o-f1", at(16, 10, 0), "8655556101")]
    s = _sweep("p_board", crm, at(17, 8, 35), pol)
    check((ledger.fact("f1", fp.PLAN_FACT) or {}).get("column") == "Day 2" and "Would move" in s.head and not crm.card_writes,
          "on the second morning the plan shows the move the live board would make, with the texts not live yet")
    check(any("The county texts are not live yet" in ln for ln in s.lines),
          "and says that while the texts are not live a day's call alone completes it")


def _board_problem_checks(check) -> None:
    from .autopilot import policy
    print("  a board the stage cannot see")
    _clean()
    pol = policy.parse(playbook(column_ids={}), EMPLOYEES)
    crm = BoardCrm(world())
    s = _sweep("p_board", crm, at(16, 8, 35), pol)
    check(s.problems and "column_ids" in " ".join(s.problems) and not crm.card_writes and "not known" in s.head,
          "live, with the board's columns unknown, it places nothing and says which setting to fill")


def _person_checks(check) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import ledger, policy
    print("  cards a person moves")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    crm = BoardCrm(world())
    _sweep("p_board", crm, at(16, 8, 35), pol)
    k, c = crm.card_of("n1")
    c["column"] = crm.cols["Day 3"]
    kb, _ = crm.card_of("b1")
    del crm.cards[kb]
    s = _sweep("p_board", crm, at(17, 8, 35), pol)
    check((ledger.fact("n1", fp.FACT) or {}).get("column") == "Day 3" and s.tally.get("cards a person moved (taken where they put them)") == 1,
          "a card a person moved is taken where they put it")
    check(ledger.fact("b1", fp.FACT) is None and ledger.fact("b1", fp.OFF_FACT)
          and "b1" not in {x["prop"] for x in crm.cards.values()}
          and s.tally.get("taken off the board by a person (left off)") == 1,
          "a card a person took off is not put back")


def _undo_checks(check) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import ledger, policy, undo
    print("  undo")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    crm = BoardCrm([_rec("u1", "201 Undo St", phones=[mob(6201)])])
    _sweep("p_board", crm, at(16, 8, 35), pol)
    crm.log_rows += [call_ev("u-1", "o-u1", at(16, 10, 0), "8655556201")]
    _texted("u1", "o-u1", at(16, 10, 5), "8655556201")
    _sweep("p_board", crm, at(17, 8, 35), pol)
    runs = [r["run_id"] for r in ledger.runs() if r["stage"] == "p_board"]
    check(len(runs) == 2, "each live run that changed the board is a run undo can find")
    steps = undo.plan(runs[0]) + undo.plan(runs[1])
    check(len(steps) == 2 and {st["what"] for st in steps} == {"card_move", "card_create"}
          and all(st["do"] == "lead_route" for st in steps), "a card move and a card placement both have an undo step")
    check(crm.in_column("Day 2") == {"u1"}, "before the undo the card sits in Day 2")
    res = undo.run(runs[0], crm=crm, commit=True)
    check(res.get("undone") == 1 and crm.in_column("Day 1") == {"u1"}, "undoing the move puts the card back in Day 1")
    res = undo.run(runs[1], crm=crm, commit=True)
    check(res.get("undone") == 1 and not crm.cards and ledger.fact("u1", fp.FACT) is None,
          "undoing the placement takes the card off the board, from wherever it sits")


def _digits_in(summary) -> bool:
    """True when a stage's whole output carries a ten-digit run: a phone number in full."""
    import re
    return bool(re.search(r"\d{10}", json.dumps(summary.as_dict(), default=str)))


def _more_rules(check) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import ledger, policy
    print("  the rules that take a card off, or back")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    recs = [_rec("m1", "601 Rule St", phones=[mob(6601)]), _rec("m2", "602 Rule St", phones=[mob(6602)]),
            _rec("m3", "603 Rule St", phones=[mob(6603)])]
    crm = BoardCrm(recs)
    s = _sweep("p_board", crm, at(16, 8, 35), pol)
    check(not _digits_in(s), "a board run never shows a phone number in full")
    crm.log_rows += [tag_ev("r1", "m1", at(16, 12, 0), ["Do Not Market"]), tag_ev("r2", "m2", at(16, 12, 0), ["Verify Deceased"])]
    crm.recs["m1"]["tags"].append("Do Not Market")
    crm.recs["m2"]["tags"].append("Verify Deceased")
    s = _sweep("p_board", crm, at(17, 8, 35), pol)
    check(ledger.fact("m1", fp.FACT) is None and "m1" not in {c["prop"] for c in crm.cards.values()}
          and any("Do Not Market" in k for k in s.head.split(";")),
          "Do Not Market takes the card off the board for good: %s" % s.head)
    check((ledger.fact("m2", fp.FACT) or {}).get("column") == "Research",
          "a possible death on a record whose owner was known sends its card back to Research before anyone calls it")
    _clean()
    pol = policy.parse(playbook(lead_column="Exhausted"), EMPLOYEES)
    crm = BoardCrm([_rec("m4", "604 Rule St", phones=[mob(6604)])])
    _sweep("p_board", crm, at(16, 8, 35), pol)
    crm.log_rows += [status_ev("r3", "m4", at(16, 12, 0), None, "Hot Lead")]
    crm.recs["m4"]["status"] = "Hot Lead"
    _sweep("p_board", crm, at(17, 8, 35), pol)
    check((ledger.fact("m4", fp.FACT) or {}).get("column") == "Exhausted" and "m4" in crm.in_column("Exhausted"),
          "with lead_column set, a lead's card moves to that column instead of leaving the board")
    _clean()
    pol = policy.parse(playbook(max_reads=2), EMPLOYEES)
    crm = BoardCrm(recs)
    s = _sweep("p_board", crm, at(16, 8, 35), pol)
    check(len(crm.cards) == 2 and s.tally.get("waiting to be placed (the run's read limit; next run)") == 1,
          "a run opens no more records than its read limit, and the rest wait for the next run")


def _afternoon_lane_checks(check) -> None:
    from .autopilot import policy
    print("  the afternoon text reads each record's own lane")
    _clean()
    book = playbook()
    book["stages"]["p_full"].update({"mode": "live", "afternoon_text": True, "sender": "Adriana",
                                     "lanes": ["ftm", "top1000"], "pipeline": ""})
    pol = policy.parse(book, EMPLOYEES)
    r = _rec("q1", "701 Lane St", phones=[mob(6701)], tags=["Top 1000"])
    crm = BoardCrm([r])
    crm.land("q1", at(10, 9, 0), tags=("Top 1000",))
    crm.log_rows += [call_ev("q-1", "o-q1", at(16, 15, 0), "8655556701")]
    s = _sweep("p_full", crm, at(16, 16, 30), pol)
    check(not any(k.startswith("dialed today, not in the") for k in s.tally),
          "a dialed Top 1,000 owner is read in its own lane, not passed over as outside the one lane the stage once read")
    book["stages"]["p_full"].update({"lanes": None})
    s = _sweep("p_full", crm, at(16, 16, 30), policy.parse(book, EMPLOYEES))
    check(s.tally.get("dialed today, not in the ftm lane: no text") == 1, "a stage that names one lane still reads that lane only")


# ---------------------------------------------------------------------- what the other stages ask
def _ctx(pol, sid, crm, when):
    from .autopilot import stages
    st = pol.stage(sid)
    return stages.Ctx(policy=pol, stage=st, crm=crm, run_id="t-%s" % sid, now=when, live=True,
                      out=stages.Summary(st.id, st.kind, st.owner, st.mode, True))


def _put(u: str, column: str, entered: datetime, **extra) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import ledger
    f = {"card": "card-%s" % u, "column": column, "column_uuid": "col-x", "entered": entered.isoformat(timespec="seconds"),
         "due": entered.date().isoformat(), "round_start": "2026-11-16", "owner": "o-" + u, "lane": "ftm",
         "checked": entered.isoformat(timespec="seconds")}
    f.update(extra)
    ledger.set_fact(u, fp.FACT, f)


def _hook_checks(check) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import mail, policy
    print("  what the other stages ask the board")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    crm = BoardCrm([_rec("k1", "301 Hook St", phones=[mob(6301)], tags=["FTM", "Courthouse Data"]),
                    _rec("k2", "302 Hook St", phones=[mob(6302)])])
    ctx = _ctx(pol, "p_text", crm, at(18, 8, 45))
    today = date(2026, 11, 18)
    _put("k1", "Research", at(16, 8, 35))
    check(fp.text_slot(ctx, "k1", "ftm", "opening", today) == {"hold": "in Research on the board: research has not named a decision maker with a number yet"},
          "a card in Research gets no text")
    _put("k1", "Day 2", at(17, 8, 35))
    sl = fp.text_slot(ctx, "k1", "ftm", "opening", today)
    check(sl and sl["day"] == 1 and sl["counter"] == 1 and sl["max_step"] == 2 and sl["since"] == "2026-11-16",
          "Day 2 stands for the second opening text, one full attempt done: %s" % sl)
    check(fp.text_slot(ctx, "k1", "ftm", "followup", today) == {"hold": "the follow-up texts start when the card reaches Exhausted"},
          "the follow-up texts wait for Exhausted")
    _put("k1", "Exhausted", at(16, 8, 35))
    sl = fp.text_slot(ctx, "k1", "ftm", "followup", today)
    check(sl and sl["day"] == 10 and sl["counter"] == 5, "in Exhausted the follow-up series starts on the timeline's day 8 (%s)" % sl)
    sl = fp.text_slot(ctx, "k1", "ftm", "opening", today)
    check(sl and sl["day"] > 2 and sl["max_step"] == 0, "and the opening series is closed")
    check(fp.text_slot(ctx, "k1", "top1000", "opening", today) is None, "a Top 1,000 record keeps its own lane days")
    plan_pol = policy.parse(playbook(board_mode="plan"), EMPLOYEES)
    check(fp.text_slot(_ctx(plan_pol, "p_text", crm, at(18, 8, 45)), "k1", "ftm", "opening", today) is None,
          "until the board is live nothing follows it, so a text stage switched live first changes nothing")
    config.MODE = "dry"
    check(fp.text_slot(ctx, "k1", "ftm", "opening", today) is None, "on a dry box the board is not live either")
    config.MODE = "live"
    check(fp.counter_for(ctx, "k1", "ftm") == 5 and fp.counter_for(ctx, "k2", "ftm") is None,
          "the attempt keeper's counter is the board's count, and a record not on the board keeps its call days")
    _put("k1", "Day 3", at(17, 8, 35))
    check(fp.counter_for(ctx, "k1", "ftm") == 2, "Day 3 has had two full attempts")
    mctx = _ctx(pol, "p_mail", crm, at(18, 10, 0))
    _put("k2", "Day 2", at(17, 8, 35))
    ok, why = mail.mail_open(mctx, crm.record("k2"))
    check(ok and "foreclosure" in why, "foreclosure urgency: a foreclosure on Day 2 is mailed at once, as its timeline says (%s)" % why)
    ok, why = mail.mail_open(mctx, crm.record("k1"))
    check(not ok and "day 8" in why, "a record without that urgency waits for Exhausted (%s)" % why)
    _put("k1", "Exhausted", at(16, 8, 35))
    check(mail.mail_open(mctx, crm.record("k1"))[0], "in Exhausted the monthly mail begins")
    _put("k1", "Exhausted", at(16, 8, 35), backlog=True)
    check(fp.mail_point(mctx, "k1", "ftm", today) is None and fp.text_slot(ctx, "k1", "ftm", "followup", today) is None,
          "a record whose round ended before the board keeps the lane's own mail and follow-up days")
    _put("k1", "Research", at(16, 8, 35))
    check(fp.mail_point(mctx, "k1", "ftm", today) is None, "a record waiting on research keeps the lane's own mail days")
    fctx = _ctx(pol, "p_full", crm, at(18, 16, 30))
    _put("k1", "Day 4", at(18, 8, 35))
    check((fp.afternoon_day(fctx, "k1", "ftm", today) or {}).get("day") == 3,
          "the afternoon text's day is the card's column: Day 4 is a call day after the opening texts")


def _textlane_checks(check) -> None:
    from .autopilot import ledger, policy, sweep
    print("  the county texts follow the board")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    recs = [_rec("t1", "401 Text St", phones=[mob(6401)]), _rec("t2", "402 Text St", phones=[mob(6402)]),
            _rec("t3", "403 Text St", phones=[mob(6403)]), _rec("t4", "404 Text St", phones=[mob(6404)])]
    crm = BoardCrm(recs)
    for u in ("t1", "t2", "t3", "t4"):
        ledger.set_fact(u, "death_screen", {"at": "2026-11-10T12:00:00+00:00", "hit": "", "detail": ""})
    _put("t1", "Day 1", at(17, 8, 35), round_start="2026-11-17")
    _put("t2", "Day 2", at(17, 8, 35))
    _put("t3", "Research", at(16, 8, 35))
    _put("t4", "Day 2", at(17, 8, 35))
    _texted("t2", "o-t2", at(16, 10, 0), "8655556402", step=1)
    _texted("t2", "o-t2", at(17, 10, 0), "8655556402", step=2)
    _texted("t4", "o-t4", at(16, 10, 0), "8655556404", step=1)
    s = sweep.run("p_text", crm=crm, now=at(18, 8, 45), pol=pol)
    b = ledger.batch(s.batch) if s.batch else None
    staged = {(it["record_uuid"], it["step"]) for it in (b or {}).get("items") or []}
    check(staged == {("t1", 1), ("t4", 2)}, "the text lane stages the text each card's column stands for: %s" % sorted(staged))
    check(s.tally.get("the text for the card's day went already: the card waits on its call") == 1,
          "a card held on its day after its text went is not texted again")
    check(s.tally.get("in Research on the board: research has not named a decision maker with a number yet") == 1,
          "a card in Research is not texted")


def _attempt_checks(check) -> None:
    from .autopilot import activity, attempts, policy
    print("  the attempt keeper on the board")
    _clean()
    pol = policy.parse(playbook(), EMPLOYEES)
    crm = BoardCrm([_rec("a1", "501 Count St", phones=[mob(6501)])])
    crm.land("a1", at(9, 9, 0))
    crm.log_rows += [call_ev("a-%d" % d, "o-a1", at(d, 10, 0), "8655556501") for d in (16, 17, 18, 19)]
    activity.sync(crm, "calls", now=at(20, 9, 30))
    _put("a1", "Day 3", at(19, 8, 35))
    ctx = _ctx(pol, "p_attempts", crm, at(20, 9, 30))
    ch = attempts.plan_changes(ctx)
    check([(c["record_uuid"], c["want"], c["board"]) for c in ch] == [("a1", 2, "Day 3")],
          "on a live board the counter is written from the board's count (two full attempts), not the four call days")
    plan_pol = policy.parse(playbook(board_mode="plan"), EMPLOYEES)
    ch = attempts.plan_changes(_ctx(plan_pol, "p_attempts", crm, at(20, 9, 30)))
    check([(c["want"], c["board"]) for c in ch] == [(4, "")], "with the board not live it counts the call days, as it always did")


# ---------------------------------------------------------------------- the account tool
class _Jwt:
    def __init__(self, account):
        body = base64.urlsafe_b64encode(json.dumps({"account": account}).encode()).decode().rstrip("=")
        self.token = "x.%s.y" % body


def _board_account(rs_mod):
    from . import selftest_restructure as srt

    class BoardAccount(srt.FakeAccount):
        """The restructure selftest's fake account, with SiftLine boards. Its key refuses the column
        route, as the real key does: a run that tried it would stop."""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.boards: dict = {}
            self.key_columns: list = []      # any column route that reached the key: there must be none

        def call(self, path, method="GET", body=None, headers=None):
            parts = [p for p in path.split("?")[0].split("/") if p]
            if parts[:4] != ["api", "internal", "siftline", "board"]:
                return super().call(path, method, body, headers)
            self.calls += 1
            self.log.append((method, path))
            rest = parts[4:]
            if "column" in rest:
                self.key_columns.append((method, path))
            if not rest and method == "GET":
                return {"results": [{"uuid": b["uuid"], "title": b["title"]} for b in self.boards.values()]}
            if not rest and method == "POST":
                u = self._new("board", body["title"])
                self.boards[u] = {"uuid": u, "title": body["title"], "columns": []}
                return {"uuid": u, "title": body["title"]}
            if len(rest) == 1 and method == "GET":
                b = self.boards.get(rest[0])
                if b is None:
                    raise self._no(method, path)
                return {"uuid": b["uuid"], "title": b["title"]}
            raise self._no(method, path, 403, '{"detail": "You do not have permission to perform this action."}')

    class Login:
        """This workstation's login: the only client that reaches the column route."""

        def __init__(self, acct, account):
            self.acct, self.api, self.calls, self.posts = acct, _Jwt(account), 0, []

        def call(self, path, method="GET", body=None, headers=None):
            self.calls += 1
            parts = [p for p in path.split("?")[0].split("/") if p]
            b = self.acct.boards[parts[4]]
            if method == "GET":
                return {"results": [dict(c) for c in sorted(b["columns"], key=lambda c: c["order"])]}
            self.posts.append(body["title"])
            c = {"uuid": self.acct._new("column", body["title"]), "title": body["title"], "order": body["order"]}
            b["columns"].append(c)
            return dict(c)

    extra = {"11. DEEP PROSPECTING (ALL TIERS)": [("Obituary - All", {"any_lists": [srt._id("list", "Obituary")]})]}
    return srt, BoardAccount(folders=srt.account(extra=extra)), Login


def _account_checks(check, tmp: Path) -> None:
    from .autopilot import ftmpipeline as fp
    from .autopilot import pipelines as pl
    from .autopilot import preset_patch as pp
    from .autopilot import restructure as rs
    print("  the account tool")
    tmp.mkdir(parents=True, exist_ok=True)
    srt, fake, Login = _board_account(rs)
    # The account as the 11-13 restructure left it.
    with srt.environ({rs.ALLOW_ENV: "1"}):
        rs.apply_plan(fake, rs.build_plan(pp.Reader(fake, now=srt.DAY, max_requests=100000), day="2026-11-13"), commit=True,
                      log_path=tmp / "restructure.jsonl", backup_dir=tmp / "bk_rs", out=srt._quiet)
    ftm, top = srt._id("tag", "FTM"), next(u for u, t in fake.tags.items() if t == "Top 1000")
    obit = srt._id("list", "Obituary")
    login = Login(fake, srt.ACCOUNT)

    def build():
        return pl.build_plan(pp.Reader(fake, now=date(2026, 11, 14), max_requests=100000), day="2026-11-14", columns_reader=login)

    plan = build()
    s = plan["summary"]
    check(not plan["blocking"] and s["presets_to_narrow"] == 9 and s["folders_to_rename"] == 5 and s["board"] == "create"
          and s["columns_to_create"] == 7, "the plan narrows the nine Priority presets, renames five folders and makes the board: %s" % s)
    bad = []
    for r in plan["narrow"]:
        a, b = copy.deepcopy(r["must_after"]), copy.deepcopy(r["must_before"])
        if a.get("any_tags") != [top] or ftm not in a["must_not"]["any_tags"] or obit not in a["must_not"]["any_lists"]:
            bad.append(r["title"])
        for m in (a, b):
            m.pop("any_tags", None)
            m["must_not"]["any_tags"] = [t for t in m["must_not"].get("any_tags") or [] if t != ftm]
            m["must_not"]["any_lists"] = [t for t in m["must_not"].get("any_lists") or [] if t != obit]
        if a != b:
            bad.append(r["title"] + " (other keys)")
    check(not bad, "each narrowed preset is anchored on Top 1000, skips FTM and the Obituary list, every other key exact: %s" % (bad or "ok"))
    saved = pl.TOP_SKIPS_COUNTY
    pl.TOP_SKIPS_COUNTY = False
    m, why, _ = pl.narrow_must(plan["narrow"][0]["must_before"], ftm, top, obit)
    pl.TOP_SKIPS_COUNTY = saved
    exact = copy.deepcopy(plan["narrow"][0]["must_before"])
    exact["any_tags"] = [top]
    check(m == exact, "with TOP_SKIPS_COUNTY off the narrowing changes the anchor and nothing else")
    county = {f["new_title"]: f["action"] for f in plan["folders"] if f["kind"] == "county"}
    check(county == {"05. FTM - CALL": "rename", "06. FTM - MAIL": "rename"} and plan["restore"]
          and all(r["county"] for r in plan["restore"]),
          "the county folders come back from the archive, holding presets that read FTM alone")
    check(plan["obituary_presets"].get("Obituary - All") is not None, "obituary records have their own door, counted")
    lines = pl.plan_lines(plan) + [pl.render_md(plan)]
    check(not any(chr(0x2014) in x or chr(0x2013) in x for x in lines), "the change list carries no dash")

    n0 = len(fake.writes())
    with srt.environ({pl.ALLOW_ENV: None}):
        dry = pl.apply_plan(fake, plan, commit=True, out=srt._quiet)
    check(dry["allowed"] is False and dry["would"] and len(fake.writes()) == n0, "apply is dry without PIPELINES_ALLOW=1")
    with srt.environ({pl.ALLOW_ENV: "1"}):
        refused = srt._raises(lambda: pl.apply_plan(fake, plan, commit=True, log_path=tmp / "x.jsonl",
                                                    backup_dir=tmp / "bk_x", columns_crm=None, out=srt._quiet), SystemExit)
        other = srt._raises(lambda: pl.apply_plan(fake, plan, commit=True, log_path=tmp / "y.jsonl", backup_dir=tmp / "bk_y",
                                                  columns_crm=Login(fake, "another-account"), out=srt._quiet), SystemExit)
    check(refused and other and len(fake.writes()) == n0,
          "apply refuses before any write without this workstation's login, or with a login to another account")
    with srt.environ({pl.ALLOW_ENV: "1"}):
        done = pl.apply_plan(fake, plan, commit=True, log_path=tmp / "main.jsonl", backup_dir=tmp / "bk", columns_crm=login,
                             out=srt._quiet)
    pri = [p for p in fake.presets.values() if p["title"].startswith("Priority - ")]
    check(all(p["filters"]["must"]["any_tags"] == [top] for p in pri) and len(pri) == 9,
          "after apply every Priority preset reads the Top 1,000 only")
    titles = set(fake.folder_titles())
    check({"01. TOP 1000 - TEXT", "02. TOP 1000 - CALL", "03. TOP 1000 - MAIL", "05. FTM - CALL", "06. FTM - MAIL"} <= titles
          and not {"02. PRIORITY - CALL", "99. Archive - 05. FTM - CALL"} & titles,
          "the Priority folders say Top 1000 and the county folders are back")
    board = next(iter(fake.boards.values()), {})
    check(board.get("title") == fp.BOARD and [c["title"] for c in sorted(board["columns"], key=lambda c: c["order"])] == list(fp.COLUMNS),
          "the board is made with its seven columns in order")
    check(done["column_ids"] == {c["title"]: c["uuid"] for c in board["columns"]},
          "apply hands back every column's id for the stage's column_ids")
    check(not fake.key_columns and login.posts == list(fp.COLUMNS),
          "the columns go through this workstation's login, never the key, and the brain never holds that login")
    bk = json.loads(Path(done["backup"]).read_text(encoding="utf-8"))
    check(Path(done["backup"]).name.startswith("presets_ty2_2026-11-14_before_pipelines") and bk["folders"],
          "apply writes a backup of every folder, preset and board before its first write")
    nw, np_ = len(fake.writes()), len(login.posts)
    with srt.environ({pl.ALLOW_ENV: "1"}):
        again = pl.apply_plan(fake, plan, commit=True, log_path=tmp / "main.jsonl", backup_dir=tmp / "bk", columns_crm=login,
                              out=srt._quiet)
    check(len(fake.writes()) == nw and len(login.posts) == np_ and not again["made"], "a second apply changes nothing")
    p2 = build()
    check(not p2["blocking"] and p2["summary"]["presets_to_narrow"] == 0 and p2["summary"]["folders_to_rename"] == 0
          and p2["board"]["action"] == "exists" and all(c["action"] == "exists" for c in p2["board"]["columns"]),
          "a plan after apply proposes nothing, the board's columns listed with the login")
    with srt.environ({pl.ALLOW_ENV: "1"}):
        res = pl.undo(fake, tmp / "main.jsonl", commit=True, out=srt._quiet)
    pri = [p for p in fake.presets.values() if p["title"].startswith("Priority - ")]
    check(all(p["filters"]["must"]["any_tags"] == [ftm, top] for p in pri) and "02. PRIORITY - CALL" in fake.folder_titles()
          and "99. Archive - 05. FTM - CALL" in fake.folder_titles(),
          "undo puts every preset filter and folder name back")
    check(any("app-only" in x for x in res["left"]) and fake.boards, "undo leaves the board and says a board delete is app-only")
    nofake = srt.FakeAccount()
    p3 = pl.build_plan(pp.Reader(nofake, now=date(2026, 11, 14), max_requests=100000), day="2026-11-14")
    check(any("Top 1000 does not exist" in b for b in p3["blocking"]),
          "an account the two-track restructure never reached is blocked: there is no Top 1000 tag to narrow to")


# ---------------------------------------------------------------------- the playbook handed over
PLAYBOOK_BLOCKS = """
  ftm_pipeline:
    kind: ftmpipeline
    owner: ops
    mode: "plan"
    schedule: ["02:50 Mon-Fri", "08:35 Mon-Fri", "17:00 Mon-Fri"]   # the night and the morning judge the days that are over; 5pm says who is short today
    summary: Every county record on the First to Market board, Research, Day 1 to Day 5, Exhausted, one column a business day and only after that day's call and text.
    # Three pipelines (2026-11-14): county records on this board, the Top 1,000 on its own presets,
    # the agency outside. A probate or obituary record waits in Research for a decision maker with a
    # number; a record whose owner is known starts at Day 1. A day without its full attempt holds the
    # card. Any lead status takes the card off the board. Before going live: run the account tool
    # (python src/agents/autopilot/pipelines.py plan, then apply) and put the column ids it prints
    # under column_ids.
    board: First to Market
    column_ids: {}
    presets:
      - FTM - 00 Needs Skipped
      - FTM - 01 Skipped No Numbers
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    obituary_presets: [Obituary - All]       # a preset cannot say "tag FTM or list Obituary": this is the board's second door
    research_first: [Probate, Obituary]
    researched_tags: [Deep Prospected, Heir Identified, Heirs Resolved]
    dm_fields: [Decision Maker]
    off_board_tags: [Wrong House]
    text_days: [1, 2, 3]            # the opening texts; add 4 and 5 once the afternoon text is live (full_attempt)
    require_text: auto              # the text half counts only while ftm_texts is live on a live box
    text_stage: ftm_texts
    call_alone_lanes: [hot_auction] # a sale inside the window moves on its call alone, as that lane always counted it
    lead_column: ""                 # a lead's card leaves the board; the lead board owns it
    backlog: counter                # a record called before the board went live starts where its counter says
    max_reads: 200
    night_reads: 400
    max_creates: 200
    max_moves: 300

  ftm_death_screen:
    kind: screen
    owner: research
    mode: "plan"
    schedule: "every 30 min 07:00-19:00 daily"    # ahead of assign and the first text
    summary: New first-to-market records checked for a death before any text or assignment, and held for research when one may have happened.
    lane: ftm
    presets:
      - FTM - 00 Needs Skipped
      - FTM - 01 Skipped No Numbers
      - FTM - 02 Ready to Call
    tag: Verify Deceased
    index_counties: [knox]
    years: 3
    on_hit: tag
    max_records: 60

  top1000_death_screen:
    kind: screen
    owner: research
    mode: "plan"
    schedule: "every 30 min 07:15-19:15 daily"
    summary: New Top 1,000 records checked for a death before any text or assignment, and held for research when one may have happened.
    lane: top1000
    presets:
      - Priority - 00 Needs Skipped
      - Priority - 01 Skipped No Numbers
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
    tag: Verify Deceased
    index_counties: [knox]
    years: 3
    on_hit: tag
    max_records: 60

  ftm_attempts:
    kind: attempts
    owner: ops
    mode: "plan"
    schedule: "every 60 min 09:00-19:00 Mon-Fri"
    summary: The call counter of every first-to-market record written from the call log, and on the live board from the board's full attempts.
    # On the live First to Market board a day counts once it is a full attempt, so the counter is the
    # board's count and every preset keyed on it shows what the board shows (ftmpipeline).
    lane: ftm
    pipeline: ftm_pipeline
    presets:
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    counters:
      "FTM - 02 Ready to Call": 0
      "FTM - 03 Call Attempt 1": 1
      "FTM - 04 Call Attempt 2": 2
      "FTM - 05 Call Attempt 3": 3
      "FTM - 06 Call Attempt 4": 4
    floor_days: 45
    first_run: staged

  top1000_attempts:
    kind: attempts
    owner: ops
    mode: "plan"
    schedule: "every 60 min 09:30-19:30 Mon-Fri"
    summary: The call counter of every Top 1,000 record written from the account's own call log, whichever dialer made the call.
    lane: top1000
    presets:
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
      - Priority - 03 Call Attempt 1
      - Priority - 04 Call Attempt 2
      - Priority - 05 Call Attempt 3
      - Priority - 06 Call Attempt 4
    counters:
      "Priority - 01 Ready to Text": 0
      "Priority - 02 Texted, Not Yet Called": 0
      "Priority - 02 Ready to Call": 0
      "Priority - 03 Call Attempt 1": 1
      "Priority - 04 Call Attempt 2": 2
      "Priority - 05 Call Attempt 3": 3
      "Priority - 06 Call Attempt 4": 4
    floor_days: 45
    first_run: staged

  ftm_assign:
    kind: assign
    owner: analysis
    lane: ftm
    mode: "plan"
    schedule: "every 30 min 08:00-18:00 Mon-Fri"
    summary: New first-to-market records handed to a caller within 30 minutes of landing, balanced by open workload.
    presets:
      - FTM - 00 Needs Skipped
      - FTM - 01 Skipped No Numbers
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    strategy: balanced
    caller_cap: 2000
    backlog: false
    max_per_run: 200
    routes: {}

  top1000_assign:
    kind: assign
    owner: analysis
    lane: top1000
    mode: "plan"
    schedule: "every 30 min 08:15-18:15 Mon-Fri"   # after first to market, so a new notice is never behind the monthly refresh
    summary: New Top 1,000 records handed to a caller, balanced by open workload, after first to market.
    presets:
      - Priority - 00 Needs Skipped
      - Priority - 01 Skipped No Numbers
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
      - Priority - 03 Call Attempt 1
      - Priority - 04 Call Attempt 2
      - Priority - 05 Call Attempt 3
      - Priority - 06 Call Attempt 4
    strategy: balanced
    caller_cap: 2000
    backlog: false
    max_per_run: 200
    routes: {}

  ftm_native_trace:
    kind: native_trace
    owner: research
    lane: ftm
    mode: "plan"
    schedule: "every 30 min 07:00-19:00 daily"
    summary: First-to-market records that land with no trace get DataSift's own skip trace, scoped to exactly them, estimate first.
    presets: [FTM - 00 Needs Skipped]
    min_age_hours: 1
    scoped: true
    every_hours: 12

  top1000_native_trace:
    kind: native_trace
    owner: research
    lane: top1000
    mode: "plan"
    schedule: "every 30 min 07:15-19:15 daily"
    summary: Top 1,000 records with no trace get DataSift's own skip trace, scoped to exactly them, estimate first.
    presets: [Priority - 00 Needs Skipped]
    min_age_hours: 1
    scoped: true
    every_hours: 12

  ftm_retrace:
    kind: retrace
    owner: research
    lane: ftm
    covers: [tracerfy]
    mode: "plan"
    schedule: "11:00 Mon-Fri"
    summary: First-to-market records DataSift traced with no number found, traced again with the next source nobody has tried.
    presets: [FTM - 01 Skipped No Numbers]
    sources: [smartskip, tracerfy]
    callable_tiers: [Dial First, Dial Second]
    deceased: deep_prospect
    entity: principals
    principals_per_day: 10
    after_sources: mail_only
    mail_only_tag: Mail Only
    deep_prospect_per_day: 3
    max_records: 50
    daily_usd: 10
    agreement_tag: Two Sources
    write_tiers: [Dial First, Dial Second]
    reserve_tiers: [Dial Third]
    keep_numbers: 3
    drop_status: DEAD
    write_line_type: true
    fixup_queue: 100
    fixup_tag: Needs Fix-Up
    lookback_days: 14

  top1000_retrace:
    kind: retrace
    owner: research
    lane: top1000
    covers: [tracerfy]
    mode: "plan"
    schedule: "11:20 Mon-Fri"
    summary: Top 1,000 records DataSift traced with no number found, traced again with the next source nobody has tried.
    presets: [Priority - 01 Skipped No Numbers]
    sources: [smartskip, tracerfy]
    callable_tiers: [Dial First, Dial Second]
    deceased: deep_prospect
    entity: principals
    principals_per_day: 10
    after_sources: mail_only
    mail_only_tag: Mail Only
    deep_prospect_per_day: 3
    max_records: 50
    daily_usd: 10
    agreement_tag: Two Sources
    write_tiers: [Dial First, Dial Second]
    reserve_tiers: [Dial Third]
    keep_numbers: 3
    drop_status: DEAD
    write_line_type: true
    fixup_queue: 100
    fixup_tag: Needs Fix-Up
    lookback_days: 14

  ftm_score:
    kind: score
    owner: research
    lane: ftm
    covers: [phone]
    mode: "plan"
    schedule: "08:30 Mon-Fri"
    summary: Every unscored number on a first-to-market record in the call round tiered before the texts and the callers start.
    presets:
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    max_records: 200
    recheck_days: 7
    daily_usd: 5
    drop_status: DEAD
    write_line_type: true

  top1000_score:
    kind: score
    owner: research
    lane: top1000
    covers: [phone]
    mode: "plan"
    schedule: "08:40 Mon-Fri"
    summary: Every unscored number in the Top 1,000 text and call lane tiered before the texts and the callers start.
    presets:
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
      - Priority - 03 Call Attempt 1
      - Priority - 04 Call Attempt 2
      - Priority - 05 Call Attempt 3
      - Priority - 06 Call Attempt 4
    max_records: 200
    recheck_days: 7
    daily_usd: 5
    drop_status: DEAD
    write_line_type: true

  ftm_mail:
    kind: mail
    owner: ops
    lane: ftm
    pipeline: ftm_pipeline             # on the live board the mail waits for Exhausted, unless the timeline mails at once (a foreclosure)
    mode: "plan"
    schedule: "10:00 Mon-Fri"
    summary: The first-to-market mail lane dropped a month apart through DataSift's own mail, staged for a person to release.
    vendor: siftmail
    internal_routes: true
    send: staged
    daily_pieces: 50
    daily_usd: 60
    wait_days: 30
    offer_pct: 0
    pieces:
      - {preset: FTM - Mail 01 Handwritten Letter, piece: handwritten_letter, product_id: 16, design: ""}
      - {preset: FTM - Mail 02 Family Postcard, piece: family_postcard, product_id: 13, design: ""}
      - {preset: FTM - Mail 03 Soft-Offer Check, piece: soft_offer_check, product_id: 5, design: ""}
      - {preset: FTM - Mail 04 Handwritten Letter, piece: handwritten_letter, product_id: 16, design: ""}
      - {preset: FTM - Mail 05 Family Postcard, piece: family_postcard, product_id: 13, design: ""}
      - {preset: FTM - Mail 06 Soft-Offer Check, piece: soft_offer_check, product_id: 5, design: ""}
    sender: {}

  top1000_mail:
    kind: mail
    owner: ops
    lane: top1000
    mode: "plan"
    schedule: "10:30 Mon-Fri"
    summary: The Top 1,000 mail lane, six pieces a month apart through DataSift's own mail, staged for a person to release.
    vendor: siftmail
    internal_routes: true
    send: staged
    daily_pieces: 50
    daily_usd: 60
    wait_days: 30
    offer_pct: 0
    pieces:
      - {preset: Priority - Mail 01 Handwritten Letter, piece: handwritten_letter, product_id: 16, design: ""}
      - {preset: Priority - Mail 02 Family Postcard, piece: family_postcard, product_id: 13, design: ""}
      - {preset: Priority - Mail 03 Soft-Offer Check, piece: soft_offer_check, product_id: 5, design: ""}
      - {preset: Priority - Mail 04 Handwritten Letter, piece: handwritten_letter, product_id: 16, design: ""}
      - {preset: Priority - Mail 05 Family Postcard, piece: family_postcard, product_id: 13, design: ""}
      - {preset: Priority - Mail 06 Soft-Offer Check, piece: soft_offer_check, product_id: 5, design: ""}
    sender: {}

  ftm_registry_scrub:
    kind: scrub
    owner: ops
    lane: ftm
    mode: "plan"
    schedule: "08:20 Mon-Fri"
    summary: The do-not-call registry flags on the first-to-market call lane remembered and counted, so no flagged number is texted or put on a call list.
    presets:
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    calls: hold
    hold_unseen: false
    max_records: 60

  top1000_registry_scrub:
    kind: scrub
    owner: ops
    lane: top1000
    mode: "plan"
    schedule: "08:25 Mon-Fri"
    summary: The do-not-call registry flags on the Top 1,000 text and call lane remembered and counted, so no flagged number is texted or put on a call list.
    presets:
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
      - Priority - 03 Call Attempt 1
      - Priority - 04 Call Attempt 2
      - Priority - 05 Call Attempt 3
      - Priority - 06 Call Attempt 4
    calls: hold
    hold_unseen: false
    max_records: 60

  ftm_texts:
    kind: textlane
    owner: ops
    lane: ftm
    pipeline: ftm_pipeline             # on the live board each card gets the text its column stands for (Day N, text N)
    mode: "plan"
    schedule: "08:45 Mon-Fri"       # after the board's morning pass (08:35), before the window opens at 9
    summary: First-to-market owners texted on Days 1 to 3 of the board, one opening text a full attempt, staged for a person to release.
    covers: [sccampaign, texttouch]
    series: opening
    presets:
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
    system: smarter_contact
    label: FTM
    numbers_per_owner: 3
    require_screen: true
    stop_on_correct: true
    urgent_copy: true
    urgent_tag: Auction Critical
    text_usd: 0.02
    daily_usd: 10
    daily_owners: 150
    max_records: 150
    max_new: 150

  top1000_texts:
    kind: textlane
    owner: ops
    lane: top1000
    mode: "plan"
    schedule: "08:50 Mon-Fri"       # after the county texts, so first to market is first under the day's caps
    summary: New Top 1,000 owners loaded for text 1 the day they land and texts 2 and 3 on the next two days, staged for a person to release.
    covers: [sccampaign, texttouch]
    series: opening
    presets:
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
      - Priority - 03 Call Attempt 1
      - Priority - 04 Call Attempt 2
    system: smarter_contact
    label: Top 1000
    numbers_per_owner: 3
    require_screen: true
    stop_on_correct: true
    urgent_copy: false              # the sale-date copy is for a courthouse notice: a Top 1,000 house has no sale date
    text_usd: 0.02
    daily_usd: 10
    daily_owners: 150
    max_records: 150
    max_new: 150

  ftm_followup_texts:
    kind: textlane
    owner: ops
    lane: ftm
    pipeline: ftm_pipeline             # on the live board the follow-ups start when the card reaches Exhausted
    mode: "plan"
    schedule: "10:15 Mon-Fri"
    summary: First-to-market follow-up texts from Exhausted on, ending with the breakup text, staged for a person to release.
    covers: [scaudit]
    series: followup
    system: smarter_contact
    label: FTM
    numbers_per_owner: 3
    require_screen: true
    stop_on_correct: true
    text_usd: 0.02
    daily_usd: 10

  top1000_followup_texts:
    kind: textlane
    owner: ops
    lane: top1000
    mode: "plan"
    schedule: "10:20 Mon-Fri"
    summary: Top 1,000 follow-up texts after the call round, ending with the breakup text, staged for a person to release.
    covers: [scaudit]
    series: followup
    system: smarter_contact
    label: Top 1000
    numbers_per_owner: 3
    require_screen: true
    stop_on_correct: true
    text_usd: 0.02
    daily_usd: 10

  ftm_refresh:
    kind: native_trace
    owner: research
    lane: ftm
    mode: "plan"
    schedule: "Sun 05:00"
    summary: Active first-to-market records last traced 180 or more days ago traced again by DataSift, free and outside the per-record cap.
    presets:
      - FTM - 01 Skipped No Numbers
      - FTM - 02 Ready to Call
      - FTM - 03 Call Attempt 1
      - FTM - 04 Call Attempt 2
      - FTM - 05 Call Attempt 3
      - FTM - 06 Call Attempt 4
    retrace_after_days: 180
    min_batch: 20
    max_records: 1000

  top1000_trace_refresh:
    kind: native_trace
    owner: research
    lane: top1000
    mode: "plan"
    schedule: "Sun 05:20"
    summary: Active Top 1,000 records last traced 180 or more days ago traced again by DataSift, free and outside the per-record cap.
    presets:
      - Priority - 01 Skipped No Numbers
      - Priority - 01 Ready to Text
      - Priority - 02 Texted, Not Yet Called
      - Priority - 02 Ready to Call
      - Priority - 03 Call Attempt 1
      - Priority - 04 Call Attempt 2
      - Priority - 05 Call Attempt 3
      - Priority - 06 Call Attempt 4
    retrace_after_days: 180
    min_batch: 20
    max_records: 1000

  full_attempt:
    kind: fullattempt
    owner: sales
    mode: "plan"
    schedule: "16:30 Mon-Fri"
    summary: The owners dialed today who still have a top-tier number nobody tried, listed while there is time to finish them.
    short_by: 2
    lane: ftm                       # the afternoon text's days; on the live board, the card's column
    pipeline: ftm_pipeline
    afternoon_text: false
    sender: Adriana
    text_by: "17:30"
    reached_seconds: 60

  autopilot_digest:
    kind: digest
    owner: ops
    mode: "plan"
    schedule: "08:00 Mon-Fri"
    summary: The morning readout of the three pipelines, what the autopilot did, what it cost, what waits on a person.
    presets:
      - FTM - 01 Skipped No Numbers
      - FTM - 02 Ready to Call
      - FTM - 06 Call Attempt 4
      - FTM - Mail 01 Handwritten Letter
      - Priority - 01 Ready to Text
      - Priority - 02 Ready to Call
      - Priority - Mail 01 Handwritten Letter
      - Hot Auction + Buy Box
      - "03 Exhausted Call -> DP"
      - Agency - 01 Queue

  kpi_alarms:
    kind: alarms
    owner: ops
    mode: "plan"
    schedule: "19:25 Mon-Fri"
    summary: The day's numbers checked against the catalog's alarms; each one is said once, with what to check first and the list to move to.
    covers: [kpi]
    ready_lists:
      - {lane: ftm, preset: FTM - 02 Ready to Call}
      - {lane: top1000, preset: Priority - 02 Ready to Call}
    trace_check:
      label: first-to-market records
      scope_tags: [FTM]
      waiting_preset: FTM - 00 Needs Skipped
      no_number_preset: FTM - 01 Skipped No Numbers
    stale_days: 4

  conventions_check:
    kind: conventions
    owner: analysis
    mode: "plan"
    schedule: "Sun 03:40"
    summary: The account's naming rules checked once a week; what breaks one is listed and nothing is changed.
    caller_folders: ["05. FTM - CALL", "02. TOP 1000 - CALL", "20. CALLER QUEUES"]
    starred_field: quick_filter
    inbound_field: Inbound Source
    mail_tag: "Mailed {yyyy}-{mm}"
    mail_days: 35
    zombie: {names: [Zombie], vacant: Vacant, foreclosure: Foreclosure}
    known_lists: [DNC, Dream 100]
    known_prefixes: ["Dispo - "]
    settings: {auto_update: true}
"""
# The twins: each stage that worked both lanes, and the stage that now works the Top 1,000 alone.
TWINS = {"ftm_death_screen": "top1000_death_screen", "ftm_attempts": "top1000_attempts", "ftm_assign": "top1000_assign",
         "ftm_native_trace": "top1000_native_trace", "ftm_retrace": "top1000_retrace", "ftm_score": "top1000_score",
         "ftm_mail": "top1000_mail", "ftm_registry_scrub": "top1000_registry_scrub", "ftm_texts": "top1000_texts",
         "ftm_followup_texts": "top1000_followup_texts", "ftm_refresh": "top1000_trace_refresh"}
REPORT_ONLY = ("digest",)          # a report may count both pipelines' presets; it works no record


def mixes(st) -> str:
    """Why a stage mixes the two pipelines, or ''. A stage that works records reads the county
    presets with the ftm lane, or the Priority presets with the top1000 lane, never both."""
    from .autopilot import stages
    if st.mode == "off" or st.kind in REPORT_ONLY:
        return ""
    titles = list(st.presets) + [p.get("preset") for p in (st.get("pieces") or []) if isinstance(p, dict)]
    top = [t for t in titles if str(t).startswith("Priority - ")]
    county = [t for t in titles if str(t).startswith("FTM - ")]
    work = stages.stage_lanes(st)
    if top and county:
        return "reads both the Priority and the county presets"
    if "ftm" in work and "top1000" in work:
        return "works both the ftm and the top1000 lanes"
    if top and work != ["top1000"]:
        return "reads the Top 1,000 presets with the lanes %s" % work
    if county and work and work != ["ftm"]:
        return "reads the county presets with the lanes %s" % work
    return ""


def _playbook_checks(check) -> None:
    import yaml
    from .autopilot import policy
    print("  the playbook blocks handed over")
    blocks = yaml.safe_load("stages:" + PLAYBOOK_BLOCKS)["stages"]
    real = yaml.safe_load((config.TEAM_DIR / "autopilot.yaml").read_text(encoding="utf-8")) or {}
    base = {k: v for k, v in real.items() if k != "stages"}
    merged = dict(real.get("stages") or {})
    merged.update(copy.deepcopy(blocks))
    try:
        pol = policy.parse(dict(base, stages=merged), EMPLOYEES)
        ok = True
    except policy.PolicyError as exc:
        pol, ok = None, False
        print("    %s" % str(exc)[:600])
    check(ok, "the handed-over blocks load with the playbook's own gate, lanes and other stages")
    if not ok:
        return
    check(all(pol.stage(sid).mode == "plan" for sid in blocks), "every handed-over block ships in plan mode")
    clash = [sid for sid in blocks if sid in (real.get("stages") or {}) and (real["stages"][sid] or {}).get("kind") != blocks[sid]["kind"]]
    check(not clash, "no handed-over block takes the id of a different stage already in the playbook: %s" % (clash or "ok"))
    bad = {sid: mixes(st) for sid, st in pol.stages.items() if mixes(st)}
    check(not bad, "no stage mixes the two pipelines: %s" % (bad or "ok"))
    check(all(sid in blocks and twin in blocks for sid, twin in TWINS.items()),
          "every stage that worked both lanes is handed over with its Top 1,000 twin")
    follows = sorted(sid for sid, st in pol.stages.items() if st.get("pipeline"))
    check(follows == sorted(["ftm_attempts", "ftm_mail", "ftm_texts", "ftm_followup_texts", "full_attempt"])
          and all(pol.stage(s).get("pipeline") == "ftm_pipeline" for s in follows),
          "the county texts, mail, attempt keeper and afternoon text follow the board, and nothing else does: %s" % follows)
    board = pol.stage("ftm_pipeline")
    check(board.owner == "ops" and board.presets and all(t.startswith("FTM - ") for t in board.presets)
          and board.get("obituary_presets") == ["Obituary - All"] and board.get("text_stage") == "ftm_texts",
          "the board stage (Quinn) reads the county presets and the obituary door, and its texts are ftm_texts")
    top_titles = {t for sid in TWINS.values() for t in pol.stage(sid).presets} | {
        p["preset"] for p in pol.stage("top1000_mail").get("pieces") or []}
    from .selftest_tracks import PRIORITY_TITLES
    check(top_titles and top_titles <= PRIORITY_TITLES, "every Top 1,000 stage reads Priority presets the account holds")
    check(chr(0x2014) not in PLAYBOOK_BLOCKS and chr(0x2013) not in PLAYBOOK_BLOCKS, "the handed-over blocks carry no dash")
