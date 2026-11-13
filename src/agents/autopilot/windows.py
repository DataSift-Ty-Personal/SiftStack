"""Relative date windows in a saved preset, turned into the absolute dates the search accepts.

A saved preset stores windows the way the UI shows them: last_direct_mailed ["36-months", "month"]
or last_updated_date [{"field": "status", "options": ["36-months", "quarter"]}]. The records search
refuses those ("Use YYYY-MM-DD"); the UI resolves them at query time (calculateDateRange in the
app bundle, read 2026-11-11). The second token is a POINT: today, yesterday, week (7 days back),
month, quarter, year, or a date. The first counts back FROM THAT POINT, not from today. So
["36-months", "month"] runs from 37 months ago to one month ago: mailed more than a month ago.
Verified live on ty+2 by count: the 30-day Not Interested window over FTM read 70 of 77, the
absolute form was accepted, and the relative form is refused.

A token this module cannot read is left out of the query and reported, never guessed.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta

UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7}
MONTHS = {"month": 1, "months": 1, "quarter": 3, "quarters": 3, "year": 12, "years": 12}
WINDOW_KEYS = ("last_direct_mailed", "last_updated_date", "last_skip_traced", "last_sms_sent",
               "last_called", "created", "last_activity")


def _months_back(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 - n, 12)
    y += d.year
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def token_date(tok, anchor: date) -> str | None:
    """A relative token ('month', '36-months', '2-weeks', 'today', 'yesterday') counted back from
    `anchor`, as YYYY-MM-DD; dates and None pass through."""
    if tok is None:
        return None
    s = str(tok).strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if s == "today":
        return anchor.isoformat()
    if s == "yesterday":
        return (anchor - timedelta(days=1)).isoformat()
    m = re.fullmatch(r"(?:(\d+)-)?([a-z]+)", s)
    if not m:
        raise ValueError(tok)
    n = int(m.group(1) or 1)
    unit = m.group(2)
    if unit in UNITS:
        return (anchor - timedelta(days=n * UNITS[unit])).isoformat()
    if unit in MONTHS:
        return _months_back(anchor, n * MONTHS[unit]).isoformat()
    raise ValueError(tok)


def _pair(v, today: date) -> list:
    """[from, to]: `to` is a point counted back from today; `from` counts back from `to`."""
    if len(v) != 2:
        raise ValueError(v)
    to = token_date(v[1], today)
    anchor = date.fromisoformat(to) if to else today
    return [token_date(v[0], anchor), to]


def _is_token(x) -> bool:
    """A relative window end ('month', '36-months', 'today') or an absolute date or an open end."""
    if x is None:
        return True
    if not isinstance(x, str):
        return False
    try:
        token_date(x, date(2000, 1, 1))
        return True
    except ValueError:
        return False


def _looks_relative(v) -> bool:
    return (isinstance(v, list) and len(v) == 2 and all(_is_token(x) for x in v)
            and any(isinstance(x, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", x) for x in v))


def resolve(must: dict, now: datetime | date | None = None) -> tuple[dict, list[str]]:
    """(search-ready must, notes). Notes name every window that was converted or dropped.

    A window is recognised by its VALUE (two ends that both read as time tokens), not by a list of
    keys: ty+2's obituary preset carries last_obituary_date, which no key list had, and the search
    400s on it. Two-word lists that are not time tokens (statuses, tags) are never touched. A known
    window key whose tokens cannot be read is dropped and reported."""
    today = (now.date() if isinstance(now, datetime) else now) or date.today()
    m = copy.deepcopy(must or {})
    notes: list[str] = []
    for key in list(m.keys()):
        v = m[key]
        try:
            if isinstance(v, list) and v and isinstance(v[0], dict) and "options" in v[0]:
                if all(_looks_relative(x.get("options")) or key in WINDOW_KEYS for x in v):
                    m[key] = [{**x, "options": _pair(x.get("options") or [], today)} for x in v]
                    notes.append("%s %s converted" % (key, [x.get("field") for x in v]))
            elif _looks_relative(v):
                m[key] = _pair(v, today)
                notes.append("%s %s -> %s" % (key, v, m[key]))
            elif key in WINDOW_KEYS and isinstance(v, list) and len(v) == 2 and not all(_is_token(x) for x in v):
                raise ValueError(v)
        except ValueError as exc:
            m.pop(key)
            notes.append("%s dropped: cannot read window token %s" % (key, exc))
    return m, notes
