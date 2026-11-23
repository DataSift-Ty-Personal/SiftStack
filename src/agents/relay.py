"""The Slack relay (Ty, 2026-11-15): "migrate the first market pipeline that's in the actual Slack
channel underneath DataSift. Can we make sure all of that is discontinued and migrate it over to
this pipeline?"

Every job that posted to #first-to-market in the DataSift workspace kept its Slack incoming-webhook
code. Only its URL changed, to https://<brain>/hooks/slack/<AGENTS_RELAY_SECRET>/<source>, and the
brain decides where each message belongs and posts it in the team's voice (agents/voice.py):

  the first-to-market digest (8:00, "FTM Nov 15 . 18 new records")   pipeline, every day (the ops
                                                                      board's "First to market"
                                                                      section in the boards layout).
                                                                      A paid subscription it says is
                                                                      ending stays there as a to-do
                                                                      and never goes to issues
  the first-to-market run report (about 6:50)                        issues when a stage failed;
                                                                      otherwise nothing, because the
                                                                      digest says the same in two lines
  the watchdog ("FTM pull is red")                                   issues, only when nothing else
                                                                      said first-to-market was broken
                                                                      that day (it exists for the
                                                                      silent box)
  the brain's and the texting app's watchdogs ("Agents brain is      issues, once a day each
  unreachable", "SMS agent is stale")
  the prospecting phone tags (10:30)                                 pipeline when it tagged something
                                                                      new, otherwise nothing
  the dial KPIs (8pm)                                                pipeline
  any other failure (a siren, FAILED, ABORTED)                       issues, once a day per problem
  anything else                                                      the team channel

One first-to-market problem a business day reaches issues: the run report, the digest and the
watchdog describe the same failure three ways. The relay posts only with the bot token, never
through a webhook, so it can never post to itself.

The watchdogs run on time on the issue fixer's box (tools/issue_fixer/watch.py), and the same checks
run late on GitHub as a backup; both send here (2026-11-20). One post per watchdog a business day
reaches issues whoever sent it, and it goes out in its own words, unrewritten, so it starts with its
watchdog's marker. When the brain cannot be reached the box posts straight to the channel with that
marker, so before a watchdog post goes out the relay also looks at today's issues channel for one.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from . import config, slack_app, store, voice

log = logging.getLogger(__name__)

FAIL_RX = re.compile(r":rotating_light:|:x:|\bFAILED\b|\bABORTED\b|\[EMPTY\]|\[BLOCKED\]|\bBLOCKED\b|\bEXPIRED\b|"
                     r"\bUNREACHABLE\b|\bis red\b|\bRED\b")
STAGE_BAD_RX = re.compile(r"\[(?:FAILED|EMPTY|BLOCKED|SKIPPED)\]|\bEXPIRED\b|\bUNREACHABLE\b|\bBLOCKED\b")
DIGEST_RX = re.compile(r"^FTM [A-Z][a-z]{2} \d{1,2}\b")
SECONDS_RX = re.compile(r"\s*\(\d+(?:\.\d+)?s\)")
# A paid subscription ending soon, in the digest's own words (ftm_health.expiring, 2026-11-19):
# "Register of Deeds subscription ends in 9 days (Dec 11). Renew it at paxsub.knoxrod.org, ...". An
# action item for a person, not breakage: it never goes to issues and never by itself makes a digest
# read as failed, and on the board it is never the line cut. Matched in the first sentence only, so a
# red reason that quotes "SUBSCRIPTION EXPIRED" is still the breakage it is.
EXPIRY_RX = re.compile(r"^[^.]{0,80}\bsubscription (?:ends|ended|has ended)\b", re.I)
# Lines in a board section after its head (board.LINES_PER_SECTION).
SECTION_LINES = 4
LAST_KEY = "relay:last"

# The first-to-market box's own reports, kept so the team can answer "did it run, and what came
# in" (2026-11-18: with nothing kept, Atlas told Ty the pull "is still by hand, so nothing ran" on a
# morning the box had put 29 notices into DataSift). A clean run report is never posted, because
# the digest says the same in two lines, but it is the only place each stage's numbers are.
FTM_KEY = "relay:ftm:recent"
FTM_KINDS = ("ftm_run", "ftm_digest", "ftm_watchdog")
FTM_KEEP = 12
FTM_LABEL = {"ftm_run": "run report", "ftm_digest": "8am digest", "ftm_watchdog": "watchdog"}

# The three outside checks, read off the head their message carries ("*Agents brain is unreachable*"),
# whatever the status: an unreachable box is the failure a watchdog exists for. kind: (lead, subject,
# group, marker). A post starts with its lead; the marker is what a post about it starts with, which for
# first-to-market is the whole family (one first-to-market problem a day, whichever job said it first).
# tools/issue_fixer/watch.py posts the same leads when the relay cannot be reached, and a test holds the two
# to the same words.
WATCHDOG_RX = re.compile(r"^(FTM pull|Agents brain|SMS agent) is ([a-z]+)\b", re.I)
WATCHDOGS = {
    "ftm_watchdog": ("First-to-market watchdog:", "the pull", "ftm", "First-to-market"),
    "agents_watchdog": ("Agents brain watchdog:", "the brain", "watchdog_agents", "Agents brain watchdog:"),
    "sms_watchdog": ("Texting app watchdog:", "the app", "watchdog_sms", "Texting app watchdog:"),
}
WATCHDOG_KIND = {"ftm pull": "ftm_watchdog", "agents brain": "agents_watchdog", "sms agent": "sms_watchdog"}


def parse(raw: bytes, content_type: str = "") -> dict:
    """A Slack incoming-webhook body: JSON, the legacy form field payload=..., or bare text."""
    body = (raw or b"").decode("utf-8", "replace")
    if "x-www-form-urlencoded" in (content_type or "").lower():
        got = parse_qs(body).get("payload") or [""]
        body = got[0]
    try:
        data = json.loads(body or "{}")
    except ValueError:
        return {"text": body}
    return data if isinstance(data, dict) else {"text": str(data)}


def extract_text(payload: dict) -> str:
    """The message's words: its text, else Discord's content, else the text inside its blocks or
    attachments. A post that carries both text and blocks keeps only the text (the old notice
    blocks repeated each record's details)."""
    for key in ("text", "content"):
        if isinstance(payload.get(key), str) and payload[key].strip():
            return payload[key]
    parts = []
    for b in payload.get("blocks") or []:
        t = (b or {}).get("text")
        if isinstance(t, dict) and t.get("text"):
            parts.append(t["text"])
        for f in (b or {}).get("fields") or []:
            if isinstance(f, dict) and f.get("text"):
                parts.append(f["text"])
    for a in payload.get("attachments") or []:
        for key in ("pretext", "text", "fallback"):
            if isinstance((a or {}).get(key), str) and a[key].strip():
                parts.append(a[key])
                break
    return "\n".join(parts)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _head(text: str) -> str:
    first = next(iter(_lines(text)), "")
    return voice.EMOJI_RX.sub("", first).replace("*", "").strip()


def classify(text: str) -> str:
    head = _head(text)
    if DIGEST_RX.match(head):
        return "ftm_digest"
    if "SiftStack FTM run" in head:
        return "ftm_run"
    m = WATCHDOG_RX.match(head)
    if m:
        return WATCHDOG_KIND[m.group(1).lower()]
    if head.startswith("FTM pipeline"):
        return "ftm_workstation"
    if head.startswith("Prospecting pipeline"):
        return "prospecting"
    if head.startswith("Dial KPIs") or head.startswith("Dial-KPI"):
        return "dial_kpis"
    return "other"


def route(text: str, source: str = "") -> list[dict]:
    """Where one relayed message goes, as [{"kind", "text", "voice", "group"}]. Pure: no posting,
    no memory. "group" names problems that are one problem however many jobs report it."""
    what = classify(text)
    failed = bool(FAIL_RX.search(text or ""))
    lines = _lines(text)
    if what == "ftm_digest":
        out = [{"kind": "pipeline", "text": text, "voice": "report"}]
        # An ending subscription is a person's to-do (the pipeline post and the board carry it), so it
        # is neither part of the reason issues hears nor, alone, what makes the digest a failure.
        said = [ln for ln in lines if not EXPIRY_RX.search(ln)]
        reason = said[2:]
        failed = bool(FAIL_RX.search("\n".join(said)))
        if failed and reason:
            out.append({"kind": "issues", "text": "First-to-market pull: " + " ".join(reason), "voice": "issue",
                        "group": "ftm"})
        return out
    if what == "ftm_run":
        if "(DRY RUN)" in text or not failed:
            return []
        bad = [SECONDS_RX.sub("", ln).replace("*", "") for ln in lines[1:] if STAGE_BAD_RX.search(ln)]
        if not bad:
            bad = [SECONDS_RX.sub("", ln).replace("*", "") for ln in lines[1:] if not ln.startswith("warn:")]
        return [{"kind": "issues", "text": "First-to-market run: " + "\n".join(bad), "voice": "issue", "group": "ftm"}]
    if what in WATCHDOGS:
        lead, subject, group, marker = WATCHDOGS[what]
        status = WATCHDOG_RX.match(_head(text)).group(2).lower()
        said = []
        for ln in lines[1:]:
            if ln.lower().startswith("check:"):
                continue                     # the developer's commands, not something the reader does
            said.append(ln.split("::", 1)[1].strip() if "::" in ln else ln)
        # Unrewritten ("plain"): the post must start with its lead, for the look at the channel below.
        return [{"kind": "issues", "text": "%s %s is %s. %s" % (lead, subject, status, " ".join(said)),
                 "voice": "plain", "group": group, "marker": marker}]
    if what == "ftm_workstation":
        return [{"kind": "issues", "text": text, "voice": "issue", "group": "workstation"}] if failed else []
    if what == "prospecting":
        if failed:
            return [{"kind": "issues", "text": text, "voice": "issue"}]
        if re.search(r"\b0 new\b|up to date", text):
            return []                        # a quiet day says nothing
        return [{"kind": "pipeline", "text": text, "voice": "update"}]
    if what == "dial_kpis":
        if _head(text).startswith("Dial-KPI"):
            return [{"kind": "issues", "text": text, "voice": "issue"}]
        return [{"kind": "pipeline", "text": text, "voice": "report"}]
    if failed:
        return [{"kind": "issues", "text": text, "voice": "issue"}]
    return [{"kind": "chat", "text": text, "voice": "update"}]


def _group_key(group: str) -> str:
    return "relay:issue:%s:%s" % (group, voice._today())


def today_texts(channel: str) -> list[str]:
    """The words of today's top-level posts in a channel (business day). Empty when Slack cannot be read:
    the watchdog post then goes out, because a missed alarm costs more than a second one."""
    if not (config.SLACK_BOT_TOKEN and channel):
        return []
    tz = ZoneInfo(getattr(config, "BUSINESS_TZ", "") or "America/New_York")
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[str] = []
    cursor = ""
    for _ in range(5):
        params = {"channel": channel, "oldest": "%.6f" % start.timestamp(), "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = slack_app._http.get("%s/conversations.history" % slack_app.API, timeout=15, params=params,
                                    headers={"Authorization": "Bearer %s" % config.SLACK_BOT_TOKEN})
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("relay: today's posts could not be read: %s", exc)
            return out
        if not data.get("ok"):
            return out
        for m in data.get("messages") or []:
            if m.get("thread_ts") and m.get("thread_ts") != m.get("ts"):
                continue                     # a reply sent to the channel too: not a post of its own
            out.append(str(m.get("text") or ""))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return out


def said_today(channel: str, marker: str) -> bool:
    """Does a post in this channel today start with this watchdog's marker?"""
    return any(t.lstrip(" *_").startswith(marker) for t in today_texts(channel))


# In the boards layout (AGENTS_SLACK_LAYOUT=boards, 2026-11-19) a relayed report is a section of its
# department's board, edited in place, instead of a post of its own: (department, section, title, rank).
BOARD_SECTION = {
    "ftm_digest": ("ops", "relay_ftm_digest", "First to market", 10),
    "prospecting": ("ops", "relay_prospecting", "Phone tagging", 88),
    "dial_kpis": ("sales", "relay_dial_kpis", "Dial numbers", 72),
}


def _boards() -> bool:
    return (getattr(config, "SLACK_LAYOUT", "") or "classic").strip().lower() == "boards"


def to_board(text: str) -> dict | None:
    """Where a relayed report sits on a board, or None for anything else: {"dept", "key", "title", "rank",
    "head", "lines"}. The head is its first line and the lines the rest, in the free cleanup.

    A subscription the digest says is ending stays a line of this section, drawn in its own words near
    the top of the ops board, and is never the line cut when the section is full. That is where it
    shows (2026-11-19) rather than on the board's "Waiting on a setting" list: that list sits last on
    the board, is the first block cut when the board runs long, and keeps a to-do by its words for a
    day and a half, so a countdown ("in 2 days", then "tomorrow") would show yesterday's line beside
    today's. The digest is sent fresh every morning, so this line counts down and goes away the
    morning after a renewal is read."""
    where = BOARD_SECTION.get(classify(text))
    if not where:
        return None
    rows = _lines(_plain(text))
    if not rows:
        return None
    dept, key, title, rank = where
    due = [r for r in rows[1:] if EXPIRY_RX.search(r)][:SECTION_LINES]
    other = [r for r in rows[1:] if not EXPIRY_RX.search(r)]
    lines = other[:SECTION_LINES - len(due)] + due
    return {"dept": dept, "key": key, "title": title, "rank": rank, "head": rows[0], "lines": lines}


def deliver(text: str, source: str = "") -> list[dict]:
    """Route one message and post it: each issue once a business day (per group, or per problem
    shape), everything in the team's voice. Returns what was posted, for the log and the tests."""
    posted: list[dict] = []
    if not config.SLACK_BOT_TOKEN:
        log.warning("relay: no bot token, so nothing is posted (a webhook here could post to itself)")
        return posted
    for p in route(text, source):
        if p["kind"] == "pipeline" and _boards():
            sec = to_board(text)
            if sec:
                from . import board
                board.put(sec["dept"], sec["key"], title=sec["title"], head=sec["head"], lines=sec["lines"],
                          rank=sec["rank"])
                posted.append({"kind": "board", "dept": sec["dept"], "text": sec["head"],
                               "ts": board.flush(sec["dept"])})
                continue
        if p["kind"] == "issues":
            group = p.get("group")
            if group and store.kv_get(_group_key(group)):
                continue
            if not group and not voice.first_today("relay_%s" % (source or "unknown"), p["text"]):
                continue
            if p.get("marker") and said_today(slack_app.channel_for("issues"), p["marker"]):
                store.kv_set(_group_key(group), store.now())   # the watchdog's box said it straight to the channel
                continue
        if p.get("voice") == "plain":
            written = voice.clean(p["text"])
        else:
            written = voice.rewrite(p["text"], p.get("voice") or "update")
        ts = slack_app.post(slack_app.channel_for(p["kind"]), written)
        if ts and p["kind"] == "issues" and p.get("group"):
            store.kv_set(_group_key(p["group"]), store.now())
        posted.append({"kind": p["kind"], "text": written, "ts": ts})
    return posted


def _plain(text: str) -> str:
    """The words without emoji codes, bold marks or stage timings, one line per line."""
    out = []
    for ln in _lines(text):
        ln = SECONDS_RX.sub("", voice.EMOJI_RX.sub("", ln)).replace("*", "").strip()
        if ln:
            out.append(ln)
    return "\n".join(out)


def remember(text: str, source: str = "") -> bool:
    """Keep a first-to-market report (run report, digest, watchdog). Anything else is not kept."""
    what = classify(text)
    if what not in FTM_KINDS:
        return False
    try:
        items = json.loads(store.kv_get(FTM_KEY) or "[]")
    except ValueError:
        items = []
    items.append({"at": store.now(), "day": voice._today(), "what": what, "source": source,
                  "dry": "(DRY RUN)" in (text or ""), "text": _plain(text)[:3000]})
    store.kv_set(FTM_KEY, json.dumps(items[-FTM_KEEP:]))
    return True


def ftm_reports(day: str | None = None) -> list[dict]:
    """The kept first-to-market reports, oldest first; with a day, only that business day's."""
    try:
        items = json.loads(store.kv_get(FTM_KEY) or "[]")
    except ValueError:
        items = []
    items = [i for i in items if isinstance(i, dict)]
    return [i for i in items if i.get("day") == day] if day else items


def when(at: str) -> str:
    """'6:57am', business time."""
    try:
        tz = ZoneInfo(getattr(config, "BUSINESS_TZ", "") or "America/New_York")
        dt = datetime.fromisoformat(str(at)).astimezone(tz)
    except (TypeError, ValueError):
        return str(at or "")[:16]
    return "%d:%02d%s" % (dt.hour % 12 or 12, dt.minute, "am" if dt.hour < 12 else "pm")


def ftm_summary(day: str | None = None) -> str:
    """One paragraph an employee can read the box's day from: today's reports in its own words, or
    when it last reported. A dry run is left out: it wrote nothing."""
    day = day or voice._today()
    real = [i for i in ftm_reports() if not i.get("dry")]
    todays = [i for i in real if i.get("day") == day]
    if not todays:
        last = real[-1] if real else None
        return ("No report from it has reached the team today yet: it runs at 6:30am, reports by about 7am, and "
                "its digest posts at 8am." + (" Its last report came on %s at %s." % (last.get("day"), when(last.get("at")))
                                              if last else ""))
    return "Its reports today: " + " | ".join(
        "%s at %s: %s" % (FTM_LABEL.get(i["what"], i["what"]), when(i.get("at")), " / ".join(str(i.get("text") or "").splitlines()))
        for i in todays)


def handle(text: str, source: str = "") -> list[dict]:
    """The relay's background work for one message. Never raises: a sender already got its "ok"."""
    try:
        remember(text, source)
    except Exception as exc:  # noqa: BLE001  (keeping a copy must never stop the message itself)
        log.warning("relay could not keep a first-to-market report: %s", exc)
    try:
        posted = deliver(text, source)
    except Exception as exc:  # noqa: BLE001
        log.exception("relay failed for a message from %s: %s", source, exc)
        posted = []
    try:
        day = voice._today()
        n = int(store.kv_get("relay:count:%s" % day) or 0)
        store.kv_set("relay:count:%s" % day, str(n + 1))
        store.kv_set(LAST_KEY, json.dumps({"at": store.now(), "source": source, "what": classify(text),
                                           "posted": [p["kind"] for p in posted]}))
    except Exception:  # noqa: BLE001
        pass
    return posted


def health() -> dict:
    """Counts only: the relayed words can name streets, and the health page answers anyone."""
    try:
        last = json.loads(store.kv_get(LAST_KEY) or "{}")
    except ValueError:
        last = {}
    return {"on": bool(config.RELAY_SECRET), "today": int(store.kv_get("relay:count:%s" % voice._today()) or 0),
            "last": {k: last.get(k) for k in ("at", "source", "what", "posted") if k in last}}
