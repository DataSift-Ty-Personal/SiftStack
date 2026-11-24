"""May this number be dialed right now? The one check every call passes before Twilio is asked.

Two kinds of answer:
  * STOP: the call is refused. The legal hours (8am to 9pm where the homeowner is), the house hours
    (9am to 7pm, on the business clock AND the homeowner's own), a phone marked do-not-call, a record the
    gate says nobody may touch (Do Not Market, a litigator, an opt-out, mail only), test mode and a
    number that is not a number.
  * ASK: the caller sees why and confirms. The gate's softer holds (sold, a death on the record, held for
    research, the call round is over, the owner is in another lane), a number marked wrong or dead, a
    number already dialed today (the house limit is one dial per number a day), the registry flag.

A number that CALLED US inside the last DIALER_CALLBACK_GRACE_MINUTES is a callback, not a cold call: the
house hours and the marketing holds (Do Not Market, an opt-out, mail only) become ASK, because the
person just asked us to call. The legal hours and a litigator never move.

The power dialer is stricter: it only queues a record the gate passes outright, and only its callable
numbers (a top tier, or a number a person confirmed), so it never asks anything.

The gate itself is the texting app's copy (sms_agent/gate_rules.py), which tests hold identical to the
brain's. It imports nothing beyond the standard library, which is why this app can carry it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sms_agent import gate_rules

from . import config, phone, store

# State telemarketing laws stricter than the federal floor, applied when the number's area code OR the
# owner's mailing address is in the state (Florida's law presumes a Florida area code is a Floridian).
# The house hours (9am to 7pm) already sit inside every window here; what these add is the per-owner cap,
# Pennsylvania's Sundays, the states whose autodialer definitions can catch a dialer that moves to the
# next number by itself (so the power dialer waits for the caller's click there), and Washington's ban on
# a recorded message left on voicemail. Sources: Fla. Stat. 501.059 and 501.616; 15 Okla. Stat. 775C;
# Md. Com. Law 14-4502; RCW 80.36.390 and 80.36.400; Pennsylvania's 2026 amendments (in force
# 2026-10-18); Tenn. Comp. R. & Regs. 1220-04-11-.02. Not legal advice: counsel keeps this table.
STATE_RULES = {
    "FL": {"hours": (8 * 60, 20 * 60), "max_per_24h": 3, "manual_only": True},
    "OK": {"hours": (8 * 60, 20 * 60), "max_per_24h": 3, "manual_only": True},
    "MD": {"hours": (8 * 60, 20 * 60), "max_per_24h": 3, "manual_only": True},
    "WA": {"hours": (8 * 60, 20 * 60), "no_prerecorded": True},
    "PA": {"hours": (9 * 60, 19 * 60), "no_sunday": True},
    "TN": {"hours": (8 * 60, 21 * 60)},
}

HARD_GATE = ("do_not_market", "litigator", "opted_out", "mail_only", "unreadable")
CALLBACK_SOFTENS = ("do_not_market", "opted_out", "mail_only")
SOFT_GATE = ("sold", "deceased", "in_research", "finished", "exhausted", "owner_lane", "listed", "unknown_status",
             "not_single_family")
SILENT_GATE = ("lead", "no_number", "return_mail", "do_not_mail")   # a lead manager calls leads all day
BAD_STATUSES_HARD = ("DNC", "CORRECT_DNC", "WRONG_DNC")
BAD_STATUSES_SOFT = ("WRONG", "DEAD")
TIER_RANK = {"Dial First": 1, "Dial Second": 2, "Dial Third": 3, "Dial Fourth": 4, "Drop": 9}


@dataclass
class Check:
    stop: list = field(default_factory=list)     # plain sentences; any one refuses the call
    ask: list = field(default_factory=list)      # plain sentences the caller confirms
    record_ok: bool = False                      # may this call be recorded
    callback: bool = False                       # the number called us recently
    zones: list = field(default_factory=list)
    notes: list = field(default_factory=list)    # information only (why a call is not recorded)

    @property
    def ok(self) -> bool:
        return not self.stop


def gate_cfg() -> gate_rules.GateConfig:
    problems: list = []
    return gate_rules.parse(config.S.gate or {}, problems)


def _mins(d: datetime) -> int:
    return d.hour * 60 + d.minute


def _clock(d: datetime) -> str:
    return d.strftime("%I:%M%p").lstrip("0").lower()


def _hours_text(h: tuple) -> str:
    def one(m):
        hh, mm = divmod(int(m), 60)
        return "%d%s%s" % (hh % 12 or 12, (":%02d" % mm) if mm else "", "am" if hh < 12 else "pm")
    return "%s to %s" % (one(h[0]), one(h[1]))


def business_day(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(config.S.business_tz)).date().isoformat()


def day_start_utc(now: datetime | None = None) -> str:
    """Midnight of today on the business clock, in UTC (the daily dial count starts there)."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(config.S.business_tz))
    mid = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return mid.astimezone(timezone.utc).isoformat(timespec="seconds")


def hours(number: str, now: datetime | None = None, window: tuple | None = None,
          business_clock: bool = True) -> tuple[bool, str]:
    """Inside `window` on the business clock (when asked) and in every zone the number may be in."""
    now = now or datetime.now(timezone.utc)
    window = window or config.S.call_hours
    if business_clock:
        here = now.astimezone(ZoneInfo(config.S.business_tz))
        if not window[0] <= _mins(here) < window[1]:
            return False, "it is %s here, outside the calling hours (%s)" % (_clock(here), _hours_text(window))
    zs = phone.zones(number) or [config.S.business_tz]
    for z in zs:
        there = now.astimezone(ZoneInfo(z))
        if not window[0] <= _mins(there) < window[1]:
            where = z.split("/")[-1].replace("_", " ")
            return False, "it is %s where this number may be (%s time), outside %s there" % (
                _clock(there), where, _hours_text(window))
    return True, ""


def states_for(number: str, rec: dict | None = None) -> list[str]:
    """Every state whose law may reach this call: the number's area code and the owner's mailing state."""
    out = []
    for s in (phone.state(number), phone.state_code((((rec or {}).get("owner") or {}).get("address") or {}).get("state") or "")):
        if s and s not in out:
            out.append(s)
    return out


def state_rule(number: str, rec: dict | None = None) -> dict:
    """The strictest of the state rules that reach this call, merged."""
    merged: dict = {"states": []}
    for s in states_for(number, rec):
        r = STATE_RULES.get(s)
        if not r:
            continue
        merged["states"].append(s)
        if "hours" in r:
            lo, hi = merged.get("hours", (0, 24 * 60))
            merged["hours"] = (max(lo, r["hours"][0]), min(hi, r["hours"][1]))
        if "max_per_24h" in r:
            merged["max_per_24h"] = min(merged.get("max_per_24h", 99), r["max_per_24h"])
        for flag in ("manual_only", "no_sunday", "no_prerecorded"):
            if r.get(flag):
                merged[flag] = True
    return merged


def state_stops(number: str, rec: dict | None, now: datetime) -> list[str]:
    """Why a state law stops this call now ([] when nothing does)."""
    rule = state_rule(number, rec)
    out = []
    if not rule["states"]:
        return out
    where = "/".join(rule["states"])
    if "hours" in rule:
        ok, why = hours(number, now, rule["hours"], business_clock=False)
        if not ok:
            out.append("%s (%s law)" % (why, where))
    if rule.get("no_sunday"):
        for z in phone.zones(number) or [config.S.business_tz]:
            if now.astimezone(ZoneInfo(z)).weekday() == 6:
                out.append("it is Sunday where this number is, and %s law forbids Sunday calls" % where)
                break
    owner_uuid = (((rec or {}).get("owner") or {}).get("uuid")) or ""
    cap = rule.get("max_per_24h")
    if cap and owner_uuid:
        since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        n = store.owner_dials_since(owner_uuid, since)
        if n >= cap:
            out.append("this owner was dialed %d times in the last 24 hours, the most %s law allows" % (n, where))
    return out


def recently_called_us(number: str, now: datetime | None = None) -> bool:
    last = store.last_inbound(number)
    if not last:
        return False
    when = store.parse(last.get("created_at"))
    now = now or datetime.now(timezone.utc)
    return bool(when and now - when <= timedelta(minutes=config.S.callback_grace_minutes))


def dials_today(number: str, now: datetime | None = None) -> int:
    return store.dials_since(number, day_start_utc(now))


def may_record(number: str, owner_mail_state: str = "", property_state: str = "") -> tuple[bool, str]:
    """DIALER_RECORD=one_party records only when neither the number's state nor the owner's mailing state
    needs every party's consent. The owner's mailing address is where they live; the property may be a
    rental two states away, so its state counts only when the mailing state is unknown."""
    mode = config.S.record
    if mode == "off":
        return False, "recording is off"
    if mode == "all":
        return True, ""
    states = [s for s in (phone.state(number), phone.state_code(owner_mail_state)) if s]
    if not phone.state_code(owner_mail_state) and property_state:
        states.append(phone.state_code(property_state))
    hit = sorted({s for s in states if s in config.ALL_PARTY_STATES})
    if hit:
        return False, "not recorded: %s needs every party's consent to record" % "/".join(hit)
    if not states:
        return False, "not recorded: the number's state is unknown"
    return True, ""


def _phone_on(rec: dict, number: str) -> dict | None:
    for p in ((rec or {}).get("owner") or {}).get("phones") or []:
        if gate_rules.digits(p.get("number")) == number:
            return p
    return None


def manual(rec: dict | None, number: str, *, row: dict | None = None, now: datetime | None = None,
           busy_owner_by: str = "") -> Check:
    """The check for a call a person chose to make (click-to-dial, a callback, a number typed in)."""
    now = now or datetime.now(timezone.utc)
    number = phone.digits(number)
    out = Check(zones=phone.zones(number))
    if not number or not phone.valid(number):
        out.stop.append("that is not a number that can be dialed")
        return out
    if config.S.calls == "off":
        out.stop.append("calling is switched off on this dialer (DIALER_CALLS=off)")
    elif config.S.calls == "test" and number not in config.S.test_numbers:
        out.stop.append("the dialer is in test mode: only the test numbers can be dialed")
    out.callback = recently_called_us(number, now)

    ok, why = hours(number, now, config.S.legal_hours, business_clock=False)
    if not ok:
        out.stop.append(why.replace("calling hours", "legal calling hours") + " (the law)")
    ok, why = hours(number, now, config.S.call_hours, business_clock=True)
    if not ok:
        (out.ask if out.callback else out.stop).append(why + (" (they called us, so it is your call)" if out.callback else ""))
    out.stop.extend(state_stops(number, rec, now))

    if busy_owner_by:
        out.stop.append("%s is on a call with this owner right now" % busy_owner_by)

    if rec:
        p = _phone_on(rec, number)
        if p is not None:
            st = str(p.get("status") or "UNKNOWN").upper()
            ptags = {t.lower() for t in gate_rules._titles(p.get("tags"))}
            cfg = gate_cfg()
            if st in BAD_STATUSES_HARD or "dnc" in ptags:
                out.stop.append("this number is marked do-not-call (%s)" % st)
            if ptags & {t.lower() for t in cfg.litigator_tags}:
                out.stop.append("this number belongs to a litigator")
            if st in BAD_STATUSES_SOFT:
                out.ask.append("this number is marked %s" % st.lower())
            if cfg.registry_flag and row and gate_rules.registry_flagged(row) == number:
                out.ask.append("this number is on the do-not-call registry")
        verdict = gate_rules.check(rec, "call", cfg=gate_cfg(), row=row)
        for code, detail in verdict.reasons:
            text = gate_rules.words(code, detail)
            if code in HARD_GATE and not (out.callback and code in CALLBACK_SOFTENS):
                out.stop.append(text)
            elif code in HARD_GATE or code in SOFT_GATE:
                out.ask.append(text + (" (they called us)" if code in CALLBACK_SOFTENS else ""))
        summ_mail = ((rec.get("owner") or {}).get("address") or {}).get("state") or ""
        prop_state = (rec.get("address") or {}).get("state") or ""
        out.record_ok, why = may_record(number, summ_mail, prop_state)
    else:
        out.record_ok, why = may_record(number)
    if not out.record_ok and config.S.record != "off":
        out.notes.append(why)

    n = dials_today(number, now)
    if n >= max(1, config.S.numbers_per_day):
        out.ask.append("this number was already dialed %s today" % ("once" if n == 1 else "%d times" % n))
    # Keep the sentences unique and in the order found.
    out.stop = list(dict.fromkeys(out.stop))
    out.ask = list(dict.fromkeys(out.ask))
    return out


def order_numbers(numbers: list[dict]) -> list[dict]:
    """A confirmed number first, then by tier, mobiles before landlines inside a tier."""
    def key(p):
        return (0 if p.get("status") == "CORRECT" else 1, TIER_RANK.get(p.get("tier") or "", 5),
                0 if p.get("type") == "MOBILE" else 1)
    return sorted(numbers, key=key)


def queue(rec: dict, row: dict | None, allow: list, now: datetime | None = None) -> tuple[bool, str, list[dict]]:
    """May the power dialer put this record in front of a caller? (yes or no, why not, the numbers in
    the order they are dialed). Only an outright pass from the gate, and only numbers that are inside the
    hours and not yet dialed today."""
    now = now or datetime.now(timezone.utc)
    verdict = gate_rules.check(rec, "call", cfg=gate_cfg(), row=row, allow=allow)
    if not verdict.ok:
        return False, verdict.reason(), []
    keep, why_not = [], ""
    for p in order_numbers(verdict.numbers):
        n = p["number"]
        if config.S.calls == "test" and n not in config.S.test_numbers:
            why_not = why_not or "test mode: no test number on the record"
            continue
        if dials_today(n, now) >= max(1, config.S.numbers_per_day):
            why_not = why_not or "every number was already dialed today"
            continue
        ok, why = hours(n, now, config.S.legal_hours, business_clock=False)
        if ok:
            ok, why = hours(n, now, config.S.call_hours, business_clock=True)
        if ok:
            stops = state_stops(n, rec, now)
            ok, why = (not stops), (stops[0] if stops else "")
        if not ok:
            why_not = why_not or why
            continue
        p = dict(p, manual_only=bool(state_rule(n, rec).get("manual_only")))
        keep.append(p)
    if not keep:
        return False, why_not or "no callable number", []
    return True, "", keep


def allow_for_preset(must: dict) -> list[str]:
    """The gate rules a preset lifts by asking for them. A preset that selects a status works that status:
    a lead follow-up queue calls leads, a second-chance queue calls Not Interested records. The rules
    about the person (Do Not Market, a litigator, an opt-out, a death, a sale, mail only) are never
    lifted this way."""
    want = set()
    for key in ("any_property_status", "property_status", "status"):
        v = (must or {}).get(key)
        for s in (v if isinstance(v, list) else [v] if v else []):
            want.add(str(s).strip().lower())
    allow = []
    if want & gate_rules.LEAD_STATUSES:
        allow.append("lead")
    if "not_interested" in want:
        allow.append("not_interested")
    if "exhausted" in want:
        allow.append("exhausted")
    if "deep prospecting" in want:
        allow.append("in_research")
    if "listed" in want:
        allow.append("listed")
    if want - gate_rules.LEAD_STATUSES - gate_rules.WORKING_STATUSES - gate_rules.FINISHED_STATUSES - {
            "sold", "deep prospecting", "exhausted", "return mail", "listed", "dnc", "opt_out"}:
        allow.append("unknown_status")
    return allow
