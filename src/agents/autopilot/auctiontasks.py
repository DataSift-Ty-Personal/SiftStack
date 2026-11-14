"""The hot auction lane, the part a tag cannot do (build plan 1.8; decisions D9, D34, D39).

The auction stage keeps two tags true: Auction Soon inside 45 days of a sale and Auction Critical
inside 14. A tag starts nothing by itself. This stage does the work the tags stand for.

  1. A CALL TASK EVERY TWO DAYS. A hot record gets the normal five-day call round first. When
     that round is over (the counter reads 5) the call lane has nothing left for it, and it is
     exactly then that the owner has the least time left. So from that day the caller who holds
     the record gets a call task every two days until the sale. A task still open is not stacked
     under a second one; it is named, because an overdue call on a sale a week out is the thing
     to see.

  2. THE LAST TWO WEEKS. Inside the critical tier a record whose buy box and equity are confirmed
     is listed for a priority package (decision D34), including one whose only numbers cannot be
     called. Inside the last week it is listed for a door knock, and only then: a door knock is
     never a default touch. Both are lists, and tasks for a named person. Nothing is mailed here.

  3. THE DAY AFTER THE SALE the record takes the status Auction Date Passed, which the gate reads
     as finished on every channel, and any call task this stage left open is closed, so nobody
     dials an owner about a sale that has happened (decision D39).

  4. ONE DEED CHECK, 45 DAYS LATER. More than half of the borrowers on a foreclosure notice
     still own the house after their sale date: the sale was postponed, cured or cancelled. So
     each closed-out record is looked at once more. First the sold guard's own rule on the
     record's last sale (a sale on or after the sale date means it sold). Then the county tax
     roll, by street, because DataSift's last sale runs weeks behind the county's own record.
     Only when the county still shows the borrower as owner does the record come back into the
     lane, tagged so the caller knows why. A new owner on the roll, or a sale on the record, and
     it stays closed. A republished notice gives a record a new sale date and is handled by the
     auction stage as a new sale.

The borrower's name is remembered while the record is hot, because by day 45 the owner on the
record may already be the buyer (a SiftMap add replaces owners), and comparing the county's owner
with THAT name would read every sold house as still owned.

The county lookup is a small client for the two Trustee sites (Knox and Blount run the same
vendor), with the street written the way the county writes it and a result kept only when its
own address is the same house. It follows tax_enricher's lookup and is not imported from it: that
module pulls in the notice scraper's browser, which the agents image does not carry. Other
counties have no roll wired here and are listed for a person. The client has never been run
against the live sites from this module (the build ran with no network), so the first live deed
checks are the ones to watch.

Every write goes through ctx.write. In plan mode the stage lists the tasks, the close-outs and
the deed checks it would make, and a "still owned" answer is not remembered, so the live run that
follows still brings the record back.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import laneroutes, lanes, ledger
from .sources import _norm_street, same_street
from .stages import (GONE_STATUSES, Ctx, _as_date, _custom_dates, _list, addr, agreed_numbers, assignee_of, dial_order,
                     mask, resolve_callers, round_ends_at, status_of)

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

# Tests swap this for a fake. In production it is county_owner below.
lookup_owner = None
COUNTY_ROLLS = {"knox": "https://knox-tn.mygovonline.com/api/v2", "blount": "https://blount-tn.mygovonline.com/api/v2"}
ROLL_TIMEOUT_S = 15
ROLL_AGENT = "county-roll-check/1.0"      # a plain client name: a public tax roll, one polite request at a time
SUFFIX_WORDS = {"ST", "AVE", "RD", "DR", "LN", "CT", "CIR", "BLVD", "PL", "TER", "PIKE", "HWY", "PKWY", "WAY", "TRL"}
UNIT_WORDS = {"APT", "UNIT", "STE", "SUITE", "LOT"}
FINAL = ("sold", "new_owner", "still_owned", "held", "unresolved")
WORKING = ("", "prospecting", "exhausted")
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}
ORG_WORDS = {"LLC", "INC", "CORP", "CO", "COMPANY", "TRUST", "TRUSTEE", "THE", "OF", "AND", "LP", "LLP", "LTD"}
NOT_LETTERS = re.compile(r"[^A-Za-z\s\-]")


# ---------------------------------------------------------------------- the county roll
def street_queries(street: str) -> list[str]:
    """The street line as written, then the way the county writes it. The county's search is
    literal about the suffix ("100 Example Street" finds nothing, "100 EXAMPLE ST" finds the
    parcel), so the suffix and any direction are abbreviated, then the suffix is dropped, then the
    house number is tried last, which is how part of the Blount roll prints it."""
    line = (street or "").split(",")[0].strip()
    toks = _norm_street(line).split()
    for i, t in enumerate(toks):
        if t in UNIT_WORDS and i >= 2:
            toks = toks[:i]              # an apartment or unit is not part of the parcel's street
            break
    out = [line]
    if len(toks) >= 2 and toks[0].isdigit():
        out.append(" ".join(toks))
        if toks[-1] in SUFFIX_WORDS and len(toks) >= 3:
            out.append(" ".join(toks[:-1]))
        out.append(" ".join(toks[1:] + toks[:1]))
    seen, uniq = set(), []
    for q in out:
        if q and q.upper() not in seen:
            seen.add(q.upper())
            uniq.append(q)
    return uniq


def same_house(street: str, roll_address: str) -> bool:
    """Is this roll address the house we asked about: the same house number and the same street,
    whether the roll prints the number first or last ("EXAMPLE DR 100")?"""
    if same_street(street, roll_address):
        return True
    parts = (roll_address or "").split()
    if len(parts) >= 2 and parts[-1].isdigit() and not parts[0].isdigit():
        return same_street(street, " ".join(parts[-1:] + parts[:-1]))
    return False


def county_owner(street: str, county: str) -> dict | None:
    """{"owner", "address"} for the parcel at exactly this street on the county's tax roll, or None
    when the roll has no parcel there. The search is fuzzy and ranked, so its first result is often
    another house: a parcel counts only when its own address has the same house number and street.
    Raises LookupError for a county with no roll wired here, and OSError when the roll does not
    answer."""
    base = COUNTY_ROLLS.get((county or "").strip().lower().replace(" county", ""))
    if not base:
        raise LookupError("no county roll is wired for %s" % (county or "this county"))
    line = (street or "").split(",")[0].strip()
    if not line[:1].isdigit():
        return None                      # no house number, so no result could be checked against it
    for q in street_queries(line):
        url = "%s/parcels/%s?detail_level=public&start=0&length=10" % (base, urllib.parse.quote(q))
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": ROLL_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=ROLL_TIMEOUT_S) as r:
                parcels = (json.loads(r.read().decode() or "{}") or {}).get("parcels") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise OSError("the county roll answered HTTP %s" % exc.code)
        for p in parcels:
            if same_house(line, p.get("parcel_address") or ""):
                return {"owner": str(p.get("owner") or ""), "address": str(p.get("parcel_address") or "")}
    return None


def _tokens(name: str) -> list[str]:
    clean = NOT_LETTERS.sub(" ", name or "").upper().replace("-", " ")
    return [t for t in clean.split() if t not in NAME_SUFFIXES]


def owner_matches(first: str, last: str, company: str, roll_owner: str) -> bool:
    """Is the owner on the county roll the borrower we knew? A person matches on surname AND first
    name (the roll prints LAST FIRST MIDDLE; a surname alone would match a son, a widow or a
    stranger, which is how a wrong house was once taken for an estate's). A company matches on the
    words of its name that are not legal filler."""
    roll = set(_tokens(roll_owner))
    if not roll:
        return False
    f, l = _tokens(first), _tokens(last)
    if f and l:
        if l[-1] not in roll:
            return False
        for tok in roll:
            if tok == f[0]:
                return True
            short, long_ = (f[0], tok) if len(f[0]) <= len(tok) else (tok, f[0])
            if len(short) >= 4 and long_.startswith(short):
                return True
        return False
    words = [t for t in _tokens(company) if t not in ORG_WORDS]
    return bool(words) and all(t in roll for t in words[:2])


def sold_since(sale: date, last_sold: str) -> bool:
    """The sold guard's rule: a last sale on or after the day that matters means the house sold.
    Here the day that matters is the auction date."""
    if not last_sold:
        return False
    try:
        import sold_guard
        return sold_guard.verdict(sale.isoformat(), last_sold) == "sold since"
    except Exception:  # noqa: BLE001  (an install without that module still has the plain rule)
        return last_sold >= sale.isoformat()


# ---------------------------------------------------------------------- what the stage reads
def sale_dates(ctx: Ctx, uuid: str, state: dict, fresh_only: bool = True) -> dict | None:
    """The record's sale dates: the note the auction stage keeps when it is fresh enough, else read
    from the record and its custom fields (two requests), inside the stage's read cap. None when
    the dates are not known and the cap is used up."""
    st = ctx.stage
    refresh = timedelta(days=float(st.get("refresh_days", 3)))
    dates = ledger.fact(uuid, "auction_dates", max_age=refresh if fresh_only else None)
    if dates is not None:
        return dates
    known = ledger.fact(uuid, "auction_dates")
    if state["reads"] >= state["max_reads"]:
        return known
    state["reads"] += 1
    rec = ctx.crm.record(uuid)
    fields = _list(st.get("date_fields")) or ["auction_date", "tax_auction_date"]
    dates = {f: rec.get(f) for f in fields if rec.get(f)}
    if st.get("trust_builtin_foreclosure_date", False) and rec.get("foreclosure_date"):
        dates["foreclosure_date"] = rec["foreclosure_date"]
    dates.update(_custom_dates(ctx, uuid, _list(st.get("custom_date_fields")) or ["Foreclosure Date"]))
    ledger.set_fact(uuid, "auction_dates", dates)
    return dates


def _split(dates: dict, today: date) -> tuple[list, list]:
    days = [d for d in (_as_date(v) for v in (dates or {}).values()) if d]
    return sorted(d for d in days if d >= today), sorted(d for d in days if d < today)


def _remember_borrower(uuid: str, rec: dict) -> None:
    if ledger.fact(uuid, "borrower") is None:
        ow = rec.get("owner") or {}
        ledger.set_fact(uuid, "borrower", {"first": " ".join(str(ow.get("first_name") or "").split()),
                                           "last": " ".join(str(ow.get("last_name") or "").split()),
                                           "company": " ".join(str(ow.get("company") or "").split())})


# ---------------------------------------------------------------------- 1. call tasks
def _call_task(ctx: Ctx, row: dict, rec: dict, sale: date, left: int, people: list, load: dict, ends: int, every: int) -> bool:
    st = ctx.stage
    u = rec["uuid"]
    if int(rec.get("predictivecall_attempts") or 0) < ends:
        ctx.out.tally["still in the call round (fewer than %d attempts)" % ends] += 1
        return False
    mine = ledger.events(u, "auction_task")
    if mine:
        last = mine[-1]
        # Run daily at the same hour, "every two days" is 48 hours less the minutes a run drifts.
        if ctx.now - datetime.fromisoformat(last["at"]) < timedelta(days=every) - timedelta(hours=6):
            ctx.out.tally["has a call task from the last %d days" % every] += 1
            return False
        task_id = last["detail"].get("task") or ""
        if task_id:
            try:
                task = laneroutes.get_task(ctx.crm, task_id)
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("could not read a call task (%s); that record waits a run" % str(exc)[:100])
                return False
            if task is not None and not laneroutes.completed_at(task):
                ctx.out.tally["the last call task is still open"] += 1
                ctx.out.line("%s: the call task from %s is still open, sale in %d day(s)" % (
                    addr(rec), laneroutes.nice_day(datetime.fromisoformat(last["at"]).astimezone(ZoneInfo(ctx.policy.tz)).date()), left))
                return False
    # A hot record keeps being called after the round: the one rule this stage lifts by itself.
    v = ctx.verdict(rec, "call", row=row, allow={"exhausted"})
    if not v.ok:
        if v.code == "no_number":
            ctx.out.tally["no usable number: mail, a package or a door knock only"] += 1
        else:
            ctx.out.tally["held back: %s" % v.reason()] += 1
        return False
    who = laneroutes.task_person(assignee_of(rec), people, load)
    if not who:
        ctx.out.problem("no active caller to give a call task to, so none were created")
        return False
    numbers = dial_order(v.numbers, agreed_numbers(u))
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    title = str(st.get("title") or "Call before the sale on {date}: {street}").format(date=laneroutes.nice_day(sale), street=addr(rec))
    notes = ("Sale in %d day(s), on %s. The five-day round is done, so this owner gets a call every %d days until the "
             "sale. Numbers to try, in order: %s." % (left, laneroutes.nice_day(sale), every,
                                                      ", ".join(mask(n["number"]) for n in numbers[:4])))
    res = ctx.write("call task", laneroutes.create_task, ctx.crm, title,
                    laneroutes.end_of_day(laneroutes.business_day(today), ctx.policy.tz), notes, who["uuid"], u)
    if ctx.live:
        task_id = str((res or {}).get("uuid") or "")
        if not task_id:
            ctx.out.problem("DataSift returned no task id for %s; check that the task exists" % addr(rec))
        ledger.change(ctx.run_id, st.id, u, "task", None, task_id)
        ledger.event(ctx.run_id, st.id, u, "auction_task", {"task": task_id, "sale": str(sale), "to": who["name"]},
                     now=ctx.now, tz=ctx.policy.tz)
    load[who["uuid"]] = load.get(who["uuid"], 0) + 1
    ctx.out.tally["call tasks"] += 1
    ctx.out.line("%s (%d days): %s, call task for %s, %d number(s)" % (laneroutes.nice_day(sale), left, addr(rec), who["name"], len(numbers)))
    return True


# ---------------------------------------------------------------------- 2. the last two weeks
def _package_ok(ctx: Ctx, rec: dict) -> tuple[bool, str]:
    """Buy box and equity confirmed (decision D34): a value inside the box and an equity figure on
    the record at or over the floor. An unknown equity is not a confirmed one."""
    st = ctx.stage
    box = _list(st.get("package_value")) or [1, 700000]
    try:
        value = float(rec.get("estimate_value") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if not (float(box[0]) <= value <= float(box[-1])):
        return False, "value outside the buy box"
    eq = rec.get("equity_percent")
    try:
        if eq is None or float(eq) < float(st.get("package_min_equity_pct", 20)):
            return False, "equity not confirmed"
    except (TypeError, ValueError):
        return False, "equity not confirmed"
    return True, ""


def _person_task(ctx: Ctx, who_setting, title: str, notes: str, what: str, property_uuid: str = "") -> str:
    """Create one task for the person a setting names. Returns their name, or '' when nobody is named."""
    people = resolve_callers(ctx, [who_setting]) if who_setting else []
    if not people:
        return ""
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    ctx.write(what, laneroutes.create_task, ctx.crm, title, laneroutes.end_of_day(laneroutes.business_day(today), ctx.policy.tz),
              notes, people[0]["uuid"], property_uuid)
    return people[0]["name"]


def _last_weeks(ctx: Ctx, hot: list, crit_days: int) -> None:
    st = ctx.stage
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    lane = str(st.get("lane") or "hot_auction")
    knock_days = int(st.get("door_knock_days") or 0)
    packages, knocks = [], []
    for sale, left, row, rec in hot:
        u = rec["uuid"]
        if st.get("priority_package", True) and left <= crit_days and (ledger.fact(u, "priority_package") or {}).get("sale") != str(sale):
            ok, why = _package_ok(ctx, rec)
            if not ok:
                ctx.out.tally["critical, no package: %s" % why] += 1
            elif ctx.passes(rec, "mail", row=row):
                packages.append((sale, left, rec))
        if st.get("door_knock", True) and (ledger.fact(u, "door_knock") or {}).get("sale") != str(sale):
            if knock_days:
                near = 0 <= left <= knock_days
            else:
                near = lanes.channel_open(lanes_cfg, lane, "door_knock", 0, 0, {"days_to_sale": left})[0]
            # A door knock is a visit: everything that stops a call task stops it, except the numbers.
            if near and ctx.passes(rec, "task", row=row, allow={"exhausted"}):
                knocks.append((sale, left, rec))
    for sale, left, rec in packages:
        value = float(rec.get("estimate_value") or 0)
        name = _person_task(ctx, st.get("package_assignee"), "Send a priority package: %s" % addr(rec),
                            "Sale in %d day(s), on %s. Buy box and equity are confirmed (value $%s, equity %s%%). "
                            "Overnight package, then note it on the record." % (
                                left, laneroutes.nice_day(sale), "{:,.0f}".format(value), rec.get("equity_percent")),
                            "priority package task", rec["uuid"])
        ctx.out.tally["priority packages to send"] += 1
        ctx.out.line("Priority package: %s, sale %s (%d days)%s" % (addr(rec), laneroutes.nice_day(sale), left,
                                                                   (", task for %s" % name) if name else ""))
        if ctx.live and name:
            ledger.set_fact(rec["uuid"], "priority_package", {"sale": str(sale), "to": name})
            ledger.event(ctx.run_id, st.id, rec["uuid"], "priority_package", {"sale": str(sale), "to": name}, now=ctx.now, tz=ctx.policy.tz)
    if packages and not st.get("package_assignee"):
        ctx.out.line("Nobody is named to send the packages yet (package_assignee), so they are a list, not tasks.")
    if knocks:
        streets = ["%s (sale %s)" % (addr(rec), laneroutes.nice_day(sale)) for sale, _, rec in knocks]
        name = _person_task(ctx, st.get("door_knock_assignee"), "Door knocks before the sale: %d house(s)" % len(knocks),
                            "Each of these has a sale inside a week. In date order:\n" + "\n".join(streets), "door knock task")
        ctx.out.tally["door knocks, sale inside a week"] += len(knocks)
        ctx.out.line("Door knock: %s%s" % ("; ".join(streets[:4]), (", task for %s" % name) if name else
                                           " (nobody is named yet: door_knock_assignee)"))
        if ctx.live and name:
            for sale, _, rec in knocks:
                ledger.set_fact(rec["uuid"], "door_knock", {"sale": str(sale), "to": name})


# ---------------------------------------------------------------------- 3. the day after the sale
def _close_tasks(ctx: Ctx, uuid: str) -> int:
    """Complete every call task this stage left open on a record whose sale has passed."""
    closed = 0
    before = set(ledger.fact(uuid, "auction_tasks_closed") or [])
    done = set(before)
    for e in ledger.events(uuid, "auction_task"):
        task_id = e["detail"].get("task") or ""
        if not task_id or task_id in done:
            continue
        try:
            task = laneroutes.get_task(ctx.crm, task_id)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a call task to close it (%s); it waits a run" % str(exc)[:100])
            continue
        if task is None or laneroutes.completed_at(task):
            done.add(task_id)             # the caller finished it, or removed it: nothing to close
            continue
        ctx.change(uuid, "task_complete", None, task_id)
        ctx.write("close call task", laneroutes.complete_task, ctx.crm, task_id)
        closed += 1
        if ctx.live:
            done.add(task_id)
    if done != before:
        ledger.set_fact(uuid, "auction_tasks_closed", sorted(done))
    return closed


def _close_out(ctx: Ctx, rows: dict, today: date, state: dict, after_status: str) -> None:
    st = ctx.stage
    window = int(st.get("after_sale_window_days", 60))
    for u, row in rows.items():
        dates = sale_dates(ctx, u, state, fresh_only=False)
        if not dates:
            continue
        upcoming, past = _split(dates, today)
        if upcoming or not past:
            continue                # a republished notice gave it a new date: it is a hot record again
        gone = (today - past[-1]).days
        if not 1 <= gone <= window:
            continue
        kept = ledger.fact(u, "deed_check") or {}
        if kept.get("verdict") == "still_owned" and kept.get("sale") == str(past[-1]):
            continue                # the deed check already brought it back for this sale
        cur = status_of(row)
        if cur in WORKING:
            ctx.change(u, "status", cur or None, after_status)
            ctx.write("after sale", ctx.crm.set_status, u, after_status)
            ctx.write("after sale note", ctx.crm.message, u,
                      "The sale date (%s) has passed, so every channel stops. The county roll is checked once, %d days "
                      "after the sale, and the record comes back only if the borrower still owns the house." % (
                          laneroutes.nice_day(past[-1]), int(st.get("deed_check_days", 45))))
            ctx.out.tally["sale date passed: set %s" % after_status] += 1
            ctx.out.line("%s: sale on %s has passed, closed out" % (addr(row), laneroutes.nice_day(past[-1])))
            if ctx.live:
                ledger.set_fact(u, "closed_out", {"sale": str(past[-1]), "from": cur})
                ledger.event(ctx.run_id, st.id, u, "after_sale", {"date": str(past[-1]), "status": after_status},
                             now=ctx.now, tz=ctx.policy.tz)
        closed = _close_tasks(ctx, u)
        if closed:
            ctx.out.tally["open call tasks closed after the sale"] += closed


# ---------------------------------------------------------------------- 4. the deed check
def _sale_of(uuid: str, today: date) -> date | None:
    c = ledger.fact(uuid, "closed_out") or {}
    if c.get("sale"):
        return _as_date(c["sale"])
    for e in reversed(ledger.events(uuid, "after_sale")):
        if e["detail"].get("date"):
            return _as_date(e["detail"]["date"])
    _, past = _split(ledger.fact(uuid, "auction_dates") or {}, today)
    return past[-1] if past else None


def judge(ctx: Ctx, rec: dict, sale: date) -> tuple[str, str]:
    """(verdict, why) for one closed-out record: sold, new_owner, still_owned, retry or unresolved."""
    u = rec.get("uuid") or ""
    last_sold = str(rec.get("last_sold") or "")[:10]
    if sold_since(sale, last_sold):
        return "sold", "the record shows a sale on %s" % last_sold
    a = rec.get("address") or {}
    street, county = str(a.get("street") or ""), str(a.get("county") or "")
    if not street:
        return "unresolved", "there is no street on the record to look up"
    try:
        found = (lookup_owner or county_owner)(street, county)
    except LookupError as exc:
        return "unresolved", str(exc)
    except Exception as exc:  # noqa: BLE001  (a roll that is down today may answer tomorrow)
        return "retry", "the county roll did not answer (%s)" % str(exc)[:80]
    if found is None:
        return "retry", "the county roll has no parcel at that street"
    ow = rec.get("owner") or {}
    who = ledger.fact(u, "borrower") or {"first": ow.get("first_name") or "", "last": ow.get("last_name") or "",
                                         "company": ow.get("company") or ""}
    if owner_matches(who.get("first") or "", who.get("last") or "", who.get("company") or "", found.get("owner") or ""):
        return "still_owned", "the county still shows the borrower as owner"
    return "new_owner", "the county shows a different owner"


def _bring_back(ctx: Ctx, rec: dict, sale: date, after_status: str) -> tuple[bool, str]:
    """The borrower still owns the house: the record comes back into its lane. The status it held
    before the close-out is restored (none, unless the playbook names one), a tag says why, and the
    rest of the gate is asked first: an owner who opted out while the record was closed stays out.
    Returns (came back, why not)."""
    st = ctx.stage
    u = rec["uuid"]
    probe = dict(rec, status=None)
    v = ctx.verdict(probe, "assign")
    if not v.ok:
        return False, v.reason()
    back = st.get("still_owned_status")
    if back is None:
        back = (ledger.fact(u, "closed_out") or {}).get("from") or None
    back = back or None
    ctx.change(u, "status", after_status, back)
    ctx.write("back in the lane", ctx.crm.set_status, u, back)
    tag = str(st.get("still_owned_tag", "Still Owned After Sale") or "")
    if tag:
        ctx.change(u, "add_tags", None, [tag])
        ctx.write("still owned tag", ctx.crm.add_tags, u, [tag], rec)
    ctx.write("still owned note", ctx.crm.message, u,
              "%d days after the sale date (%s) the county still shows the borrower as owner, so the sale did not go "
              "through. Back in the lane." % (int(st.get("deed_check_days", 45)), laneroutes.nice_day(sale)))
    return True, ""


def _deed_checks(ctx: Ctx, today: date, state: dict, after_status: str) -> None:
    st = ctx.stage
    wait = int(st.get("deed_check_days", 45))
    if wait <= 0:
        return
    window = int(st.get("deed_check_window_days", 45))
    per_run = int(st.get("deed_check_per_run", 10))
    tries_max = int(st.get("deed_check_tries", 3))
    checked = 0
    for row in ctx.crm.rows({"any_property_status": [after_status]}, cap=int(st.get("max_scan", 2000))):
        u = row["uuid"]
        fact = ledger.fact(u, "deed_check") or {}
        if fact.get("verdict") in FINAL:
            continue
        sale = _sale_of(u, today)
        if sale is None:
            dates = sale_dates(ctx, u, state, fresh_only=False)
            _, past = _split(dates or {}, today)
            sale = past[-1] if past else None
        if sale is None:
            ctx.out.tally["closed out with no sale date on file"] += 1
            continue
        gone = (today - sale).days
        if gone < wait:
            ctx.out.tally["waiting for day %d after the sale" % wait] += 1
            continue
        if gone > wait + window:
            ledger.set_fact(u, "deed_check", {"verdict": "unresolved", "sale": str(sale), "why": "the check window passed",
                                              "at": ctx.now.isoformat(timespec="seconds")})
            ctx.out.problem("%s: the sale was %d days ago and nobody settled who owns it; check by hand" % (addr(row), gone))
            continue
        plan_note = None if ctx.live else ledger.fact(u, "deed_check_plan", max_age=timedelta(hours=20))
        if plan_note is not None:
            ctx.out.tally["deed check: %s" % plan_note.get("label", "checked earlier today")] += 1
            continue
        if checked >= per_run:
            ctx.out.tally["deed checks left for the next run (%d a run)" % per_run] += 1
            continue
        checked += 1
        rec = ctx.crm.record(u)
        verdict, why = judge(ctx, rec, sale)
        stamp = ctx.now.isoformat(timespec="seconds")
        label = {"sold": "sold", "new_owner": "a new owner on the county roll", "still_owned": "borrower still owns it",
                 "retry": "not settled yet", "unresolved": "cannot be checked here"}[verdict]
        if verdict == "still_owned":
            came, held = _bring_back(ctx, rec, sale, after_status)
            if not came:
                label = "borrower still owns it, but held (%s)" % held
            ctx.out.tally["deed check: %s" % label] += 1
            if came:
                ctx.out.line("%s: %s, %s the lane" % (addr(rec), why, "back in" if ctx.live else "would come back into"))
            if ctx.live:
                ledger.set_fact(u, "deed_check", {"verdict": "still_owned" if came else "held", "sale": str(sale),
                                                  "why": held, "at": stamp})
                ledger.event(ctx.run_id, st.id, u, "deed_check", {"verdict": "still_owned" if came else "held",
                                                                  "sale": str(sale)}, now=ctx.now, tz=ctx.policy.tz)
            else:
                # Not remembered as settled: nothing was changed, and the live run must still do it.
                ledger.set_fact(u, "deed_check_plan", {"label": label, "at": stamp})
            continue
        ctx.out.tally["deed check: %s" % label] += 1
        if verdict in ("sold", "new_owner"):
            # What the county says is knowledge, kept in plan mode too. The record stays closed.
            ledger.set_fact(u, "deed_check", {"verdict": verdict, "sale": str(sale), "why": why, "at": stamp})
            ctx.out.line("%s: %s, stays closed" % (addr(rec), why))
            if ctx.live:
                ledger.event(ctx.run_id, st.id, u, "deed_check", {"verdict": verdict, "sale": str(sale)}, now=ctx.now, tz=ctx.policy.tz)
        elif verdict == "unresolved":
            ledger.set_fact(u, "deed_check", {"verdict": "unresolved", "sale": str(sale), "why": why, "at": stamp})
            ctx.out.problem("%s: the deed check could not be made (%s); check by hand" % (addr(rec), why))
        else:
            tries = int(fact.get("tries") or 0) + 1
            if not ctx.live:
                ledger.set_fact(u, "deed_check_plan", {"label": label, "at": stamp})
            elif tries >= tries_max:
                ledger.set_fact(u, "deed_check", {"verdict": "unresolved", "sale": str(sale), "why": why, "at": stamp, "tries": tries})
                ctx.out.problem("%s: %d tries and the deed check is not settled (%s); check by hand" % (addr(rec), tries, why))
            else:
                ledger.set_fact(u, "deed_check", {"tries": tries, "why": why, "at": stamp})


# ---------------------------------------------------------------------- the stage
def run_auctiontasks(ctx: Ctx) -> None:
    st = ctx.stage
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    state = {"reads": 0, "max_reads": int(st.get("max_reads", 150))}
    tag = str(st.get("tag", "Auction Soon"))
    after_status = str(st.get("after_sale_status", "Auction Date Passed") or "")
    crit_days = int(st.get("critical_days", 14))
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    tl = lanes_cfg.timeline(str(st.get("lane") or "hot_auction")) or {}
    after = tl.get("calls_after_round")
    every = int(st.get("call_every_days") or (after.get("every_days", 2) if isinstance(after, dict) else 2))
    ends = round_ends_at(ctx)
    tag_uuid = ctx.crm.tag_uuid(tag)
    if not tag_uuid:
        ctx.out.problem("there is no %r tag in this account, so no record reads as a hot auction" % tag)
    tagged = {r["uuid"]: r for r in ctx.crm.rows({"all_tags": [tag_uuid], "must_not": {"any_property_status": GONE_STATUSES}},
                                                 cap=int(st.get("max_scan", 2000)))} if tag_uuid else {}
    ctx.out.found = len(tagged)
    people = resolve_callers(ctx)
    hot = []
    for u, row in tagged.items():
        dates = sale_dates(ctx, u, state)
        if dates is None:
            ctx.out.tally["sale date not read yet (read cap, next run)"] += 1
            continue
        upcoming, _ = _split(dates, today)
        if not upcoming:
            continue
        if state["reads"] >= state["max_reads"]:
            ctx.out.tally["not read this run (read cap %d)" % state["max_reads"]] += 1
            continue
        state["reads"] += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid"):
            continue
        _remember_borrower(u, rec)          # who the borrower is, while the record still says so
        hot.append((upcoming[0], (upcoming[0] - today).days, row, rec))
    hot.sort(key=lambda x: x[0])
    load: dict[str, int] = {}
    made = sum(1 for sale, left, row, rec in hot if _call_task(ctx, row, rec, sale, left, people, load, ends, every))
    _last_weeks(ctx, hot, crit_days)
    # Records whose sale has passed are no longer tagged, so they are found by the lane's own tags.
    scope = [ctx.crm.tag_uuid(str(t)) for t in _list(st.get("scope_tags"))]
    passed = dict(tagged)
    if scope and all(scope):
        for r in ctx.crm.rows({"all_tags": scope, "must_not": {"any_property_status": GONE_STATUSES}},
                              cap=int(st.get("max_scan", 2000))):
            passed.setdefault(r["uuid"], r)
    elif scope:
        ctx.out.problem("scope_tags %s: one of those tags does not exist, so passed sales were looked for only among "
                        "the records still tagged %s" % (_list(st.get("scope_tags")), tag))
    if st.get("close_out", True) and after_status:
        _close_out(ctx, passed, today, state, after_status)
    if after_status:
        _deed_checks(ctx, today, state, after_status)
    closed = sum(n for k, n in ctx.out.tally.items() if k.startswith("sale date passed"))
    back = ctx.out.tally.get("deed check: borrower still owns it", 0)
    settled = back + ctx.out.tally.get("deed check: sold", 0) + ctx.out.tally.get("deed check: a new owner on the county roll", 0)
    ctx.out.acted = made + closed + back + ctx.out.tally.get("priority packages to send", 0) + ctx.out.tally.get(
        "door knocks, sale inside a week", 0)
    verb = "created" if ctx.live else "would create"
    parts = ["%d hot record(s) with a sale ahead: %s %d call task(s)" % (len(hot), verb, made)]
    if closed:
        parts.append("%s %d passed sale(s)" % ("closed out" if ctx.live else "would close out", closed))
    if settled:
        parts.append("%d deed check(s) settled, %d %s the lane" % (settled, back, "back in" if ctx.live else "to come back into"))
    ctx.out.head = "; ".join(parts) + "."
