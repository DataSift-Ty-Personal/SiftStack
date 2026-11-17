"""The closer handoff text (rules C5.21, C8.13, C8.21).

Why it exists. When the lead manager hands a seller to the closer, the course sends one short text
from the closer before the closer's first call: it names the teammate the seller spoke with and says
the call comes in a few minutes from this number, so the seller picks up a number they have never
seen and never has to tell their story twice (C8.21: never make the seller repeat answers at a
handoff). Nothing sent it: the texting app's speed-to-lead text covers New Lead only, and nobody was
told when a lead moved to acquisitions (grepped "closer|handoff text" in src/sms_agent and the lead
stages on 2026-11-15: no hits).

The stage kind is `closertext` because `handoff` is already the base kind that hands a preset's
records to a teammate's workflow (stages.run_handoff).

THE STAGE NEVER SENDS. It finds each move INTO a closer status (`statuses`, default underwrite: the
account's Acquisitions status) in the brain's copy of the account log, and each card moved into a
handoff column when `columns` names one (a card moved to Send to Acquisitions sets no status in this
account, so a status alone can miss the handoff), and for each:
  * gates the record on the text channel with exactly one rule lifted, the lead rule (this is a lead);
    a hard hold (Do Not Market, a litigator, an opt-out, a sold house, a death, mail only) stops the
    text and the ping alike;
  * names the teammate: whoever had the last real conversation with the seller (an answered call of
    `talk_seconds` or more that was not the closer), else `lead_manager`, else the record's assignee;
  * picks the number: a mobile a person marked CORRECT (the one the conversation was on first),
    else the number of that conversation; never one marked wrong, dead or do not call in any form,
    nor one the registry scrub flagged;
  * writes the text, checks the sending hours on both clocks and the house limits (limits.room), and
    stages it as a batch (ap_batches, kind closer_text) for a person to release;
  * pings the closer in the pipeline channel (an alert, once), with the batch number to release.
The release runs the gate, the number checks and the hours again and writes a load file for the
texting side, from the closer's own line (`closer_line`). Nothing in this file talks to a texting
system.

The text is a promise with a time in it ("I'll call you in a few minutes"), so it is good for
`good_for_minutes` after the move and never released later; a move outside the sending hours gets
the ping and no text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, store
from . import activity, budget, gate, lanes, ledger, limits, scrub
from .stages import Ctx, Summary, addr, assignee_of
from .textlane import _DASHES, _LINK, _LIST, _PLAIN, _PLATFORM, _PROMISE, _SHOUT, _SITUATION, _VOICE

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

KIND = "closer_text"                # the ap_batches kind (release.RELEASE routes it here)
MARK = "closertext"                 # ap_facts on a status event: what a live run decided
PLAN_MARK = "closertext_plan"       # kept apart, so a plan run never blocks a live one
LAST = "closertext_last"            # ap_facts on a record: the last handoff text staged for it
DONE = ("staged", "skipped")
SERIES = "handoff"
TOUCH = "closer handoff"
CAP = 306                           # two text segments, at the worst-case names and street
# Gate reasons that stop the ping as well as the text: nobody should be told to call this seller.
HARD = {"unreadable", "do_not_market", "litigator", "opted_out", "sold", "deceased", "mail_only"}
_TEN_DIGITS = re.compile(r"\d{10}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
PLACEHOLDERS = {"first", "closer", "teammate", "addr"}
WORST = {"first": "Christopher", "closer": "Alexandria", "teammate": "Bartholomew", "addr": "12345 Old Sampleton Pike"}

# The copy. It names the teammate (so the seller knows who sent the closer), says the call comes from
# this number in a few minutes, and says they will not start over (C8.21). No question: it is a
# heads-up, and the call is the conversation. No company name, no list, no number, no link.
NAMED = (
    "Hi {first}, this is {closer}. {teammate} filled me in on {addr}, so no need to start over. "
    "I'll call you from this number in a few minutes.",
    "Hi {first}, {closer} here, I work with {teammate}. They caught me up on {addr}, so you won't have to "
    "repeat yourself. I'll give you a call from this number in a few minutes.",
    "Hi {first}, it's {closer}, I work with {teammate} and have your notes on {addr}. I'll call you from "
    "this number in a few minutes, so you won't have to go over it all again.",
)
NO_NAME = (
    "Hi, this is {closer}. {teammate} filled me in on {addr}, so no need to start over. "
    "I'll call you from this number in a few minutes.",
    "Hi, {closer} here, I work with {teammate}. They caught me up on {addr}, so you won't have to "
    "repeat yourself. I'll give you a call from this number in a few minutes.",
    "Hi, it's {closer}, I work with {teammate} and have your notes on {addr}. I'll call you from "
    "this number in a few minutes, so you won't have to go over it all again.",
)

DEFAULTS = {
    "statuses": ["underwrite"],     # the closer statuses, exactly as the account stores them
    # Lead board columns whose card move is a handoff too, as the log stores them (column ids, the same
    # ids lead_clock's column_ids hold). Measured 2026-11-15: a card moved to Send to Acquisitions sets
    # no status in prod-account, so a status-only trigger misses that handoff until a sequence sets one.
    "columns": [],
    "closer": "",                   # the closer's first name: the text is from them
    "closer_line": "",              # the closer's own number: the text goes from it, and so does the call
    "lead_manager": "",             # the teammate named when the call log names nobody
    "talk_seconds": 60,             # an answered call this long is a conversation
    "lookback_days": 30,            # how far back the conversation is looked for
    "good_for_minutes": 60,         # how long after the move the text is still true
    "once_days": 7,                 # one handoff text a record in this many days
    "max_items": 10,
    "max_reads": 40,
    "first_days": 1,
    "text_usd": 0.02,
    "system": "smrtphone",          # the texting system the closer's line is on (the text ledger records it)
    "ping": True,                   # tell the closer in the pipeline channel
    "sync": True,
    "sync_calls": 60,
}


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    out = []
    v = st.get("statuses")
    if v is not None and (not isinstance(v, list) or not v or [x for x in v if not str(x or "").strip()]):
        out.append("stage %s: statuses lists the closer statuses exactly as the account stores them" % st.id)
    v = st.get("columns")
    if v is not None and (not isinstance(v, list) or [x for x in v if not str(x or "").strip()]):
        out.append("stage %s: columns lists lead board column ids, as the log stores them" % st.id)
    line = st.get("closer_line")
    if line and not gate.digits(line):
        out.append("stage %s: closer_line must be the closer's ten-digit number" % st.id)
    for k, lo, hi in (("talk_seconds", 1, 3600), ("lookback_days", 1, 365), ("good_for_minutes", 5, 240),
                      ("once_days", 0, 90), ("max_items", 1, 200), ("max_reads", 1, 2000), ("first_days", 1, 30),
                      ("sync_calls", 1, 2000)):
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            out.append("stage %s: %s must be a whole number from %d to %d" % (st.id, k, lo, hi))
    v = st.get("text_usd")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
        out.append("stage %s: text_usd is what one text costs, in dollars" % st.id)
    return out


# ---------------------------------------------------------------------- the copy
def copy_problems(text: str, *, named: bool) -> list[str]:
    """Every reason one variant must not ship, with the text lane's own word lists."""
    out = []
    used = set(_PLACEHOLDER.findall(text))
    if used - PLACEHOLDERS:
        return ["unknown placeholder %s" % sorted(used - PLACEHOLDERS)]
    if named != ("first" in used):
        out.append("a named variant must greet by first name, and a no-name one must not")
    for k, what in (("closer", "does not say who is texting"), ("teammate", "does not name the teammate"),
                    ("addr", "does not name the street")):
        if k not in used:
            out.append(what)
    if any(d in text for d in _DASHES):
        out.append("an em or en dash")
    if ";" in text:
        out.append("a semicolon")
    if any(ch.isdigit() for ch in text) or "$" in text:
        out.append("a number or a dollar amount in the copy")
    if _LINK.search(text):
        out.append("a link")
    for rx, label in ((_LIST, "names the list"), (_SITUATION, "names the owner's situation"),
                      (_PLATFORM, "a word texting platforms refuse"), (_PROMISE, "promises an ending"),
                      (_VOICE, "does not read like a person"), (_SHOUT, "shouting")):
        m = rx.search(text)
        if m:
            out.append("%s: %s" % (label, m.group(0)))
    stray = sorted({c for c in text if c not in _PLAIN})
    if stray:
        out.append("characters that re-encode the message: %s" % stray)
    if "?" in text:
        out.append("a question (a heads-up asks nothing; the call is the conversation)")
    n = len(text.format(**WORST))
    if n > CAP:
        out.append("%d characters at the worst case (cap %d)" % (n, CAP))
    return out


def audit() -> list[str]:
    out = []
    for named, pool in ((True, NAMED), (False, NO_NAME)):
        for i, text in enumerate(pool, 1):
            out += ["%s variant %d: %s" % ("named" if named else "no-name", i, p) for p in copy_problems(text, named=named)]
    return out


def _street(rec: dict) -> str:
    a = (rec or {}).get("address") or {}
    s = " ".join(str(a.get("street") or "").split()) if isinstance(a, dict) else str(a)
    return s.title() if s.isupper() else s


def _first(name) -> str:
    """A first name as a text may print it: the first word, letters only, never all capitals."""
    parts = str(name or "").split()
    word = re.sub(r"[^A-Za-z'-]", "", parts[0]) if parts else ""
    if not word or len(word.replace("'", "").replace("-", "")) < 2:
        return ""
    return word.title() if word.isupper() or word.islower() else word


def render(record_uuid: str, first: str, closer: str, teammate: str, street: str) -> str:
    """The text for one seller. The variant is chosen by the record, so a re-run writes the same text."""
    pool = NAMED if first else NO_NAME
    i = int(hashlib.sha256(record_uuid.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[i].format(first=first, closer=closer, teammate=teammate, addr=street)


def greeting(owner: dict) -> str:
    """The seller's first name, by the text lane's own rule (never a surname, never a company)."""
    from .textlane import greeting_name
    try:
        return greeting_name(owner)
    except Exception:  # noqa: BLE001  (no name reads fine; the wrong name reads like a list)
        return ""


# ---------------------------------------------------------------------- who, and which number
def teammate_for(ctx: Ctx, rec: dict, owner_uuid: str, st: dict, closer: str) -> tuple[str, str, str]:
    """(the teammate's first name, the number the conversation was on, how it was found). The last
    real conversation names the teammate: an answered call of talk_seconds or more, either way, not
    the closer's. Else the stage's lead_manager, else the record's assignee (unless that is the closer)."""
    since = ctx.now - timedelta(days=float(st["lookback_days"]))
    name, number = "", ""
    for e in activity.events("owner.call.answered", resource=owner_uuid, since=since, until=ctx.now):
        d = e.get("data") or {}
        if int(d.get("duration") or 0) < int(st["talk_seconds"]):
            continue
        who = _first(d.get("caller"))
        if not who or who.lower() == closer.lower():
            continue
        name, number = who, gate.digits(d.get("number"))
    if name:
        return name, number, "call"
    lm = _first(st.get("lead_manager"))
    if lm and lm.lower() != closer.lower():
        return lm, "", "setting"
    a = assignee_of(rec)
    if a:
        try:
            for u in ctx.crm.users() or []:
                if isinstance(u, dict) and str(u.get("uuid") or "") == a:
                    who = _first(u.get("first_name") or u.get("name"))
                    if who and who.lower() != closer.lower():
                        return who, "", "assignee"
        except budget.BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001  (no teammate named: the text is held, the ping still goes)
            pass
    return "", "", ""


def pick_number(rec: dict, usable: list[dict], talked: str) -> dict | None:
    """The number the text goes to. Only a number the gate let through (a mobile, not wrong, dead or do
    not call, not a litigator's, not registry flagged), and of those a number a person marked CORRECT,
    the conversation's own first; failing that, the conversation's number itself."""
    phones = {gate.digits(p.get("number")): p for p in ((rec.get("owner") or {}).get("phones") or [])}
    ok = []
    for n in usable or []:
        p = phones.get(n.get("number")) or {}
        status = str(p.get("status") or n.get("status") or "UNKNOWN").upper()
        if status in gate.BAD_PHONE:
            continue
        ok.append(dict(n, status=status, stored=p.get("number") or n.get("number")))
    confirmed = [n for n in ok if n["status"] == "CORRECT"]
    for n in confirmed:
        if talked and n["number"] == talked:
            return n
    if confirmed:
        return confirmed[0]
    for n in ok:
        if talked and n["number"] == talked:
            return n
    return None


def _row_for(crm, owner_uuid: str, record_uuid: str) -> dict | None:
    """The record's search row: the only place the do-not-call registry flag shows. Every row the
    owner has is remembered on the way (scrub.note_rows), so a flag on another record counts here too."""
    try:
        rows = crm.owner_records(owner_uuid, cap=25) if owner_uuid else []
    except budget.BudgetExhausted:
        raise
    except Exception:  # noqa: BLE001
        return None
    scrub.note_rows(rows)
    return next((r for r in rows if r.get("uuid") == record_uuid), None)


def still_handed(rec: dict, how: str, want: set) -> bool:
    """Is the record still with the closer? A move by status must still read a closer status. A move by
    card carries no status of its own (Send to Acquisitions sets none), so it must still read a lead."""
    status = lanes.status_of(rec)
    return status in want if how == "status" else status in lanes.LEAD_STATUSES


def _mark(subject: str, live: bool) -> dict:
    v = ledger.fact(subject, MARK if live else PLAN_MARK)
    return v if isinstance(v, dict) else {}


def _set_mark(subject: str, live: bool, status: str, now: datetime, **detail) -> None:
    ledger.set_fact(subject, MARK if live else PLAN_MARK, dict(detail, status=status, at=now.isoformat(timespec="seconds")))


def _when(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


def ping_text(street: str, closer: str, teammate: str, batch: int = 0, held: str = "") -> str:
    """The alert for the closer. Never a phone number (a street that looks like one is left out)."""
    where = street if not _TEN_DIGITS.search(street.replace("-", "")) else "A lead"
    who = "%s, call the seller in the next few minutes" % closer if closer else "The closer should call the seller in the next few minutes"
    spoke = " (%s spoke with them)" % teammate if teammate else ""
    text = "%s moved to acquisitions. %s%s." % (where, who, spoke)
    if batch:
        text += " The text saying you will call is batch %d: release it first (release batch %d)." % (batch, batch)
    elif held:
        text += " No handoff text: %s." % held
    return text


# ---------------------------------------------------------------------- the stage
def expire_stale(ctx: Ctx) -> None:
    """A handoff text is released inside its hour or not at all. A staged batch whose every text is past
    the time it was good for is closed here, its owners' places in the text ledger given back."""
    for b in ledger.batches(status="staged", stage=ctx.stage.id):
        full = ledger.batch(b["id"])
        if not full or full["kind"] != KIND:
            continue
        ends = [_when(it.get("send_by")) for it in full["items"]]
        if not ends or any(x is None or ctx.now <= x for x in ends):
            continue
        streets = ", ".join(it.get("street") or "" for it in full["items"][:2])
        ctx.out.problem("batch %d (the handoff text for %s) was not released in time, so nothing was sent" % (b["id"], streets))
        limits.drop_text(b["id"], status="expired")
        ledger.batch_set(b["id"], "expired", {"why": "not released before the text stopped being true"})


def run_closertext(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    out = ctx.out
    live = ctx.live
    tz = ctx.policy.tz
    bad_copy = audit()
    if bad_copy:
        out.problem("the handoff text copy failed its check (%s), so nothing is staged" % bad_copy[0])
        return
    closer = _first(st["closer"])
    line = gate.digits(st["closer_line"])
    if not closer or not line:
        out.problem("the closer is not named (closer and closer_line in the stage block), so no handoff text is staged; "
                    "the closer is still pinged")
    if live:
        expire_stale(ctx)
    cols = {str(c).strip().lower() for c in (st["columns"] or []) if str(c).strip()}
    if st["sync"]:
        for group in ("state", "calls") + (("board",) if cols else ()):
            try:
                activity.sync(ctx.crm, group, now=ctx.now, max_calls=int(st["sync_calls"]))
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                out.problem("could not read the %s log (%s), so a handoff may be missed" % (group, str(exc)[:80]))
    want = {str(s).strip().lower() for s in st["statuses"] if str(s).strip()}
    kv = "autopilot:closertext:%s:%s" % (ctx.stage.id, "live" if live else "plan")
    start = _when(store.kv_get(kv)) or (ctx.now - timedelta(days=float(st["first_days"])))
    moves = []
    for e in activity.events("property.status.updated", since=start - activity.OVERLAP, until=ctx.now):
        d = e.get("data") or {}
        to, frm = str(d.get("to") or "").strip().lower(), str(d.get("from") or "").strip().lower()
        if to in want and frm not in want and e.get("resource"):
            moves.append((e, "status"))
    if cols:
        for e in activity.events("card.moved", since=start - activity.OVERLAP, until=ctx.now):
            d = e.get("data") or {}
            col, frm = str(d.get("column") or "").strip().lower(), str(d.get("column_from") or "").strip().lower()
            if col in cols and frm not in cols and e.get("resource"):
                moves.append((e, "card"))
    newest: dict[str, dict] = {}
    via: dict[str, str] = {}
    for e, how in sorted(moves, key=lambda m: (m[0]["when"], str(m[0]["id"]))):
        older = newest.get(e["resource"])
        if older is not None and _mark("closertext:%s" % older["id"], live).get("status") not in DONE:
            _set_mark("closertext:%s" % older["id"], live, "skipped", ctx.now, why="a later move of the same lead")
        newest[e["resource"]] = e
        via[str(e["id"])] = how
    good_for = timedelta(minutes=float(st["good_for_minutes"]))
    lim = limits.settings(ctx.policy)
    reads = 0
    open_at: list[datetime] = []
    items, would, held = [], [], Counter()
    pings: list[tuple[str, str, str, dict | None, str]] = []      # (street, teammate, record, item, held)
    for u, e in sorted(newest.items(), key=lambda kv_: kv_[1]["when"]):
        subject = "closertext:%s" % e["id"]
        if _mark(subject, live).get("status") in DONE:
            continue

        def skip(why: str) -> None:
            held[why] += 1
            _set_mark(subject, live, "skipped", ctx.now, why=why)

        moved = e["when"]
        send_by = moved + good_for
        if ctx.now > send_by:
            skip("moved more than %d minutes ago, so a text promising a call in a few minutes would not be true"
                 % int(st["good_for_minutes"]))
            continue
        if len(items) + len(would) >= int(st["max_items"]) or reads >= int(st["max_reads"]):
            held["waiting for a later run (cap)"] += 1
            open_at.append(moved)
            continue
        reads += 1
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out.problem("could not read a lead that moved to acquisitions (%s)" % str(exc)[:80])
            open_at.append(moved)
            continue
        if not rec or not rec.get("uuid"):
            skip("the record is gone")
            continue
        how_moved = via.get(str(e["id"]), "status")
        if not still_handed(rec, how_moved, want):
            skip("the lead has moved on from acquisitions")
            continue
        owner = rec.get("owner") or {}
        owner_uuid = str(owner.get("uuid") or "")
        street = _street(rec) or addr(rec)
        if _is_entity(owner):
            skip("a company or trust owns it (people only)")
            continue
        last = ledger.fact(u, LAST)
        last_at = _when(last.get("at")) if isinstance(last, dict) else None
        if last_at and ctx.now - last_at < timedelta(days=float(st["once_days"])):
            skip("a handoff text was already staged for this seller on %s" % last_at.date().isoformat())
            continue
        reads += 1
        row = _row_for(ctx.crm, owner_uuid, u)
        v = ctx.verdict(rec, "text", row=row, allow=("lead",), lane="lead")
        hard = next((gate.TEXT[c] % d if "%s" in gate.TEXT[c] else gate.TEXT[c] for c, d in v.reasons if c in HARD), "")
        if hard:
            # Any hard reason, not only the first: the gate sorts a listed house ahead of mail only.
            out.tally["held back: %s" % hard] += 1
            skip("held back by the gate (%s)" % hard)
            continue
        teammate, talked, how = teammate_for(ctx, rec, owner_uuid, st, closer or "")
        if how:
            out.tally["teammate named from the %s" % {"call": "call log", "setting": "stage setting",
                                                         "assignee": "assignee"}[how]] += 1
        usable = scrub.split(v.numbers, "text", ctx.policy, row, "lead", ctx.now)[0] if v.ok else []
        number = pick_number(rec, usable, talked)
        why = ""
        if not v.ok:
            why = v.reason()
        elif number is None:
            why = "no mobile number a person confirmed"
        elif not teammate:
            why = "nobody on the team is known to have spoken with the seller"
        elif not closer or not line:
            why = "the closer is not named"
        when = max(ctx.now, moved)
        if not why:
            r = limits.room(owner_uuid, "text", when, policy=ctx.policy, lim=lim, tz=tz, number=number["number"],
                            system="", campaign=False)
            if not r.ok:
                why = r.why
        if why:
            skip(why)
            if st["ping"]:
                pings.append((street, teammate, u, None, why))
            continue
        text = render(u, greeting(owner), closer, teammate, street)
        if any(ch in text for ch in _DASHES) or len(text) > CAP or _PLACEHOLDER.search(text):
            skip("the text did not pass its check once the names were in")
            continue
        item = {"record_uuid": u, "owner_uuid": owner_uuid, "street": street, "number": number["number"],
                "stored": number.get("stored") or number["number"], "status": number["status"], "tier": number.get("tier") or "",
                "send_at": limits._iso(when), "send_by": limits._iso(send_by), "moved_at": limits._iso(moved),
                "text": text, "closer": closer, "closer_line": line, "teammate": teammate, "event": e["id"],
                "system": str(st["system"]), "day": limits._day(when, tz), "price": float(st["text_usd"]),
                "talked": bool(talked and talked == number["number"]), "via": how_moved}
        if not live:
            would.append(item)
            _set_mark(subject, live, "staged", ctx.now, record=u)
            if st["ping"]:
                pings.append((street, teammate, u, item, ""))
            continue
        items.append(item)
        _set_mark(subject, live, "staged", ctx.now, record=u)
    batch = 0
    if items:
        usd = round(sum(float(it["price"]) for it in items), 4)
        batch = ledger.batch_create(ctx.stage.id, KIND, items, usd)
        for it in items:
            limits.stage_text(stage=ctx.stage.id, batch_id=batch, owner_uuid=it["owner_uuid"], record_uuid=it["record_uuid"],
                              numbers=[{"number": it["number"], "send_at": it["send_at"], "tier": it["tier"]}],
                              system=it["system"], series=SERIES, step=1, touch=TOUCH, tz=tz, campaign=False,
                              usd_each=float(it["price"]), when=ctx.now)
            ledger.set_fact(it["record_uuid"], LAST, {"at": ctx.now.isoformat(timespec="seconds"), "batch": batch})
            if st["ping"]:
                pings.append((it["street"], it["teammate"], it["record_uuid"], it, ""))
        out.batch = batch
    for street, teammate, _, item, why in pings:
        out.alert(ping_text(street, closer, teammate, batch=batch if (item and live) else 0, held=why))
    store.kv_set(kv, (min(open_at) if open_at else ctx.now).isoformat(timespec="seconds"))
    staged = items if live else would
    out.found = len(staged) + sum(held.values())
    out.acted = len(staged)
    for why, n in held.most_common(4):
        out.tally["no text: %s" % why] += n
    if staged:
        out.head = "%s %d handoff text%s for a lead that moved to acquisitions%s." % (
            "Staged" if live else "Would stage", len(staged), "" if len(staged) == 1 else "s",
            (" as batch %d, for release" % batch) if live else "")
        for it in staged[:8]:
            out.line("%s: from %s, naming %s" % (it["street"], it["closer"], it["teammate"]))
    elif held:
        top = held.most_common(1)[0][0]
        out.head = "No handoff text: %d lead%s moved to acquisitions (%s)." % (
            sum(held.values()), "" if sum(held.values()) == 1 else "s", top)


# ---------------------------------------------------------------------- the release
def load_path(batch_id: int) -> Path:
    return Path(config.DATA_DIR) / "handoff" / ("batch_%d.json" % int(batch_id))


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, now: datetime | None = None,
            uploaded: bool = False) -> dict:
    """A person releases a staged handoff text. Every item is checked again first: still inside the
    hour it is true for, still in a closer status, the gate on the text channel (the lead rule lifted,
    nothing else), the number still a usable mobile that is not wrong, dead or do not call and not
    registry flagged, and the sending hours and house limits. What passes is written to a load file for
    the texting side and counted in the text ledger. Dry unless commit. It hands the load over; it
    does not send."""
    from . import policy as policy_mod
    b = ledger.batch(int(batch_id))
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["kind"] != KIND:
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    pol = pol or policy_mod.load()
    st_obj = pol.stage(b["stage"])
    st = settings(st_obj)
    now = now or datetime.now(timezone.utc)
    tz = pol.tz
    if crm is None:
        from .crm import CRM
        crm = CRM()
    want = {str(s).strip().lower() for s in st["statuses"] if str(s).strip()}
    lim = limits.settings(pol)
    out = Summary(stage=st_obj.id, kind=st_obj.kind, owner=st_obj.owner, mode=st_obj.mode, live=commit)
    ctx = Ctx(policy=pol, stage=st_obj, crm=crm, run_id="release-%d" % int(batch_id), now=now, live=commit, out=out)
    kept, held, why_count = [], [], Counter()

    def hold(it: dict, why: str) -> None:
        why_count[why] += 1
        held.append({"record": it["record_uuid"][:8], "street": it.get("street") or "", "why": why})

    for it in b["items"]:
        if now > (_when(it.get("send_by")) or now):
            hold(it, "too late: the promise of a call in a few minutes is no longer true")
            continue
        try:
            rec = crm.record(it["record_uuid"])
        except Exception as exc:  # noqa: BLE001
            hold(it, "the record could not be read (%s)" % str(exc)[:60])
            continue
        if not still_handed(rec, it.get("via") or "status", want):
            hold(it, "the lead is no longer with acquisitions")
            continue
        row = _row_for(crm, it["owner_uuid"], it["record_uuid"])
        v = ctx.verdict(rec, "text", row=row, allow=("lead",), lane="lead")
        if not v.ok:
            hold(it, v.reason())
            continue
        usable = {n["number"] for n in scrub.split(v.numbers, "text", pol, row, "lead", now)[0]}
        phone = next((p for p in ((rec.get("owner") or {}).get("phones") or [])
                      if gate.digits(p.get("number")) == it["number"]), None)
        if it["number"] not in usable or phone is None or str(phone.get("status") or "").upper() in gate.BAD_PHONE:
            hold(it, "the number is no longer one a text may use")
            continue
        when = max(_when(it.get("send_at")) or now, now)
        r = limits.room(it["owner_uuid"], "text", when, policy=pol, lim=lim, tz=tz, number=it["number"], system="",
                        campaign=False, exclude_batch=int(batch_id))
        if not r.ok:
            hold(it, r.why)
            continue
        kept.append(dict(it, send_at=limits._iso(when)))
    if not commit:
        return {"batch": int(batch_id), "dry": True, "sent": len(kept), "of": len(b["items"]), "would_hold": dict(why_count)}

    # The load file first: if it cannot be written, nothing has been counted and the batch is still staged.
    path = ""
    if kept:
        load = {"batch": int(batch_id), "stage": b["stage"], "kind": KIND, "series": SERIES, "released": limits._iso(now),
                "from": kept[0]["closer_line"], "system": kept[0]["system"],
                "contacts": [{"phone": it["number"], "record": it["record_uuid"], "owner": it["owner_uuid"],
                              "text": it["text"], "send_at": it["send_at"], "send_by": it["send_by"],
                              "closer": it["closer"], "teammate": it["teammate"], "street": it["street"]} for it in kept],
                "held": held}
        p = load_path(batch_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(load, indent=1, default=str), encoding="utf-8")
        path = str(p)
    for it in kept:
        limits.release_text(int(batch_id), it["record_uuid"], [it["number"]])
        ledger.event(ctx.run_id, st_obj.id, it["record_uuid"], "text",
                     {"series": SERIES, "step": 1, "numbers": 1, "system": it["system"], "est_usd": it["price"]},
                     now=now, tz=tz)
    limits.drop_text(int(batch_id))          # whatever is still staged in this batch was held back
    ledger.batch_set(int(batch_id), "sent" if kept else "held",
                     {"owners": len(kept), "held": dict(why_count), "load": path})
    if kept:
        said = ("Handed over, not sent: the texting side sends it from %s's line. The text: \"%s\"" % (
            kept[0]["closer"], kept[0]["text"]))
    else:
        said = "Nothing passed the checks, so nothing was handed over."
    return {"batch": int(batch_id), "run": ctx.run_id, "sent": len(kept), "of": len(b["items"]), "held": dict(why_count),
            "load": path, "file": path, "problems": out.problems, "next": said}
