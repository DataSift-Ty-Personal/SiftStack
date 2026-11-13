"""Sold guard: a record is Sold only when the house sold AFTER it became a lead.

What went wrong (measured 2026-11-12 on ty+2). Two SiftMap presets ("Knox - Recently Sold",
"Blount - Recently Sold") ran every night in in-account mode with a FIXED floor,
`extra_last_sale_date_min: 2023-01-01`, and tagged every match `recently sold`; two sequences then
set the status to Sold. A fixed floor never moves, so "recently" had grown to 46 months: any owner
who bought since January 2023 was marked Sold the night after the record arrived. On a fresh lead
that "sale" is the current owner's own purchase. On top of that, one backlog pull on 2026-06-25
created about 24,600 records for houses that had already sold; every later lead at one of those
addresses attached to a record that was already Sold.

The rule here is the one the SiftMap filter cannot express: compare the last sale with the day
the record became a lead.

    lead day   the day the record was created, or, for a record born with the sold tag, the day
               it first joined one of our lead lists
    floor      the earliest dated distress on the record (notice month, lien filing quarter,
               obituary date; a probate notice reaches back a year), never later than the lead day
    last sale before the floor          the owner's own purchase: tag off, status back to what it was
    last sale on or after the lead day  a real sale: tag on, status Sold
    in between                          cannot be told apart (it may have sold at its own auction
                                        before we uploaded the notice): left alone and counted
    dated within 120 days before the lead day and NOT on the record at birth
                                        learned late, so the owner we hold may be the seller:
                                        left alone and counted

    python src/sold_guard.py audit                    # read-only counts
    python src/sold_guard.py presets [--commit]       # show / switch off the nightly SiftMap tagging
    python src/sold_guard.py repair [--limit N] [--commit] [--include-distress]
    python src/sold_guard.py mark [--commit]          # real sales the tag missed
    python src/sold_guard.py daily [--commit]         # repair + mark, for the scheduler
    python src/sold_guard.py undo [--commit]          # put back every change in the log, newest first
    python src/sold_guard.py reentry [--commit] [--force]     # January: sales older than 3 years come back
    python src/sold_guard.py monthly-audit [--month YYYY-MM]  # last month's sold leads and how far we got

Dry by default. Every write is appended to output/sold_guard/changes.jsonl first-read-then-write,
so `undo` can restore the exact prior tag and status.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

OUT = Path(os.environ.get("SIFTSTACK_STATE_DIR") or os.environ.get("SIFTSTACK_OUTPUT_DIR") or ROOT / "output") / "sold_guard"   # the change log must outlive a deploy
TAG = "recently sold"
# Lists a record only joins because WE pulled or uploaded it as a lead.
# Lists a record only joins because WE put it there as a lead. An UPLOAD list is written by our
# courthouse uploader, which also corrects the owner, so a later join there re-dates the lead. A
# PULL list (by id, by preset) leaves the owner alone, so it only counts for a record that was
# born in the sold backlog. Not "Dispo - Flip Buyers": an older acquisition record that joined it
# after its owner sold to an investor is a real sale for the acquisition side.
# SOLD_GUARD_EXTRA_UPLOAD_LISTS names the lists a NEW courthouse feed uploads to (comma separated),
# so a lead from that feed that lands on an old Sold record is revived like any other. Empty by
# default: a list is added here the day its feed is switched on, not before, because every name
# in this tuple widens which Sold records the daily pass is willing to put back.
_EXTRA_UPLOAD_LISTS = tuple(x.strip() for x in (os.environ.get("SOLD_GUARD_EXTRA_UPLOAD_LISTS") or "").split(",")
                            if x.strip())
UPLOAD_LISTS = ("Foreclosure", "Probate", "Obituary", "Liens", "Condemned", "Pre-Foreclosure",
                "Trustee Deed") + _EXTRA_UPLOAD_LISTS
PULL_LISTS = ("Dispo - VIP Buyers", "D4D Top 3000", "Knox Blount - Top 15K DPD", "Dream 100",
              "Land - Builder Lots", "Land - Entitlement Tracts")
OUR_LISTS = UPLOAD_LISTS + PULL_LISTS
# Lists DataSift attaches by itself when the CURRENT owner shows distress (opt in: --include-distress).
DISTRESS_LISTS = ("Tax Delinquent", "Vacant", "Pre-Foreclosures - Notice of Default", "Pre-Foreclosures - Lis Pendens",
                  "Pre-Foreclosures - Notice of Foreclosure", "Pre-Foreclosures - Final Judgement", "Bankruptcy",
                  "Tired Landlord", "Pre-Probate/Deceased", "Estate Sales", "Estate and Heirs", "Judgments", "Divorce")
AUTOMATION = {"sequence", "bulk"}                 # a status change from either was not a person's call
MIN_SALE_USD = 10000.0                            # below this a transfer is a family deed, not a sale
DATA_LAG_DAYS = 120                               # how long a closed sale can take to reach the record
MARK_LOOKBACK_DAYS = 400
LOG_EVENTS = "property.created,property.status.updated,property.lists.added"


# ------------------------------------------------------------------- the rule --

def _day(value) -> str:
    return str(value)[:10] if value else ""


def born_with_tag(events: list[dict]) -> bool:
    for e in events:
        if e.get("event_type") == "property.created":
            pl = e.get("payload") or {}
            tags = pl.get("tags") or (pl.get("property") or {}).get("tags") or []
            return TAG in [str(t).lower() for t in tags]
    return False


def lead_day(events: list[dict], lead_lists) -> str:
    """The day the record became the lead it is today, or '' when it never was one (a sold house
    nobody pulled). A later courthouse upload re-dates it: the notice is about whoever owns it now."""
    def joins(names):
        want = {x.lower() for x in names}
        return [_day(e.get("timestamp")) for e in events if e.get("event_type") == "property.lists.added"
                and want & {str(x).lower() for x in ((e.get("payload") or {}).get("lists") or [])}]
    created = min((_day(e.get("timestamp")) for e in events if e.get("event_type") == "property.created"), default="")
    if born_with_tag(events):
        return max(joins(lead_lists), default="")
    uploads = [x for x in lead_lists if x in UPLOAD_LISTS or x in DISTRESS_LISTS]
    return max([created] + joins(uploads)) if created else max(joins(uploads), default="")


MONTH_RX = re.compile(r"^(20\d\d)-(\d\d)$")              # the notice month tag on a courthouse record
FILED_RX = re.compile(r"^filed_(20\d\d)-q([1-4])$")        # the county filing quarter on a lien record
PROBATE_LOOKBACK_DAYS = 365                               # an estate can sell the house before the notice runs


def evidence_floor(rec: dict, lead: str) -> str:
    """The earliest dated sign of distress on the record, never later than the lead day. We often
    upload a notice weeks after it ran, and a house can sell at its own auction in between: only a
    sale OLDER than this line is safely the current owner's own purchase."""
    tags = [t.lower() for t in tag_names(rec)]
    days = [lead] if lead else []
    months = sorted(t for t in tags if MONTH_RX.match(t))
    if months:
        first = date(int(months[0][:4]), int(months[0][5:7]), 1)
        if "probate" in tags:
            first -= timedelta(days=PROBATE_LOOKBACK_DAYS)
        days.append(first.isoformat())
    for t in tags:
        m = FILED_RX.match(t)
        if m:
            days.append("%s-%02d-01" % (m.group(1), (int(m.group(2)) - 1) * 3 + 1))
    obit = _day(rec.get("last_obituary_date"))
    if obit:
        days.append(obit)
    return min(days, default="")


def birth_sold(events: list[dict]) -> str:
    """The last sale the record carried the moment it was created ('' when it carried none)."""
    for e in events:
        if e.get("event_type") == "property.created":
            return _day(((e.get("payload") or {}).get("property") or {}).get("last_sold"))
    return ""


def verdict(lead: str, last_sold: str, floor: str = "", born_sold: str = "") -> str:
    if not lead:
        return "not a lead"
    if not last_sold:
        return "no sale date"
    if last_sold >= lead:
        return "sold since"
    if floor and floor < lead and last_sold >= floor:
        return "sold between the filing and our pull"
    # Deeds reach the data weeks after closing. A sale dated shortly before the record arrived and
    # NOT on it at birth was learned later, so the owner we hold may be the seller: do not revive.
    recent = (date.fromisoformat(lead) - timedelta(days=DATA_LAG_DAYS)).isoformat()
    if last_sold != born_sold and last_sold >= recent:
        return "sold just before we pulled it"
    return "own purchase"


def sold_flip(events: list[dict]) -> dict | None:
    """The newest status change that landed on Sold: what it replaced and who made it."""
    flips = []
    for e in events:
        if e.get("event_type") != "property.status.updated":
            continue
        ch = ((e.get("payload") or {}).get("property") or {}).get("status")
        if isinstance(ch, list) and len(ch) == 2 and str(ch[1] or "").lower() == "sold":
            flips.append({"at": str(e.get("timestamp") or ""), "from": ch[0], "source": str(e.get("source") or "")})
    return max(flips, key=lambda f: f["at"]) if flips else None


def decide(rec: dict, events: list[dict], lead_lists) -> dict:
    """What to do with one record that carries the sold tag."""
    status = str(rec.get("status") or "").strip()
    lead, sold = lead_day(events, lead_lists), _day(rec.get("last_sold"))
    floor = evidence_floor(rec, lead)
    born = birth_sold(events)
    v = verdict(lead, sold, floor, born)
    out = {"verdict": v, "lead_day": lead, "floor": floor, "last_sold": sold, "born_sold": born, "status": status,
           "action": "leave", "status_to": status}
    if v == "sold since":
        if not status:                              # the sequence never fired on this one
            out.update(action="set sold", status_to="sold")
        return out
    if v != "own purchase":
        return out
    if status.lower() != "sold":
        out["action"] = "untag"
        return out
    flip = sold_flip(events)
    if flip is None or flip["source"] not in AUTOMATION:
        out["action"], out["verdict"] = "leave", "marked sold by a person"
        return out
    out.update(action="restore", status_to=flip["from"])
    return out


def should_mark(rec: dict, events: list[dict], lead_lists) -> str:
    """'' when an untagged record with a recent sale is a real sale since the lead; else why not."""
    lead, sold, born = lead_day(events, lead_lists), _day(rec.get("last_sold")), birth_sold(events)
    v = verdict(lead, sold, evidence_floor(rec, lead), born)
    # A sale dated just before the record arrived counts when the record was born carrying an
    # OLDER sale: the newer one was learned afterwards, so the owner we hold is the seller.
    late = v == "sold just before we pulled it" and bool(born) and born < sold
    if v != "sold since" and not late:
        return v
    try:
        price = float(rec.get("last_sale_price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price < MIN_SALE_USD:
        return "no real price (family transfer)"
    return ""


# ----------------------------------------------------------------------- CRM --

class Crm:
    def __init__(self, gap: float = 0.5):
        import datasift_api_upload as up
        self.up, self.api, self.gap = up, up.Api(), gap

    def call(self, path, method="GET", body=None, headers=None):
        for attempt in range(6):
            try:
                time.sleep(self.gap)
                return self.api.call(path, method, body, headers=headers)
            except self.up.ApiError as exc:
                if getattr(exc, "code", 0) not in (429, 502, 503, 504):
                    raise
                m = re.search(r"available in (\d+)", str(exc))
                time.sleep(int(m.group(1)) + 2 if m else 10 * (attempt + 1))
        raise RuntimeError("gave up on " + path)

    def titles(self, path: str) -> dict:
        r = self.call(path)
        rows = r.get("results", r) if isinstance(r, dict) else r
        return {(x.get("title") or "").strip(): x["uuid"] for x in rows}

    def search(self, must: dict, limit: int = 1, offset: int = 0) -> dict:
        return self.call("/api/internal/property/", "POST",
                         {"limit": limit, "offset": offset, "ordering": "-created", "query": {"must": must}},
                         headers={"x-http-method-override": "GET"})

    def page(self, must: dict) -> list[dict]:
        out, off = [], 0
        while True:
            r = self.search(must, 100, off)
            page = r.get("results") or []
            out += page
            off += 100
            if not page or off >= (r.get("count") or 0) or off >= 9900:
                return out

    def record(self, uuid: str) -> dict:
        return self.call("/api/internal/property/%s/" % uuid)

    def events(self, uuid: str) -> list[dict]:
        out, off = [], 0
        while True:
            r = self.call("/api/internal/property/%s/logs/?limit=100&offset=%d&any_event_type=%s" % (uuid, off, LOG_EVENTS))
            page = r.get("results") or []
            out += page
            off += 100
            if not page or off >= (r.get("count") or 0) or off >= 500:
                return out

    def untag(self, uuid: str) -> None:
        self.call("/api/internal/property/%s/remove-tags/" % uuid, "POST", {"tags": TAG})

    def tag(self, uuid: str) -> None:
        self.call("/api/internal/property/%s/add-tags/" % uuid, "POST", {"tags": TAG})

    def set_status(self, uuid: str, status) -> None:
        # The status route takes a name OR null. A PATCH of the record refuses null
        # ("This field may not be null"), verified live 2026-11-12.
        self.call("/api/internal/property/%s/status/" % uuid, "POST", {"status": status or None})


def tag_names(rec: dict) -> list[str]:
    return [str((t.get("title") or t.get("name")) if isinstance(t, dict) else t) for t in rec.get("tags") or []]


# ---------------------------------------------------------------- change log --

def _log(entry: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "changes.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def read_log() -> list[dict]:
    p = OUT / "changes.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def apply(crm: Crm, uuid: str, street: str, d: dict, commit: bool) -> bool:
    """Carry out one decision. The status goes first, so a refused write leaves the record exactly
    as it was; the change is logged with the prior state either way, so `undo` can put it back."""
    if d["action"] == "leave" or not commit:
        return False
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "uuid": uuid, "street": street, "action": d["action"],
             "status_from": d["status"], "status_to": d["status_to"],
             "tag_removed": d["action"] in ("restore", "untag", "reenter"),
             "tag_added": d["action"] == "mark", "why": d["verdict"], "lead_day": d["lead_day"], "last_sold": d["last_sold"]}
    try:
        if (d["status_to"] or "") != (d["status"] or ""):
            crm.set_status(uuid, d["status_to"])
        if entry["tag_removed"]:
            crm.untag(uuid)
        if entry["tag_added"]:
            crm.tag(uuid)
    except Exception as exc:
        _log(dict(entry, error=str(exc)[:200]))
        raise
    _log(entry)
    return True


def verify(crm: Crm, uuid: str, d: dict) -> str:
    rec = crm.record(uuid)
    has = TAG in [t.lower() for t in tag_names(rec)]
    status = str(rec.get("status") or "")
    want_tag = d["action"] == "mark" or (d["action"] == "set sold")
    if has != want_tag:
        return "tag reads %s, wanted %s" % (has, want_tag)
    if status.lower() != str(d["status_to"] or "").lower():
        return "status reads %r, wanted %r" % (status, d["status_to"])
    return ""


# ------------------------------------------------------------------ commands --

def candidates(crm: Crm, lists: dict, tag_uuid: str, lead_lists) -> dict:
    """Tagged records that are, or have become, leads: born as one, or sitting on a lead list."""
    base = {"property_type": "clean", "any_tags": [tag_uuid]}
    out = {}
    for x in crm.page(dict(base, created=["2026-06-26", "2100-01-01"])):
        out[x["uuid"]] = x
    ours = [lists[t] for t in lead_lists if t in lists]
    for i in range(0, len(ours), 6):                 # small groups keep each result under the 10,000-row cap
        for x in crm.page(dict(base, any_lists=ours[i:i + 6])):
            out.setdefault(x["uuid"], x)
    return out


def _cache() -> dict:
    p = OUT / "judged.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        d = {}
    return {"repair": d.get("repair") or {}, "mark": d.get("mark") or {}, "reentry": d.get("reentry") or {}}


def _save_cache(c: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "judged.json").write_text(json.dumps(c), encoding="utf-8")


def row_sig(row: dict) -> str:
    """What a search row shows of a record's state. A new list or tag, or a status change, moves it."""
    return "%s|%s|%s" % (row.get("list_count"), row.get("tag_count"), row.get("status") or "")


def repair(args) -> int:
    """Tagged leads: put back the ones whose 'sale' is the owner's own purchase. A record judged
    'leave' is not read again until something on it changes, so the daily pass stays small."""
    crm = Crm()
    lists, tags = crm.titles("/api/internal/list/?limit=1000"), crm.titles("/api/internal/tag/?limit=10000")
    lead_lists = OUR_LISTS + (DISTRESS_LISTS if args.include_distress else ())
    cand = candidates(crm, lists, tags[TAG], lead_lists)
    cache = _cache()
    seen = {} if getattr(args, "fresh", False) else cache["repair"]
    todo = [u for u in cand if seen.get(u) != row_sig(cand[u])]
    unchanged = len(cand) - len(todo)
    todo = todo[:args.limit] if args.limit else todo
    print("%d tagged lead records, %d unchanged since they were last judged, %d to judge (%s)"
          % (len(cand), unchanged, len(todo), "LIVE" if args.commit else "dry"), flush=True)
    counts, changed, checked, plan = Counter(), 0, Counter(), []
    for i, uuid in enumerate(todo, 1):
        street = ((cand[uuid].get("address") or {}).get("street") or "")
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            if TAG not in [t.lower() for t in tag_names(rec)]:
                counts["tag already gone"] += 1
                continue
            d = decide(rec, events, lead_lists)
            counts["%s: %s" % (d["action"], d["verdict"])] += 1
            plan.append(dict(d, uuid=uuid, street=street))
            if d["action"] == "leave" and args.commit:
                cache["repair"][uuid] = row_sig(cand[uuid])
            if apply(crm, uuid, street, d, args.commit):
                changed += 1
                if checked[d["action"]] < 3 or changed % 100 == 0:   # the first three of each kind, then one in a hundred
                    bad = verify(crm, uuid, d)
                    checked[d["action"]] += 1
                    if bad:
                        print("STOPPED: %s did not read back (%s)" % (street, bad), flush=True)
                        _save_cache(cache)
                        return 2
        except Exception as exc:  # noqa: BLE001  (one unreadable record never stops the pass)
            counts["error: %s" % str(exc)[:50]] += 1
        if i % 100 == 0:
            _save_cache(cache)
            print("  %d of %d judged, %d changed" % (i, len(todo), changed), flush=True)
    _save_cache(cache)
    (OUT / "repair_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    print("Repair: %d judged, %d changed%s." % (len(todo), changed, "" if args.commit else " (dry run: nothing written)"))
    args.stats = dict(getattr(args, "stats", None) or {}, restored=changed, repair_judged=len(todo),
                      repair_errors=sum(v for k, v in counts.items() if k.startswith("error")))
    return 0


def sale_weeks(today: date, days: int = MARK_LOOKBACK_DAYS) -> list[tuple[str, str]]:
    """Calendar weeks (Monday to Sunday) back to the lookback. Fixed weeks keep the cache keys stable."""
    monday = today - timedelta(days=today.weekday())
    out = []
    while monday >= today - timedelta(days=days):
        out.append((monday.isoformat(), (monday + timedelta(days=6)).isoformat()))
        monday -= timedelta(days=7)
    return out


def mark(args) -> int:
    """Real sales the tag missed: untagged records whose last sale is on or after their lead day.
    The search is cut by the week of the sale, so a record is read once per sale and never again."""
    crm = Crm()
    tags = crm.titles("/api/internal/tag/?limit=10000")
    cache, today = _cache(), date.today()
    todo = {}
    for a, b in sale_weeks(today):
        for x in crm.page({"property_type": "clean", "last_sold": [a, b], "must_not": {"any_tags": [tags[TAG]]}}):
            key = "%s|%s" % (x["uuid"], a)
            if str(x.get("status") or "").lower() != "sold" and key not in cache["mark"]:
                todo[key] = x
    keys = list(todo)[:args.limit] if args.limit else list(todo)
    print("%d untagged records with a sale in the last %d days not judged yet, %d this pass (%s)"
          % (len(todo), MARK_LOOKBACK_DAYS, len(keys), "LIVE" if args.commit else "dry"), flush=True)
    counts, changed, plan = Counter(), 0, []
    for key in keys:
        x = todo[key]
        uuid, street = x["uuid"], ((x.get("address") or {}).get("street") or "")
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            why = should_mark(rec, events, OUR_LISTS)
            if why:
                counts["leave: " + why] += 1
            else:
                status = str(rec.get("status") or "")
                d = {"action": "mark", "verdict": "sold since", "status": status, "status_to": "sold",
                     "lead_day": lead_day(events, OUR_LISTS), "last_sold": _day(rec.get("last_sold"))}
                counts["mark sold"] += 1
                plan.append(dict(d, uuid=uuid, street=street, price=rec.get("last_sale_price"),
                                 born_sold=birth_sold(events)))
                changed += apply(crm, uuid, street, d, args.commit)
            if args.commit:
                cache["mark"][key] = why or "marked"
        except Exception as exc:  # noqa: BLE001
            counts["error: %s" % str(exc)[:50]] += 1
    _save_cache(cache)
    (OUT / "mark_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    print("Mark: %d real sales%s." % (counts["mark sold"], "" if args.commit else " (dry run: nothing written)"))
    args.stats = dict(getattr(args, "stats", None) or {}, marked=changed, mark_judged=len(keys),
                      mark_errors=sum(v for k, v in counts.items() if k.startswith("error")))
    return 0


def daily(commit: bool = False, limit: int = 0) -> dict:
    """One pass for the scheduler: revive leads that landed on an old Sold record, mark real sales."""
    args = argparse.Namespace(commit=commit, limit=limit, include_distress=False, fresh=False, stats={})
    rc = repair(args) or mark(args)
    return dict(args.stats, rc=rc)


# ------------------------------------------------------- the yearly re-entry --
#
# The teaching keeps the sold window trailing about three years and resets it every January 1:
# a house that sold more than three years ago is no longer "recently sold", and the person who
# bought it then is today's owner and a prospect like any other. Until this existed nothing ever
# took a record back out of Sold, so the Sold pile only grew.

REENTRY_YEARS = int(os.environ.get("SOLD_REENTRY_YEARS") or 3)
REENTRY_LOOKBACK_MONTHS = int(os.environ.get("SOLD_REENTRY_LOOKBACK_MONTHS") or 24)
REENTRY_MAX_PER_RUN = int(os.environ.get("SOLD_REENTRY_MAX_PER_RUN") or 150)
OWNER_SLACK_DAYS = 45       # how far before the sale date an owner record may be dated and still be the buyer's


def reentry_cutoff(today: date, years: int = REENTRY_YEARS) -> str:
    """Sales before this day have left the window: January 1 of (this year minus `years`).

    On any day of 2027 with a three year window that is 2024-01-01, so the year leaving is 2023.
    A fixed day of the year on purpose: the window moves once, on January 1, and stays put."""
    return date(today.year - years, 1, 1).isoformat()


def reentry_due(today: date) -> bool:
    """The yearly pass runs in January (SOLD_REENTRY_MONTHS widens it for a pass that needs longer)."""
    try:
        months = max(1, int(os.environ.get("SOLD_REENTRY_MONTHS") or 1))
    except ValueError:
        months = 1
    return today.month <= months


def reentry_months(cutoff: str, months: int = REENTRY_LOOKBACK_MONTHS) -> list[tuple[str, str]]:
    """Calendar months ending the day before the cutoff, newest first. One search per month keeps
    every result under the records search's 10,000 row wall."""
    y, m, _ = (int(x) for x in cutoff.split("-"))
    out = []
    for _i in range(max(1, months)):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        first = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        out.append((first.isoformat(), (nxt - timedelta(days=1)).isoformat()))
    return out


def reentry_decide(rec: dict, events: list[dict], cutoff: str) -> dict:
    """What to do with one Sold record whose sale may have left the window.

    It comes back only when ALL of these hold, and each "leave" is counted by its reason:
      * the status is Sold and the last sale is older than the cutoff
      * automation marked it Sold (a person's Sold is never undone: it may be our own deal)
      * the owner on the record is the BUYER. A record born with the sold tag came in through
        the sold pull, which replaces owners, so its owner is the buyer. Any other record must
        show an owner dated at or after the sale; otherwise the person on it is the seller we
        once marketed to, and re-entering it would call a stranger's house and ask for someone
        who left three years ago.
      * it is not on one of our courthouse lists. Those name the PREVIOUS owner's distress, and
        a returning record must not ride into the first-to-market lane on somebody else's notice.
    """
    status = str(rec.get("status") or "").strip()
    sold = _day(rec.get("last_sold"))
    out = {"verdict": "", "action": "leave", "status": status, "status_to": status, "last_sold": sold,
           "lead_day": "", "born_sold": birth_sold(events)}

    def leave(why: str) -> dict:
        out["verdict"] = why
        return out

    if status.lower() != "sold":
        return leave("not marked Sold")
    if not sold:
        return leave("no sale date")
    if sold >= cutoff:
        return leave("sold inside the window")
    born = born_with_tag(events)
    flip = sold_flip(events)
    if flip is not None and flip["source"] not in AUTOMATION:
        return leave("marked sold by a person")
    if flip is None and not born:
        return leave("no record of who marked it sold")
    if not born:
        owned = _day(rec.get("owned_since"))
        floor = (date.fromisoformat(sold) - timedelta(days=OWNER_SLACK_DAYS)).isoformat()
        if not owned or owned < floor:
            return leave("the owner on the record is from before the sale")
    lists = {str((x.get("title") or x.get("name")) if isinstance(x, dict) else x).strip().lower()
             for x in rec.get("lists") or []}
    ours = sorted(lists & {x.lower() for x in UPLOAD_LISTS})
    if ours:
        return leave("on a courthouse list from the previous owner")
    reset = (os.environ.get("SOLD_REENTRY_STATUS") or "").strip()
    out.update(verdict="sold before the window", action="reenter", status_to=reset)
    return out


def reentry(args) -> int:
    """The January pass: Sold records whose sale left the three year window come back as the new
    owner's record (status reset, sold tag off). Dry by default; every change is in the change
    log with its prior status, so `undo` puts it back."""
    today = getattr(args, "today", None) or date.today()
    args.stats = dict(getattr(args, "stats", None) or {}, reentered=0, reentry_judged=0, reentry_errors=0,
                      reentry_due=reentry_due(today), reentry_waiting=0)
    if not args.stats["reentry_due"] and not getattr(args, "force", False):
        print("Re-entry: not due. The window moves on January 1; pass --force to run it now.")
        return 0
    cutoff = reentry_cutoff(today)
    crm = getattr(args, "crm", None) or Crm()
    tags = crm.titles("/api/internal/tag/?limit=10000")
    cache = _cache()
    seen = {} if getattr(args, "fresh", False) else cache["reentry"]
    cand: dict = {}
    for a, b in reentry_months(cutoff):
        for x in crm.page({"property_type": "clean", "any_tags": [tags[TAG]], "last_sold": [a, b]}):
            if str(x.get("status") or "").lower() == "sold":
                cand.setdefault(x["uuid"], x)
    todo = [u for u in cand if seen.get(u) != row_sig(cand[u])]
    limit = args.limit or REENTRY_MAX_PER_RUN
    waiting = max(0, len(todo) - limit)
    todo = todo[:limit]
    print("%d Sold record(s) with a sale before %s, %d to judge this pass, %d waiting (%s)"
          % (len(cand), cutoff, len(todo), waiting, "LIVE" if args.commit else "dry"), flush=True)
    counts, changed, checked, plan = Counter(), 0, 0, []
    for i, uuid in enumerate(todo, 1):
        street = ((cand[uuid].get("address") or {}).get("street") or "")
        try:
            rec, events = crm.record(uuid), crm.events(uuid)
            d = reentry_decide(rec, events, cutoff)
            counts["%s: %s" % (d["action"], d["verdict"])] += 1
            plan.append(dict(d, uuid=uuid, street=street))
            if d["action"] == "leave" and args.commit:
                cache["reentry"][uuid] = row_sig(cand[uuid])
            if apply(crm, uuid, street, d, args.commit):
                changed += 1
                if checked < 3 or changed % 100 == 0:
                    bad = verify(crm, uuid, d)
                    checked += 1
                    if bad:
                        print("STOPPED: %s did not read back (%s)" % (street, bad), flush=True)
                        _save_cache(cache)
                        args.stats.update(reentered=changed, reentry_judged=i, reentry_waiting=waiting)
                        return 2
        except Exception as exc:  # noqa: BLE001  (one unreadable record never stops the pass)
            counts["error: %s" % str(exc)[:50]] += 1
        if i % 100 == 0:
            _save_cache(cache)
    _save_cache(cache)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "reentry_plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    for k, v in counts.most_common():
        print("%6d  %s" % (v, k))
    would = sum(v for k, v in counts.items() if k.startswith("reenter"))
    print("Re-entry: %d judged, %d %s%s." % (len(todo), changed if args.commit else would,
                                           "put back into marketing" if args.commit else "would come back",
                                           "" if args.commit else " (dry run: nothing written)"))
    args.stats.update(reentered=changed, reentry_would=would, reentry_judged=len(todo), reentry_waiting=waiting,
                      reentry_errors=sum(v for k, v in counts.items() if k.startswith("error")))
    return 0


# -------------------------------------------------------- the monthly audit --
#
# The by-hand sold audit: every month, look at what sold and ask why each one was missed. This
# reads last month's sales among the records we hold and says, for the ones that were OUR leads
# when they sold, how far we got: how long we had it, whether anyone called, texted or mailed,
# and what status it was sitting in. Read-only on the account; it writes two files beside the
# change log.

AUDIT_MAX = int(os.environ.get("SOLD_AUDIT_MAX") or 400)
ATTEMPT_FIELDS = (("predictivecall_attempts", "calls"), ("sms_attempts", "texts"),
                  ("directmail_attempts", "mail"), ("rvm_attempts", "voicemail drops"))


def audit_month(today: date, month: str = "") -> tuple[str, str, str]:
    """(YYYY-MM, first day, last day) for the month to audit: the one named, else last month."""
    if month:
        y, m = int(month[:4]), int(month[5:7])
    else:
        y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return "%04d-%02d" % (y, m), first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _attempt(rec: dict, key: str) -> int:
    for holder in (rec, rec.get("owner") or {}):
        try:
            if holder.get(key) not in (None, ""):
                return int(holder.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def audit_row(rec: dict, events: list[dict], lead_lists=None) -> dict:
    """One sold record, as the audit sees it. `was_lead` is the question that matters: was this
    ours to win when it sold?"""
    lead_lists = lead_lists or OUR_LISTS
    lead, sold, born = lead_day(events, lead_lists), _day(rec.get("last_sold")), birth_sold(events)
    v = verdict(lead, sold, evidence_floor(rec, lead), born)
    flip = sold_flip(events)
    lists = [str((x.get("title") or x.get("name")) if isinstance(x, dict) else x) for x in rec.get("lists") or []]
    ours = [x for x in lists if x in OUR_LISTS]
    attempts = {label: _attempt(rec, key) for key, label in ATTEMPT_FIELDS}
    days = None
    if lead and sold and sold >= lead:
        days = (date.fromisoformat(sold) - date.fromisoformat(lead)).days
    try:
        price = float(rec.get("last_sale_price") or 0) or None
    except (TypeError, ValueError):
        price = None
    return {"street": ((rec.get("address") or {}).get("street") or ""), "was_lead": v == "sold since",
            "verdict": v, "lead_day": lead, "sold": sold, "days_as_lead": days, "price": price,
            "lists": ", ".join(ours), "status_before": (flip or {}).get("from") or "",
            "marked_by": (flip or {}).get("source") or "", "assigned": bool(rec.get("assigned_to")),
            "untouched": not any(attempts.values()), **attempts}


def audit_summary(month: str, rows: list[dict]) -> dict:
    leads = [r for r in rows if r["was_lead"]]
    days = sorted(r["days_as_lead"] for r in leads if r["days_as_lead"] is not None)
    by_status = Counter((r["status_before"] or "no status") for r in leads)
    by_list = Counter(x.strip() for r in leads for x in r["lists"].split(",") if x.strip())
    return {"month": month, "sold_records": len(rows), "were_our_leads": len(leads),
            "never_touched": sum(1 for r in leads if r["untouched"]),
            "never_called": sum(1 for r in leads if not r["calls"]),
            "never_mailed": sum(1 for r in leads if not r["mail"]),
            "never_texted": sum(1 for r in leads if not r["texts"]),
            "unassigned": sum(1 for r in leads if not r["assigned"]),
            "median_days_as_lead": days[len(days) // 2] if days else None,
            "status_before_sold": dict(by_status.most_common(8)), "by_list": dict(by_list.most_common(8)),
            "not_leads": dict(Counter(r["verdict"] for r in rows if not r["was_lead"]).most_common())}


def audit_text(s: dict) -> str:
    """The summary as a few plain lines."""
    if not s["were_our_leads"]:
        return "%s: %d record(s) we hold sold, none while it was our lead." % (s["month"], s["sold_records"])
    lines = ["%s: %d of our leads sold (%d sold record(s) read)." % (s["month"], s["were_our_leads"], s["sold_records"]),
             "%d were never touched, %d never called, %d never texted, %d never mailed, %d had no caller assigned."
             % (s["never_touched"], s["never_called"], s["never_texted"], s["never_mailed"], s["unassigned"])]
    if s["median_days_as_lead"] is not None:
        lines.append("We held a lead a median %d days before it sold." % s["median_days_as_lead"])
    if s["by_list"]:
        lines.append("By list: " + ", ".join("%s %d" % kv for kv in s["by_list"].items()) + ".")
    return "\n".join(lines)


def monthly_audit(args) -> int:
    """Last month's sales among the records we hold, and how far we got on the ones that were
    ours. Runs once a month (the first run after the month ends); read-only on the account."""
    import csv as _csv

    today = getattr(args, "today", None) or date.today()
    month, first, last = audit_month(today, getattr(args, "month", "") or "")
    args.stats = dict(getattr(args, "stats", None) or {}, audit_month=month, audit_ran=False)
    OUT.mkdir(parents=True, exist_ok=True)
    out_json = OUT / ("audit_%s.json" % month)
    if out_json.exists() and not getattr(args, "force", False) and not getattr(args, "month", ""):
        print("Sold audit for %s is already written (%s). Pass --force to run it again." % (month, out_json))
        try:
            args.stats["audit_summary"] = json.loads(out_json.read_text(encoding="utf-8")).get("summary")
        except (OSError, ValueError):
            pass
        return 0
    crm = getattr(args, "crm", None) or Crm()
    tags = crm.titles("/api/internal/tag/?limit=10000")
    cand: dict = {}
    day = date.fromisoformat(first)
    while day.isoformat() <= last:                     # by week, to stay under the 10,000 row wall
        end = min(day + timedelta(days=6), date.fromisoformat(last))
        for x in crm.page({"property_type": "clean", "any_tags": [tags[TAG]],
                           "last_sold": [day.isoformat(), end.isoformat()]}):
            cand.setdefault(x["uuid"], x)
        day = end + timedelta(days=1)
    limit = args.limit or AUDIT_MAX
    uuids = list(cand)[:limit]
    print("%d tagged record(s) sold in %s, %d read" % (len(cand), month, len(uuids)), flush=True)
    rows, errors = [], 0
    for uuid in uuids:
        try:
            rows.append(dict(audit_row(crm.record(uuid), crm.events(uuid)), uuid=uuid))
        except Exception:  # noqa: BLE001
            errors += 1
    summary = audit_summary(month, rows)
    summary["not_read"] = max(0, len(cand) - len(uuids))
    summary["errors"] = errors
    out_json.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    cols = ["street", "was_lead", "verdict", "lead_day", "sold", "days_as_lead", "price", "lists", "status_before",
            "marked_by", "assigned", "untouched", "calls", "texts", "mail", "voicemail drops", "uuid"]
    with (OUT / ("audit_%s.csv" % month)).open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (not r["was_lead"], r["street"])))
    print(audit_text(summary))
    args.stats.update(audit_ran=True, audit_summary=summary)
    return 0


def presets(args) -> int:
    """The nightly SiftMap presets that hand out the sold tag. Off means auto-add off, nothing deleted."""
    import datasift_api_upload as up
    from siftmap_standalone import SiftMapClient
    sm = SiftMapClient(api_key=up.env().get("REISIFT_API_KEY"))
    rows, path = [], "/filters/"
    while path:
        r = sm._request(path)
        rows += r.get("results", r) if isinstance(r, dict) else r
        nxt = r.get("next") if isinstance(r, dict) else None
        path = nxt.replace("https://map.reisift.io", "") if nxt else None
    hit = [f for f in rows if TAG in [str(t).lower() for t in f.get("tags") or []]]
    for f in hit:
        floor = ((f.get("filter_data") or {}).get("filters") or {}).get("extra_last_sale_date_min")
        print("[%s] %-26s active=%s auto_add=%s sale floor=%s" % (f["id"], f.get("name"), f.get("is_active"),
                                                                 f.get("auto_add_enabled"), floor))
        if args.commit and f.get("auto_add_enabled"):
            _log({"at": datetime.now().isoformat(timespec="seconds"), "action": "preset auto-add off", "preset": f["id"],
                  "name": f.get("name")})
            sm._request("/filters/%s/" % f["id"], "PATCH", {"auto_add_enabled": False})
            back = sm._request("/filters/%s/" % f["id"])
            print("     auto-add now reads %s" % back.get("auto_add_enabled"))
            if back.get("auto_add_enabled"):
                return 2
    if not args.commit:
        print("(dry: pass --commit to switch auto-add off on the %d preset(s) still on)"
              % sum(1 for f in hit if f.get("auto_add_enabled")))
    return 0


def audit(_args) -> int:
    crm = Crm()
    lists, tags = crm.titles("/api/internal/list/?limit=1000"), crm.titles("/api/internal/tag/?limit=10000")
    base = {"property_type": "clean", "any_tags": [tags[TAG]]}
    n = crm.search(base).get("count") or 0
    born = crm.search(dict(base, created=["2000-01-01", "2026-06-25T23:59:59"])).get("count") or 0
    print("tagged %r: %d | born in the sold backlog pull: %d | created since as leads: %d" % (TAG, n, born, n - born))
    for t in OUR_LISTS + DISTRESS_LISTS:
        if t in lists:
            k = crm.search(dict(base, any_lists=[lists[t]])).get("count") or 0
            if k:
                print("  %-40s %5d tagged" % (t, k))
    return 0


def undo(args) -> int:
    crm, n = Crm(), 0
    for e in reversed(read_log()):
        if not e.get("uuid") or e.get("undone") or e.get("error"):
            continue
        print("%s  %s: tag %s, status %r -> %r" % (e["street"][:30], e["action"],
              "back on" if e.get("tag_removed") else ("off" if e.get("tag_added") else "unchanged"),
              e["status_to"], e["status_from"]))
        if args.commit:
            if e.get("tag_removed"):
                crm.tag(e["uuid"])
            if e.get("tag_added"):
                crm.untag(e["uuid"])
            if (e["status_to"] or "") != (e["status_from"] or ""):
                crm.set_status(e["uuid"], e["status_from"])
        n += 1
    print("%d change(s) %s" % (n, "put back" if args.commit else "would be put back (dry)"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "presets", "repair", "mark", "daily", "undo", "reentry", "monthly-audit"):
        p = sub.add_parser(name)
        p.add_argument("--commit", action="store_true")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--include-distress", action="store_true",
                       help="also treat DataSift's own distress lists as a reason a record is a lead")
        p.add_argument("--fresh", action="store_true", help="judge every record again, ignoring earlier verdicts")
        if name in ("reentry", "monthly-audit"):
            p.add_argument("--force", action="store_true",
                           help="run even when it is not due (re-entry outside January, an audit already written)")
        if name == "monthly-audit":
            p.add_argument("--month", default="", help="YYYY-MM to audit (default: last month)")
    a = ap.parse_args(argv)
    if a.cmd == "daily":
        return daily(a.commit, a.limit)["rc"]
    return {"audit": audit, "presets": presets, "repair": repair, "mark": mark, "undo": undo,
            "reentry": reentry, "monthly-audit": monthly_audit}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
