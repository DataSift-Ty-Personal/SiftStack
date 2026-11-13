"""Dial mode by list size, and the dialer file (build plan 4.3, decision D36).

A caller can work a few hundred records one at a time. Past a thousand that is months of clicking,
and the teaching moves the list to a power dialer: a file of numbers the dialer works top to
bottom while the caller only talks. The Bulk lane is that size from its first day.

For each preset the stage counts the list and says how to dial it:

    under 500 records     one at a time. Nothing is exported.
    500 to 999            still one at a time (the stage says the list is close).
    1,000 or more         the power dialer, from a file a person loads.

The file is built here, so the rules that protect a homeowner are in it before anybody dials:

  * THE GATE ON EVERY RECORD. Each record is read and passed through the suppression gate for a
    call. A held record contributes no row.
  * ONLY NUMBERS A COLD CALL MAY USE: Dial First and Dial Second, never wrong, dead or
    do-not-call, never a litigator's, never the number the do-not-call registry flagged, and only
    numbers nobody has reached yet (no status, or no answer). A number marked Correct belongs to a
    conversation somebody already had, not to a cold list.
  * AN OWNER APPEARS ONCE, whatever number of records they hold. A phone belongs to the owner.
  * LANDLINES GO LAST in the file, after every mobile (plan 1.14), and a number two sources agree
    on comes before one source's word.
  * A NUMBER RESTS after it has been dialed on five separate days with no answer, until the next
    quarter (the rest window). The call log is what counts the days.
  * A record that went into a file in the last week is not put in the next one.

Full numbers are in the file and nowhere else. Everything this stage prints or posts shows a
number by its last four digits, and the file stays on the box: it is not linked from Slack.

A live run writes the file; a plan run counts what it would hold and writes nothing. The file is
the staging step: nothing is dialed until a person loads it. Because that can be days later,
`recheck()` runs the gate over a file again and drops whoever opted out, sold or died since.

DataSift can also build the full list itself: with `datasift_export: true` the stage keeps a
weekly scheduled export on each preset over the threshold (a documented route), filtered to the
same tiers and statuses. That file comes from DataSift, costs this app no record reads, and has
not been through the gate, so it is the fallback, not the default.
"""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import activity, gate, laneroutes, lanes, ledger, windows
from .stages import Ctx, Summary, _list, addr, agreed_numbers, dial_order, mask, stage_rows

log = logging.getLogger(__name__)

COLUMNS = ["phone", "first_name", "last_name", "street", "city", "state", "zip", "tier", "line_type", "two_sources",
           "record_uuid", "owner_uuid", "preset"]
ACTION = "dial_export"


def dial_mode(count: int, under: int, over: int) -> str:
    """single: one record at a time; near: still one at a time, close to the dialer; power: a file."""
    if count < under:
        return "single"
    return "near" if count < over else "power"


def rested(owner_uuid: str, numbers: list[str], since: datetime, days: int, tz: str) -> set[str]:
    """The numbers dialed on `days` or more separate days since `since`. Their status is still
    unset (or they would not be on a cold list), so nobody answered on any of those days."""
    if days <= 0 or not owner_uuid:
        return set()
    seen: Counter = Counter()
    for _, dialed in activity.dial_days(owner_uuid, since=since, tz=tz).items():
        for n in dialed:
            seen[n] += 1
    return {n for n in numbers if seen[n] >= days}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60] or "list"


def _keep_export(ctx: Ctx, title: str, tiers: set, statuses: set) -> None:
    """Keep a weekly scheduled export on this preset, built by DataSift itself (the documented
    route). The tier tags are sent by their ids: a tag that does not exist would match nothing and
    the export would arrive empty, so a missing tag stops here and is said."""
    st = ctx.stage
    preset = (ctx.crm.presets() or {}).get(title) or {}
    if not preset.get("uuid"):
        return
    tag_ids = [ctx.crm.tag_uuid(t) for t in sorted(tiers)]
    if not all(tag_ids):
        ctx.out.problem("the tier tags (%s) are not all in this account, so DataSift's own export was not set for %s"
                        % (", ".join(sorted(tiers)), title))
        return
    try:
        have = laneroutes.scheduled_export(ctx.crm, preset["uuid"])
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the scheduled export on %s (%s)" % (title, str(exc)[:100]))
        return
    if have and have.get("enabled", True):
        ctx.out.tally["presets with DataSift's own scheduled export already set"] += 1
        return
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    body = laneroutes.export_body(tag_ids, sorted(statuses), "dialer_%s" % _slug(title), today,
                                  str(st.get("recurrence", "weekly")))
    ctx.write("scheduled export", laneroutes.set_scheduled_export, ctx.crm, preset["uuid"], body, bool(have))
    if ctx.live:
        ledger.event(ctx.run_id, st.id, "", "scheduled_export", {"preset": title}, now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["presets given DataSift's own scheduled export" if ctx.live
                  else "presets that would get DataSift's own scheduled export"] += 1


def number_rows(ctx: Ctx, rec: dict, row: dict, title: str, tiers: set, statuses: set, rest_days: int,
                rest_since: datetime) -> tuple[list[dict], str]:
    """(the file rows for one record, why it has none). The gate decides whether the record may be
    called at all and which numbers a call may use; this narrows those to the cold-list rules."""
    v = ctx.verdict(rec, "call", row=row)
    if not v.ok:
        return [], "held back: %s" % v.reason()
    ow = rec.get("owner") or {}
    numbers = [x for x in v.numbers if x["tier"] in tiers and x["status"] in statuses]
    if not numbers:
        return [], "no top-tier number that nobody has reached yet"
    resting = rested(ow.get("uuid") or "", [x["number"] for x in numbers], rest_since, rest_days, ctx.policy.tz)
    if resting:
        ctx.out.tally["numbers resting (dialed on %d separate days, no answer)" % rest_days] += len(resting)
    numbers = [x for x in numbers if x["number"] not in resting]
    if not numbers:
        return [], "every number is resting"
    agreed = agreed_numbers(rec.get("uuid") or "")
    pa = rec.get("address") or {}
    out = []
    for x in dial_order(numbers, agreed):
        out.append({"phone": x["number"], "first_name": " ".join(str(ow.get("first_name") or "").split()),
                    "last_name": " ".join(str(ow.get("last_name") or "").split()),
                    "street": pa.get("street") or "", "city": pa.get("city") or "", "state": pa.get("state") or "",
                    "zip": str(pa.get("postal_code") or pa.get("zip5") or "")[:5], "tier": x["tier"],
                    "line_type": x["type"] or "UNKNOWN", "two_sources": "yes" if x["number"] in agreed else "",
                    "record_uuid": rec.get("uuid") or "", "owner_uuid": ow.get("uuid") or "", "preset": title})
    return out, ""


def write_file(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_dialexport(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    zone = ZoneInfo(ctx.policy.tz)
    under, over = int(st.get("single_dial_under", 500)), int(st.get("power_dialer_over", 1000))
    tiers = set(_list(st.get("tiers"))) or set(cfg.callable_tiers)
    statuses = {str(s).upper() for s in (_list(st.get("statuses")) or ["UNKNOWN", "NO_ANSWER"])}
    rest_days = int(st.get("rest_after_days", 5))
    rest_since = ctx.now - timedelta(days=float(st.get("rest_window_days", 90)))
    max_reads = int(st.get("max_reads", 400))
    tl = lanes_cfg.timeline(str(st.get("lane") or ""))
    ends = int(tl.get("round_ends_at", 5)) if tl else None
    if rest_days > 0:
        try:
            sync = activity.sync(ctx.crm, "calls", now=ctx.now, floor_days=int(st.get("floor_days", 45)),
                                 max_calls=int(st.get("max_log_calls", 400)))
            if not sync["complete"]:
                ctx.out.problem("the call log is still catching up (read through %s), so a number that should be "
                                "resting may be in the file" % (sync["through"] or "nothing yet"))
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read the call log (%s); no number was rested" % str(exc)[:100])
    # What went into a file this week, by record AND by owner: an owner dialed from one record's row
    # must not come back the same week through another record they hold.
    lately = ledger.events(action=ACTION, since=ctx.now - timedelta(days=float(st.get("every_days", 7))))
    recent = {e["record_uuid"] for e in lately} | {e["detail"].get("owner") for e in lately} - {None, ""}
    file_rows: list[dict] = []
    owners: set[str] = set()
    records: list[tuple[str, str]] = []
    per_preset: Counter = Counter()
    reads = 0
    # A lane worked in tranches is dialed a tranche at a time too (plan 4.1): only the current
    # tranche's records go in the file, and the tranche's size, not the whole list's, picks the dial
    # mode. Without it the file would hold the whole backlog the tranches exist to hold back.
    tranche_rows = stage_rows(ctx, cap=int(st.get("max_scan", 5000))) if st.get("tranche") else None
    in_preset = Counter(t for t, _ in tranche_rows) if tranche_rows is not None else None
    for title in st.presets:
        try:
            must = ctx.crm.preset_must(title)
        except KeyError as exc:
            ctx.out.problem("%s (the stage skipped it)" % exc)
            continue
        must, notes = windows.resolve(must, ctx.now)
        for n in notes:
            if "dropped" in n:
                ctx.out.problem("%s: %s" % (title, n))
        count = in_preset.get(title, 0) if in_preset is not None else ctx.crm.count(must)
        ctx.out.found += count
        mode = dial_mode(count, under, over)
        if mode == "single":
            ctx.out.line("%s: %d record(s), dial them one at a time." % (title, count))
            continue
        if mode == "near":
            ctx.out.line("%s: %d record(s), still one at a time (the power dialer takes over at %d)." % (title, count, over))
            continue
        ctx.out.tally["lists for the power dialer"] += 1
        if st.get("datasift_export", False):
            _keep_export(ctx, title, tiers, statuses)
        listed = ([r for t, r in tranche_rows if t == title] if tranche_rows is not None
                  else ctx.crm.rows(must, cap=int(st.get("max_scan", 5000))))
        for row in listed:
            u = row.get("uuid") or ""
            ou = (row.get("owner") or {}).get("uuid") or u
            if not u:
                continue
            if ou in owners:
                ctx.out.tally["another record of an owner already in the file"] += 1
                continue
            if u in recent or ou in recent:
                ctx.out.tally["already in a file this week"] += 1
                continue
            first = gate.check_row(row, "call", cfg=cfg, allow=_list(st.get("gate_allow")))
            if not first.ok:
                ctx.out.tally["held back: %s" % first.reason()] += 1
                continue
            if reads >= max_reads:
                ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
                continue
            reads += 1
            rec = ctx.crm.record(u)
            if ends is not None and int(rec.get("predictivecall_attempts") or 0) >= ends:
                ctx.out.tally["the call round is over"] += 1
                continue
            rows, why = number_rows(ctx, rec, row, title, tiers, statuses, rest_days, rest_since)
            if not rows:
                ctx.out.tally[why] += 1
                continue
            owners.add(ou)
            records.append((u, ou))
            per_preset[title] += len(rows)
            file_rows += rows
            if len(records) <= 3:
                ctx.out.line("%s: %d number(s), %s" % (addr(rec), len(rows), ", ".join(mask(r["phone"]) for r in rows[:3])))
    # Landlines last across the whole file. The sort is stable, so each owner's numbers stay in
    # the order they should be dialed within each half.
    file_rows.sort(key=lambda r: 1 if r["line_type"] == "LANDLINE" else 0)
    ctx.out.acted = len(records)
    for title, n in per_preset.items():
        ctx.out.tally["numbers from %s" % title] = n
    if not file_rows:
        if ctx.out.tally.get("lists for the power dialer"):
            ctx.out.head = "The power dialer lists hold no number to export this week."
        else:
            ctx.out.head = "No list is large enough for the power dialer (it takes over at %d records)." % over
        return
    landlines = sum(1 for r in file_rows if r["line_type"] == "LANDLINE")
    if not ctx.live:
        ctx.out.head = "Would write a dialer file of %d number(s) on %d owner(s), %d landline(s) last. Nothing was written." % (
            len(file_rows), len(records), landlines)
        return
    path = Path(config.DATA_DIR) / "dialexport" / ctx.run_id / ("%s_%s.csv" % (st.id, ctx.now.astimezone(zone).date().isoformat()))
    write_file(path, file_rows)
    for u, ou in records:
        ledger.event(ctx.run_id, st.id, u, ACTION, {"file": path.name, "owner": ou}, now=ctx.now, tz=ctx.policy.tz)
    ctx.out.head = ("Wrote the dialer file: %d number(s) on %d owner(s), %d landline(s) last. It is on the box at %s. "
                    "Load it today; a file left for days should be checked again first." % (
                        len(file_rows), len(records), landlines, path))


def recheck(path, stage_id: str, *, crm=None, pol=None, now: datetime | None = None) -> dict:
    """The gate again, at release (plan 0.1, 0.17): read a dialer file, pass every record in it
    through the gate once more, and write `<name>_checked.csv` without the owners who opted out,
    sold, died or became a lead since the file was built. The original file is left as it was."""
    from . import policy as policy_mod
    path = Path(path)
    pol = pol or policy_mod.load()
    st = pol.stage(stage_id)
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=False)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="dial-check", now=now or datetime.now(timezone.utc), live=False, out=out)
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    verdicts: dict[str, object] = {}
    kept = []
    for r in rows:
        u = r.get("record_uuid") or ""
        if u not in verdicts:
            verdicts[u] = ctx.verdict(crm.record(u), "call")
        v = verdicts[u]
        usable = {x["number"] for x in v.numbers} if v.ok else set()
        if v.ok and r.get("phone") in usable:
            kept.append(r)
        else:
            out.tally["dropped: %s" % (v.reason() if not v.ok else "the number may no longer be called")] += 1
    target = path.with_name(path.stem + "_checked.csv")
    write_file(target, kept)
    return {"file": str(target), "numbers": len(kept), "dropped": len(rows) - len(kept), "why": dict(out.tally)}
