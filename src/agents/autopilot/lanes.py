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

THE CHANNEL MAP (build plan 4.8). The same table is written for the lanes below first to market
(CHANNEL_MAP): Priority 1 and Strong text first, call beside the texts, mail when the round ends
and go to research at exhaustion; Bulk gets three texts, then the power dialer once a month to
attempt 6, then six months of mail; a door knock is only ever for a sale a week out. A row there
restricts nothing until the playbook switches it on for a lane (`timeline: true`, or a mapping of
overrides), because a lane with no written timeline has always meant "not restricted" and stages
already run that way.

TRANCHES (build plan 4.1). The lanes below first to market hold thousands of records that are
already in the account. Working all of them at once would be tens of thousands of texts and a
scoring bill to match, so a lane is worked in tranches: a fixed set of records drawn once, and the next set is
drawn only when the current one has been scored, texted and dialed. Which records are in a tranche
is the autopilot's own memory (one fact per record), never a tag in the account. A lane may also
carry its own daily caps, so the Strong lane cannot spend the first-to-market lane's budget.
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
CHANNELS = ("screen", "trace", "score", "assign", "text", "call", "mail", "followup_text", "research", "door_knock")
DOOR_KNOCK_DAYS = 7        # a door knock is only for a sale this close (decision D3)

# The channel map (build plan 4.8; decisions D3, D7, D20). Written here, switched on per lane in
# the playbook. Keys beyond the first-to-market set:
#   research_at: "exhaustion"   research waits for the call round to end (a possible death, a
#                               record with no number and returned mail still go at once)
#   call_every_days             the dial cadence when calls are not daily (the power dialer)
#   dial_mode: "power"          the lane is dialed from an export, not one record at a time
#   mail_after_round            mail opens only when the round is over, never on a day count
#   calls_after_round           the hot auction lane keeps calling after the round, by task
#   door_knock_days             how close a sale must be before a door knock
_COLD = {
    "round_ends_at": 5,
    "opening_texts": [0, 1, 2],
    "call_days": [1, 2, 3, 4, 5],
    "mail_first_day": 8,
    "mail_every_days": 30,
    "mail_pieces": 6,                  # six pieces on everything below first to market (D7)
    "mail_at_once": ["no_number"],
    "followup_texts": {"first_day": 8, "last_day": 18, "count": 4},
    "research_at": "exhaustion",
}
CHANNEL_MAP = {
    "hot_auction": dict(DEFAULT_TIMELINES["ftm"], calls_after_round={"every_days": 2}, door_knock_days=DOOR_KNOCK_DAYS),
    "priority1": dict(_COLD),
    "strong": dict(_COLD),
    "bulk": {
        "round_ends_at": 6,                # monthly dials to attempt 6
        "opening_texts": [0, 1, 2],        # three texts first
        "call_days": [3],                  # then the power dialer
        "call_every_days": 30,
        "dial_mode": "power",
        "mail_after_round": True,          # then six months of mail
        "mail_every_days": 30,
        "mail_pieces": 6,
        "mail_at_once": [],
        "followup_texts": {"count": 0},
        "research_at": "exhaustion",
    },
}
# The facts that send a record to research at once on a lane whose research waits for exhaustion.
# Two spellings of each are read, because the stages that ask do not all use the same words.
RESEARCH_AT_ONCE = {"deceased": "a possible death", "possible_death": "a possible death", "no_number": "no number",
                    "return_mail": "returned mail", "returned_mail": "returned mail"}
TRANCHE_KEYS = ("size", "presets", "waiting_presets", "carry_over", "min_days", "check_hours", "scan")
CAP_KEYS = ("daily_usd", "outreach_daily_usd")


@dataclass
class Lanes:
    markers: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_MARKERS.items()})
    timelines: dict = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_TIMELINES)))
    presets: dict = field(default_factory=dict)      # lane -> the presets that hold its working records
    tranches: dict = field(default_factory=dict)     # lane -> {size, presets, waiting_presets, carry_over, min_days ...}
    caps: dict = field(default_factory=dict)         # lane -> {daily_usd, outreach_daily_usd}

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
        _parse_tranche(name, cfg.get("tranche"), out, problems)
        _parse_caps(name, cfg.get("caps"), out, problems)
        tl = cfg.get("timeline")
        if tl is not None:
            written = out.timelines.get(name) or CHANNEL_MAP.get(name)
            if tl is True:
                # `timeline: true` switches on the row the channel map already writes for this lane.
                if not written:
                    problems.append("lanes.%s.timeline: true needs a written timeline, and %s has none; give the mapping" % (name, name))
                    continue
                tl = {}
            if not isinstance(tl, dict):
                problems.append("lanes.%s.timeline must be a mapping (or true, to use the written one)" % name)
                continue
            base = json.loads(json.dumps(written or DEFAULT_TIMELINES["ftm"]))
            base.update(tl)
            if not isinstance(base.get("round_ends_at"), int) or base["round_ends_at"] < 1:
                problems.append("lanes.%s.timeline.round_ends_at must be a whole number of call attempts" % name)
            out.timelines[name] = base
    return out


def _parse_tranche(name: str, block, out: Lanes, problems: list[str]) -> None:
    if block is None:
        return
    if isinstance(block, int) and not isinstance(block, bool):
        block = {"size": block}
    if not isinstance(block, dict):
        problems.append("lanes.%s.tranche must be a size or a mapping (%s)" % (name, ", ".join(TRANCHE_KEYS)))
        return
    bad = [k for k in block if k not in TRANCHE_KEYS]
    if bad:
        problems.append("lanes.%s.tranche has %s (settings are %s)" % (name, bad, ", ".join(TRANCHE_KEYS)))
    size = block.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        problems.append("lanes.%s.tranche.size must be a whole number of records" % name)
        return
    share = block.get("carry_over", 0.05)
    if not isinstance(share, (int, float)) or isinstance(share, bool) or not 0 <= share < 1:
        problems.append("lanes.%s.tranche.carry_over must be a share from 0 up to 1 (0.05 lets 5%% of a tranche wait)" % name)
        share = 0.05
    out.tranches[name] = {"size": size,
                          "presets": [str(x) for x in (block.get("presets") or [])],
                          "waiting_presets": [str(x) for x in (block.get("waiting_presets") or [])],
                          "carry_over": float(share), "min_days": float(block.get("min_days", 7) or 0),
                          "check_hours": float(block.get("check_hours", 12) or 0), "scan": int(block.get("scan", 5000) or 5000)}


def _parse_caps(name: str, block, out: Lanes, problems: list[str]) -> None:
    if block is None:
        return
    if not isinstance(block, dict):
        problems.append("lanes.%s.caps must be a mapping (%s)" % (name, ", ".join(CAP_KEYS)))
        return
    caps = {}
    for k, v in block.items():
        if k not in CAP_KEYS:
            problems.append("lanes.%s.caps has %r (caps are %s)" % (name, k, ", ".join(CAP_KEYS)))
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            problems.append("lanes.%s.caps.%s must be a dollar amount" % (name, k))
        else:
            caps[k] = float(v)
    if caps:
        out.caps[name] = caps


def lane_cap(cfg: Lanes | None, lane: str, kind: str) -> float | None:
    """The lane's own daily cap in dollars (kind: daily_usd for data spend, outreach_daily_usd for
    mail), or None when the playbook sets none for it."""
    v = ((getattr(cfg, "caps", None) or {}).get(lane) or {}).get(kind)
    return None if v is None else float(v)


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
    facts = facts or {}
    if channel == "door_knock":
        # The one rule that holds in every lane, written or not: a door knock is for a sale a week
        # out (facts: days_to_sale). It costs a person an afternoon, so it is never a default touch.
        limit = int((tl or {}).get("door_knock_days", DOOR_KNOCK_DAYS))
        left = facts.get("days_to_sale")
        if left is not None and 0 <= int(left) <= limit:
            return True, ""
        return False, "a door knock is only for a sale inside %d days" % limit
    if not tl:
        return True, ""
    ends = int(tl.get("round_ends_at", 5))
    over = counter >= ends
    if channel == "research":
        done = over or facts.get("exhausted") or facts.get("research_class") == "exhausted"
        if tl.get("research_at") == "exhaustion" and not done:
            # A possible death, a record nobody can dial and returned mail cannot wait for a call
            # round that will never finish.
            for k, why in RESEARCH_AT_ONCE.items():
                if facts.get(k):
                    return True, "researched at once (%s)" % why
            return False, "research starts when the call round is over"
        return True, ""
    if channel in ("screen", "trace", "score", "assign"):
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
            after = tl.get("calls_after_round")
            if after:
                every = int(after.get("every_days", 2)) if isinstance(after, dict) else 2
                return True, "after the round, a call task every %d days until the sale" % every
            return False, "the round ended at %d attempts" % ends
        if weekday >= 5:
            return False, "call days are weekdays"
        first_call = min(tl.get("call_days") or [1])
        if day < first_call:
            if first_call <= 1:
                return False, "calls start the day after the record lands"
            return False, "calls start on day %d, after the opening texts" % first_call
        return True, ""
    if channel == "mail":
        for k in tl.get("mail_at_once") or []:
            if facts.get(k):
                return True, "mailed at once (%s)" % k.replace("_", " ")
        if tl.get("mail_after_round"):
            if over:
                return True, ""
            return False, "mail starts when the call round ends, at %d attempts" % ends
        first = int(tl.get("mail_first_day", 8))
        if over or day >= first:
            return True, ""
        return False, "mail starts on day %d, when the call round ends" % first
    if channel == "followup_text":
        fu = tl.get("followup_texts") or {}
        if "count" in fu and not int(fu.get("count") or 0):
            return False, "this lane has no follow-up texts"
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
    calls = tl.get("call_days") or [1]
    texts = ", ".join(str(x) for x in tl.get("opening_texts") or [])
    at_once = ", ".join(x.replace("_", " ") for x in tl.get("mail_at_once") or []) or "nothing"
    out = ["Day 0: screen, trace, score, assign, text 1."]
    if tl.get("call_every_days"):
        out.append("From day %d: one dial every %d days, from the power dialer; opening texts on days %s." % (
            min(calls), int(tl["call_every_days"]), texts))
    else:
        out.append("Days %s: one full call attempt each weekday; opening texts on days %s." % (
            " to ".join(str(x) for x in (min(calls), max(calls))), texts))
    out.append("The call round ends when the counter reads %d." % tl["round_ends_at"])
    if tl.get("mail_after_round"):
        out.append("Mail starts when the call round ends (at once for: %s)." % at_once)
    else:
        out.append("Day %d: the first mail piece (at once for: %s) and %d follow-up texts through day %d." % (
            tl.get("mail_first_day", 8), at_once, fu.get("count", 0), fu.get("last_day", 0)))
    out.append("Then one piece every %d days, %d in all." % (tl.get("mail_every_days", 30), tl.get("mail_pieces", 0)))
    if tl.get("research_at") == "exhaustion":
        out.append("Research starts when the call round is over (at once for a possible death, no number or returned mail).")
    after = tl.get("calls_after_round")
    if after:
        out.append("After the round, a call task every %d days until the sale." % (
            int(after.get("every_days", 2)) if isinstance(after, dict) else 2))
    if tl.get("door_knock_days"):
        out.append("A door knock only for a sale inside %d days." % int(tl["door_knock_days"]))
    return out


# ---------------------------------------------------------------------- tranches, remembered
def _tranche_key(lane: str) -> str:
    return "autopilot:tranche:%s" % lane


def _tranche_fact(lane: str) -> str:
    return "tranche:%s" % lane


def tranche_state(lane: str) -> dict | None:
    """The lane's current tranche: {"n", "opened", "size", "checked", "waiting"}, or None when the
    lane has never opened one."""
    raw = store.kv_get(_tranche_key(lane))
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("n") else None


def tranche_members(lane: str, n: int | None = None) -> set[str]:
    """The records in tranche `n` of a lane; every record that has ever been in one when n is None
    (a record is drawn once, so the next tranche never repeats the last)."""
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (_tranche_fact(lane),)).fetchall()
    out = set()
    for r in rows:
        if n is None:
            out.add(r["record_uuid"])
            continue
        try:
            if int(json.loads(r["value"])) == int(n):
                out.add(r["record_uuid"])
        except (TypeError, ValueError):
            continue
    return out


def tranche_open(lane: str, uuids: list[str], now: datetime) -> dict:
    """Draw the next tranche: these records, numbered one past the last. One transaction, so a
    restart cannot leave half a tranche behind."""
    prev = tranche_state(lane) or {}
    n = int(prev.get("n") or 0) + 1
    stamp = store.now()
    with store._LOCK, store._db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany("INSERT INTO ap_facts (record_uuid, key, value, at) VALUES (?,?,?,?) "
                          "ON CONFLICT(record_uuid, key) DO UPDATE SET value=excluded.value, at=excluded.at",
                          [(u, _tranche_fact(lane), json.dumps(n), stamp) for u in uuids if u])
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    state = {"n": n, "opened": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "size": len([u for u in uuids if u]),
             "checked": "", "waiting": len([u for u in uuids if u])}
    store.kv_set(_tranche_key(lane), json.dumps(state))
    return state


def tranche_note(lane: str, **changes) -> dict | None:
    """Remember something about the current tranche (when it was last checked, how many of its
    records still wait)."""
    state = tranche_state(lane)
    if state is None:
        return None
    state.update(changes)
    store.kv_set(_tranche_key(lane), json.dumps(state, default=str))
    return state
