"""The three lead workflows of the leadprep track: the property check on a new lead, the closer handoff
text and the primary phone. Zero network: Zillow, the photo read, the photo hosts and DataSift are all
fakes, the account log is seeded straight into the brain's copy, and the database is this section's own
file. Every name, street, id and number here is invented; the numbers use the never-assigned 865-000
exchange.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .selftest_autopilot import EMPLOYEES, _rec
from .selftest_autopilot import FakeCRM as _BaseCRM

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 17, 13, 0, tzinfo=ET).astimezone(timezone.utc)     # a Tuesday, 1pm Eastern
EM, EN = chr(0x2014), chr(0x2013)
MINE = {
    "leadprep": ("leadprep", "run_leadprep", "paid", False, False),
    "closertext": ("handoff", "run_closertext", "outreach", False, False),
    "primaryphone": ("primaryphone", "run_primaryphone", "free", False, False),
}


class FakeCRM(_BaseCRM):
    """The autopilot selftest's fake DataSift, plus the owner update and two switches."""
    sticks = True            # False: the owner update answers and changes nothing
    fail_message = False     # True: the notes route raises

    def __init__(self, records, users=None):
        super().__init__(records, users=users or [{"uuid": "u-dana", "first_name": "Dana", "is_active": True}])
        self.reads = 0

    def record(self, u):
        self.reads += 1
        return super().record(u)

    def update_owner(self, owner_uuid, fields):
        self.writes.append(("owner", owner_uuid, dict(fields)))
        if self.sticks:
            for r in self.recs.values():
                if r["owner"]["uuid"] == owner_uuid:
                    r["owner"].update(fields)
        return {}

    def message(self, u, text, pin=False):
        if self.fail_message:
            raise RuntimeError("the notes route did not answer")
        return super().message(u, text, pin)


# ---------------------------------------------------------------------- log rows, shaped like the route's
def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def status_ev(i, rec, when, old, new):
    return {"id": "lp-st-%s" % i, "event_type": "property.status.updated", "resource": rec, "resource_type": "property",
            "source": "internal-api", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"property": {"status": [old, new]}}}


def answered(i, owner, when, number, caller="Dana", duration=180):
    return {"id": "lp-call-%s" % i, "event_type": "owner.call.answered", "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": number, "origin_number": "8650000998", "direction": "outbound",
                                 "duration": duration, "source": "kixie", "uuid": "c-%s" % i, "extra": {},
                                 "user": {"first_name": caller, "uuid": "u-%s" % caller.lower()}}}}


def card_ev(i, card, prop, when, old_col, new_col):
    return {"id": "lp-card-%s" % i, "event_type": "card.moved", "resource": card, "resource_type": "card",
            "source": "internal-api", "author": "person@example.com", "timestamp": _ts(when),
            "payload": {"card": {"prop": prop},
                        "extra_info": {"card": {"prop": prop, "column": [old_col, new_col], "board": ["b-lead"]}}}}


def phone_ev(i, owner, when, number, old, new, source="kixie"):
    return {"id": "lp-ph-%s" % i, "event_type": "owner.phone.status.updated", "resource": owner, "resource_type": "owner",
            "source": source, "author": "system", "timestamp": _ts(when),
            "payload": {"owner": {"phone": "+1" + number, "status": [old, new]}}}


def ph(n, status="UNKNOWN", tags=("Dial First",), kind="MOBILE"):
    return {"number": "+1" + n, "type": kind, "tags": list(tags), "status": status}


def details(photos=6, listed="2026-06-03", removed="2026-08-20", price=189000, status="OFF_MARKET",
            sold=("2009-03-02", 82000)):
    hist = []
    if removed:
        hist.append({"date": removed, "event": "Listing removed", "price": price})
    if listed:
        hist.append({"date": listed, "event": "Listed for sale", "price": price})
    if sold:
        hist.append({"date": sold[0], "event": "Sold", "price": sold[1]})
    d = {"homeStatus": status, "priceHistory": hist,
         "attributionInfo": {"agentName": "Robin Agent", "brokerName": "Sample Realty"}}
    if photos:
        d["originalPhotos"] = [{"mixedSources": {"jpeg": [
            {"url": "https://photos.example.test/h%d_384.jpg" % i, "width": 384},
            {"url": "https://photos.example.test/h%d_768.jpg" % i, "width": 768},
            {"url": "https://photos.example.test/h%d_1536.jpg" % i, "width": 1536}]}} for i in range(photos)]
    return d


def on_market():
    return {"homeStatus": "FOR_SALE", "price": 199000, "daysOnZillow": 12,
            "priceHistory": [{"date": "2026-11-05", "event": "Listed for sale", "price": 199000}],
            "attributionInfo": {"agentName": "Robin Agent", "brokerName": "Sample Realty"}}


def _pol(stages: dict):
    from .autopilot import policy
    pb = {"business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
          "caps": {"daily_usd": 25, "record_usd": 1.0, "outreach_daily_usd": 60}, "people": {"callers": ["Dana"]},
          "stages": copy.deepcopy(stages)}
    return policy.parse(pb, EMPLOYEES)


def _run(sid, pol, crm, now=NOW, live=False):
    from .autopilot import sweep
    config.MODE = "live" if live else "dry"
    try:
        return sweep.run(sid, crm=crm, now=now, pol=pol)
    finally:
        config.MODE = "dry"


def _fresh() -> None:
    from . import store
    from .autopilot import limits, scrub
    limits.ensure()
    scrub.ensure()
    with store._LOCK, store._db() as c:
        for t in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_log", "ap_texts", "ap_dnc", "ap_phones",
                  "ap_owner_lane"):
            c.execute("DELETE FROM " + t)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%' OR key LIKE 'slack:once:%'")


def _register():
    """These kinds are added to the registry by the orchestrator. Until then the section adds them for
    itself, so the stages run through the real playbook loader and the real sweep, and puts the tables
    back afterwards."""
    from .autopilot import policy, registry
    saved = (dict(registry.EXTRA), policy.KINDS, dict(policy.SPEND_BY_KIND), policy.NO_PRESETS)
    for k, row in MINE.items():
        registry.EXTRA.setdefault(k, row)
    policy.KINDS = tuple(dict.fromkeys(policy.KINDS + tuple(MINE)))
    policy.SPEND_BY_KIND.update({k: r[2] for k, r in MINE.items()})
    policy.NO_PRESETS = tuple(dict.fromkeys(policy.NO_PRESETS + tuple(k for k, r in MINE.items() if not r[3])))
    return saved


def _restore(saved) -> None:
    from .autopilot import policy, registry
    registry.EXTRA.clear()
    registry.EXTRA.update(saved[0])
    policy.KINDS, policy.NO_PRESETS = saved[1], saved[3]
    policy.SPEND_BY_KIND.clear()
    policy.SPEND_BY_KIND.update(saved[2])


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import leadprep
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.ANTHROPIC_API_KEY, config.SLACK_VOICE)
    env_keep = os.environ.get("OPENWEBNINJA_API_KEY")
    hooks = (leadprep.ZILLOW, leadprep.ASK, leadprep.DOWNLOAD)
    config.DATA_DIR = Path(tmp) / "leadprep"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SLACK_VOICE = "plain"
    store.init()
    saved = _register()
    try:
        print("  the property check")
        _leadprep(check)
        print("  the primary phone")
        _primary(check)
        print("  the closer handoff text")
        _closer(check)
    finally:
        _restore(saved)
        leadprep.ZILLOW, leadprep.ASK, leadprep.DOWNLOAD = hooks
        config.DATA_DIR, config.DB_PATH, config.MODE, config.ANTHROPIC_API_KEY, config.SLACK_VOICE = keep
        if env_keep is None:
            os.environ.pop("OPENWEBNINJA_API_KEY", None)
        else:
            os.environ["OPENWEBNINJA_API_KEY"] = env_keep


# ====================================================================== the property check
def _leadprep(check) -> None:
    from .autopilot import activity, leadprep, ledger, policy, sweep
    _fresh()
    t = NOW - timedelta(minutes=30)
    recs = [_rec("lp1", "1 Lead Example St", status="new_lead"),
            _rec("lp2", "2 Lead Example St", status="Hot Lead"),
            _rec("lp3", "3 Lead Example St", status="new_lead", first="", last="", company="Sample Holdings LLC"),
            _rec("lp4", "4 Lead Example St", status="new_lead", tags=["Do Not Market"]),
            _rec("lp5", "5 Lead Example St", status="new_lead"),
            _rec("lp6", "6 Lead Example St", status="new_lead")]
    activity._store([status_ev(1, "lp1", t, "prospecting", "new_lead"), status_ev(2, "lp2", t, "Warm Lead", "Hot Lead"),
                     status_ev(3, "lp3", t, None, "new_lead"), status_ev(4, "lp4", t, "prospecting", "new_lead"),
                     status_ev(5, "lp5", t, "prospecting", "new_lead"), status_ev(6, "lp6", t, "prospecting", "new_lead")])
    table = {"1 Lead Example St": {"code": "ok", "calls": 1, "data": details()},
             "5 Lead Example St": {"code": "ok", "calls": 1, "data": on_market()},
             "6 Lead Example St": {"code": "none", "calls": 1, "data": {}}}
    zcalls, asked = [], []

    def zillow(where, key):
        zcalls.append(where)
        for k, v in table.items():
            if where.startswith(k):
                return copy.deepcopy(v)
        return {"code": "ok", "calls": 1, "data": details()}

    def ask(system, content, model, max_tokens):
        asked.append({"content": content, "model": model, "system": system})
        return (json.dumps({"condition": "needs a little updating",
                            "saw": "Dated kitchen %s carpet in the bedrooms, newer roof." % EM}),
                {"input_tokens": 2400, "output_tokens": 60}, "end_turn")

    leadprep.ZILLOW, leadprep.ASK = zillow, ask
    block = {"kind": "leadprep", "owner": "sales", "mode": "plan", "schedule": "every 10 min 09:00-18:00 Mon-Fri",
             "sync": False}
    crm = FakeCRM(recs)

    # --- plan mode: reads and gates, estimates, spends nothing
    out = _run("lp", _pol({"lp": block}), crm)
    check(out.head.startswith("Would check 3 new leads on Zillow") and out.would_usd > 0,
          "plan mode names the new leads it would check and what that would cost")
    check(zcalls == [] and asked == [] and crm.msgs == [] and out.usd == 0,
          "plan mode makes no Zillow call, reads no photo, writes no note and spends nothing")
    check("1 Lead Example St, Knoxville" in out.lines and not [ln for ln in out.lines if ln.startswith("2 Lead")],
          "a record moving between two lead statuses is not a new lead")

    # --- live mode: one paid check per new lead, one note each
    live_pol = _pol({"lp": dict(block, mode="live")})
    out = _run("lp", live_pol, crm, live=True)
    notes = {u: text for u, text in crm.msgs}
    check(len(zcalls) == 3, "a plan run never blocks the live run: the live run checks the same three leads")
    check(not [w for w in zcalls if w.startswith(("2 ", "3 ", "4 "))],
          "no Zillow call for a lead moving between lead statuses, a company or a record the gate holds")
    check(zcalls and zcalls[0] == "1 Lead Example St Knoxville TN 37917",
          "the details call is asked for the street, city, state and ZIP with spaces, the way property_enricher asks")
    check(sorted(notes) == ["lp1", "lp5", "lp6"], "one Property check note is written on each lead checked")
    n1 = notes.get("lp1", "")
    check(n1.startswith("Property check (Zillow, read Nov 17, 2026)"), "the note says where it came from and when")
    check("Listing: listed for sale Jun 3, 2026 at $189,000 with Robin Agent of Sample Realty, taken off the market "
          "Aug 20, 2026 after 78 days (expired or pulled)." in n1,
          "the note gives the last listing: when, the list price, the agent, how and when it ended, and how long it ran")
    check("may still hold a listing agreement" in n1, "a listing pulled in the last six months asks who they listed with")
    check("Last sold: Mar 2, 2009 for $82,000." in n1, "the note gives the last sale and its price")
    check("Condition from 4 listing photos: needs a little updating. Seen: Dated kitchen, carpet in the bedrooms, newer roof."
          in n1, "the condition is one of the three phrases, with what the photos showed")
    check("Ask the seller whether they still show the house as it is today" in n1,
          "the note asks whether the listing photos are out of date")
    check(all(EM not in v and EN not in v for v in notes.values()), "no note carries an em or en dash, even when the model wrote one")
    check(len(asked) == 1, "only a house with photos gets a photo read")
    imgs = [b for b in (asked[0]["content"] if asked else []) if b.get("type") == "image"]
    check(len(imgs) == 4 and all(b["source"]["type"] == "url" and b["source"]["url"].endswith("_768.jpg") for b in imgs),
          "at most max_photos photos are sent, by address, at the asked-for size")
    check(asked and asked[0]["model"] == "claude-haiku-5-5", "the photos are read by Claude Haiku 5.5 by default")
    check("Listing: for sale right now at $199,000 with Robin Agent of Sample Realty, 12 days on Zillow." in notes.get("lp5", "")
          and "not a target until the listing ends" in notes.get("lp5", ""),
          "a house listed with an agent right now is said to be no target")
    check("Photos: Zillow has none for this house. Ask the seller to text a few" in notes.get("lp5", ""),
          "a house with no photos says so and asks the seller for photos")
    check("Zillow has no record of this address" in notes.get("lp6", ""), "an address Zillow does not know gets an honest note")
    check(out.usd > 0 and ledger.spent(stage="lp") > 0, "what the checks cost is booked against the stage's day")
    check(out.head.startswith("Checked 3 new leads on Zillow: 2 listed in the last 6 months, 1 on the market now, "
                              "1 with the photos read"), "the live head counts the listings, the market and the photos")
    alert = next((a for a in out.alerts if "5 Lead Example St" in a), "")
    check("is for sale on Zillow right now at $199,000 with an agent" in alert,
          "a new lead whose house is for sale with an agent right now raises an alert")
    check(not [a for a in out.alerts if any(ch.isdigit() for ch in a.replace("5 Lead", "").replace("199,000", ""))],
          "an alert carries no phone number")
    posts = sweep.posts({"id": 9, "workflow": "lp", "source": "schedule"}, out.as_dict())
    check(any(p["kind"] == "pipeline" and "5 Lead Example St" in p["text"] for p in posts),
          "the alert goes to the pipeline channel")

    # --- a second run checks nobody twice; a lead back again inside the window is not paid for again
    _run("lp", live_pol, crm, live=True)
    check(len(zcalls) == 3, "a second run checks no lead twice")
    activity._store([status_ev(7, "lp1", NOW - timedelta(minutes=8), "new_lead", "not_interested"),
                     status_ev(8, "lp1", NOW - timedelta(minutes=2), "not_interested", "new_lead")])
    out = _run("lp", live_pol, crm, live=True)
    check(len(zcalls) == 3 and out.tally.get("checked within the last 90 days") == 1,
          "a house checked in the last recheck_days is not checked again when it comes back as a lead")
    activity._store([status_ev(9, "lp1", NOW - timedelta(minutes=1), "new_lead", "underwrite")])
    crm.recs["lp1"]["status"] = "underwrite"
    _run("lp_re", _pol({"lp_re": dict(block, mode="live", recheck_statuses=["underwrite"])}), crm, live=True)
    check(len(zcalls) == 4, "a move into a recheck status checks the house again, inside the recheck window too")
    crm.recs["lp1"]["status"] = "new_lead"

    # --- the run's cap: what does not fit waits for the next run
    activity._store([status_ev(10, "lp7", NOW - timedelta(minutes=5), "prospecting", "new_lead"),
                     status_ev(11, "lp8", NOW - timedelta(minutes=4), "prospecting", "new_lead")])
    crm.recs.update({r["uuid"]: r for r in (_rec("lp7", "7 Lead Example St", status="new_lead"),
                                             _rec("lp8", "8 Lead Example St", status="new_lead"))})
    cap_pol = _pol({"lp_cap": dict(block, mode="live", per_run=1)})
    before = len(zcalls)
    out = _run("lp_cap", cap_pol, crm, live=True)
    check(len(zcalls) == before + 1 and "waiting: the run's cap of 1 checks" in out.tally,
          "per_run caps the paid checks of one run")
    _run("lp_cap", cap_pol, crm, live=True)
    check(len(zcalls) == before + 2 and "lp8" in {u for u, _ in crm.msgs}, "the lead the cap held back is checked on the next run")

    # --- a refused key: said, nothing marked done, the lead is checked once the key works
    activity._store([status_ev(12, "lp9", NOW - timedelta(minutes=3), "prospecting", "new_lead")])
    crm.recs["lp9"] = _rec("lp9", "9 Lead Example St", status="new_lead")
    table["9 Lead Example St"] = {"code": "refused", "calls": 1, "data": {}, "detail": "HTTP 401"}
    out = _run("lp_cap", cap_pol, crm, live=True)
    check(any("Zillow refused the key" in p for p in out.problems) and "lp9" not in {u for u, _ in crm.msgs},
          "a refused Zillow key is a problem in words, and the lead is not marked done")
    table["9 Lead Example St"] = {"code": "ok", "calls": 1, "data": details(photos=0)}
    _run("lp_cap", cap_pol, crm, live=True)
    check("lp9" in {u for u, _ in crm.msgs}, "the lead a refused key held back is checked once the key works")

    # --- a note that fails: the paid result is kept, and the next run writes it without paying again
    activity._store([status_ev(13, "lp10", NOW - timedelta(minutes=3), "prospecting", "new_lead")])
    crm.recs["lp10"] = _rec("lp10", "10 Lead Example St", status="new_lead")
    crm.fail_message = True
    out = _run("lp_cap", cap_pol, crm, live=True)
    paid = len(zcalls)
    check(any("could not write a property check note" in p for p in out.problems), "a note that fails to write is said")
    crm.fail_message = False
    _run("lp_cap", cap_pol, crm, live=True)
    check(len(zcalls) == paid and "lp10" in {u for u, _ in crm.msgs},
          "the next run writes the failed note from the kept result, with no second Zillow call")

    # --- the stage's day cap refuses before anything is paid
    activity._store([status_ev(14, "lp11", NOW - timedelta(minutes=3), "prospecting", "new_lead")])
    crm.recs["lp11"] = _rec("lp11", "11 Lead Example St", status="new_lead")
    before = len(zcalls)
    out = _run("lp_budget", _pol({"lp_budget": dict(block, mode="live", daily_usd=0.001)}), crm, live=True)
    check(len(zcalls) == before and any(k.startswith("waiting: stage cap") for k in out.tally),
          "a check the day cap does not cover is not made: it waits")

    # --- no Zillow key: a problem in words, no crash, no record read
    leadprep.ZILLOW = None
    os.environ.pop("OPENWEBNINJA_API_KEY", None)
    bare = FakeCRM([_rec("lp11", "11 Lead Example St", status="new_lead")])
    out = _run("lp_budget", _pol({"lp_budget": dict(block, mode="live")}), bare, live=True)
    check(any("OPENWEBNINJA_API_KEY" in p for p in out.problems) and out.head.startswith("No property check ran")
          and bare.reads == 0, "with no Zillow key a live run says so and reads nothing")
    leadprep.ZILLOW = zillow

    # --- no Anthropic key: the listing is still checked, the photos are said not read. (A stage with no
    # per-run cap of 1: the lead the day cap held back above is older and is checked first.)
    more_pol = _pol({"lp_more": dict(block, mode="live")})
    activity._store([status_ev(15, "lp12", NOW - timedelta(minutes=3), "prospecting", "new_lead")])
    crm.recs["lp12"] = _rec("lp12", "12 Lead Example St", status="new_lead")
    leadprep.ASK = None
    config.ANTHROPIC_API_KEY = ""
    seen = len(asked)
    out = _run("lp_more", more_pol, crm, live=True)
    note12 = next((txt for u, txt in crm.msgs if u == "lp12"), "")
    check("not read this time (no Anthropic key on this box)" in note12 and len(asked) == seen
          and any("Anthropic key" in p for p in out.problems),
          "with no Anthropic key the listing is noted, the photos are said not read, and nothing crashes")
    leadprep.ASK = ask

    # --- a reply outside the scale is never passed on
    activity._store([status_ev(16, "lp13", NOW - timedelta(minutes=3), "prospecting", "new_lead")])
    crm.recs["lp13"] = _rec("lp13", "13 Lead Example St", status="new_lead")
    leadprep.ASK = lambda s, c, m, n: ("It looks pretty nice to me.", {"input_tokens": 900, "output_tokens": 9}, "end_turn")
    _run("lp_more", more_pol, crm, live=True)
    note13 = next((txt for u, txt in crm.msgs if u == "lp13"), "")
    check("not read this time (the photo read did not give one of the three phrases)" in note13,
          "a photo read that is not one of the three phrases is left out of the note")
    leadprep.ASK = ask

    # --- the photos, the listing history and the reply, piece by piece
    urls = ["u%d" % i for i in range(24)]
    check(leadprep.spread(urls, 4) == ["u0", "u8", "u15", "u23"] and leadprep.spread(urls[:3], 4) == urls[:3],
          "the photos read are the first one and the rest spread across the set")
    got, total, field = leadprep.photo_urls(details(photos=2), 4)
    check(total == 2 and field == "originalPhotos" and got[0].endswith("_768.jpg"),
          "the photo field is recorded, and each photo is taken at the size nearest the asked-for width")
    check(leadprep.photo_urls({"homeStatus": "OFF_MARKET"}, 4) == ([], 0, ""), "an answer with no photos gives none")
    today = NOW.astimezone(ET).date()
    pend = leadprep.listing_facts({"homeStatus": "PENDING", "priceHistory": [
        {"date": "2026-10-01", "event": "Listed for sale", "price": 210000},
        {"date": "2026-10-20", "event": "Pending sale", "price": 210000}]}, today, 6)
    check(pend["on_market"] and leadprep.listing_sentence(pend, 6) == "pending sale right now (listed Oct 1, 2026 at $210,000)."
          and "under contract" in leadprep.listing_advice(pend), "a pending sale is said, with what to ask the seller")
    rent = leadprep.listing_facts({"homeStatus": "OFF_MARKET", "priceHistory": [
        {"date": "2019-02-10", "event": "Listed for sale", "price": 120000},
        {"date": "2019-05-01", "event": "Sold", "price": 118000},
        {"date": "2026-09-01", "event": "Listed for rent", "price": 1450, "postingIsRental": True},
        {"date": "2026-09-30", "event": "Listing removed", "price": 1450}]}, today, 6)
    check(not rent["recent"] and rent["rent_recent"] and rent["how"] == "sold",
          "a removed rental listing closes the rental, not the old sale listing")
    check(leadprep.listing_sentence(rent, 6).startswith("not listed in the last 6 months (last listed for sale Feb 10, 2019"),
          "a sale listing older than the window is said to be outside it")
    fsbo = leadprep.listing_facts({"homeStatus": "FOR_SALE", "price": 150000, "listingSubType": {"isFSBO": True},
                                   "priceHistory": [{"date": "2026-11-01", "event": "Listed for sale", "price": 150000}]}, today, 6)
    check(fsbo["by_owner"] and leadprep._alert("x", {"listing": fsbo}) == "", "a house for sale by its owner raises no alert")
    auc = leadprep.listing_facts({"homeStatus": "FOR_SALE", "listingSubType": {"isForAuction": True}}, today, 6)
    check(auc["status"] == "auction" and not auc["on_market"], "an auction listing is not read as a listing with an agent")
    check(leadprep.parse_reply('{"condition": "Needs A Lot Of Work", "saw": "Water stains"}') == ("needs a lot of work", "Water stains")
          and leadprep.parse_reply('{"condition": "great"}')[0] == "", "only the three phrases or cannot tell are taken")

    # --- a photo the API cannot open by its address is downloaded and sent as data, once
    calls = []

    def fussy(system, content, model, max_tokens):
        calls.append([b["source"]["type"] for b in content if b.get("type") == "image"])
        if len(calls) == 1:
            raise leadprep.PhotoFetchError("could not download the image")
        return '{"condition": "excellent", "saw": "New kitchen."}', {"input_tokens": 700, "output_tokens": 20}, "end_turn"

    leadprep.ASK = fussy
    leadprep.DOWNLOAD = lambda url, n: (b"\xff\xd8\xff\xe0photo", "image/jpeg")
    r = leadprep.read_condition(["https://photos.example.test/a_768.jpg"], "claude-haiku-5-5")
    check(r["condition"] == "excellent" and calls == [["url"], ["base64"]],
          "a photo the API cannot fetch is downloaded and sent as data on the second try")
    leadprep.ASK, leadprep.DOWNLOAD = ask, None

    # --- the playbook loader refuses a bad block
    try:
        policy.parse({"business": {"tz": "America/New_York"}, "caps": {}, "people": {},
                      "stages": {"lp": dict(block, max_photos=50)}}, EMPLOYEES)
        said = ""
    except policy.PolicyError as exc:
        said = str(exc)
    check("max_photos must be a whole number from 0 to 10" in said, "the playbook check refuses a bad max_photos")


# ====================================================================== the primary phone
def _primary(check) -> None:
    from .autopilot import activity, ledger, policy, primaryphone, scrub
    _fresh()
    t = NOW - timedelta(minutes=20)
    recs = []

    def add(u, phones, primary=None, **kw):
        r = _rec(u, "%s Phone Example Rd" % u[2:], phones=phones, **kw)
        if primary:
            r["owner"]["primary_phone"] = "+1" + primary
        recs.append(r)

    add("pp1", [ph("8650000201"), ph("8650000202", "CORRECT")], primary="8650000201")
    add("pp2", [ph("8650000211"), ph("8650000212", "WRONG")], primary="8650000211")
    add("pp3", [ph("8650000221"), ph("8650000222", "CORRECT_DNC")])
    add("pp4", [ph("8650000231"), ph("8650000232", "CORRECT")])
    add("pp5", [ph("8650000241"), ph("8650000242", "CORRECT", tags=["Litigator"])])
    add("pp6", [ph("8650000251", "CORRECT")], primary="8650000251")
    add("pp7", [ph("8650000261", "CORRECT"), ph("8650000262", "CORRECT")], primary="8650000261")
    add("pp8", [ph("8650000271"), ph("8650000272", "CORRECT")], first="", last="", company="Sample Holdings LLC")
    add("pp9", [ph("8650000281"), ph("8650000282", "CORRECT")], tags=["Do Not Market"])
    add("pp10", [ph("8650000291"), ph("8650000292", "CORRECT")], status="not_interested")
    add("pp11", [ph("8650000301", "CORRECT"), ph("8650000302", "CORRECT")])
    scrub.note("8650000232", True)
    activity._store([phone_ev(1, "o-pp1", t, "8650000202", "UNKNOWN", "CORRECT"),
                     phone_ev(2, "o-pp2", t, "8650000212", "UNKNOWN", "CORRECT"),
                     phone_ev(3, "o-pp3", t, "8650000222", "UNKNOWN", "CORRECT_DNC"),
                     phone_ev(4, "o-pp4", t, "8650000232", "UNKNOWN", "CORRECT"),
                     phone_ev(5, "o-pp5", t, "8650000242", "UNKNOWN", "CORRECT"),
                     phone_ev(6, "o-pp6", t, "8650000251", "UNKNOWN", "CORRECT"),
                     phone_ev(7, "o-pp7", t, "8650000262", "UNKNOWN", "CORRECT"),
                     phone_ev(8, "o-pp8", t, "8650000272", "UNKNOWN", "CORRECT"),
                     phone_ev(9, "o-pp9", t, "8650000282", "UNKNOWN", "CORRECT"),
                     phone_ev(10, "o-pp10", t, "8650000292", "UNKNOWN", "CORRECT", source="internal-api"),
                     phone_ev(11, "o-pp11", t - timedelta(minutes=5), "8650000301", "UNKNOWN", "CORRECT"),
                     phone_ev(12, "o-pp11", t, "8650000302", "UNKNOWN", "CORRECT")])
    block = {"kind": "primaryphone", "owner": "sales", "mode": "plan", "schedule": "every 30 min 09:00-19:00 Mon-Fri",
             "sync": False}
    crm = FakeCRM(recs)
    out = _run("pp", _pol({"pp": block}), crm)
    owner_writes = lambda: [w for w in crm.writes if w[0] == "owner"]
    check(out.head == "Would set the primary phone on 3 owners to the number a caller just confirmed."
          and owner_writes() == [], "plan mode names the primaries it would set and writes none")
    live_pol = _pol({"pp": dict(block, mode="live")})
    out = _run("pp", live_pol, crm, live=True)
    got = {w[1]: w[2] for w in owner_writes()}
    check(got.get("o-pp1") == {"primary_phone": "+18650000202"},
          "a newly confirmed number becomes the primary, written exactly as the record stores it")
    check(got.get("o-pp10") == {"primary_phone": "+18650000292"},
          "a confirmed number on a record marked Not Interested is still made primary, and a person's own mark counts")
    check(got.get("o-pp11") == {"primary_phone": "+18650000302"}, "the newest confirmed number of an owner wins")
    check(len(got) == 3, "nothing else is written")
    check("o-pp2" not in got and out.tally.get("left alone: the number now reads WRONG") == 1,
          "a number marked CORRECT and then WRONG is never made primary")
    check("o-pp3" not in got, "a number marked do not call in any form is never made primary")
    check("o-pp4" not in got and out.tally.get("left alone: the number is on the do-not-call registry") == 1,
          "a number the registry scrub flagged is never made primary")
    check("o-pp5" not in got and out.tally.get("left alone: the number is a litigator's") == 1,
          "a litigator's number is never made primary")
    check("o-pp6" not in got and out.tally.get("left alone: it is already the primary") == 1,
          "a number that is already the primary is left alone")
    check("o-pp7" not in got and out.tally.get("left alone: the primary is already a confirmed number") == 1,
          "a primary that is already a confirmed number is never replaced")
    check("o-pp8" not in got, "a company or trust is left alone (people only)")
    check("o-pp9" not in got and any(k.startswith("held back: tagged Do Not Market") for k in out.tally),
          "a record the gate holds gets no primary phone")
    ch = [c for c in ledger.changes(record_uuid="pp1") if c["what"] == "primary_phone"]
    check(len(ch) == 1 and ch[0]["before"] == {"owner_uuid": "o-pp1", "primary_phone": "+18650000201"}
          and ch[0]["after"]["primary_phone"] == "+18650000202", "the primary it replaced is kept for undo")
    check(any("the number ending 0202 in place of the number ending 0201" in ln for ln in out.lines)
          and not [ln for ln in out.lines if "8650000" in ln], "the lines name numbers by their last four digits only")
    n = len(owner_writes())
    _run("pp", live_pol, crm, live=True)
    check(len(owner_writes()) == n, "a second run sets nothing twice")
    primaryphone.undo_step(crm, ch[0])
    check(crm.recs["pp1"]["owner"]["primary_phone"] == "+18650000201", "undo puts the old primary back")
    check(primaryphone.UNDOABLE == ("primary_phone",), "the stage names the change kind undo.py must route to it")
    check(primaryphone.refusal(ph("8650000999", "CORRECT_DNC"), "8650000999", None).startswith("the number now reads"),
          "the refusal reads the number's status as the record shows it now")

    # --- a write that does not stick stops the run
    _fresh()
    q = [_rec("pq1", "1 Stick Example Rd", phones=[ph("8650000311", "CORRECT")]),
         _rec("pq2", "2 Stick Example Rd", phones=[ph("8650000321", "CORRECT")])]
    activity._store([phone_ev(20, "o-pq1", t, "8650000311", "UNKNOWN", "CORRECT"),
                     phone_ev(21, "o-pq2", t, "8650000321", "UNKNOWN", "CORRECT")])
    flat = FakeCRM(q)
    flat.sticks = False
    out = _run("pp", live_pol, flat, live=True)
    check(any("did not stick" in p for p in out.problems) and len([w for w in flat.writes if w[0] == "owner"]) == 1,
          "the first write is read back, and one that did not stick stops the rest of the run")

    # --- sources: a stage limited to the dialer leaves a person's mark alone
    _fresh()
    one = FakeCRM([_rec("ps1", "1 Source Example Rd", phones=[ph("8650000331", "CORRECT")])])
    activity._store([phone_ev(30, "o-ps1", t, "8650000331", "UNKNOWN", "CORRECT", source="internal-api")])
    _run("pp_k", _pol({"pp_k": dict(block, mode="live", sources=["kixie"])}), one, live=True)
    check(not [w for w in one.writes if w[0] == "owner"], "sources limits which writers' marks are acted on")
    try:
        policy.parse({"business": {"tz": "America/New_York"}, "caps": {}, "people": {},
                      "stages": {"pp": dict(block, allow=["everything"])}}, EMPLOYEES)
        said = ""
    except policy.PolicyError as exc:
        said = str(exc)
    check("allow has" in said, "the playbook check refuses an allow word the gate does not know")


# ====================================================================== the closer handoff text
def _closer(check) -> None:
    from .autopilot import activity, handoff, ledger, limits, policy, sweep
    _fresh()
    moved = NOW - timedelta(minutes=5)
    recs = []

    def add(u, number, status="CORRECT", **kw):
        r = _rec(u, "%s Close Example St" % u[2:], status="underwrite", phones=[ph(number, status)], **kw)
        recs.append(r)
        return r

    add("cl1", "8650000401")
    add("cl2", "8650000411")
    add("cl3", "8650000421", status="WRONG")
    add("cl4", "8650000431")["row_phone"] = {"number": "8650000431", "doNotCall": True}
    add("cl5", "8650000441", deceased=True)
    add("cl6", "8650000451")
    add("cl7", "8650000461")
    rows = [status_ev(40 + i, u, moved, "Hot Lead", "underwrite") for i, u in enumerate(("cl1", "cl2", "cl3", "cl4", "cl5", "cl7"))]
    rows.append(status_ev(49, "cl6", NOW - timedelta(hours=3), "Hot Lead", "underwrite"))
    rows += [answered(1, "o-cl1", NOW - timedelta(days=2), "8650000401"),
             answered(2, "o-cl2", NOW - timedelta(days=1), "8650000411"),
             answered(3, "o-cl3", NOW - timedelta(days=1), "8650000421"),
             answered(4, "o-cl4", NOW - timedelta(days=1), "8650000431"),
             answered(5, "o-cl1", NOW - timedelta(days=1), "8650000401", caller="Quentin"),
             answered(6, "o-cl2", NOW - timedelta(hours=3), "8650000411", duration=12)]
    activity._store(rows)
    block = {"kind": "closertext", "owner": "sales", "mode": "plan", "schedule": "every 5 min 09:00-18:00 Mon-Fri",
             "closer": "Quentin", "closer_line": "8650000999", "lead_manager": "Robin", "sync": False}
    crm = FakeCRM(recs)

    check(handoff.audit() == [], "every handoff text variant passes its own copy check")
    bad = handoff.copy_problems("Hi {first}, it's {closer}. Did {teammate} call about {addr}%s yes?" % EM, named=True)
    check(any("question" in p for p in bad) and any("dash" in p for p in bad),
          "the copy check refuses a question and an em dash")
    check(any("teammate" in p for p in handoff.copy_problems("Hi, this is {closer} about {addr}.", named=False)),
          "the copy check refuses a text that does not name the teammate")

    # --- plan mode: says what it would stage, stages nothing, pings the closer once
    out = _run("cl", _pol({"cl": block}), crm)
    check(out.head == "Would stage 3 handoff texts for a lead that moved to acquisitions.",
          "plan mode says how many handoff texts it would stage")
    check(ledger.batches(stage="cl") == [], "plan mode stages no batch")
    a1 = next((a for a in out.alerts if a.startswith("1 Close Example St")), "")
    check(a1 == "1 Close Example St moved to acquisitions. Quentin, call the seller in the next few minutes (Dana spoke with them).",
          "the closer is pinged once with the house and the teammate who spoke with the seller")
    check(not [a for a in out.alerts if "8650000" in a.replace("-", "")], "a ping never carries a phone number")
    check(any(a.startswith("3 Close Example St") and "No handoff text: no usable number for a text" in a for a in out.alerts),
          "a lead whose only mobile is marked WRONG gets the ping and no text")
    check(any(a.startswith("4 Close Example St") and "No handoff text" in a for a in out.alerts),
          "a number the registry scrub flagged gets no text")
    check(not [a for a in out.alerts if a.startswith(("5 Close", "6 Close"))],
          "no ping for an owner who died, nor for a move too old to promise a call")
    check(any(k.startswith("no text: moved more than 60 minutes ago") for k in out.tally),
          "a move older than good_for_minutes gets no text")

    # --- live mode: one batch, staged for a person, nothing sent
    live_pol = _pol({"cl": dict(block, mode="live")})
    out = _run("cl", live_pol, crm, live=True)
    staged = ledger.batches(status="staged", stage="cl")
    check(len(staged) == 1 and staged[0]["kind"] == "closer_text" and staged[0]["count"] == 3,
          "a live run stages one batch of handoff texts for a person to release")
    b = ledger.batch(staged[0]["id"]) if staged else {"items": [], "id": 0}
    items = {it["record_uuid"]: it for it in b["items"]}
    it1 = items.get("cl1", {})
    check(it1.get("number") == "8650000401" and it1.get("closer_line") == "8650000999",
          "the text goes to the confirmed mobile, from the closer's own line")
    text = it1.get("text", "")
    check(text.startswith("Hi Pat,") and "Quentin" in text and "Dana" in text and "1 Close Example St" in text
          and "few minutes" in text, "the text greets the seller, names the closer and the teammate, the street and the time")
    check("?" not in text and EM not in text and EN not in text and len(text) <= handoff.CAP,
          "the text asks nothing, carries no dash and fits two segments")
    check(items.get("cl7", {}).get("teammate") == "Robin",
          "with no conversation in the log the lead manager the stage names is the teammate")
    check(items.get("cl2", {}).get("teammate") == "Dana", "a call too short to be a conversation does not name the teammate")
    check(it1.get("teammate") == "Dana", "the closer's own call never makes the closer the teammate")
    rows_staged = limits.rows(batch_id=b["id"], statuses=("staged",))
    check(len(rows_staged) == 3, "each staged text holds the owner's place in the text ledger")
    check(not handoff.load_path(b["id"]).exists() and not [w for w in crm.writes if w[0] == "sms_attempts"],
          "staging sends nothing and writes nothing to the record")
    check(any("batch %d" % b["id"] in a for a in out.alerts), "the live ping names the batch to release")
    posts = sweep.posts({"id": 11, "workflow": "cl", "source": "schedule"}, out.as_dict())
    check(any(p["kind"] == "pipeline" and "release" in p["text"] for p in posts), "the ping goes to the pipeline channel")

    # --- a second move of the same lead inside once_days is not texted again
    activity._store([status_ev(60, "cl1", NOW - timedelta(minutes=2), "underwrite", "Hot Lead"),
                     status_ev(61, "cl1", NOW - timedelta(minutes=1), "Hot Lead", "underwrite")])
    out = _run("cl", live_pol, crm, live=True)
    check(len(ledger.batches(stage="cl")) == 1 and any(k.startswith("no text: a handoff text was already staged") for k in out.tally),
          "a lead moved back and forth is texted once")

    # --- the release: re-checked, then handed over as a load file, never sent from here
    dry = handoff.release(b["id"], crm=crm, pol=live_pol, now=NOW + timedelta(minutes=2))
    check(dry.get("dry") and dry.get("sent") == 3 and not handoff.load_path(b["id"]).exists(),
          "a dry release says what would go and writes nothing")
    crm.recs["cl2"]["owner"]["opt_out"] = True
    crm.recs["cl7"]["owner"]["phones"][0]["status"] = "WRONG"
    rel = handoff.release(b["id"], crm=crm, pol=live_pol, commit=True, now=NOW + timedelta(minutes=2))
    check(rel.get("sent") == 1 and rel.get("held", {}).get("the owner opted out (opt-out flag)") == 1,
          "an owner who opted out after staging is held back at release")
    check(any("no usable number" in k or "no longer one a text may use" in k for k in rel.get("held", {})),
          "a number marked WRONG after staging is held back at release")
    load = json.loads(handoff.load_path(b["id"]).read_text(encoding="utf-8")) if handoff.load_path(b["id"]).exists() else {}
    check(load.get("from") == "8650000999" and [c["phone"] for c in load.get("contacts", [])] == ["8650000401"],
          "the release writes a load file for the texting side, from the closer's line, with only what passed")
    check(ledger.batch(b["id"])["status"] == "sent" and "Handed over, not sent" in rel.get("next", "")
          and text in rel.get("next", ""), "the release hands the load over and shows the text; it sends nothing itself")
    check(len(limits.rows(batch_id=b["id"], statuses=("released",))) == 1,
          "only the released text counts in the text ledger")

    # --- too late: a text staged and not released inside its hour is never released
    activity._store([status_ev(62, "cl8", NOW - timedelta(minutes=2), "Hot Lead", "underwrite"),
                     answered(7, "o-cl8", NOW - timedelta(days=1), "8650000471")])
    crm.recs["cl8"] = _rec("cl8", "8 Close Example St", status="underwrite", phones=[ph("8650000471", "CORRECT")])
    _run("cl", live_pol, crm, live=True)
    late = [x for x in ledger.batches(status="staged", stage="cl")]
    rel = handoff.release(late[0]["id"], crm=crm, pol=live_pol, commit=True, now=NOW + timedelta(hours=2)) if late else {}
    check(rel.get("sent") == 0 and any(k.startswith("too late") for k in rel.get("held", {})),
          "a release after the text's hour holds it back: the promise of a call would not be true")
    activity._store([status_ev(63, "cl9", NOW - timedelta(minutes=2), "Hot Lead", "underwrite"),
                     answered(8, "o-cl9", NOW - timedelta(days=1), "8650000481")])
    crm.recs["cl9"] = _rec("cl9", "9 Close Example St", status="underwrite", phones=[ph("8650000481", "CORRECT")])
    _run("cl", live_pol, crm, live=True)
    left = ledger.batches(status="staged", stage="cl")
    out = _run("cl", live_pol, crm, now=NOW + timedelta(hours=2), live=True)
    check(left and ledger.batch(left[0]["id"])["status"] == "expired" and any("was not released in time" in p for p in out.problems),
          "a batch nobody released inside its hour is closed by the next run, and that is said")

    # --- the closer not named: a problem, no text, the ping still goes
    activity._store([status_ev(64, "cl10", NOW - timedelta(minutes=2), "Hot Lead", "underwrite"),
                     answered(9, "o-cl10", NOW - timedelta(days=1), "8650000491")])
    crm.recs["cl10"] = _rec("cl10", "10 Close Example St", status="underwrite", phones=[ph("8650000491", "CORRECT")])
    out = _run("cl_x", _pol({"cl_x": dict(block, mode="live", closer="", closer_line="")}), crm, live=True)
    check(any("the closer is not named" in p for p in out.problems) and not ledger.batches(stage="cl_x")
          and any("No handoff text: the closer is not named" in a for a in out.alerts),
          "with no closer named nothing is staged, and the lead is still pinged")

    # --- outside the sending hours: the ping, no text
    late_now = datetime(2026, 11, 17, 18, 30, tzinfo=ET).astimezone(timezone.utc)
    activity._store([status_ev(65, "cl11", late_now - timedelta(minutes=2), "Hot Lead", "underwrite"),
                     answered(10, "o-cl11", late_now - timedelta(days=1), "8650000501")])
    crm.recs["cl11"] = _rec("cl11", "11 Close Example St", status="underwrite", phones=[ph("8650000501", "CORRECT")])
    out = _run("cl_h", _pol({"cl_h": dict(block, mode="live")}), crm, now=late_now, live=True)
    check(not ledger.batches(stage="cl_h") and any("No handoff text" in a and "outside" in a for a in out.alerts),
          "a move outside the sending hours gets the ping and no text")
    try:
        policy.parse({"business": {"tz": "America/New_York"}, "caps": {}, "people": {},
                      "stages": {"cl": dict(block, closer_line="not a number")}}, EMPLOYEES)
        said = ""
    except policy.PolicyError as exc:
        said = str(exc)
    check("closer_line must be the closer's ten-digit number" in said, "the playbook check refuses a closer_line that is no number")

    # --- a card moved into a handoff column counts, though that column sets no status
    _fresh()
    crm.recs["cl12"] = _rec("cl12", "12 Close Example St", status="Hot Lead", phones=[ph("8650000511", "CORRECT")])
    crm.recs["cl13"] = _rec("cl13", "13 Close Example St", status="Hot Lead", phones=[ph("8650000521", "CORRECT")])
    activity._store([card_ev(1, "card-12", "cl12", NOW - timedelta(minutes=3), "col-hot", "col-send-acq"),
                     card_ev(2, "card-13", "cl13", NOW - timedelta(minutes=3), "col-hot", "col-warm"),
                     answered(11, "o-cl12", NOW - timedelta(days=1), "8650000511"),
                     answered(12, "o-cl13", NOW - timedelta(days=1), "8650000521")])
    col_block = dict(block, columns=["col-send-acq"])
    out = _run("cl_col", _pol({"cl_col": col_block}), crm)
    check(out.head == "Would stage 1 handoff text for a lead that moved to acquisitions."
          and any(a.startswith("12 Close Example St moved to acquisitions") for a in out.alerts),
          "a card moved into a handoff column is a handoff, though that column sets no status")
    check(not [a for a in out.alerts if a.startswith("13 Close")], "a card moved into any other column is not a handoff")
    col_live = _pol({"cl_col": dict(col_block, mode="live")})
    _run("cl_col", col_live, crm, live=True)
    cb = ledger.batches(status="staged", stage="cl_col")
    item = ledger.batch(cb[0]["id"])["items"][0] if cb else {}
    check(item.get("via") == "card" and item.get("record_uuid") == "cl12", "the staged text remembers it came from a card move")
    rel = handoff.release(cb[0]["id"], crm=crm, pol=col_live, now=NOW + timedelta(minutes=1)) if cb else {}
    check(rel.get("sent") == 1, "a card-move handoff passes the release check while the record is still a lead")
    crm.recs["cl12"]["status"] = "not_interested"
    rel = handoff.release(cb[0]["id"], crm=crm, pol=col_live, now=NOW + timedelta(minutes=1)) if cb else {}
    check(rel.get("sent") == 0, "a card-move handoff whose record is no longer a lead is held back at release")


if __name__ == "__main__":
    raise SystemExit("run through: python src/agents/cli.py selftest")
