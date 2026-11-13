"""Lanes: which part of the business an owner is in, and what each lane does on which day
(build plan 0.13 and 0.20).

Two ideas live here.

ONE OWNER, ONE LANE. A phone, a call log and a mailbox belong to the owner, not to a property, so
an owner with a foreclosure and two rentals must not be texted by three programs at once. The
lanes are ordered; the highest one any of an owner's records sits in wins and the owner's other
records are held. Research is not a lane: it runs beside the lane while the owner is alive.

    lead > heirs > hot_auction > ftm > obituary > priority1 > strong > bulk

ONE TIMELINE PER LANE. Each lane has one written timeline that every stage reads, so no two stages
can disagree about what happens after the call round: when a channel opens is asked here, never
decided inside a stage. The first-to-market timeline (the newest teaching, September and October
2026):

    day 0        screen, trace, score, assign, text 1
    days 1 to 5  one full call attempt each weekday; texts 2 and 3 on days 1 and 2
                 the round ends when the call counter reads 5
    day 8        the first mail piece and four follow-up texts over two weeks
    after        one piece every 30 days

Days are counted in business days from the day the record entered its lane. A record with no
usable number, and a foreclosure (a notice lands a median 34 days before its sale), are mailed at
once instead of on day 8.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .. import store

ORDER = ("lead", "heirs", "hot_auction", "ftm", "obituary", "priority1", "strong", "bulk")

# The statuses that mean "this owner is a lead", exactly as the account stores them (a search needs
# the stored spelling; everything else compares in lower case).
LEAD_STATUS_VALUES = ["new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead", "Ghosting Lead",
                      "follow_up", "lead", "underwrite", "under_contract", "push_deal", "transactions", "refer_lead"]
LEAD_STATUSES = {s.lower() for s in LEAD_STATUS_VALUES}
# Finished for good, as stored: a record in one of these holds no lane.
GONE_STATUS_VALUES = ["sold", "closed", "close_out", "lost_deal", "listed", "dnc", "opt_out", "Dead Lead",
                      "not_interested", "buyer", "buyer_found", "buyer_lost", "Auction Date Passed"]

# How a lane is recognised on a full record: any of these tags or lists (case does not matter).
DEFAULT_MARKERS = {
    "heirs": {"tags": ["Heir Identified"]},
    "hot_auction": {"tags": ["Auction Soon"]},
    "ftm": {"tags": ["FTM"]},
    "obituary": {"tags": ["Obituary"], "lists": ["Obituary"]},
    "priority1": {"tags": ["Priority 1"]},
    "strong": {"tags": ["Priority 2"]},
}

DEFAULT_TIMELINES = {
    "ftm": {
        "round_ends_at": 5,                # the call counter that ends the call round
        "opening_texts": [0, 1, 2],        # business days on which texts 1, 2 and 3 go out
        "call_days": [1, 2, 3, 4, 5],
        "mail_first_day": 8,
        "mail_every_days": 30,
        "mail_pieces": 12,
        "mail_at_once": ["no_number", "foreclosure"],
        "followup_texts": {"first_day": 8, "last_day": 18, "count": 4},
    },
}
CHANNELS = ("screen", "trace", "score", "assign", "text", "call", "mail", "followup_text", "research")


@dataclass
class Lanes:
    markers: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_MARKERS.items()})
    timelines: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_TIMELINES)))
    presets: dict = field(default_factory=dict)      # lane -> the presets that hold its working records

    def timeline(self, lane: str) -> dict | None:
        return self.timelines.get(lane)


def parse(block: dict | None, problems: list[str]) -> Lanes:
    """The playbook's optional `lanes:` block over the defaults above."""
    out = Lanes()
    for name, cfg in (block or {}).items():
        if name not in ORDER:
            problems.append("lanes has %r (lanes are %s)" % (name, ", ".join(ORDER)))
            continue
        if not isinstance(cfg, dict):
            problems.append("lanes.%s must be a mapping" % name)
            continue
        m = {k: [str(x) for x in (cfg.get(k) or [])] for k in ("tags", "lists") if cfg.get(k)}
        if m:
            out.markers[name] = m
        if cfg.get("presets"):
            out.presets[name] = [str(x) for x in cfg["presets"]]
        tl = cfg.get("timeline")
        if tl is not None:
            if not isinstance(tl, dict):
                problems.append("lanes.%s.timeline must be a mapping" % name)
                continue
            base = dict(out.timelines.get(name) or DEFAULT_TIMELINES["ftm"])
            base.update(tl)
            if not isinstance(base.get("round_ends_at"), int) or base["round_ends_at"] < 1:
                problems.append("lanes.%s.timeline.round_ends_at must be a whole number of call attempts" % name)
            out.timelines[name] = base
    return out


def _low(items) -> set[str]:
    out = set()
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.add(str(n).strip().lower())
    return out


def status_of(rec: dict) -> str:
    s = (rec or {}).get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title") or ""
    return str(s or "").strip().lower()


def lane_of(rec: dict, cfg: Lanes | None = None) -> str:
    """The lane ONE record puts its owner in, from its status, tags and lists."""
    cfg = cfg or Lanes()
    if status_of(rec) in LEAD_STATUSES:
        return "lead"
    tags, lists = _low(rec.get("tags")), _low(rec.get("lists"))
    for lane in ORDER[1:]:
        m = cfg.markers.get(lane)
        if not m:
            continue
        if tags & {t.lower() for t in m.get("tags", [])} or lists & {t.lower() for t in m.get("lists", [])}:
            return lane
    return "bulk"


def rank(lane: str) -> int:
    return ORDER.index(lane) if lane in ORDER else len(ORDER)


def top_lane(lanes: list[str]) -> str:
    return min(lanes, key=rank) if lanes else "bulk"


# ---------------------------------------------------------------------- the owner's lane, remembered
def set_owner_lane(owner_uuid: str, lane: str, records: dict) -> None:
    """records: record uuid -> the lane that record alone would be in."""
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_owner_lane (owner_uuid, lane, records, at) VALUES (?,?,?,?) "
                  "ON CONFLICT(owner_uuid) DO UPDATE SET lane=excluded.lane, records=excluded.records, at=excluded.at",
                  (owner_uuid, lane, json.dumps(records), store.now()))


def owner_lane(owner_uuid: str, max_age: timedelta | None = None) -> dict | None:
    if not owner_uuid:
        return None
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT lane, records, at FROM ap_owner_lane WHERE owner_uuid=?", (owner_uuid,)).fetchone()
    if not r:
        return None
    if max_age is not None and datetime.now(timezone.utc) - datetime.fromisoformat(r["at"]) > max_age:
        return None
    return {"lane": r["lane"], "records": json.loads(r["records"] or "{}"), "at": r["at"]}


def replace_all(owners: dict) -> None:
    """Swap in a fresh owner map in one transaction (owner uuid -> {record uuid: lane}), so an owner
    who left every lane does not keep last night's row."""
    stamp = store.now()
    with store._LOCK, store._db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM ap_owner_lane")
            c.executemany("INSERT INTO ap_owner_lane (owner_uuid, lane, records, at) VALUES (?,?,?,?)",
                          [(ou, top_lane(list(recs.values())), json.dumps(recs), stamp) for ou, recs in owners.items() if ou])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def owners_in_two_lanes() -> list[dict]:
    """Owners whose records sit in more than one lane (the nightly check should read zero once the
    preset patch has run)."""
    out = []
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT owner_uuid, lane, records FROM ap_owner_lane").fetchall():
            recs = json.loads(r["records"] or "{}")
            lanes_ = sorted(set(recs.values()), key=rank)
            if len(lanes_) > 1:
                out.append({"owner_uuid": r["owner_uuid"], "lane": r["lane"], "lanes": lanes_, "records": recs})
    return out


# ---------------------------------------------------------------------- the timeline
def business_days(start: date, end: date) -> int:
    """Whole weekdays from start to end: 0 on the day itself, 1 on the next weekday."""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def channel_open(cfg: Lanes, lane: str, channel: str, day: int, counter: int = 0, facts: dict | None = None,
                 weekday: int = 0) -> tuple[bool, str]:
    """May this channel act on a record on business day `day` of its lane, with the call counter at
    `counter`? Returns (yes or no, the reason in plain words). A lane with no timeline answers yes:
    the table restricts only the lanes it describes."""
    tl = cfg.timeline(lane)
    if not tl:
        return True, ""
    facts = facts or {}
    ends = int(tl.get("round_ends_at", 5))
    over = counter >= ends
    if channel in ("screen", "trace", "score", "assign", "research"):
        return True, ""
    if channel == "text":
        days = list(tl.get("opening_texts") or [])
        if over:
            return False, "the call round is over, so the opening texts are closed"
        if day > (max(days) if days else 0):
            return False, "the opening texts end on day %d" % (max(days) if days else 0)
        return True, ""
    if channel == "call":
        if over:
            return False, "the round ended at %d attempts" % ends
        if weekday >= 5:
            return False, "call days are weekdays"
        if day < min(tl.get("call_days") or [1]):
            return False, "calls start the day after the record lands"
        return True, ""
    if channel == "mail":
        for k in tl.get("mail_at_once") or []:
            if facts.get(k):
                return True, "mailed at once (%s)" % k.replace("_", " ")
        first = int(tl.get("mail_first_day", 8))
        if over or day >= first:
            return True, ""
        return False, "mail starts on day %d, when the call round ends" % first
    if channel == "followup_text":
        fu = tl.get("followup_texts") or {}
        if not (over or day >= int(fu.get("first_day", 8))):
            return False, "follow-up texts start after the call round"
        if day > int(fu.get("last_day", 18)) + 5:
            return False, "the follow-up series is over"
        return True, ""
    return True, ""


def describe(cfg: Lanes, lane: str) -> list[str]:
    """The lane's timeline as plain lines, for the digest and the docs."""
    tl = cfg.timeline(lane)
    if not tl:
        return ["%s has no written timeline yet." % lane]
    fu = tl.get("followup_texts") or {}
    return [
        "Day 0: screen, trace, score, assign, text 1.",
        "Days %s: one full call attempt each weekday; opening texts on days %s." % (
            " to ".join(str(x) for x in (min(tl["call_days"]), max(tl["call_days"]))),
            ", ".join(str(x) for x in tl.get("opening_texts") or [])),
        "The call round ends when the counter reads %d." % tl["round_ends_at"],
        "Day %d: the first mail piece (at once for: %s) and %d follow-up texts through day %d." % (
            tl["mail_first_day"], ", ".join(x.replace("_", " ") for x in tl.get("mail_at_once") or []) or "nothing",
            fu.get("count", 0), fu.get("last_day", 0)),
        "Then one piece every %d days, %d in all." % (tl["mail_every_days"], tl["mail_pieces"]),
    ]
