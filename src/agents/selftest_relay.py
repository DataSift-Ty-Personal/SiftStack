"""The Slack relay (Ty, 2026-11-15): the jobs that posted to #first-to-market post to the brain
instead, and each message lands in pipeline, issues or the team channel. Zero network: a recording
fake for slack_app.post, the free cleanup instead of the model, its own database. The message
shapes copy the real senders; every name and account here is invented.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

DOT = chr(0x00B7)
GREEN_DIGEST = ":white_check_mark: *FTM Nov 14* %s 18 new records\n\nForeclosure 12 %s Probate 0 %s Condemned 0 %s Liens 6" % (
    DOT, DOT, DOT, DOT)
RED_DIGEST = (":rotating_light: *FTM Nov 15* %s 0 new records\n\nForeclosure 0 %s Probate 0 %s Condemned 0\n"
              "Stage notices returned nothing. 0 notices scraped." % (DOT, DOT, DOT))
RED_RUN = (":rotating_light: *SiftStack FTM run* (COMMIT)\n"
           "  *notices* [EMPTY] 0 notices scraped. Not a normal quiet day: check the notice gate. (540.8s)\n"
           "  *capture* [OK] condemnations: 12 this cycle, 26 cumulative (9.1s)\n"
           "  *county* [OK] 17 records, 17 into DataSift (43.8s)\n"
           "      warn: sold guard could not read 1 record(s)")
GREEN_RUN = (":white_check_mark: *SiftStack FTM run* (COMMIT)\n  *notices* [OK] 13 records, 13 into DataSift (1016.3s)\n"
             "  *county* [OK] 23 records, 23 into DataSift (102.8s)\n      warn: sold guard could not read 1 record(s)")
DRY_RUN = ":white_check_mark: *SiftStack FTM run* (DRY RUN)\n  *notices* [OK] 0 records ready (310.1s)"
WATCHDOG = ("*FTM pull is red*\nRED 2026-11-13 last_run=2026-11-13T11:39:19 :: Stage notices returned nothing. "
            "0 notices scraped.\n\nThis came from the watchdog, not the box, so it also fires when the daily digest "
            "never arrives.\nCheck: `fly status -a example-app` then `fly logs -a example-app`")
# The watchdogs as the issue fixer's box and the GitHub workflows send them (2026-11-20): every status, not
# just red. An unreachable box used to read as a chat message.
FOOT = ("\n\nThis came from the watchdog on the issue fixer's box, so it also fires when the box itself goes quiet.\n"
        "Check: `fly status -a example-app` then `fly logs -a example-app`")
FTM_GONE = "*FTM pull is unreachable*\nThe machine did not answer (flyctl exit 1). It is most likely stopped." + FOOT
BRAIN_GONE = ("*Agents brain is unreachable*\nThe health page did not answer (timed out). The machine is most likely "
              "stopped, out of credit or mid-deploy." + FOOT)
BRAIN_RED = "*Agents brain is red*\nThe worker thread is dead, so nothing queued will run." + FOOT
TEXTING_STALE = ("*SMS agent is stale*\nThe receiver answered but the worker has not run a pass in 41 minutes." + FOOT)
ABORTED = (":rotating_light: FTM pipeline ABORTED at pre-flight (2026-11-15, commit):\n"
           "- the admin token has < 0.5h left - refresh before running.")
PROSPECT_QUIET = ":white_check_mark: Prospecting pipeline: up to date (1235 prospecting records, 7525 phones tagged, 0 new)."
PROSPECT_NEW = ":white_check_mark: Prospecting pipeline (COMMIT): 1235 prospecting records, 3 new phone-tier tags applied."
DIAL = ":telephone_receiver: Dial KPIs %s Fri Nov 13, 2026\n*Example Account*  %s  12 records worked\n*Dials*\n40" % (DOT, DOT)
DIAL_FAIL = ":rotating_light: Dial-KPI run FAILED for an example account 2026-11-14: HTTP 400 on the records search"
OTHER = "Caller ID check: all 24 numbers clean."
OTHER_FAIL = ":rotating_light: Caller ID check: 2 numbers flagged as spam."
# The digest when a paid subscription is ending (ftm_health.expiring, 2026-11-19): amber reads as the
# tick, and the line is a person's to-do.
ROD_ENDING = "Register of Deeds subscription ends in 10 days (Dec 11). Renew it at paxsub.knoxrod.org, on the Subscription tab."
TNPN_ENDING = ("tnpublicnotice subscription ends in 10 days (Dec 11). Renew it on tnpublicnotice.com "
               "(My Smart Search, then Renew).")
AMBER_ENDING = (":white_check_mark: *FTM Dec 1* %s 3 new records\n\nForeclosure 2 %s Probate 1 %s Condemned 0\n%s\n%s"
                % (DOT, DOT, DOT, ROD_ENDING, TNPN_ENDING))
RED_ENDING = (":rotating_light: *FTM Dec 9* %s 0 new records\n\nForeclosure 0 %s Probate 0 %s Condemned 0\n"
              "Stage notices returned nothing. 0 notices scraped.\n%s\n%s"
              % (DOT, DOT, DOT, ROD_ENDING.replace("in 10 days", "in 2 days"), TNPN_ENDING.replace("in 10 days", "in 2 days")))


def run(check, tmp: Path) -> None:
    from . import relay, slack_app, store, voice
    keep = {k: getattr(config, k) for k in ("DATA_DIR", "DB_PATH", "SLACK_CHANNEL", "SLACK_PIPELINE_CHANNEL",
                                           "SLACK_ISSUES_CHANNEL", "SLACK_VOICE", "SLACK_BOT_TOKEN", "RELAY_SECRET")}
    saved = (slack_app.post, voice._today, relay.handle, relay.today_texts, slack_app._http)
    config.DATA_DIR = Path(tmp) / "relay"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    store.init()
    sent: list = []

    def fake_post(channel, text, *, thread_ts="", employee=None):
        sent.append((channel, text))
        return "1700.%d" % len(sent)
    slack_app.post = fake_post
    config.SLACK_CHANNEL, config.SLACK_PIPELINE_CHANNEL, config.SLACK_ISSUES_CHANNEL = "CHOME", "CPIPE", "CISSUE"
    config.SLACK_VOICE, config.SLACK_BOT_TOKEN = "plain", "xoxb-test"
    channel_today: list = []
    relay.today_texts = lambda channel: list(channel_today)      # what the issues channel holds today
    try:
        _checks(check, sent, channel_today, saved[3])
    finally:
        slack_app.post, voice._today, relay.handle, relay.today_texts, slack_app._http = saved
        for k, v in keep.items():
            setattr(config, k, v)


def _kinds(posts) -> list:
    return [p["kind"] for p in posts]


def _checks(check, sent: list, channel_today: list, saved_today_texts) -> None:
    from . import relay, voice
    print("  what each message is")
    check([relay.classify(t) for t in (GREEN_DIGEST, RED_RUN, WATCHDOG, ABORTED, PROSPECT_QUIET, DIAL, DIAL_FAIL, OTHER)]
          == ["ftm_digest", "ftm_run", "ftm_watchdog", "ftm_workstation", "prospecting", "dial_kpis", "dial_kpis", "other"],
          "every sender that posted to #first-to-market is recognised by its own first line")

    print("  where it goes")
    check(_kinds(relay.route(GREEN_DIGEST)) == ["pipeline"], "the first-to-market digest goes to the pipeline every day")
    red = relay.route(RED_DIGEST)
    check(_kinds(red) == ["pipeline", "issues"] and red[1]["group"] == "ftm" and "returned nothing" in red[1]["text"],
          "a red digest still goes to the pipeline, and its reason goes to issues")
    check(relay.route(GREEN_RUN) == [] and relay.route(DRY_RUN) == [],
          "a clean run report or a dry run posts nothing: the digest says the same in two lines")
    run = relay.route(RED_RUN)
    check(_kinds(run) == ["issues"] and "0 notices scraped" in run[0]["text"] and "540.8" not in run[0]["text"]
          and "condemnations" not in run[0]["text"] and "warn:" not in run[0]["text"],
          "a failed run report goes to issues with only the stage that failed, no timings, no healthy stages")
    wd = relay.route(WATCHDOG)
    check(_kinds(wd) == ["issues"] and "fly status" not in wd[0]["text"] and "returned nothing" in wd[0]["text"],
          "the watchdog goes to issues without the developer's commands")
    check(_kinds(relay.route(ABORTED)) == ["issues"], "a job that stopped at its pre-flight check is an issue")
    check(relay.route(PROSPECT_QUIET) == [] and _kinds(relay.route(PROSPECT_NEW)) == ["pipeline"],
          "the phone tagging says nothing on a quiet day and goes to the pipeline when it tagged something")
    check(_kinds(relay.route(DIAL)) == ["pipeline"] and _kinds(relay.route(DIAL_FAIL)) == ["issues"],
          "the dial KPIs go to the pipeline, and a dial KPI run that failed goes to issues")
    check(_kinds(relay.route(OTHER)) == ["chat"] and _kinds(relay.route(OTHER_FAIL)) == ["issues"],
          "anything else goes to the team channel, unless it says something broke")

    print("  a paid subscription that is ending")
    amber = relay.route(AMBER_ENDING)
    check(relay.classify(AMBER_ENDING) == "ftm_digest" and _kinds(amber) == ["pipeline"]
          and ROD_ENDING in amber[0]["text"] and TNPN_ENDING in amber[0]["text"],
          "a digest that says a subscription is ending goes to the pipeline with that line, and nothing goes to issues")
    red_e = relay.route(RED_ENDING)
    check(_kinds(red_e) == ["pipeline", "issues"] and "returned nothing" in red_e[1]["text"]
          and "subscription" not in red_e[1]["text"] and "Renew" not in red_e[1]["text"],
          "on a red day issues hears only what broke: an ending subscription is a to-do, not breakage")
    ended = AMBER_ENDING.replace("ends in 10 days (Dec 11)", "has ended")
    check(_kinds(relay.route(ended)) == ["pipeline"] and relay.EXPIRY_RX.search("Register of Deeds subscription has ended."),
          "a subscription that has ended is never by itself an issue (the stage that fails for it is)")
    check(not relay.EXPIRY_RX.search("Stage liens FAILED: REGISTER OF DEEDS SUBSCRIPTION EXPIRED. Renew it."),
          "a red reason that names a lapse is still breakage, not an ending-subscription line")
    sec = relay.to_board(RED_ENDING)
    check(sec and sec["head"].startswith("FTM Dec 9") and len(sec["lines"]) == 4
          and sec["lines"][1].startswith("Stage notices returned nothing")
          and sec["lines"][2].startswith("Register of Deeds subscription ends in 2 days")
          and sec["lines"][3].startswith("tnpublicnotice subscription ends in 2 days"),
          "the board section holds the counts, the reason and both ending subscriptions")
    crowded = relay.to_board(RED_ENDING.replace("0 notices scraped.", "0 notices scraped.\nA fifth line.\nA sixth line."))
    check(crowded and len(crowded["lines"]) == 4 and crowded["lines"][-2:] == [r for r in relay._lines(
              relay._plain(RED_ENDING)) if relay.EXPIRY_RX.search(r)],
          "a section with more lines than fit drops other lines first: an ending subscription is never the one cut")
    check(relay.to_board(OTHER) is None and relay.to_board(DIAL)["lines"] == relay._lines(relay._plain(DIAL))[1:5]
          and relay.to_board(PROSPECT_NEW)["lines"] == relay._lines(relay._plain(PROSPECT_NEW))[1:5],
          "every other report's section is what it was")

    print("  one first-to-market problem a day")
    day = ["2026-11-16"]
    voice._today = lambda: day[0]
    sent.clear()
    relay.deliver(RED_RUN, "ftm")
    relay.deliver(RED_DIGEST, "ftm")
    relay.deliver(WATCHDOG, "watchdog")
    check([s[0] for s in sent] == ["CISSUE", "CPIPE"],
          "the run report, the digest and the watchdog are one problem: issues hears it once, the pipeline gets the digest")
    day[0] = "2026-11-17"
    sent.clear()
    relay.deliver(GREEN_RUN, "ftm")
    relay.deliver(GREEN_DIGEST, "ftm")
    relay.deliver(WATCHDOG, "watchdog")
    check([s[0] for s in sent] == ["CPIPE", "CISSUE"],
          "when the box said nothing was wrong, the watchdog's red still reaches issues (it exists for the silent box)")
    sent.clear()
    relay.deliver(OTHER_FAIL, "workstation")
    relay.deliver(OTHER_FAIL, "workstation")
    check([s[0] for s in sent] == ["CISSUE"], "any other problem is said once a day")
    check(all(":rotating_light:" not in s[1] and "*" not in s[1] for s in sent), "relayed posts carry no emoji codes or bold marks")

    print("  the watchdogs, on time from the fixer's box and late from GitHub")
    check([relay.classify(t) for t in (FTM_GONE, BRAIN_GONE, BRAIN_RED, TEXTING_STALE)]
          == ["ftm_watchdog", "agents_watchdog", "agents_watchdog", "sms_watchdog"],
          "every watchdog is recognised by its head at every status, unreachable included")
    gone = relay.route(BRAIN_GONE)
    check(_kinds(gone) == ["issues"] and gone[0]["group"] == "watchdog_agents" and gone[0]["voice"] == "plain"
          and gone[0]["text"].startswith("Agents brain watchdog: the brain is unreachable.")
          and "did not answer" in gone[0]["text"] and "fly status" not in gone[0]["text"],
          "an unreachable brain goes to issues in its own words, starting with its marker (it went to the team channel)")
    ftm_gone = relay.route(FTM_GONE)
    check(_kinds(ftm_gone) == ["issues"] and ftm_gone[0]["group"] == "ftm"
          and ftm_gone[0]["text"].startswith("First-to-market watchdog: the pull is unreachable."),
          "an unreachable first-to-market box is a first-to-market problem, said once a day with the others")
    texting = relay.route(TEXTING_STALE)
    check(texting[0]["group"] == "watchdog_sms" and texting[0]["text"].startswith("Texting app watchdog: the app is stale."),
          "a stale texting app goes to issues under its own marker")
    day[0] = "2026-11-20"
    sent.clear()
    relay.deliver(BRAIN_GONE, "watchdog")           # the fixer's box, on time
    relay.deliver(BRAIN_RED, "watchdog")            # GitHub, eight hours late, a different reason
    check([s[0] for s in sent] == ["CISSUE"] and sent[0][1].startswith("Agents brain watchdog: the brain is unreachable."),
          "one post per watchdog a day whoever sends it, unrewritten so it keeps its marker")
    day[0] = "2026-11-21"
    sent.clear()
    channel_today[:] = ["Agents brain watchdog: the brain is unreachable. The brain's relay did not answer, so this "
                        "came straight from the watchdog on the issue fixer's box."]
    relay.deliver(BRAIN_RED, "watchdog")
    check(sent == [] and relay.store.kv_get(relay._group_key("watchdog_agents")),
          "a red the box posted straight to the channel (the relay was down) keeps GitHub's later one out, and is remembered")
    channel_today.clear()
    relay.deliver(TEXTING_STALE, "watchdog")
    relay.deliver(TEXTING_STALE, "watchdog")
    check([s[0] for s in sent] == ["CISSUE"], "another watchdog is its own post, once")
    day[0] = "2026-11-22"
    sent.clear()
    relay.deliver(BRAIN_RED, "watchdog")
    check([s[0] for s in sent] == ["CISSUE"], "the next business day the brain's watchdog is said again")

    print("  reading today's posts in the channel")
    from . import slack_app

    class _Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    pages = [{"ok": True, "messages": [{"ts": "3.0", "text": "Agents brain watchdog: the brain is red."},
                                       {"ts": "2.5", "thread_ts": "1.0", "text": "Texting app watchdog: a reply sent to the channel too"}],
              "response_metadata": {"next_cursor": "more"}},
             {"ok": True, "messages": [{"ts": "1.0", "text": "Lead check: 2 leads"}]}]
    asked: list = []

    class _Http:
        def get(self, url, timeout=0, params=None, headers=None):
            asked.append(dict(params or {}))
            return _Resp(pages[(len(asked) - 1) % len(pages)])

    keep_texts, keep_http = relay.today_texts, slack_app._http
    relay.today_texts = saved_today_texts
    slack_app._http = _Http()
    try:
        texts = relay.today_texts("CISSUE")
        check(texts == ["Agents brain watchdog: the brain is red.", "Lead check: 2 leads"] and asked[1].get("cursor") == "more"
              and float(asked[0]["oldest"]) > 0,
              "today's posts are read from midnight, every page, without the replies a thread also sent to the channel")
        check(relay.said_today("CISSUE", "Agents brain watchdog:") and not relay.said_today("CISSUE", "Texting app watchdog:"),
              "a marker is found only at the start of a post of its own")

        class _Down:
            def get(self, *a, **k):
                raise OSError("no network")
        slack_app._http = _Down()
        check(relay.today_texts("CISSUE") == [], "a channel that cannot be read holds nothing: the alarm goes out")
    finally:
        relay.today_texts, slack_app._http = keep_texts, keep_http

    print("  what the team can read back")
    # 2026-11-18: nothing was kept, so Atlas could not see a clean morning run and said it never ran.
    from datetime import date
    from . import query
    day[0] = "2026-11-18"
    for t, src in ((GREEN_RUN, "ftm"), (GREEN_DIGEST, "ftm"), (DRY_RUN, "ftm"), (DIAL, "workstation"), (OTHER, "workstation")):
        relay.handle(t, src)
    kept = relay.ftm_reports("2026-11-18")
    check([k["what"] for k in kept] == ["ftm_run", "ftm_digest", "ftm_run"] and [k["dry"] for k in kept] == [False, False, True],
          "the first-to-market run report, its digest and a dry run are kept; the dial KPIs and anything else are not")
    check(all("*" not in k["text"] and ":white_check_mark:" not in k["text"] and "1016.3" not in k["text"] for k in kept)
          and "13 records, 13 into DataSift" in kept[0]["text"],
          "a kept report is the box's own words without emoji codes, bold marks or timings")
    said = relay.ftm_summary("2026-11-18")
    check("13 records, 13 into DataSift" in said and "18 new records" in said and "DRY" not in said,
          "the summary an employee reads carries the run report and the digest, and leaves the dry run out")
    later = relay.ftm_summary("2026-11-19")
    check("No report" in later and "2026-11-18" in later, "a morning with no report yet says so, and when the box last reported")
    keep_today = query._today
    query._today = lambda: date(2026, 11, 18)
    try:
        ans = query.team_query({"about": "ftm"})
    finally:
        query._today = keep_today
    check(ans["reported_today"] and len(ans["reports"]) == 3 and ans["reports"][2]["dry_run"]
          and any("records_query" in x for x in ans["limits"]) and any("nobody runs it by hand" in x for x in ans["limits"]),
          "team_query about=ftm returns the day's reports, flags the dry run, and says why a created-today count is smaller")
    for _ in range(20):
        relay.remember(GREEN_RUN, "ftm")
    check(len(relay.ftm_reports()) == relay.FTM_KEEP, "only the last %d reports are kept" % relay.FTM_KEEP)

    print("  safety")
    config.SLACK_BOT_TOKEN = ""
    sent.clear()
    check(relay.deliver(OTHER, "workstation") == [] and sent == [],
          "with no bot token the relay posts nothing, so it can never post to itself through a webhook")
    config.SLACK_BOT_TOKEN = "xoxb-test"

    print("  reading a webhook body")
    blocks = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "From blocks"}}]}
    check(relay.extract_text(blocks) == "From blocks" and relay.extract_text({"text": "Words", **blocks}) == "Words"
          and relay.extract_text({"content": "Discord words"}) == "Discord words",
          "the words come from text first, then blocks, then Discord's content")
    form = relay.parse(b"payload=%7B%22text%22%3A%22form+words%22%7D", "application/x-www-form-urlencoded")
    check(form == {"text": "form words"} and relay.parse(b"bare words") == {"text": "bare words"},
          "a legacy form body and a bare body are read too")

    print("  the route on the brain")
    from fastapi.testclient import TestClient
    from . import brain
    c = TestClient(brain.app)
    config.RELAY_SECRET = "relay-secret"
    seen: list = []
    relay.handle = lambda text, source="": seen.append((source, text)) or []
    check(c.post("/hooks/slack/wrong/ftm", json={"text": OTHER}).status_code == 404,
          "a wrong secret gets a 404 and nothing is posted")
    r = c.post("/hooks/slack/relay-secret/ftm", json={"text": RED_DIGEST})
    import time
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    check(r.status_code == 200 and r.text == "ok" and seen and seen[0][0] == "ftm",
          "the right secret answers ok at once, the way Slack does, and the message is handled behind it")
    dry = c.post("/hooks/slack/relay-secret/ftm?dry=1", json={"text": RED_DIGEST}).json()
    check(dry["what"] == "ftm_digest" and [p["kind"] for p in dry["posts"]] == ["pipeline", "issues"] and len(seen) == 1,
          "dry=1 says where a message would go and posts nothing")
    check(c.post("/hooks/slack/relay-secret/ftm", json={}).status_code == 400, "an empty message is refused like Slack does")
    config.RELAY_SECRET = ""
    check(c.post("/hooks/slack/relay-secret/ftm", json={"text": OTHER}).status_code == 404,
          "with no relay secret set, the route does not exist")
    health = c.get("/health").json().get("slack_relay") or {}
    check(set(health) >= {"on", "today", "last"} and "text" not in json.dumps(health),
          "the health page counts relayed messages and never shows their words")
