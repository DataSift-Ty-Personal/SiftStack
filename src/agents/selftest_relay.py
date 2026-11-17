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
ABORTED = (":rotating_light: FTM pipeline ABORTED at pre-flight (2026-11-15, commit):\n"
           "- the admin token has < 0.5h left - refresh before running.")
PROSPECT_QUIET = ":white_check_mark: Prospecting pipeline: up to date (1235 prospecting records, 7525 phones tagged, 0 new)."
PROSPECT_NEW = ":white_check_mark: Prospecting pipeline (COMMIT): 1235 prospecting records, 3 new phone-tier tags applied."
DIAL = ":telephone_receiver: Dial KPIs %s Fri Nov 13, 2026\n*Example Account*  %s  12 records worked\n*Dials*\n40" % (DOT, DOT)
DIAL_FAIL = ":rotating_light: Dial-KPI run FAILED for an example account 2026-11-14: HTTP 400 on the records search"
OTHER = "Caller ID check: all 24 numbers clean."
OTHER_FAIL = ":rotating_light: Caller ID check: 2 numbers flagged as spam."


def run(check, tmp: Path) -> None:
    from . import relay, slack_app, store, voice
    keep = {k: getattr(config, k) for k in ("DATA_DIR", "DB_PATH", "SLACK_CHANNEL", "SLACK_PIPELINE_CHANNEL",
                                           "SLACK_ISSUES_CHANNEL", "SLACK_VOICE", "SLACK_BOT_TOKEN", "RELAY_SECRET")}
    saved = (slack_app.post, voice._today, relay.handle)
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
    try:
        _checks(check, sent)
    finally:
        slack_app.post, voice._today, relay.handle = saved
        for k, v in keep.items():
            setattr(config, k, v)


def _kinds(posts) -> list:
    return [p["kind"] for p in posts]


def _checks(check, sent: list) -> None:
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
