"""The daily touch audit (build plan 0.23): was every touch allowed?

"No wrong touch" is only a promise until something counts the wrong ones. Each morning this stage
takes yesterday's touches (every outbound dial in the call log, every mail piece in the autopilot's
own ledger) and asks the gate about each one. A touch that the gate would refuse is named.

The hard part is time. A record that reads Not Interested this morning may have been perfectly
callable when it was dialed yesterday: the call is WHY it is Not Interested. So a touch is a
violation only when the thing that forbids it is older than the touch. The log gives the moment a
status or a tag landed; where the log has no such moment (the condition is older than the log
reaches, or it is a flag the log does not record), the condition is taken as older than the touch.

What it checks for a dial:
  * the owner had no record a caller could work: every record of theirs was held by the gate before
    the dial (Do Not Market, an opt-out, a litigator, sold, a finished status, a death);
  * the number itself was already marked wrong, dead or do-not-call;
  * the dial was outside calling hours;
  * the same number was dialed three or more times in one day;
  * one caller ID line carried more dials in a day than a line should (plan 4.6: 25). A line that
    works past its cap is the line the carriers flag as spam, and a flagged line is answered by
    nobody, which reads in the day's numbers as many dials and almost no conversations.

Texts are not in DataSift's log (Smarter Contact and the texting app keep their own), so this run
covers calls and mail; the texting ledgers join when their app runs the same gate.

It reads only. It speaks only when it finds something.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import activity, gate, ledger
from .stages import Ctx, addr

log = logging.getLogger(__name__)

# What makes a dial wrong. A LEAD is not on the list: the lead manager calls leads on purpose, and
# the log cannot tell her follow-up from a cold dial. A NOT INTERESTED owner is re-called on purpose
# too, but only once the second-chance wait is over (see _too_soon).
HARD = ("do_not_market", "litigator", "opted_out", "sold", "deceased", "finished")


def _minutes(text: str) -> tuple[int, int]:
    a, b = str(text).split("-")
    ha, ma = (int(x) for x in a.split(":"))
    hb, mb = (int(x) for x in b.split(":"))
    return ha * 60 + ma, hb * 60 + mb


def condition_since(ctx: Ctx, rec: dict, code: str, detail: str, cfg: gate.GateConfig) -> datetime | None:
    """When the condition behind a gate reason began on this record, from the log. None means the
    log does not show it starting, so it is older than the log reaches."""
    u = rec.get("uuid") or ""
    if code in ("finished", "lead", "sold") or (code == "opted_out" and detail.startswith("status")):
        want = detail.replace("status ", "") if detail else ("sold" if code == "sold" else "")
        hits = [e for e in activity.events("property.status.updated", resource=u)
                if not want or str(e["data"].get("to") or "").lower() == want]
        if hits:
            return hits[-1]["when"]
    names = {"do_not_market": cfg.do_not_market_tags, "litigator": cfg.litigator_tags, "sold": cfg.sold_tags,
             "deceased": cfg.deceased_tags}.get(code)
    if names:
        low = {t.lower() for t in names}
        hits = [e for e in activity.events("property.tags.added", resource=u)
                if low & {str(t).lower() for t in e["data"].get("tags") or []}]
        if hits:
            return hits[0]["when"]
    return None


def run_audit(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    zone = ZoneInfo(ctx.policy.tz)
    for group in ("calls", "state"):
        sync = activity.sync(ctx.crm, group, now=ctx.now, floor_days=int(st.get("floor_days", 45)),
                             max_calls=int(st.get("max_log_calls", 400)))
        if not sync["complete"]:
            ctx.out.problem("the %s log is still catching up (read through %s); this audit may be short" % (
                group, sync["through"] or "nothing yet"))
    # Yesterday, business-local, unless the playbook asks for more days.
    days = int(st.get("days", 1))
    end_local = ctx.now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=days)
    hours = _minutes(str(st.get("call_hours") or "09:00-19:00"))
    dials = [e for e in activity.events(activity.DIAL, since=start_local, until=end_local)
             if e["data"].get("direction") != "inbound"]
    by_owner: dict[str, list] = defaultdict(list)
    for e in dials:
        by_owner[e["resource"]].append(e)
    ctx.out.found = len(dials)
    findings: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)

    # --- cheap checks straight off the log
    per_number_day: Counter = Counter()
    for e in dials:
        local = e["when"].astimezone(zone)
        m = local.hour * 60 + local.minute
        if not hours[0] <= m < hours[1]:
            findings["dialed outside calling hours"] += 1
            examples["dialed outside calling hours"].append("%s at %s" % (e["data"].get("caller") or "a caller", local.strftime("%I:%M%p").lstrip("0").lower()))
        per_number_day[(e["data"].get("number"), local.date().isoformat())] += 1
    for (num, _day), n in per_number_day.items():
        if num and n >= int(st.get("repeat_dials", 3)):
            findings["the same number dialed %d or more times in a day" % int(st.get("repeat_dials", 3))] += 1
    # dials per caller ID line per day (the line is the number the owner sees ringing)
    line_cap = int(st.get("line_cap", 25))
    per_line_day: Counter = Counter()
    for e in dials:
        line = e["data"].get("line") or ""
        if line:
            per_line_day[(line, e["when"].astimezone(zone).date().isoformat())] += 1
    if per_line_day:
        lines = {ln for ln, _ in per_line_day}
        ctx.out.tally["caller ID lines used"] = len(lines)
        ctx.out.tally["most dials on one line in a day"] = max(per_line_day.values())
        over = [(ln, n) for (ln, _d), n in per_line_day.items() if n > line_cap]
        if over:
            label = "a caller ID line carried more than %d dials in a day" % line_cap
            findings[label] += len(over)
            worst_line, worst_n = max(over, key=lambda x: x[1])
            examples[label].append("the line ending %s made %d" % (worst_line[-4:], worst_n))
    # numbers already marked bad before the dial
    for ou, evs in by_owner.items():
        marks = [x for x in activity.events("owner.phone.status.updated", resource=ou)
                 if str(x["data"].get("to") or "").upper() in gate.BAD_PHONE]
        for e in evs:
            for mk in marks:
                if mk["data"].get("number") == e["data"].get("number") and mk["when"] < e["when"]:
                    findings["dialed a number already marked wrong, dead or do-not-call"] += 1
                    break

    # --- the gate, per owner (one search and a few record reads each, so capped)
    max_owners = int(st.get("max_owners", 150))
    second_chance = int(st.get("second_chance_days", 30))
    checked = 0
    for ou, evs in list(by_owner.items()):
        if checked >= max_owners:
            ctx.out.tally["owners not checked against the gate (cap %d)" % max_owners] += 1
            continue
        checked += 1
        first_dial = min(e["when"] for e in evs)
        try:
            rows = ctx.crm.owner_records(ou, cap=25)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a dialed owner's records (%s)" % str(exc)[:100])
            continue
        if not rows:
            continue
        open_record = False
        worst = None
        for r in rows[: int(st.get("max_records_per_owner", 6))]:
            rec = ctx.crm.record(r["uuid"])
            v = gate.check(rec, "call", cfg=cfg, allow=("mail_only", "exhausted", "in_research", "unknown_status", "listed"),
                           lanes_cfg=getattr(ctx.policy, "lanes", None))
            hard = [(c, d) for c, d in v.reasons if c in HARD]
            old = []
            for c, d in hard:
                began = condition_since(ctx, rec, c, d, cfg)
                if c == "finished" and d == "not_interested":
                    # The second-chance lane calls a no again after its wait. A dial sooner than that
                    # is the violation; a no older than the log reaches has waited long enough.
                    if began is not None and began < first_dial and (first_dial - began) < timedelta(days=second_chance):
                        old.append((c, "not_interested, said %d day(s) before this call" % (first_dial - began).days, rec))
                    continue
                if began is None or began < first_dial:
                    old.append((c, d, rec))
            if not old:
                open_record = True
                break
            worst = worst or old[0]
        if not open_record and worst:
            code, detail, rec = worst
            label = "dialed an owner the gate holds: %s" % (gate.TEXT[code] % detail if "%s" in gate.TEXT[code] else gate.TEXT[code])
            findings[label] += 1
            examples[label].append(addr(rec))

    # --- mail pieces the autopilot itself sent
    mailed = [e for e in ledger.events(action="mail", since=start_local.astimezone(timezone.utc))
              if datetime.fromisoformat(e["at"]) < end_local]
    for e in mailed[: int(st.get("max_mail", 200))]:
        rec = ctx.crm.record(e["record_uuid"])
        v = gate.check(rec, "mail", cfg=cfg, lanes_cfg=getattr(ctx.policy, "lanes", None))
        when = datetime.fromisoformat(e["at"])
        for c, d in v.reasons:
            began = condition_since(ctx, rec, c, d, cfg)
            if c in HARD + ("do_not_mail", "return_mail") and (began is None or began < when):
                findings["mailed a record the gate holds: %s" % (gate.TEXT[c] % d if "%s" in gate.TEXT[c] else gate.TEXT[c])] += 1
                examples["mailed a record the gate holds"].append(addr(rec))
                break

    total = sum(findings.values())
    ctx.out.acted = total
    ctx.out.tally.update(findings)
    for label, n in findings.most_common(8):
        ex = examples.get(label) or []
        ctx.out.problem("%d: %s%s" % (n, label, (" (for example %s)" % ", ".join(ex[:2])) if ex else ""))
    span = "yesterday" if days == 1 else "the last %d days" % days
    # The dialer names the caller on the answered event, not on the dial itself.
    callers = Counter(e["data"].get("caller") for e in activity.events("owner.call.answered", since=start_local, until=end_local)
                      if e["data"].get("caller") and e["data"].get("direction") != "inbound")
    if not dials and not mailed:
        ctx.out.head = "No dials or mail pieces %s to audit." % span
    else:
        ctx.out.head = "%s: %d dial(s) to %d owner(s)%s and %d mail piece(s). %s" % (
            span.capitalize(), len(dials), len(by_owner),
            (" (answered calls: " + ", ".join("%s %d" % kv for kv in callers.most_common(4)) + ")") if callers else "", len(mailed),
            ("%d touch(es) should not have happened." % total) if total else "Every one passes the gate.")
