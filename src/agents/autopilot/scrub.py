"""The do-not-call registry scrub (build plan 0.22, decision D32).

DataSift checks the numbers it skip traces against the national do-not-call registry and marks
the ones it finds. The catch, met twice already: the mark exists ONLY on a search row, and only
for the one number that row shows. The full record never carries it (the field is absent, not
false). So a check written against a record can never fire, and a number flagged on one record's
row is, on the owner's other record, just a number.

Decision D32 (made 2026-11-13): a number carrying the flag is never texted. Whether it may be
called is a question for an attorney; until that is answered a flagged number is not called
either, which leaves mail.

What this module does about it:

  * It REMEMBERS every flagged number any stage sees on a search row, in the autopilot's own table,
    so the rule holds for that number wherever it turns up again: on another record, on a full
    record that shows no flag, on a later day.
  * It never forgets one by itself. A row that later shows the same number without the flag does
    not clear it, because "no flag" has meant "the field was not sent" before, and the price of a
    wrong clear is a text to a registered number. A person clears a number, with forget().
  * The scrub stage reads a lane's presets ahead of the text and call stages and reports how much
    of the lane the rule removes: the share of shown numbers that are flagged, and, for the flagged
    records it opens, how many are left with nothing to text or nothing to call.
  * split() is the one call a text or call stage makes on a record's numbers.

The flag is honored where it is visible. A record with three numbers shows the registry's answer
for one of them; the other two are unknown until some row shows them. `hold_unseen: true` on the
scrub stage holds those back as well, which is stricter than the decision and costs most of the
lane, so it is off unless switched on.

Measured read-only on 2026-11-12: 257 of 568 numbers shown on first-to-market call-lane rows carried
the flag (45%), and 38 of 100 on Priority 1 Ready to Call. A lane loses about half of the numbers
it shows, which is why the share is reported every day and not assumed.

Numbers are shown by their last four digits, never whole.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config, store
from . import gate, ledger

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_dnc (
    number TEXT PRIMARY KEY, flagged INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT,
    flagged_at TEXT, clear_seen TEXT, record_uuid TEXT, lane TEXT);
CREATE INDEX IF NOT EXISTS ap_dnc_flagged ON ap_dnc(flagged);
"""
_READY: set = set()
EFFECT_FACT = "registry_effect"


def ensure() -> None:
    """Create the table the first time this process touches this database. The table belongs to
    this module, so the module makes it: nothing else has to change for the scrub to work."""
    key = str(config.DB_PATH)
    if key in _READY:
        return
    with store._LOCK, store._db() as c:
        c.executescript(SCHEMA)
    _READY.add(key)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def last4(number) -> str:
    return str(number or "")[-4:]


# ---------------------------------------------------------------------- what a row shows
def shown(row: dict) -> tuple[str, bool | None]:
    """(the number a search row shows, its registry answer). The answer is True (flagged), False
    (the row carries the field and it is off) or None (the row does not say)."""
    ph = (row or {}).get("phone") or {}
    if not isinstance(ph, dict):
        return "", None
    n = gate.digits(ph.get("number"))
    if not n or "doNotCall" not in ph:
        return n, None
    return n, bool(ph.get("doNotCall"))


def _note(c, n: str, flagged: bool, record_uuid: str, lane: str, at: str) -> bool:
    """One answer, on an open connection. True when the number is flagged for the first time."""
    row = c.execute("SELECT flagged FROM ap_dnc WHERE number=?", (n,)).fetchone()
    if row is None:
        c.execute("INSERT INTO ap_dnc (number, flagged, first_seen, last_seen, flagged_at, clear_seen, record_uuid, lane) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (n, 1 if flagged else 0, at, at, at if flagged else "", "" if flagged else at, record_uuid or "", lane or ""))
        return bool(flagged)
    if flagged:
        c.execute("UPDATE ap_dnc SET flagged=1, last_seen=?, flagged_at=CASE WHEN flagged=1 THEN flagged_at ELSE ? END, "
                  "record_uuid=CASE WHEN flagged=1 THEN record_uuid ELSE ? END, lane=CASE WHEN flagged=1 THEN lane ELSE ? END "
                  "WHERE number=?", (at, at, record_uuid or "", lane or "", n))
        return not row["flagged"]
    c.execute("UPDATE ap_dnc SET last_seen=?, clear_seen=? WHERE number=?", (at, at, n))
    return False


def note(number: str, flagged: bool, record_uuid: str = "", lane: str = "", now: datetime | None = None) -> bool:
    """Remember what a row said about a number. Returns True when the number is flagged for the
    first time. A flag is never taken back here (see the module note)."""
    n = gate.digits(number)
    if not n:
        return False
    ensure()
    with store._LOCK, store._db() as c:
        return _note(c, n, bool(flagged), record_uuid, lane, _stamp(now))


def note_row(row: dict, lane: str = "", now: datetime | None = None) -> tuple[str, bool | None, bool]:
    """Remember one search row's answer. Returns (number, answer, newly flagged)."""
    n, answer = shown(row)
    if not n or answer is None:
        return n, answer, False
    return n, answer, note(n, answer, (row or {}).get("uuid") or "", lane, now)


def note_rows(rows, lane: str = "", now: datetime | None = None) -> dict:
    """Remember every answer in a page of search rows, in one transaction. Returns the counts the
    scrub reports: rows, numbers shown, rows that say either way, flagged, and newly flagged."""
    out = {"rows": 0, "shown": 0, "answered": 0, "flagged": 0, "new": 0}
    seen = []
    for row in rows or []:
        out["rows"] += 1
        n, answer = shown(row)
        out["shown"] += bool(n)
        if not n or answer is None:
            continue
        out["answered"] += 1
        out["flagged"] += bool(answer)
        seen.append((n, answer, (row or {}).get("uuid") or ""))
    if not seen:
        return out
    ensure()
    at = _stamp(now)
    with store._LOCK, store._db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            for n, answer, u in seen:
                out["new"] += bool(_note(c, n, answer, u, lane, at))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return out


def status_of(number: str) -> str:
    """flagged, clear (a row showed it with the flag off, and no row ever showed it on) or unseen."""
    n = gate.digits(number)
    if not n:
        return "unseen"
    ensure()
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT flagged, clear_seen FROM ap_dnc WHERE number=?", (n,)).fetchone()
    if row is None:
        return "unseen"
    if row["flagged"]:
        return "flagged"
    return "clear" if row["clear_seen"] else "unseen"


def is_flagged(number: str) -> bool:
    return status_of(number) == "flagged"


def flagged_among(numbers) -> set[str]:
    nums = sorted({gate.digits(n) for n in numbers or []} - {""})
    if not nums:
        return set()
    ensure()
    out: set[str] = set()
    with store._LOCK, store._db() as c:
        for i in range(0, len(nums), 400):
            part = nums[i:i + 400]
            q = "SELECT number FROM ap_dnc WHERE flagged=1 AND number IN (%s)" % ",".join("?" * len(part))
            out |= {r["number"] for r in c.execute(q, part).fetchall()}
    return out


def flagged_since(numbers) -> dict:
    """number -> the moment its flag was first seen (UTC, ISO), for the flagged ones among these.
    The nightly check uses it: a touch is a breach only when the flag was already known."""
    nums = sorted({gate.digits(n) for n in numbers or []} - {""})
    if not nums:
        return {}
    ensure()
    out: dict[str, str] = {}
    with store._LOCK, store._db() as c:
        for i in range(0, len(nums), 400):
            part = nums[i:i + 400]
            q = "SELECT number, flagged_at FROM ap_dnc WHERE flagged=1 AND number IN (%s)" % ",".join("?" * len(part))
            out.update({r["number"]: r["flagged_at"] or "" for r in c.execute(q, part).fetchall()})
    return out


def count() -> int:
    ensure()
    with store._LOCK, store._db() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM ap_dnc WHERE flagged=1").fetchone()["n"])


def forget(number: str, now: datetime | None = None) -> bool:
    """A PERSON clears a number (the registry answer was checked by hand, or the attorney's answer
    changed the rule for it). Nothing in the autopilot calls this on its own."""
    n = gate.digits(number)
    if not n:
        return False
    ensure()
    with store._LOCK, store._db() as c:
        cur = c.execute("UPDATE ap_dnc SET flagged=0, clear_seen=? WHERE number=? AND flagged=1", (_stamp(now), n))
        return bool(cur.rowcount)


# ---------------------------------------------------------------------- the rule, for a stage
@dataclass
class Settings:
    calls: str = "hold"          # hold: a flagged number is not called either, until the attorney answers
    hold_unseen: bool = False    # also hold a number no row has ever shown with the flag off


def settings(policy) -> Settings:
    """The rule as the playbook sets it: the first scrub stage's block, whatever its mode. The rule
    is the decision; the stage is only the daily sweep, so switching the sweep off does not lift it.
    A value that cannot be read means hold: a typo must never open the calls."""
    out = Settings()
    for st in (getattr(policy, "stages", None) or {}).values():
        if getattr(st, "kind", "") != "scrub":
            continue
        calls = str(st.get("calls", "hold")).strip().lower()
        out.calls = calls if calls in ("hold", "allow") else "hold"
        out.hold_unseen = bool(st.get("hold_unseen", False))
        break
    return out


def block_problems(st) -> list[str]:
    """What is wrong with a scrub stage's block, for the playbook checker."""
    out = []
    if str(st.get("calls", "hold")).strip().lower() not in ("hold", "allow"):
        out.append("stage %s: calls must be hold or allow, not %r" % (st.id, st.get("calls")))
    v = st.get("max_records")
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
        out.append("stage %s: max_records must be a whole number" % st.id)
    return out


def split(numbers: list, channel: str, policy=None, row: dict | None = None, lane: str = "",
          now: datetime | None = None) -> tuple[list, list]:
    """(the numbers this channel may use, the ones the registry rule takes out). `numbers` are the
    gate's usable numbers ({"number": ...}); `row` is the search row the record came from, when the
    caller has it, so its answer is remembered before the rule is applied.

    A text never uses a flagged number. A call does not either while the playbook says `calls:
    hold`, which is the default until the attorney answers."""
    if row:
        note_row(row, lane, now)
    cfg = settings(policy)
    if channel not in ("text", "call"):
        return list(numbers or []), []
    if channel == "call" and cfg.calls == "allow":
        return list(numbers or []), []
    bad = flagged_among([n.get("number") for n in numbers or []])
    keep, out = [], []
    for n in numbers or []:
        num = gate.digits(n.get("number"))
        if num in bad or (cfg.hold_unseen and status_of(num) != "clear"):
            out.append(n)
        else:
            keep.append(n)
    return keep, out


# ---------------------------------------------------------------------- the stage
def run_scrub(ctx) -> None:
    """Sweep the stage's presets: remember every flag the rows show, say what share of the lane's
    shown numbers is flagged, and open a bounded number of the flagged records to say how many are
    left with nothing to text or nothing to call. Reads the account, writes only the autopilot's
    own table (knowledge, so it is kept in plan mode too)."""
    from .stages import stage_rows

    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    lane = str(st.get("lane") or "")
    rule = settings(ctx.policy)
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No record in the lane to scrub."
        return
    groups: dict[str, list] = {}
    for title, row in rows:
        groups.setdefault(title, []).append(row)
    by_preset: dict[str, dict] = {}
    total = {"rows": 0, "shown": 0, "answered": 0, "flagged": 0, "new": 0}
    for title, part in groups.items():
        by_preset[title] = note_rows(part, lane, ctx.now)
        for k, v in by_preset[title].items():
            total[k] += v
    flagged_rows = [row for _, row in rows if shown(row)[1]]
    if total["shown"] and not total["answered"]:
        ctx.out.problem("no row in the lane says whether its number is on the registry; the search may have stopped "
                        "sending the flag, so nothing new was learned today")
    for title, agg in by_preset.items():
        if agg["shown"]:
            ctx.out.line("%s: %d of %d shown numbers flagged (%d%%)" % (
                title, agg["flagged"], agg["shown"], round(100.0 * agg["flagged"] / agg["shown"])))

    # What the rule costs the lane, record by record. Only a flagged record can lose anything, and
    # each one is opened once a week at most, a bounded number a run.
    max_reads = int(st.get("max_records", 60))
    recheck = timedelta(days=float(st.get("recheck_days", 7)))
    reads = 0
    effect = {"read": 0, "no_text": 0, "no_call": 0}
    for row in flagged_rows:
        u = row.get("uuid") or ""
        fact = ledger.fact(u, EFFECT_FACT, max_age=recheck) if u else None
        if fact is None:
            if reads >= max_reads:
                ctx.out.tally["flagged records not opened yet (read cap, next run)"] += 1
                continue
            reads += 1
            try:
                rec = ctx.crm.record(u)
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("could not open a flagged record (%s)" % str(exc)[:100])
                continue
            if not rec.get("uuid"):
                continue
            fact = {}
            for channel in ("text", "call"):
                before = gate.usable_numbers(rec, channel, cfg, None)
                after, _ = split(before, channel, ctx.policy, row, lane, ctx.now)
                fact[channel] = [len(before), len(after)]
            ledger.set_fact(u, EFFECT_FACT, fact)      # knowledge about the record, kept in plan mode too
        effect["read"] += 1
        effect["no_text"] += bool(fact.get("text", [0, 0])[0] and not fact.get("text", [0, 0])[1])
        effect["no_call"] += bool(fact.get("call", [0, 0])[0] and not fact.get("call", [0, 0])[1])
    share = round(100.0 * total["flagged"] / total["shown"]) if total["shown"] else 0
    ctx.out.tally["numbers shown on the lane's rows"] = total["shown"]
    ctx.out.tally["flagged by the do-not-call registry"] = total["flagged"]
    ctx.out.tally["flagged numbers remembered, all lanes"] = count()
    if effect["read"]:
        ctx.out.tally["flagged records opened"] = effect["read"]
        ctx.out.tally["of those, left with no number to text"] = effect["no_text"]
        if rule.calls == "hold":
            ctx.out.tally["of those, left with no number to call (mail only)"] = effect["no_call"]
    ctx.out.acted = total["new"]
    store.kv_set("autopilot:scrub:%s" % st.id, json.dumps({
        "day": ledger.day(ctx.now, ctx.policy.tz), "lane": lane, "shown": total["shown"], "flagged": total["flagged"],
        "share": share, "new": total["new"], "no_text": effect["no_text"], "no_call": effect["no_call"],
        "opened": effect["read"]}))
    calls = "held from calls until the attorney answers" if rule.calls == "hold" else "still called"
    lane_words = ("the %s lane" % lane.replace("_", " ")) if lane else "these presets"
    opened = ""
    if effect["read"]:
        opened = " Of %d flagged records opened, %d have nothing left to text%s." % (
            effect["read"], effect["no_text"], (" and %d nothing left to call" % effect["no_call"]) if rule.calls == "hold" else "")
    ctx.out.head = ("%d of %d numbers shown in %s carry the do-not-call registry flag (%d%%), %d new today. "
                    "They are never texted and are %s.%s" % (
                        total["flagged"], total["shown"], lane_words, share, total["new"], calls, opened))


def recent_line(stage_id: str) -> str:
    """The last sweep in one plain line, for the morning digest."""
    raw = store.kv_get("autopilot:scrub:%s" % stage_id)
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except ValueError:
        return ""
    return "Do-not-call registry, %s: %d of %d shown numbers flagged (%d%%)" % (
        str(d.get("day") or "")[5:], d.get("flagged", 0), d.get("shown", 0), d.get("share", 0))
