"""Slack channels by purpose and the team's voice (Ty, 2026-11-15): routing a scheduled stage's posts
to the pipeline or issues channel, the free cleanup, the guarded rewrite, once-a-day issues, and a
crashed stage said in plain words. Zero network: a recording fake for slack_app.post and a fake model.
Every name and street here is invented.
"""
from __future__ import annotations

from pathlib import Path

from . import config


class FakeAsk:
    """The rewrite model: answers what it is told to, counts calls."""

    def __init__(self, reply="", raise_=None):
        self.reply, self.raise_, self.calls = reply, raise_, 0

    def __call__(self, system, user):
        self.calls += 1
        if self.raise_:
            raise self.raise_
        return (self.reply(system, user) if callable(self.reply) else self.reply), 400, 80


def run(check, tmp: Path) -> None:
    from . import slack_app, store, voice
    keep = {k: getattr(config, k) for k in ("DATA_DIR", "DB_PATH", "SLACK_CHANNEL", "SLACK_PIPELINE_CHANNEL", "SLACK_ISSUES_CHANNEL",
                                           "SLACK_VOICE", "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "SLACK_VOICE_DAILY",
                                           "PLAN_POSTS", "SLACK_VOICE_MODEL")}
    saved_post, saved_ask, saved_check, saved_today = slack_app.post, voice.ASK, voice.CHECK, voice._today
    # Its own database, always: the path is fixed when config loads, so a section run on its own would
    # otherwise write its tasks, cache and spend into the real agents.db (found 2026-11-15).
    config.DATA_DIR = Path(tmp) / "slack_split"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    store.init()
    sent: list = []

    def fake_post(channel, text, *, thread_ts="", employee=None):
        sent.append((channel, text, thread_ts))
        return "1700.%d" % len(sent)
    slack_app.post = fake_post
    try:
        _checks(check, sent)
    finally:
        slack_app.post, voice.ASK, voice.CHECK, voice._today = saved_post, saved_ask, saved_check, saved_today
        for k, v in keep.items():
            setattr(config, k, v)


def _checks(check, sent: list) -> None:
    from . import slack_app, store, voice, worker
    from .autopilot import sweep
    print("  channels by purpose")
    config.SLACK_CHANNEL, config.SLACK_PIPELINE_CHANNEL, config.SLACK_ISSUES_CHANNEL = "CHOME", "", ""
    check(slack_app.channel_for("pipeline") == "CHOME" and slack_app.channel_for("issues") == "CHOME",
          "before the new channels exist, every post still lands in the team channel")
    config.SLACK_PIPELINE_CHANNEL, config.SLACK_ISSUES_CHANNEL = "CPIPE", "CISSUE"
    check(slack_app.channel_for("pipeline") == "CPIPE" and slack_app.channel_for("issues") == "CISSUE"
          and slack_app.channel_for("chat") == "CHOME",
          "with them set, the pipeline, issues and talk each have their own channel")

    print("  the free cleanup")
    raw = (":warning: *ftm death screen* _(plan: nothing written)_ No new record to screen.\n"
           ":small_orange_diamond: the obituary index did not answer (HTTPConnectionPool(host='<http://example.org|example.org>', "
           "port=80): Rea); those records were not screened and will be tried again")
    c = voice.clean(raw)
    check(":warning:" not in c and "plan: nothing written" not in c and "HTTP" not in c and "host=" not in c
          and "*" not in c and "will be tried again" in c,
          "the cleanup drops the emoji, the plan tag and the error text, and keeps what it means")
    check(voice.clean("29 sale(s) inside 45 days, 1 new lead(s)") == "29 sales inside 45 days, 1 new lead",
          "a count is said once: 29 sales, 1 lead, never lead(s)")
    check(voice.clean("Today final at 7:45: 412 dials, the 12:30:00 run") == "Today final at 7:45: 412 dials, the 12:30:00 run"
          and voice.clean(":white_check_mark: done") == "done",
          "a time is never taken for an emoji code (\"7:45:\" lost its minutes before 2026-11-16); a real code still goes")
    check(voice.clean("3 touch(es), 1 batch(es), 2 company(ies), 1 company(ies), status(es)")
          == "3 touches, 1 batch, 2 companies, 1 company, statuses",
          "the other plural forms the stages write read as plain words too")
    keep_model = config.SLACK_VOICE_MODEL
    config.SLACK_VOICE_MODEL = "claude-haiku-5-5"
    cheap = voice.price()
    config.SLACK_VOICE_MODEL = "some-new-model"
    check(cheap == (0.10, 0.50) and voice.price() == voice.DEFAULT_PRICE,
          "a rewrite is priced for the model that wrote it, and an unknown model is priced high, never low")
    config.SLACK_VOICE_MODEL = keep_model
    check(voice.stage_name("ftm_death_screen") == "County death check" and voice.stage_name("top1000_new_thing") == "Top 1,000 new thing",
          "a stage is named in plain words")

    print("  the rewrite, guarded")
    config.SLACK_VOICE, config.ANTHROPIC_API_KEY = "ty", ""
    good = FakeAsk("The obituary site was down.\nWe could not check owners today.\nIt tries again soon.\nNothing for you to do.")
    voice.ASK, voice.CHECK = good, (lambda source, written: True)
    t = store.enqueue("ops", "touch_audit", "", subject="test", source="schedule")[0]
    out = voice.issue_text("ftm_death_screen", "the obituary index did not answer; those records will be tried again", t)
    check(out.startswith("The obituary site was down") and good.calls == 1 and store.spent(task_id=t) > 0,
          "an issue is rewritten in plain words, and its small cost is booked against the task that posted it")
    again = voice.issue_text("ftm_death_screen", "the obituary index did not answer; those records will be tried again", t)
    check(again == out and good.calls == 1, "the same post is not rewritten twice: the first rewrite is kept")
    voice.ASK = FakeAsk("We have 37 sales coming up.")
    check(voice.update_text("ftm_auction", "29 sale(s) inside 45 days", []) == "Auction calendar: 29 sales inside 45 days",
          "a rewrite that writes a number it was not given is refused, and the cleaned post goes out instead")
    voice.ASK = FakeAsk("Auctions: 29 sales soon %s busy week" % chr(0x2014))
    check(chr(0x2014) not in voice.update_text("ftm_auction", "29 sale(s) within 45 days", []),
          "a rewrite with an em dash is refused")
    voice.ASK = FakeAsk("The site gave an HTTP error.")
    check("HTTP" not in voice.issue_text("x_stage", "the site broke (HTTP 500)"), "an issue may never carry error text")
    voice.ASK = FakeAsk(raise_=RuntimeError("model down"))
    check(voice.update_text("ftm_auction", "3 sale(s) next week", []) == "Auction calendar: 3 sales next week",
          "when the model cannot be reached, the cleaned post still goes out")
    config.SLACK_VOICE_DAILY, capped = 0, FakeAsk("never used")
    voice.ASK = capped
    check(voice.update_text("ftm_auction", "4 sale(s) this week", []) == "Auction calendar: 4 sales this week" and capped.calls == 0,
          "past the daily cap the model is not asked at all")
    config.SLACK_VOICE_DAILY = 400
    voice.ASK = FakeAsk("The site was down. We will try again. Nothing for you to do.")
    split = voice.issue_text("x_stage", "the site was down; it will be tried again")
    check(split == "The site was down.\nWe will try again.\nNothing for you to do.",
          "an issue is one short sentence a line, even when the model runs them together")
    voice.ASK = FakeAsk("Nobody was called yesterday.")
    voice.CHECK = lambda source, written: False
    check(voice.issue_text("call_audit_x", "the audit crashed partway") == "Call audit x: the audit crashed partway",
          "a rewrite the second look says adds something is refused, and the cleaned post goes out")
    voice.CHECK = None
    judged = FakeAsk(lambda system, user: "PASS" if system == voice.JUDGE else "The site was slow.")
    voice.ASK = judged
    check(voice.update_text("x_stage", "the site was slow", []) == "The site was slow." and judged.calls == 2,
          "with no test hook, the second look is a real call to the model, and PASS lets the rewrite through")
    voice.ASK = FakeAsk(lambda system, user: "FAIL" if system == voice.JUDGE else "The site was slow again.")
    check(voice.update_text("x_stage", "the site was slow at noon", []) == "X stage: the site was slow at noon",
          "and FAIL stops it")
    voice.ASK = FakeAsk(lambda system, user: (_ for _ in ()).throw(RuntimeError("down")) if system == voice.JUDGE
                        else "The site was slow at night.")
    check(voice.update_text("x_stage", "the site was slow at night", []) == "X stage: the site was slow at night",
          "a second look that cannot be made counts as a no: the cleaned post goes out")
    config.SLACK_VOICE = "plain"
    voice.ASK = voice.CHECK = None

    print("  what a scheduled stage posts")
    day = ["2026-11-16"]
    voice._today = lambda: day[0]
    plan = {"stage": "touch_audit", "kind": "audit", "live": False, "acted": 3, "head": "30 touches should not have happened",
            "lines": [], "problems": ["12: the same number dialed 3 or more times in a day"]}
    p1 = sweep.posts({"id": 1, "workflow": "touch_audit", "source": "schedule"}, plan)
    check([p["kind"] for p in p1] == ["issues"], "a plan-mode stage posts only its problem, to the issues channel")
    p2 = sweep.posts({"id": 1, "workflow": "touch_audit", "source": "schedule"},
                     dict(plan, problems=["14: the same number dialed 3 or more times in a day"]))
    check(p2 == [], "the same problem with a new count is not said again that day")
    day[0] = "2026-11-17"
    check([p["kind"] for p in sweep.posts({"id": 1, "workflow": "touch_audit", "source": "schedule"}, plan)] == ["issues"],
          "while it lasts, a problem is said once each business day")
    quiet = dict(plan, problems=[])
    check(sweep.posts({"id": 1, "workflow": "touch_audit", "source": "schedule"}, quiet) == [],
          "a plan-mode stage with nothing wrong posts nothing; the morning digest carries it")
    lead = {"stage": "speed_to_lead", "kind": "leadclock", "live": False, "acted": 1, "head": "1 new lead is waiting",
            "problems": ["Owner: 1 Example St, Knoxville has waited 18 min for a first call. Call now."]}
    pl = sweep.posts({"id": 2, "workflow": "speed_to_lead", "source": "schedule"}, lead)
    check([p["kind"] for p in pl] == ["pipeline"] and "1 Example St" in pl[0]["text"],
          "a lead nobody has called is an alert for the team, in the pipeline channel, with its address")
    dg = sweep.posts({"id": 3, "workflow": "autopilot_digest", "source": "schedule"},
                     {"stage": "autopilot_digest", "kind": "digest", "live": False, "head": "Morning readout",
                      "lines": ["252 dials yesterday"], "problems": []})
    check([p["kind"] for p in dg] == ["pipeline"], "the morning digest goes to the pipeline channel")
    news = {"stage": "lead_appointments", "kind": "appointments", "live": False, "acted": 0, "head": "", "lines": [],
            "problems": [], "alerts": ["Appointment set: 1 Example St, Tue 2pm", "Appointment set: 7 Example St, Tue 3pm"]}
    first = sweep.posts({"id": 8, "workflow": "lead_appointments", "source": "schedule"}, news)
    check([p["kind"] for p in first] == ["pipeline", "pipeline"],
          "two alerts that differ only by a house number and a time are both posted, to the pipeline channel")
    check(sweep.posts({"id": 8, "workflow": "lead_appointments", "source": "schedule"}, news) == [],
          "an alert is posted once ever, not again when the stage reads the same appointment tomorrow")
    live = {"stage": "ftm_assign", "kind": "assign", "live": True, "acted": 4, "head": "Assigned 4 records to Emerson", "lines": []}
    check([p["kind"] for p in sweep.posts({"id": 4, "workflow": "ftm_assign", "source": "schedule"}, live)] == ["pipeline"],
          "what a live stage did is a short update in the pipeline channel")
    auction = {"stage": "ftm_auction", "kind": "auction", "live": False, "acted": 0, "head": "6 sales inside 45 days",
               "lines": ["Nov 18: 1 Example St, Knoxville"], "problems": []}
    ap = sweep.posts({"id": 5, "workflow": "ftm_auction", "source": "schedule"}, auction)
    check([p["kind"] for p in ap] == ["pipeline"] and "1 Example St" in ap[0]["text"],
          "a report the team works from (the auction calendar) still posts, in the pipeline channel, streets intact")
    cadence = {"stage": "cadence_tuner", "kind": "cadence", "live": False, "acted": 0, "head": "2 changes to consider",
               "lines": [], "problems": ["the dial log could not be read"]}
    check([p["kind"] for p in sweep.posts({"id": 6, "workflow": "cadence_tuner", "source": "schedule"}, cadence)]
          == ["chat", "issues"],
          "an owner's report stays in the team channel, and what broke in it goes to issues")
    config.PLAN_POSTS = "off"
    check(sweep.posts({"id": 7, "workflow": "touch_audit", "source": "schedule"},
                      dict(plan, problems=["a brand new problem"])) == [] and
          [p["kind"] for p in sweep.posts({"id": 3, "workflow": "autopilot_digest", "source": "schedule"},
                                          {"stage": "autopilot_digest", "kind": "digest", "live": False, "head": "x",
                                           "lines": [], "problems": []})] == ["pipeline"],
          "AGENTS_PLAN_POSTS=off silences every plan-mode stage but the morning digest")
    config.PLAN_POSTS = "digest"

    print("  the worker routes them")
    sent.clear()
    task = {"id": t, "workflow": "touch_audit", "source": "schedule", "agent": "ops"}
    check(worker._routed(task) and not worker._routed(dict(task, slack_thread="1700.9"))
          and not worker._routed(dict(task, source="slack")),
          "only a scheduled stage run with no thread is routed; anything asked in Slack answers in its thread")
    worker._route(task, [{"kind": "issues", "text": "a"}, {"kind": "pipeline", "text": "b"}], None, None)
    check([s[0] for s in sent] == ["CISSUE", "CPIPE"] and all(s[2] == "" for s in sent),
          "each routed post goes to its own channel, none threaded under another")
    sent.clear()
    day[0] = "2026-11-18"
    worker._fail(dict(task, slack_channel=""), None, "RuntimeError: the CRM answered 502")
    check(len(sent) == 1 and sent[0][0] == "CISSUE" and ":x:" not in sent[0][1] and "RuntimeError" not in sent[0][1]
          and "502" in sent[0][1],
          "a scheduled stage that crashed is said in the issues channel, without the programmer's words")
    sent.clear()
    worker._fail(dict(task, slack_channel=""), None, "RuntimeError: the CRM answered 502")
    check(sent == [], "and only once that day")
    print("  the team channel keeps its own notices")
    config.SLACK_BOT_TOKEN = "xoxb-test"
    sent.clear()
    slack_app.notice("I can hear this channel now.", None, kind="chat")
    slack_app.notice("A webhook arrived with no record id.", None)
    check([s[0] for s in sent] == ["CHOME", "CISSUE"], "a word about hearing stays in the team channel; breakage goes to issues")
    config.SLACK_BOT_TOKEN = ""
