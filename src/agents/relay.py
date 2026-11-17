"""The Slack relay (Ty, 2026-11-15): "migrate the first market pipeline that's in the actual Slack
channel underneath DataSift. Can we make sure all of that is discontinued and migrate it over to
this pipeline?"

Every job that posted to #first-to-market in the DataSift workspace kept its Slack incoming-webhook
code. Only its URL changed, to https://<brain>/hooks/slack/<AGENTS_RELAY_SECRET>/<source>, and the
brain decides where each message belongs and posts it in the team's voice (agents/voice.py):

  the first-to-market digest (8:00, "FTM Nov 15 . 18 new records")   pipeline, every day
  the first-to-market run report (about 6:50)                        issues when a stage failed;
                                                                      otherwise nothing, because the
                                                                      digest says the same in two lines
  the watchdog on GitHub ("FTM pull is red")                         issues, only when nothing else
                                                                      said first-to-market was broken
                                                                      that day (it exists for the
                                                                      silent box)
  the prospecting phone tags (10:30)                                 pipeline when it tagged something
                                                                      new, otherwise nothing
  the dial KPIs (8pm)                                                pipeline
  any other failure (a siren, FAILED, ABORTED)                       issues, once a day per problem
  anything else                                                      the team channel

One first-to-market problem a business day reaches issues: the run report, the digest and the
watchdog describe the same failure three ways. The relay posts only with the bot token, never
through a webhook, so it can never post to itself.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs

from . import config, slack_app, store, voice

log = logging.getLogger(__name__)

FAIL_RX = re.compile(r":rotating_light:|:x:|\bFAILED\b|\bABORTED\b|\[EMPTY\]|\[BLOCKED\]|\bBLOCKED\b|\bEXPIRED\b|"
                     r"\bUNREACHABLE\b|\bis red\b|\bRED\b")
STAGE_BAD_RX = re.compile(r"\[(?:FAILED|EMPTY|BLOCKED|SKIPPED)\]|\bEXPIRED\b|\bUNREACHABLE\b|\bBLOCKED\b")
DIGEST_RX = re.compile(r"^FTM [A-Z][a-z]{2} \d{1,2}\b")
SECONDS_RX = re.compile(r"\s*\(\d+(?:\.\d+)?s\)")
LAST_KEY = "relay:last"


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
    if "FTM pull is red" in head:
        return "ftm_watchdog"
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
        reason = lines[2:]
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
    if what == "ftm_watchdog":
        said = []
        for ln in lines[1:]:
            if ln.lower().startswith("check:"):
                continue                     # the developer's commands, not something the reader does
            said.append(ln.split("::", 1)[1].strip() if "::" in ln else ln)
        return [{"kind": "issues", "text": "First-to-market watchdog: the pull is red. " + " ".join(said),
                 "voice": "issue", "group": "ftm"}]
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


def deliver(text: str, source: str = "") -> list[dict]:
    """Route one message and post it: each issue once a business day (per group, or per problem
    shape), everything in the team's voice. Returns what was posted, for the log and the tests."""
    posted: list[dict] = []
    if not config.SLACK_BOT_TOKEN:
        log.warning("relay: no bot token, so nothing is posted (a webhook here could post to itself)")
        return posted
    for p in route(text, source):
        if p["kind"] == "issues":
            group = p.get("group")
            if group and store.kv_get(_group_key(group)):
                continue
            if not group and not voice.first_today("relay_%s" % (source or "unknown"), p["text"]):
                continue
        written = voice.rewrite(p["text"], p.get("voice") or "update")
        ts = slack_app.post(slack_app.channel_for(p["kind"]), written)
        if ts and p["kind"] == "issues" and p.get("group"):
            store.kv_set(_group_key(p["group"]), store.now())
        posted.append({"kind": p["kind"], "text": written, "ts": ts})
    return posted


def handle(text: str, source: str = "") -> list[dict]:
    """The relay's background work for one message. Never raises: a sender already got its "ok"."""
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
