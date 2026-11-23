"""One message per department a day, kept current in place (Ty, 2026-11-19: "way too many to really
digest ... consolidate the updates and streamline those processes").

Each department channel (sales, marketing, ops) gets ONE message a business day. Every stage that has
something to say writes its own section of it, and the message is edited, never posted again: an edit
notifies nobody, so the channel shows one new message a day plus whatever a person must act on now.
The detail behind a line (a deep prospect's whole report, the morning update in full) goes in that
message's thread, where it is there for whoever opens it and pings nobody.

The ops board also carries the setup to-do list: a setting a person has to make (create a tag, name the
closer). It is kept from day to day until the stage that asked stops asking, so a to-do is listed once,
not said again every morning.

The board is drawn by the code from the stages' own words, cleaned the free way (voice.clean), never
rewritten by a model: the numbers are the message, and a section changes many times a day.

State lives in the brain's kv table:
  board:<dept>:<YYYY-MM-DD>  {"channel", "ts", "sections": {key: section}, "sig", "changed"}
  board:setup                {stage id: {"title", "who", "items": [...], "at"}}
Nothing here raises: a Slack outage must not fail the work it reports on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config, store

log = logging.getLogger(__name__)

TITLES = {"sales": "Sales", "marketing": "Marketing", "ops": "Ops"}
MAX_CHARS = 3600           # Slack shows about 4,000 characters before "show more"; the header needs room
LINES_PER_SECTION = 4
ENTRIES_KEPT = 4           # a live stage that acted several times today shows its latest runs
SETUP_KEY = "board:setup"
SETUP_SHOWN = 8
SETUP_KEEP_H = 36          # a to-do no stage has asked for in a day and a half comes off the list
FOOTER = "_To ask about a line, start a message here with one of our names (Atlas, Diggs, Roma or Quinn)._"
BULLET = "• "
_LOCK = threading.RLock()  # the two worker threads and the relay's thread share one board


def _tz() -> ZoneInfo:
    return ZoneInfo(getattr(config, "BUSINESS_TZ", "") or "America/New_York")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(_tz())).astimezone(_tz())


def day(now: datetime | None = None) -> str:
    return _now(now).strftime("%Y-%m-%d")


def clock(now: datetime | None = None) -> str:
    """9:02am, the way the team writes a time."""
    t = _now(now)
    return "%d:%02d%s" % (t.hour % 12 or 12, t.minute, "am" if t.hour < 12 else "pm")


def _date_words(now: datetime | None = None) -> str:
    t = _now(now)
    return "%s %s %d" % (t.strftime("%a"), t.strftime("%b"), t.day)


def _key(dept: str, now: datetime | None = None) -> str:
    return "board:%s:%s" % (dept, day(now))


def _load(key: str) -> dict:
    try:
        d = json.loads(store.kv_get(key) or "{}")
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _save(key: str, d: dict) -> None:
    store.kv_set(key, json.dumps(d, default=str))


def _clean(text) -> str:
    from . import voice
    return voice.clean(str(text or "")).replace("\n", " ").strip()


# ---------------------------------------------------------------------- writing a section
def put(dept: str, key: str, *, title: str, who: str = "", head: str = "", lines=(), entry: str = "",
        rank: int = 500, now: datetime | None = None) -> None:
    """Write one stage's section of today's board for `dept`. head and lines replace what the section
    said before; an `entry` is added to the section's runs today (a live stage that acted) and the latest
    ENTRIES_KEPT are shown. A put with only an entry keeps the section's head and lines. Nothing is
    posted until flush()."""
    with _LOCK:
        k = _key(dept, now)
        d = _load(k)
        secs = d.setdefault("sections", {})
        s = secs.get(key) or {"entries": [], "runs": 0, "head": "", "lines": []}
        s.update({"title": title, "who": who, "rank": int(rank), "at": clock(now)})
        if head or lines or not entry:
            s["head"] = _clean(head)
            s["lines"] = [c for c in (_clean(x) for x in (lines or [])) if c and c != "..."]
        if entry:
            s["entries"] = (s.get("entries") or [])[-(ENTRIES_KEPT - 1):] + ["%s %s" % (clock(now), _clean(entry))]
            s["runs"] = int(s.get("runs") or 0) + 1
        secs[key] = s
        _save(k, d)


def remove(dept: str, key: str, now: datetime | None = None) -> bool:
    with _LOCK:
        k = _key(dept, now)
        d = _load(k)
        if key not in (d.get("sections") or {}):
            return False
        d["sections"].pop(key)
        _save(k, d)
        return True


def set_setup(stage_id: str, title: str, who: str, items: list, now: datetime | None = None) -> bool:
    """One stage run's setup to-dos. Each is kept, across days, until the stage has not asked for it in
    SETUP_KEEP_H: many stages ask only when they have work for it (lead notes asks for its fields only
    when there was a call), so one quiet run does not mean the setting was made. True when the list as
    shown changed, so the caller knows the ops board needs drawing again."""
    from . import channels
    with _LOCK:
        d = _load(SETUP_KEY)
        cur = d.get(stage_id) or {}
        known = dict(cur["items"]) if isinstance(cur.get("items"), dict) else {}
        stamp = _now(now).isoformat(timespec="seconds")
        changed = False
        for x in items or []:
            text = _clean(x)
            if not text:
                continue
            key = channels.cause(text)[0]
            if (known.get(key) or {}).get("text") != text:
                changed = True
            known[key] = {"text": text, "last": stamp}
        cutoff = _now(now) - timedelta(hours=SETUP_KEEP_H)
        for key in list(known):
            try:
                last = datetime.fromisoformat(str(known[key].get("last") or ""))
            except ValueError:
                last = None
            if last is None or last < cutoff:
                known.pop(key)
                changed = True
        if known:
            d[stage_id] = {"title": title, "who": who, "items": known, "at": stamp}
            _save(SETUP_KEY, d)            # saved every run: "last" moves even when the words do not
        elif stage_id in d:
            d.pop(stage_id)
            _save(SETUP_KEY, d)
            changed = True
        return changed


def setup_items() -> dict:
    return _load(SETUP_KEY)


def _setup_texts(s: dict) -> list[str]:
    items = s.get("items") or {}
    if isinstance(items, dict):
        return sorted(str((v or {}).get("text") or "") for v in items.values() if (v or {}).get("text"))
    return [str(x) for x in items]


# ---------------------------------------------------------------------- drawing it
def section_lines(s: dict, lines_per: int = LINES_PER_SECTION) -> list[str]:
    """One section in words: its title, who and when, its head, then its lines (latest runs first)."""
    who = ", ".join(x for x in (s.get("who") or "", s.get("at") or "") if x)
    first = "*%s*" % (s.get("title") or "Update") + (" (%s)" % who if who else "")
    if s.get("head"):
        first += ": " + s["head"]
    out = [first]
    entries = list(s.get("entries") or [])
    body = entries + list(s.get("lines") or [])
    runs = int(s.get("runs") or len(entries))
    if entries and runs > len(entries):
        out.append("%d runs today, the latest %d:" % (runs, len(entries)))
    for ln in body[:lines_per]:
        out.append(BULLET + ln)
    if len(body) > lines_per:
        out.append(BULLET + "and %d more" % (len(body) - lines_per))
    return out


def _setup_block(setup: dict) -> list[str]:
    todo = []
    for sid, s in sorted((setup or {}).items(), key=lambda kv: str(kv[1].get("title") or kv[0])):
        for item in _setup_texts(s):
            todo.append("%s: %s" % (s.get("title") or sid, item))
    if not todo:
        return []
    block = ["*Waiting on a setting* (%d): once it is made, the stage that asked stops asking" % len(todo)]
    block += [BULLET + t for t in todo[:SETUP_SHOWN]]
    if len(todo) > SETUP_SHOWN:
        block.append(BULLET + "and %d more" % (len(todo) - SETUP_SHOWN))
    return block


def render(dept: str, d: dict, setup: dict | None = None, now: datetime | None = None) -> tuple[str, str]:
    """(text, signature), or ("", "") when there is nothing to say. The signature covers what the board
    says and not the header's time, so a board is edited only when its words change."""
    def order(kv):
        r = kv[1].get("rank")
        return (500 if r is None else int(r), kv[0])          # rank 0 is a rank, not a missing one
    secs = sorted((d.get("sections") or {}).items(), key=order)
    blocks = [section_lines(s) for _, s in secs if (s.get("head") or s.get("lines") or s.get("entries"))]
    if dept == "ops":
        sb = _setup_block(setup if setup is not None else setup_items())
        if sb:
            blocks.append(sb)
    if not blocks:
        return "", ""
    body, used, dropped = [], 0, 0
    for b in blocks:
        text = "\n".join(b)
        if used + len(text) + 2 > MAX_CHARS:
            text = b[0]                                   # the section's first line alone
            if used + len(text) + 2 > MAX_CHARS:
                dropped += 1
                continue
        body.append(text)
        used += len(text) + 2
    if dropped:
        body.append("And %d more section%s that did not fit; ask any of us here." % (dropped, "" if dropped == 1 else "s"))
    main = "\n\n".join(body)
    sig = hashlib.sha256(main.encode("utf-8")).hexdigest()[:16]
    changed = d.get("changed") if d.get("sig") == sig and d.get("changed") else clock(now)
    header = "*%s today*, %s (last change %s)" % (TITLES.get(dept, dept.title()), _date_words(now), changed)
    return header + "\n\n" + main + "\n\n" + FOOTER, sig


# ---------------------------------------------------------------------- sending it
def flush(dept: str, now: datetime | None = None) -> str:
    """Post today's board, or edit it when its words changed. Returns what happened in a word:
    posted, updated, unchanged, empty, off (no bot token or channel), failed."""
    from . import slack_app
    with _LOCK:
        k = _key(dept, now)
        d = _load(k)
        text, sig = render(dept, d, now=now)
        if not text:
            return "empty"
        if d.get("ts") and d.get("sig") == sig:
            return "unchanged"
        if not config.SLACK_BOT_TOKEN:
            return "off"
        channel = slack_app.channel_for(dept)
        if not channel:
            return "off"
        if d.get("ts") and d.get("channel") == channel:
            ok, err = slack_app.update(channel, d["ts"], text)
            if ok:
                d.update({"sig": sig, "changed": clock(now) if d.get("sig") != sig else d.get("changed")})
                _save(k, d)
                return "updated"
            if err not in slack_app.GONE:
                log.warning("board %s: could not edit today's message (%s)", dept, err)
                return "failed"
        ts = slack_app.post(channel, text)
        if not ts:
            return "failed"
        d.update({"channel": channel, "ts": ts, "sig": sig, "changed": clock(now)})
        _save(k, d)
        return "posted"


def reply(dept: str, text: str, employee=None, now: datetime | None = None) -> str:
    """The detail behind a line, in today's board's thread (posting the board first if it is not up
    yet). Returns the reply's ts, or "" when it could not be threaded (then nothing is posted, so the
    detail never lands loose in the channel)."""
    from . import slack_app
    if not (text or "").strip():
        return ""
    with _LOCK:
        d = _load(_key(dept, now))
        if not d.get("ts"):
            flush(dept, now)
            d = _load(_key(dept, now))
        if not d.get("ts"):
            return ""
        return slack_app.post(d["channel"], text, thread_ts=d["ts"], employee=employee)


def today(dept: str, now: datetime | None = None) -> dict:
    """Today's board for one department, as stored (for /health, team_query and the tests)."""
    return _load(_key(dept, now))
