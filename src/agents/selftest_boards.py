"""Slack by department, one board a day (Ty, 2026-11-19: "segment the notifications ... like a
marketing, a sales, and ops channel ... then consolidate the updates"). The department map, the problem
sorter, one cause for many records, the board drawn and edited in place, the setup to-do list, the plan a
stage run makes, the worker carrying it out, the relay's reports as board sections, and the KPI
scoreboard in the team channel. Zero network: a recording fake stands where Slack's HTTP door is.
Every name and street here is invented.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")


class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeSlack:
    """Slack's Web API door: records every call, answers ok with a fresh ts, or an error when told to."""

    def __init__(self):
        self.calls, self.fail = [], {}

    def post(self, url, json=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, dict(json or {})))
        if method in self.fail:
            return FakeResp({"ok": False, "error": self.fail[method]})
        return FakeResp({"ok": True, "ts": "1800.%04d" % len(self.calls)})

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
        return FakeResp({"ok": True, "permalink": "https://example.slack.test/p1"})

    def sent(self, method="chat.postMessage"):
        return [p for m, p in self.calls if m == method]


def run(check, tmp: Path) -> None:
    from . import board, slack_app, store, voice
    names = ("DATA_DIR", "DB_PATH", "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_PIPELINE_CHANNEL", "SLACK_ISSUES_CHANNEL",
             "SLACK_SALES_CHANNEL", "SLACK_MARKETING_CHANNEL", "SLACK_OPS_CHANNEL", "SLACK_LAYOUT", "SLACK_VOICE",
             "PLAN_POSTS")
    keep = {k: getattr(config, k) for k in names}
    saved = (slack_app._http, board._now, voice._today)
    config.DATA_DIR = Path(tmp) / "boards"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    store.init()
    fake = FakeSlack()
    clock = [datetime(2026, 3, 5, 9, 2, tzinfo=ET)]          # a Thursday
    slack_app._http = fake
    board._now = lambda now=None: (now or clock[0]).astimezone(ET)
    voice._today = lambda: clock[0].strftime("%Y-%m-%d")
    config.SLACK_VOICE, config.PLAN_POSTS = "plain", "digest"
    try:
        _channels(check)
        _cause(check)
        _board(check, fake, clock)
        _plans(check)
        _worker(check, fake, clock)
        _relay_and_kpi(check, fake, clock)
    finally:
        slack_app._http, board._now, voice._today = saved
        for k, v in keep.items():
            setattr(config, k, v)


# ---------------------------------------------------------------------- the department map
def _channels(check) -> None:
    from . import channels, slack_app
    from .autopilot import registry
    print("  every stage has a department")
    built_in = {"mail", "attempts", "screen", "lanes", "revoke", "watchdog", "audit", "capture", "leadcheck",
                "fullattempt", "assign", "native_trace", "retrace", "score", "auction", "reactivate", "handoff", "digest"}
    kinds = set(registry.EXTRA) | built_in
    missing = sorted(k for k in kinds if k not in channels.KIND_DEPARTMENT)
    check(not missing, "every stage kind is mapped to sales, marketing or ops (unmapped: %s)" % (", ".join(missing) or "none"))
    check(set(channels.KIND_DEPARTMENT.values()) <= set(channels.DEPARTMENTS),
          "the map names only the three departments")
    check(channels.department("textlane") == "marketing" and channels.department("leadclock") == "sales"
          and channels.department("researchqueue") == "ops" and channels.department("a_kind_nobody_wrote") == "ops",
          "texts are marketing, a lead to call is sales, research is ops, and an unknown kind is ops")
    check(channels.department("textlane", "x", {"slack": "sales"}) == "sales"
          and channels.department("textlane", "x", {"slack": "nowhere"}) == "marketing",
          "a stage's own `slack:` setting wins, and a misspelled one falls back to its kind")
    lookup = lambda sid: ("appointments", None) if sid == "lead_appointments" else ("", None)  # noqa: E731
    check(channels.task_department({"source": "autopilot:lead_appointments", "workflow": "quick_screen"}, lookup) == "sales"
          and channels.task_department({"source": "autopilot:research_queue", "workflow": "deep_prospect"}, lookup) == "ops"
          and channels.task_department({"source": "slack", "workflow": "outreach_health"}, lookup) == "marketing",
          "work a stage queued reports where that stage does; anything else by what the work is")

    print("  each department finds its channel")
    config.SLACK_CHANNEL, config.SLACK_PIPELINE_CHANNEL, config.SLACK_ISSUES_CHANNEL = "CTEAM", "", ""
    config.SLACK_SALES_CHANNEL = config.SLACK_MARKETING_CHANNEL = config.SLACK_OPS_CHANNEL = ""
    check(all(slack_app.channel_for(d) == "CTEAM" for d in channels.DEPARTMENTS),
          "with no department channel set, every board lands in the team channel")
    config.SLACK_PIPELINE_CHANNEL = "CPIPE"
    check(all(slack_app.channel_for(d) == "CPIPE" for d in channels.DEPARTMENTS),
          "with only the pipeline channel, all three boards go there")
    config.SLACK_OPS_CHANNEL = "COPS"
    check(slack_app.channel_for("sales") == "COPS" and slack_app.channel_for("pipeline") == "CPIPE",
          "the ops channel takes a department that has none of its own; the classic pipeline keeps its channel")
    config.SLACK_SALES_CHANNEL, config.SLACK_MARKETING_CHANNEL, config.SLACK_ISSUES_CHANNEL = "CSALES", "CMKT", "CISS"
    check(slack_app.channel_for("sales") == "CSALES" and slack_app.channel_for("marketing") == "CMKT"
          and slack_app.channel_for("ops") == "COPS" and slack_app.channel_for("issues") == "CISS"
          and slack_app.channel_for("chat") == "CTEAM",
          "with all of them set, each department, the issues and the team channel are their own")
    config.SLACK_LAYOUT = "boards"
    boards_pipe = slack_app.channel_for("pipeline")
    config.SLACK_LAYOUT = "classic"
    check(boards_pipe == "COPS" and slack_app.channel_for("pipeline") == "CPIPE",
          "the boards layout retires the pipeline channel (a straggler lands in ops); classic keeps it")

    print("  what a problem is")
    cases = [
        ("Owner: 100 Example St, Knoxville has waited 35 min for a first call and nobody has called. "
         "This new lead needs a call now.", "leadclock", "alert"),
        ("the closer is not named (closer and closer_line in the stage block), so no handoff text is staged",
         "closertext", "setup"),
        ("there is no 'Sample Redemption' tag in this account, so nothing is read from it", "researchqueue", "setup"),
        ("there is no preset titled 'Sample Queue' in this account; create it", "paidlookup", "setup"),
        ("custom fields for the call answers do not exist yet, so those answers stay in the note only", "leadnotes", "setup"),
        ("12 leads with no open task (for example 100 Example St, Knoxville)", "leadcheck", "finding"),
        ("Sequence Call New Lead: 10 of 10 texts it sent in the last 7 days failed. Fix its text action",
         "accountchecks", "finding"),
        ("7: dialed outside 9am to 7pm on the business clock", "limits", "finding"),
        ("100 Example St, Knoxville: the sale was 60 days ago and nobody settled who owns it; check by hand",
         "auctiontasks", "finding"),
        ("could not read a record's log for its lane entry date (HTTP 500 on GET /api/internal/property/x/logs/)",
         "textlane", "breakage"),
        ("The outreach limit check failed.", "limits", "breakage"),
        ("return_signals must be a mapping (statuses, tags, flag)", "researchqueue", "breakage"),
        ("could not write the research note on 100 Example St, Knoxville", "screen", "breakage"),
        ("the board has no column named Sample Column; name it under `columns` on the stage", "leadclock", "setup"),
    ]
    wrong = [(t[:50], k, want, channels.classify(t, k)) for t, k, want in cases if channels.classify(t, k) != want]
    check(not wrong, "a seller to call is an alert, a missing setting is setup, the team's to-do is a finding, "
                     "and our own failure is breakage (wrong: %s)" % (wrong or "none"))


# ---------------------------------------------------------------------- one cause, many records
def _cause(check) -> None:
    from . import channels
    print("  one cause, many records")
    many = ["could not write the research note on %d Example St, Knoxville" % (100 + i) for i in range(26)]
    g = channels.group(many)
    check(len(g) == 1 and g[0]["count"] == 26 and "26 records" in g[0]["text"]
          and "for example 100 Example St, Knoxville; 101 Example St, Knoxville; 102 Example St, Knoxville; and 23 more"
          in g[0]["text"],
          "26 houses with one failure are one problem that names three of them and counts the rest")
    a = channels.cause("the county tax bills for 100 Sample Ave, Demo Town could not be read: back taxes left blank")
    b = channels.cause("the county tax bills for 200 Example Rd, Knoxville could not be read: back taxes left blank")
    check(a[0] == b[0] and a[2] == "100 Sample Ave, Demo Town",
          "a city in Title Case ends the address (\"Knoxville could not\" is not a city)")
    c = channels.cause("could not write the research note on 300 Sample Creek Dr, Testville")
    check(c[2] == "300 Sample Creek Dr, Testville",
          "a street whose name holds a suffix word (Creek) is read to its real suffix")
    u1 = channels.cause("could not read a record's log (HTTP 500 on GET /api/internal/property/"
                        "0f3c2b1a-1111-4222-8333-944455556666/logs/?limit=100&offset=0)")
    u2 = channels.cause("could not read a record's log (HTTP 502 on GET /api/internal/property/"
                        "9a8b7c6d-2222-4333-8444-955566667777/logs/?limit=100&offset=100)")
    check(u1[0] == u2[0] and "HTTP" not in u1[1] and "/api/" not in u1[1],
          "the programmer's detail in parentheses never splits one cause in two")
    two = channels.group(["could not read an incomplete record", "the outreach limit check failed",
                          "could not read an incomplete record"])
    check(len(two) == 2 and two[0]["text"] == "could not read an incomplete record",
          "different causes stay apart, and a problem with no record keeps its own words")


# ---------------------------------------------------------------------- the board
def _board(check, fake, clock) -> None:
    from . import board, slack_app
    print("  the board, drawn")
    config.SLACK_BOT_TOKEN = "xoxb-test"
    board.put("sales", "speed_to_lead", title="Speed to lead", who="Roma", head="Every new lead was called.", rank=10)
    for i in range(6):
        clock[0] += timedelta(minutes=10)
        board.put("sales", "auction_tasks", title="Auction tasks", who="Atlas",
                  entry="Closed out %d passed sales." % (i + 1), rank=45)
    board.put("sales", "lead_check", title="Lead check", who="Quinn", head="8 leads read.",
              lines=["%d leads with no open task" % n for n in range(1, 7)], rank=50)
    d = board.today("sales")
    text, sig = board.render("sales", d)
    order = [text.find("*Speed to lead*"), text.find("*Auction tasks*"), text.find("*Lead check*")]
    check(text.startswith("*Sales today*, Thu Mar 5 (last change ") and order == sorted(order) and -1 not in order,
          "the board opens with its department and date, then each section in its set order")
    check("6 runs today, the latest 4:" in text and "Closed out 6 passed sales." in text
          and "Closed out 2 passed sales." not in text,
          "a stage that ran six times shows its latest four runs and says how many there were")
    check("• and 2 more" in text and text.count("leads with no open task") == 4,
          "a long section shows four lines and counts the rest")
    later, sig2 = board.render("sales", d, now=clock[0] + timedelta(hours=2))
    check(sig2 == sig, "the signature is the board's words, not its clock: a board is edited only when it changes")

    print("  the board, posted once and edited after")
    config.SLACK_SALES_CHANNEL = "CSALES"
    fake.calls.clear()
    check(board.flush("sales") == "posted" and len(fake.sent()) == 1 and fake.sent()[0]["channel"] == "CSALES",
          "the first flush of the day posts the board in its department's channel")
    check(board.flush("sales") == "unchanged" and len(fake.calls) == 1,
          "nothing changed, so nothing is sent")
    board.put("sales", "speed_to_lead", title="Speed to lead", who="Roma", head="One lead is waiting.", rank=10)
    check(board.flush("sales") == "updated" and len(fake.sent("chat.update")) == 1
          and fake.sent("chat.update")[0]["ts"] == board.today("sales")["ts"]
          and "One lead is waiting." in fake.sent("chat.update")[0]["text"],
          "a change edits today's message in place: no new post, nobody pinged")
    fake.fail["chat.update"] = "message_not_found"
    board.put("sales", "speed_to_lead", title="Speed to lead", who="Roma", head="Two leads are waiting.", rank=10)
    n = len(fake.sent())
    check(board.flush("sales") == "posted" and len(fake.sent()) == n + 1,
          "a board somebody deleted is posted again rather than lost")
    fake.fail.clear()
    fake.fail["chat.update"] = "ratelimited"
    board.put("sales", "speed_to_lead", title="Speed to lead", who="Roma", head="Three leads are waiting.", rank=10)
    check(board.flush("sales") == "failed" and len(fake.sent()) == n + 1,
          "an edit Slack refused for another reason posts nothing new (the next run tries again)")
    fake.fail.clear()
    check(board.flush("sales") == "updated", "and the next flush brings it up to date")
    ts_today = board.today("sales")["ts"]
    clock[0] += timedelta(days=1)
    board.put("sales", "speed_to_lead", title="Speed to lead", who="Roma", head="Every new lead was called.", rank=10)
    check(board.flush("sales") == "posted" and board.today("sales")["ts"] != ts_today,
          "a new business day starts a new board; yesterday's stays as it was")
    r = board.reply("sales", "The detail behind a line.", None)
    check(r and fake.sent()[-1]["thread_ts"] == board.today("sales")["ts"],
          "detail goes in the board's thread, where it pings nobody")
    config.SLACK_BOT_TOKEN = ""
    check(board.flush("sales") in ("unchanged", "off") and board.reply("marketing", "x", None) == "",
          "with no Slack app nothing is sent and nothing lands loose in a channel")
    config.SLACK_BOT_TOKEN = "xoxb-test"

    print("  the setup to-do list")
    t0 = clock[0]
    check(board.set_setup("closer_handoff", "Closer handoff", "Roma", ["the closer is not named"], now=t0),
          "a new setting to make goes on the list")
    check(not board.set_setup("closer_handoff", "Closer handoff", "Roma", ["the closer is not named"], now=t0 + timedelta(hours=1)),
          "the same to-do again changes nothing")
    check(not board.set_setup("closer_handoff", "Closer handoff", "Roma", [], now=t0 + timedelta(hours=20))
          and board.setup_items().get("closer_handoff"),
          "one run that does not mention it does not take it off (many stages ask only when they have work)")
    text, _ = board.render("ops", board.today("ops"))
    check("*Waiting on a setting* (1)" in text and "Closer handoff: the closer is not named" in text,
          "the ops board lists it, even on a day no stage has written to the board yet")
    check(board.set_setup("closer_handoff", "Closer handoff", "Roma", [], now=t0 + timedelta(hours=board.SETUP_KEEP_H + 2))
          and not board.setup_items().get("closer_handoff"),
          "a to-do nobody has asked for in a day and a half comes off")
    sales_text, _ = board.render("sales", board.today("sales"), setup={"x": {"title": "X", "items": {"k": {"text": "y"}}}})
    check("Waiting on a setting" not in sales_text, "the to-do list is on the ops board only")

    print("  a full board stays readable")
    for i in range(40):
        board.put("ops", "stage_%02d" % i, title="Stage %02d" % i, who="Diggs", head="x" * 150,
                  lines=["y" * 150] * 4, rank=i)
    big, _ = board.render("ops", board.today("ops"))
    check(len(big) < 3900 and "that did not fit" in big and "*Stage 00*" in big,
          "a board past Slack's length shows what fits, the first sections first, and says how much did not")
    for i in range(40):
        board.remove("ops", "stage_%02d" % i)
    board.put("ops", "dash", title="Dash test", who="Diggs", head="ranges 4" + chr(0x2013) + "6 " + chr(0x2014) + " done")
    board.flush("ops")
    sent = (fake.sent("chat.update") + fake.sent())[-1]["text"]
    check(chr(0x2014) not in sent and chr(0x2013) not in sent, "no em or en dash reaches Slack from a board")
    board.remove("ops", "dash")


# ---------------------------------------------------------------------- the plan a stage run makes
def _plans(check) -> None:
    from .autopilot import sweep
    print("  the plan a stage run makes")
    sched = lambda wf: {"id": 1, "workflow": wf, "source": "schedule", "agent": "ops"}  # noqa: E731
    p = sweep.board_plan(sched("autopilot_digest"), {"stage": "autopilot_digest", "kind": "digest", "live": False,
                                                     "head": "Last 24h: 5 scored.", "lines": ["Preset A: 3", "Preset B: 1"],
                                                     "problems": ["Pacing: one list end to end before the next."]})
    check(p["dept"] == "ops" and p["section"]["head"] == "Last 24h: 5 scored."
          and p["section"]["lines"] == ["Pacing: one list end to end before the next."]
          and p["replies"] and "Preset A: 3" in p["replies"][0] and not p["issues"],
          "the morning update is an ops section with its notes, and the whole update goes in the thread")
    p = sweep.board_plan(sched("ftm_auction"), {"stage": "ftm_auction", "kind": "auction", "live": True, "acted": 3,
                                                "head": "4 sales inside 45 days.", "lines": ["Mar 6: 100 Example St, Knoxville"]})
    check(p["dept"] == "sales" and p["section"] == {"head": "4 sales inside 45 days.",
                                                    "lines": ["Mar 6: 100 Example St, Knoxville"]},
          "a report is its head and lines on its department's board")
    p = sweep.board_plan(sched("ftm_texts"), {"stage": "ftm_texts", "kind": "textlane", "live": True, "acted": 6, "batch": 3,
                                              "head": "Batch 3 (6 on text 1): 9 texts to 6 owners released. In Smarter "
                                                      "Contact: 2 campaigns.", "lines": ["100 Example St: text 1"]})
    check(p["dept"] == "marketing" and p["section"]["entry"] == "Batch 3 (6 on text 1): 9 texts to 6 owners released."
          and not p["alerts"],
          "a live run that acted is one more entry, its first sentence; a released batch needs nobody")
    p = sweep.board_plan(sched("ftm_mail"), {"stage": "ftm_mail", "kind": "mail", "live": True, "acted": 2, "batch": 4,
                                             "head": "Staged batch 4: 31 letters, about $59. Nothing is sent until a "
                                                     "person releases it: release batch 4."})
    check(len(p["alerts"]) == 1 and p["alerts"][0]["rule"] == "once" and "batch 4" in p["alerts"][0]["text"],
          "a batch waiting for a person is its own post, said once")
    p = sweep.board_plan(sched("lead_check"), {"stage": "lead_check", "kind": "leadcheck", "live": False, "acted": 4,
                                               "head": "8 leads read.", "problems": [
                                                   "3 leads with no open task (for example 100 Example St, Knoxville)",
                                                   "could not read a lead's tasks (HTTP 503 on GET /api/x)"]})
    check(p["dept"] == "sales" and p["section"]["lines"] == ["3 leads with no open task (for example 100 Example St, Knoxville)"]
          and len(p["issues"]) == 1 and "HTTP" not in p["issues"][0]["text"],
          "a plan-mode check's to-dos are board lines; only its own failure goes to #issues")
    p = sweep.board_plan(sched("top1000_death_screen"), {
        "stage": "top1000_death_screen", "kind": "screen", "live": True, "acted": 0,
        "problems": ["could not write the research note on %d Example St, Knoxville" % (100 + i) for i in range(12)]})
    check(p["section"] is None and len(p["issues"]) == 1 and "12 records" in p["issues"][0]["text"],
          "twelve failures with one cause are one issue")
    p = sweep.board_plan(sched("closer_handoff"), {"stage": "closer_handoff", "kind": "closertext", "live": False,
                                                   "problems": ["the closer is not named (closer and closer_line in "
                                                                "the stage block)"]})
    check(p["setup"] and not p["issues"] and p["section"] is None,
          "a setting nobody made is a to-do on the ops board, never an issue")
    p = sweep.board_plan(sched("call_screen"), {"stage": "call_screen", "kind": "callscreen", "live": False,
                                                "head": "Would screen 6 calls: 6 to transcribe."})
    check(p["section"] is None, "a plan-mode report that only says what it would do stays off the board")
    p = sweep.board_plan(sched("kpi_tracker"), {"stage": "kpi_tracker", "kind": "kpitracker", "live": False,
                                                "problems": ["a tab could not be read"]})
    check(p["section"] is None and p["setup"] is None and not p["issues"],
          "the KPI tracker speaks for itself, so its run plans nothing")
    # The 4:30 full-attempt list (2026-11-19): the run IS its list of owners still short of a full attempt.
    # As one more entry of a run log the board showed the head and dropped every owner on the list.
    short = ["100 Example St, Knoxville: 1 number not tried today (ending 0101)",
             "101 Example St, Knoxville: 2 numbers not tried today (ending 0102, 0103)"]
    head = "5 owners dialed today: 3 got a full attempt, 2 are one or two numbers short."
    fa = {"stage": "full_attempt", "kind": "fullattempt", "live": True, "acted": 2, "head": head, "lines": short}
    p = sweep.board_plan(sched("full_attempt"), fa)
    check(p["dept"] == "sales" and p["section"] == {"head": head, "lines": short} and not p["alerts"] and not p["issues"],
          "a live full-attempt run puts its head and its whole list of owners still short on the sales board")
    none_short = "4 owners dialed today: 4 got a full attempt, 0 are one or two numbers short."
    p = sweep.board_plan(sched("full_attempt"), dict(fa, acted=0, head=none_short, lines=[]))
    check(p["section"] == {"head": none_short, "lines": []},
          "a live run that finds nobody short says so, so an earlier list does not stay up on the board")
    p = sweep.board_plan(sched("full_attempt"), dict(fa, live=False))
    check(p["section"] is None, "a plan-mode full-attempt list stays off the board, as every plan-mode list does")
    config.PLAN_POSTS = "off"
    p = sweep.board_plan(sched("lead_check"), {"stage": "lead_check", "kind": "leadcheck", "live": False,
                                               "problems": ["3 leads with no open task"]})
    check(p["section"] is None and p["setup"] is None and not p["issues"],
          "AGENTS_PLAN_POSTS=off keeps a plan-mode stage quiet, as it always has")
    config.PLAN_POSTS = "digest"


# ---------------------------------------------------------------------- the worker carries it out
def _worker(check, fake, clock) -> None:
    from . import board, store, worker
    from .autopilot import sweep
    print("  the worker carries the plan out")
    config.SLACK_BOT_TOKEN = "xoxb-test"
    config.SLACK_SALES_CHANNEL, config.SLACK_MARKETING_CHANNEL, config.SLACK_OPS_CHANNEL = "CSALES", "CMKT", "COPS"
    config.SLACK_ISSUES_CHANNEL, config.SLACK_LAYOUT = "CISS", "boards"
    clock[0] = datetime(2026, 3, 9, 8, 30, tzinfo=ET)          # a Monday, a fresh day
    roma = types.SimpleNamespace(display_name="Roma", icon="", icon_emoji=":telephone_receiver:")
    t = store.enqueue("sales", "speed_to_lead", "", subject="test", source="schedule")[0]
    task = {"id": t, "workflow": "speed_to_lead", "source": "schedule", "agent": "sales"}
    plan = {"dept": "sales", "stage": "speed_to_lead", "title": "Speed to lead", "rank": 10,
            "section": {"head": "1 new lead waiting.", "lines": []}, "setup": [],
            "alerts": [{"text": "Owner: 100 Example St, Knoxville has waited 35 min. This new lead needs a call now.",
                        "rule": "daily"}], "issues": [], "replies": []}
    fake.calls.clear()
    worker._deliver(task, plan, roma, None)
    posts = fake.sent()
    check([p["channel"] for p in posts] == ["CSALES", "CSALES"] and posts[0]["text"].startswith("*Sales today*")
          and "100 Example St" in posts[1]["text"] and posts[1].get("username") == "Roma",
          "the board posts in sales, and the lead to call is its own post there under Roma's name")
    fake.calls.clear()
    worker._deliver(task, plan, roma, None)
    check(fake.sent() == [] and fake.sent("chat.update") == [],
          "the same run again says nothing: the board has not changed and the alert was said today")
    issue = {"key": "could not read an incomplete record", "text": "could not read an incomplete record"}
    a = dict(plan, stage="complete_records", section=None, alerts=[], issues=[issue])
    b = dict(plan, stage="second_chances", section=None, alerts=[], issues=[issue])
    worker._deliver(dict(task, workflow="complete_records"), a, None, None)
    worker._deliver(dict(task, workflow="second_chances"), b, None, None)
    check([p["channel"] for p in fake.sent()] == ["CISS"],
          "one cause hit by two stages is one post in #issues that day")
    fake.calls.clear()
    worker._deliver(task, dict(plan, section=None, alerts=[], setup=["the closer is not named"]), roma, None)
    check(fake.sent() and fake.sent()[0]["channel"] == "COPS" and "Waiting on a setting" in fake.sent()[0]["text"],
          "a setup to-do lands on the ops board, whichever department the stage is in")
    t3 = store.enqueue("sales", "full_attempt", "", subject="test", source="schedule")[0]
    fa_task = {"id": t3, "workflow": "full_attempt", "source": "schedule", "agent": "sales"}
    fake.calls.clear()
    worker._deliver(fa_task, sweep.board_plan(fa_task, {
        "stage": "full_attempt", "kind": "fullattempt", "live": True, "acted": 1,
        "head": "3 owner(s) dialed today: 2 got a full attempt, 1 are one or two numbers short.",
        "lines": ["102 Example St, Knoxville: 1 number(s) not tried today (ending 0104)"]}), roma, None)
    drawn = (fake.sent("chat.update") + fake.sent())[-1:] or [{}]
    check(drawn[0].get("channel") == "CSALES" and "*Full attempts* (Roma"
          in str(drawn[0].get("text")) and "102 Example St, Knoxville: 1 number not tried today (ending 0104)" in str(drawn[0].get("text")),
          "the worker draws the live full-attempt list on the sales board, one line for each owner still short")
    for i in range(30):
        board.put("sales", "crowd_%02d" % i, title="Crowd %02d" % i, who="Atlas", head="x" * 150, lines=["y" * 150] * 4,
                  rank=sweep.BOARD_RANK["appointments"] + i)
    crowded, _ = board.render("sales", board.today("sales"))
    check("that did not fit" in crowded and "102 Example St, Knoxville: 1 number not tried today (ending 0104)" in crowded,
          "on a crowded sales board the full-attempt list ranks with the afternoon's to-dos, so it is drawn before any "
          "section is cut")
    for i in range(30):
        board.remove("sales", "crowd_%02d" % i)

    print("  work a stage queued lands in the board's thread")
    diggs = types.SimpleNamespace(display_name="Diggs", icon="", icon_emoji=":mag:")
    t2 = store.enqueue("research", "deep_prospect", "u-1", subject="100 Example St", source="autopilot:research_queue")[0]
    dp = {"id": t2, "workflow": "deep_prospect", "source": "autopilot:research_queue", "agent": "research"}
    fake.calls.clear()
    ok = worker._board_result(dp, ":mag: *Deep prospect: 100 Example St*  (Research agent, live)\nOwner: Living\n"
                                  "Who signs: the owner", diggs)
    ops = board.today("ops")
    check(ok and any(p.get("thread_ts") == ops["ts"] and p.get("username") == "Diggs" for p in fake.sent())
          and "Deep prospect: 100 Example St" in str(ops["sections"].get("results:deep_prospect")),
          "a deep prospect the queue handed on goes in full into the ops board's thread, with one line on the board")
    check((store.task(t2) or {}).get("slack_thread") == ops["ts"],
          "a reply in that thread with no name goes to the employee who did the work")
    check(not worker._board_result(dict(dp, source="slack"), "x", diggs)
          and not worker._board_result(dict(dp, slack_thread="1800.1"), "x", diggs),
          "an ask from Slack still answers in its own thread")
    config.SLACK_LAYOUT = "classic"
    check(not worker._board_result(dp, "x", diggs), "in the classic layout nothing changes")
    check(worker._boards() is False and sweep.posts({"id": 1, "workflow": "ftm_auction", "source": "schedule"},
                                                    {"stage": "ftm_auction", "kind": "auction", "live": True,
                                                     "head": "1 sale", "lines": []})[0]["kind"] == "pipeline",
          "classic is the default, and its posts are what they were")


# ---------------------------------------------------------------------- the relay and the scoreboard
def _relay_and_kpi(check, fake, clock) -> None:
    from . import board, relay
    from .autopilot import kpisinks
    print("  the relay's reports become board sections")
    config.SLACK_LAYOUT, config.SLACK_BOT_TOKEN = "boards", "xoxb-test"
    clock[0] = datetime(2026, 3, 10, 8, 0, tzinfo=ET)
    fake.calls.clear()
    out = relay.deliver(":white_check_mark: *FTM Mar 10* - 4 new records\nForeclosure 1 - Probate 3 - Condemned 0", "ftm")
    ops = board.today("ops")
    check(out and out[0]["kind"] == "board" and "relay_ftm_digest" in ops.get("sections", {})
          and [p["channel"] for p in fake.sent()] == ["COPS"] and "*First to market*" in fake.sent()[0]["text"],
          "the first-to-market digest is the ops board's first section, not a post of its own")
    # 2026-11-19: a subscription ending soon is a to-do a person must see before the run that fails.
    fake.calls.clear()
    ending = ("Register of Deeds subscription ends in 9 days (Mar 19). "
              "Renew it at paxsub.knoxrod.org, on the Subscription tab.")
    relay.deliver(":white_check_mark: *FTM Mar 10* - 4 new records\nForeclosure 1 - Probate 3 - Condemned 0\n" + ending,
                  "ftm")
    lines = (board.today("ops").get("sections", {}).get("relay_ftm_digest") or {}).get("lines") or []
    edits = fake.sent("chat.update")
    check(ending in lines and edits and edits[-1]["channel"] == "COPS" and (board.BULLET + ending) in edits[-1]["text"]
          and fake.sent() == [],
          "an amber digest's ending subscription is a line of the First to market section on the ops board, "
          "and nothing goes to #issues")
    relay.deliver("Dial KPIs, Sample Team, Mon Mar 9: 120 dials, 2 leads", "workstation")
    check("relay_dial_kpis" in board.today("sales").get("sections", {}), "the dial numbers sit on the sales board")
    fake.calls.clear()
    relay.deliver(":rotating_light: SiftStack FTM run FAILED\nnotices [FAILED] login unreachable", "ftm")
    check([p["channel"] for p in fake.sent()] == ["CISS"], "a failed run still goes to #issues")

    print("  the KPI tracker is the team channel's scoreboard")
    config.SLACK_CHANNEL = "CTEAM"
    fake.calls.clear()
    kpisinks.post_daily({"slack": {"lines": ["KPI tracker refreshed Tue at 7:55 AM.", "Dials 120"]}}, {"slack": "edit"},
                        "", "2026-03-10")
    check(fake.sent() and fake.sent()[0]["channel"] == "CTEAM", "in the boards layout the daily scoreboard is in the team channel")
    config.SLACK_LAYOUT, config.SLACK_PIPELINE_CHANNEL = "classic", "CPIPE"
    fake.calls.clear()
    kpisinks.post_daily({"slack": {"lines": ["KPI tracker refreshed Tue at 7:56 AM.", "Dials 121"]}}, {"slack": "edit"},
                        "", "2026-03-11")
    check(fake.sent() and fake.sent()[0]["channel"] == "CPIPE", "and in the classic layout it stays where it was")
