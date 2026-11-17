"""Mail counted from a date (Ty, 2026-11-16): the auction countdown and the probate table, their timing
rules by in-home day, the gate, the probate house check, one campaign per record, the SiftMail send
date and floor, the piece that cannot be ordered, the batch shape mail.release releases, and the
checks at release. Zero network, its own database. Every name, street, id and number is invented;
numbers use the never-assigned 865-000 exchange.
"""
from __future__ import annotations

import copy
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, FakeCRM, _rec

TZ = "America/New_York"
NOW = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)      # Tuesday 10:00 Eastern
SENDER = {"name": "Pat", "street": "1 Return Rd", "city": "Knoxville", "state": "TN", "zip": "37917", "phone": "8650000100"}
AUCTION_PIECES = [
    {"id": "A1", "piece": "handwritten_letter", "product_id": 16, "envelope": 16, "design": "tpl-a1"},
    {"id": "A2", "piece": "options_letter", "product_id": 5, "envelope": 5, "design": "tpl-a2"},
    {"id": "A3", "piece": "snap_pack", "product_id": 18, "design": "tpl-a3"},
    {"id": "A4", "piece": "postcard", "product_id": 14, "design": "tpl-a4"},
    {"id": "A5", "piece": "closing_letter", "product_id": 1, "envelope": 1, "design": "tpl-a5"},
    {"id": "A6", "piece": "last_note", "product_id": 5, "envelope": 7, "design": "tpl-a6"},
]
AUCTION_PRICES = {"handwritten_letter": 1.89, "options_letter": 1.23, "snap_pack": 1.28, "postcard": 0.74,
                  "closing_letter": 1.10, "last_note": 1.23}
PROBATE_PIECES = [
    {"id": "PB1", "piece": "letter", "product_id": 5, "envelope": 5, "design": "tpl-p1"},
    {"id": "PB2", "piece": "handwritten_letter", "product_id": 16, "envelope": 16, "design": "tpl-p2"},
    {"id": "PB3", "piece": "options_letter", "product_id": 3, "envelope": 3, "design": "tpl-p3"},
    {"id": "PB5", "piece": "letter", "product_id": 5, "envelope": 7, "design": "tpl-p5"},
    {"id": "PB6", "piece": "handwritten_letter", "product_id": 16, "envelope": 17, "design": "tpl-p6"},
    {"id": "PB7", "piece": "steps_letter", "product_id": 1, "envelope": 1, "design": "tpl-p7"},
    {"id": "PB8", "piece": "handwritten_letter", "product_id": 16, "envelope": 16, "design": "tpl-p8"},
    {"id": "PB9", "piece": "letter", "product_id": 5, "envelope": 5, "design": "tpl-p9"},
    {"id": "EVG_T2", "piece": "family_postcard", "product_id": 14, "design": "tpl-t2"},
    {"id": "EVG_T4", "piece": "postcard", "product_id": 14, "design": "tpl-t4"},
]
STEPS = [{"piece": "PB1", "at": "on_entry"}, {"piece": "PB2", "after_days": 14}, {"piece": "PB3", "after_days": 14},
         {"piece": "EVG_T4", "after_days": 30}, {"piece": "PB5", "after_days": 30}, {"piece": "PB6", "after_days": 30},
         {"piece": "PB7", "after_days": 30}, {"piece": "EVG_T2", "after_days": 30}, {"piece": "PB8", "after_days": 30},
         {"piece": "PB9", "after_days": 30}, {"piece": "EVG_T4", "after_days": 30}, {"piece": "PB8", "after_days": 30},
         {"piece": "PB9", "after_days": 30}, {"piece": "EVG_T2", "after_days": 30}]
FILL = {"first": "A1", "middle": ["A2", "A3", "A4"], "last": ["A5", "A6"]}


def playbook(audit_csv: Path) -> dict:
    auction = {"kind": "mailcadence", "owner": "ops", "mode": "live", "schedule": "09:45 Mon-Fri", "lane": "ftm",
               "anchor": "sale_date", "vendor": "siftmail", "internal_routes": True, "send": "staged",
               "scope": {"lists": ["Foreclosure"], "tags": ["FTM"]},
               "clock": {"anchor_field": "Foreclosure Date", "refresh_days": 0, "fill": dict(FILL),
                         "after_sale": {"grace_days": 14, "hand_to": "ftm_mail", "start_at_step": 2}},
               "min_batch": 1, "daily_usd": 40, "prices": dict(AUCTION_PRICES), "pieces": copy.deepcopy(AUCTION_PIECES),
               "sender": dict(SENDER)}
    probate = {"kind": "mailcadence", "owner": "ops", "mode": "live", "schedule": "10:15 Mon-Fri", "lane": "ftm",
               "anchor": "filed_date", "vendor": "siftmail", "internal_routes": True, "send": "staged",
               "scope": {"lists": ["Probate"], "tags": ["FTM"], "not_tags": ["Wrong House", "Estate Sold"]},
               "require": "decedent_house", "audit_csv": str(audit_csv),
               "clock": {"anchor_field": "Probate Open Date", "min_gap_days": 14, "steps": copy.deepcopy(STEPS),
                         "after_month_12": {"every_days": 75, "rotate": ["EVG_T4", "PB8", "EVG_T2", "PB9"], "until_month": 24}},
               "min_batch": 1, "daily_usd": 40, "prices": {"options_letter": 1.22, "steps_letter": 1.10},
               "pieces": copy.deepcopy(PROBATE_PIECES), "sender": dict(SENDER)}
    monthly = {"kind": "mail", "owner": "ops", "mode": "live", "schedule": "10:00 Mon-Fri", "vendor": "csv", "lane": "ftm",
               "pieces": [{"preset": "FTM - Mail 01 Handwritten Letter", "piece": "handwritten_letter"}]}
    return {"business": {"tz": TZ, "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
            "caps": {"daily_usd": 25, "record_usd": 1.0, "outreach_daily_usd": 500},
            "people": {"callers": ["Emerson"]}, "gate": {"read_fields": True}, "lanes": {"ftm": {"tags": ["FTM"]}},
            "stages": {"auction_mail": auction, "probate_mail": probate, "ftm_mail": monthly}}


class CRM(FakeCRM):
    """The autopilot selftest's fake DataSift, plus each launched campaign's touch (the send date)."""

    def __init__(self, records, custom=None):
        super().__init__(records)
        self.custom = dict(custom or {})
        self.rec_logs = {}
        self.campaigns = []

    def mail_campaign(self, uuids, title, touches, estimate, campaign_type="single-touch"):
        if not estimate:
            self.campaigns.append({"uuids": tuple(uuids), "touch": dict(touches[0])})
        return super().mail_campaign(uuids, title, touches, estimate, campaign_type)


def _sale(u, street, sale, **kw):
    r = _rec(u, street, **kw)
    r["lists"] = ["Foreclosure"]
    return r, {"Foreclosure Date": sale}


def _estate(u, street, opened, tags=(), **kw):
    r = _rec(u, street, **kw)
    r["tags"] = ["FTM", "Courthouse Data", "Probate"] + list(tags)
    r["lists"] = ["Probate"]
    if opened:
        r["probate_open_date"] = opened
    return r


def _at(y, m, d, hour=15):
    return datetime(y, m, d, hour, 0, tzinfo=timezone.utc)


def _register():
    """The orchestrator adds the registry line; until then the kind is registered for this section only."""
    from .autopilot import policy, registry
    if "mailcadence" in registry.EXTRA:
        return None
    saved = (policy.KINDS, policy.NO_PRESETS)
    registry.EXTRA["mailcadence"] = ("mailcadence", "run_mailcadence", "outreach", False, False)
    policy.KINDS = policy.KINDS + ("mailcadence",)
    policy.NO_PRESETS = policy.NO_PRESETS + ("mailcadence",)
    policy.SPEND_BY_KIND["mailcadence"] = "outreach"
    return saved


def _unregister(saved) -> None:
    if saved is None:
        return
    from .autopilot import policy, registry
    registry.EXTRA.pop("mailcadence", None)
    policy.KINDS, policy.NO_PRESETS = saved
    policy.SPEND_BY_KIND.pop("mailcadence", None)


def _clean() -> None:
    from . import store
    with store._LOCK, store._db() as c:
        for table in ("ap_events", "ap_facts", "ap_changes", "ap_batches", "ap_owner_lane", "ap_phones"):
            c.execute("DELETE FROM " + table)
        c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:%'")


def _write_audit(path: Path) -> None:
    rows = [("p20", "OK", ""), ("p20b", "OK", ""), ("p24", "OK", ""), ("p70", "OK", ""),
            ("p400", "SURNAME_ONLY", "Likely fine: name variant"),
            ("punc", "PR_OWN_HOME", "Wrong house: the PR's own home"), ("pinh", "DIFFERENT_OWNER", "Work it: PR owns it and lives elsewhere"),
            ("pnodec", "NO_DECEDENT", ""), ("pkeep", "PR_OWN_HOME", "Wrong house: the PR's own home")]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["verdict", "suggested", "street", "uuid"])
        for u, verdict, suggested in rows:
            w.writerow([verdict, suggested, "", u])


def run(check, tmp: Path) -> None:
    from . import store
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE)
    config.DATA_DIR = Path(tmp) / "mailcadence"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    saved = _register()
    try:
        _checks(check, config.DATA_DIR)
    finally:
        _unregister(saved)
        config.DATA_DIR, config.DB_PATH, config.MODE = keep


def _checks(check, tmp: Path) -> None:
    from .autopilot import ledger, mail, mailcadence as mc, policy
    from .autopilot import stages, sweep
    audit_csv = tmp / "probate_address_audit_test.csv"
    _write_audit(audit_csv)
    pol = policy.parse(playbook(audit_csv), EMPLOYEES)
    config.MODE = "live"

    # ------------------------------------------------------------------ the send date and the in-home day
    print("  send date")
    sends = {(2026, 11, 16, 10): date(2026, 11, 18), (2026, 11, 16, 23): date(2026, 11, 19), (2026, 11, 20, 10): date(2026, 11, 24),
             (2026, 11, 20, 23): date(2026, 11, 24), (2026, 11, 21, 23): date(2026, 11, 24), (2026, 11, 22, 23): date(2026, 11, 25),
             (2026, 11, 19, 22): date(2026, 11, 24)}
    got = {k: mc.next_send_date(_at(*k)) for k in sends}
    check(got == sends, "SiftMail's send date: a weekday two business days out, three after 22:00 UTC except on a Friday or "
                        "Saturday (%r)" % {k: v for k, v in got.items() if v != sends[k]})
    check(all(d.weekday() < 5 for d in got.values()), "a send date is never a Saturday or a Sunday")
    check(mc.in_home(NOW, TZ, 7, 3) == date(2026, 11, 24),
          "a piece staged Tuesday lands about a week later: the later of the send date plus three days and today plus seven")

    # ------------------------------------------------------------------ the countdown, by in-home day
    print("  countdown")
    o = mc.options(pol.stage("auction_mail"))
    designs = mc.designs_of(pol.stage("auction_mail"))
    c = mc.countdown_of(o, designs)
    every = {d.id: d for d in designs}

    def walk(lead, avail=None, sale=date(2026, 12, 31), sent=None, rerun=False):
        avail = every if avail is None else avail
        out = list(sent or [])
        for day in range(lead):
            land = sale - timedelta(days=lead - 7 - day)
            dec = mc.countdown_next(out, sale, land, c, avail, rerun)
            if dec.design:
                out.append((dec.design.id, land))
            if dec.final:
                break
        return [(i, (sale - d).days) for i, d in out]

    s34, s27, s20 = walk(34), walk(27), walk(20)
    check([i for i, _ in s34] == ["A1", "A2", "A3", "A4", "A5", "A6"] and [i for i, _ in s27] == ["A1", "A2", "A3", "A5", "A6"]
          and [i for i, _ in s20] == ["A1", "A2", "A5", "A6"],
          "the SPEC's leads: 34 days gets all six, 27 drops A4, 20 keeps A1 and A2 and the closing pair (%r %r %r)" % (s34, s27, s20))
    check(s34[0][1] == 27 and s27[0][1] == 20 and s20[0][1] == 13,
          "the first piece goes the day the record comes in, and lands about 7 days later")
    check(all(d >= 2 for _, d in s34 + s27 + s20 + walk(47) + walk(12) + walk(9)),
          "nothing lands later than 2 days before the sale, whatever the lead")
    gaps_out = [a[1] - b[1] for a, b in zip(s34, s34[1:]) if b[1] > 10]
    gaps_in = [a[1] - b[1] for a, b in zip(s34, s34[1:]) if b[1] <= 10]
    check(gaps_out and all(5 <= g <= 7 for g in gaps_out) and gaps_in and all(3 <= g <= 5 for g in gaps_in),
          "5 to 7 days apart while more than 10 days out, 3 to 4 inside the last ten (the first one in may be 5) (%r %r)" % (gaps_out, gaps_in))
    s47 = walk(47)
    check([i for i, _ in s47] == ["A1", "A2", "A3", "A4", "A5", "A6"] and [d for _, d in s47][-3:] == [10, 7, 4],
          "a long lead never runs past six pieces, and the earliest slots stay empty so the last ten days keep their cadence (%r)" % s47)
    check(walk(8) == [] and [i for i, _ in walk(12)] == ["A1", "A6"],
          "too close for any piece to land in time gets nothing; a short lead gets the opener and the last note")
    check(len({i for i, _ in s34}) == len(s34), "each piece of a countdown is a different one")
    no_snap = {k: v for k, v in every.items() if k != "A3"}
    s34n = walk(34, no_snap)
    check("A3" not in [i for i, _ in s34n] and [i for i, _ in s34n] == ["A1", "A2", "A4", "A5", "A6"],
          "a piece that cannot be ordered is left out and its slot goes to the next one (%r)" % s34n)
    sale0 = date(2026, 12, 31)
    done = [(x, sale0 - timedelta(days=d)) for x, d in s34]
    new_sale = sale0 + timedelta(days=28)
    again = mc.countdown_next(done, new_sale, new_sale - timedelta(days=7), c, every, True)
    later = mc.countdown_next(done, new_sale, new_sale - timedelta(days=7), c, every, False)
    check(again.design and again.design.id == "A5" and later.design is None and later.final,
          "a postponed sale with every piece gone runs the closing pair once more, starting with A5; without the move, nothing")
    twice = done + [("A5", new_sale - timedelta(days=7)), ("A6", new_sale - timedelta(days=4))]
    check(mc.countdown_next(twice, new_sale + timedelta(days=30), new_sale + timedelta(days=20), c, every, False).design is None,
          "the closing pair runs at most once more")
    half = [(x, sale0 - timedelta(days=d)) for x, d in s34[:2]]
    moved = mc.countdown_next(half, new_sale, new_sale - timedelta(days=10), c, every, False)
    check(moved.design is not None and moved.design.id == "A3",
          "a moved sale re-times the pieces left: the next one waits for the new date's slots, in order")
    with_fb = copy.deepcopy(AUCTION_PIECES) + [{"id": "A3L", "piece": "closing_letter", "product_id": 1, "envelope": 1, "design": "tpl-x"}]
    with_fb[2]["fallback"] = "A3L"
    st_fb = copy.deepcopy(playbook(audit_csv)["stages"]["auction_mail"])
    st_fb["pieces"] = with_fb
    out_fb = stages.Summary("t", "mailcadence", "ops", "plan", False)
    av_fb = mc.usable(mc.designs_of(st_fb), mc.options(st_fb), mail.vendor_for(policy.Stage("t", "mailcadence", "ops", "plan", "", [], [], st_fb)), out_fb)
    check(av_fb["A3"].id == "A3L" and any("Its fallback A3L goes in its place" in p for p in out_fb.problems),
          "a piece that cannot be ordered is replaced by the fallback it names, and the run says so")
    out_np = stages.Summary("t", "mailcadence", "ops", "plan", False)
    av_np = mc.usable(designs, o, mail.vendor_for(pol.stage("auction_mail")), out_np)
    check("A3" not in av_np and any(p.startswith("Piece A3 cannot be ordered") for p in out_np.problems),
          "the Snap Pack (product 18) has no price row in this account: refused with a plain problem")
    st_priced = dict(pol.stage("auction_mail").params, mail_type_prices={18: 1.28})
    check("A3" in mc.usable(designs, mc.options(st_priced), mail.vendor_for(pol.stage("auction_mail"))),
          "once the price row exists and is listed under mail_type_prices, the Snap Pack can be ordered")
    check(set(mc.usable(designs, o, mail.Vendor())) == set(every),
          "an outside vendor's file is not held to SiftMail's price table")

    # ------------------------------------------------------------------ the probate table
    print("  probate table")
    t = mc.table_of(mc.options(pol.stage("probate_mail")))
    pav = {d.id: d for d in mc.designs_of(pol.stage("probate_mail"))}
    opened = date(2026, 1, 5)
    first = mc.table_next([], opened, opened + timedelta(days=7), t, pav)
    check(first.design and first.design.id == "PB1", "the first probate piece goes the week the estate opens")
    seq, day = [], opened + timedelta(days=7)
    while day <= opened + timedelta(days=800):
        dec = mc.table_next(seq, opened, day, t, pav)
        if dec.design:
            seq.append((dec.design.id, day))
        day += timedelta(days=1)
    ids = [i for i, _ in seq]
    ages = [(d - opened).days for _, d in seq]
    check(ids[:14] == [s["piece"] for s in STEPS], "the table runs in its order through month 12 (%r)" % ids[:14])
    check(ages[1] - ages[0] == 14 and ages[2] - ages[1] == 14 and all(b - a == 30 for a, b in zip(ages[2:13], ages[3:14])),
          "two more about two weeks apart, then monthly (%r)" % ages[:14])
    tail = seq[14:]
    check(tail and all((b[1] - a[1]).days >= 75 for a, b in zip(seq[13:], seq[14:])) and all((d - opened).days <= 730 for _, d in tail),
          "after month 12 one piece about every 75 days, and none after month 24 (%r)" % [((d - opened).days, i) for i, d in tail])
    check(all(a[0] != b[0] for a, b in zip(seq, seq[1:])), "never the same probate piece twice in a row")
    check(all((b[1] - a[1]).days >= 14 for a, b in zip(seq, seq[1:])), "never two probate pieces closer than 14 days")
    check(mc.table_next(seq, opened, opened + timedelta(days=740), t, pav).final, "the probate campaign ends at month 24")
    late = mc.table_next([], date(2025, 9, 1), date(2026, 11, 24), t, pav)
    check(late.design and late.design.id == "EVG_T4", "a record that comes in after month 12 starts on the rotation")
    near = mc.table_next([("PB1", date(2026, 11, 14))], date(2026, 11, 10), date(2026, 11, 24), t, pav)
    check(near.design is None and "14 days" in near.why, "a probate piece never lands within 14 days of the one before")

    # ------------------------------------------------------------------ the playbook block
    print("  block")
    check(mc.block_problems(pol.stage("auction_mail")) == [] and mc.block_problems(pol.stage("probate_mail")) == [],
          "the SPEC's two blocks load clean")

    def bad(stage, **change):
        st = copy.deepcopy(playbook(audit_csv)["stages"][stage])
        for k, v in change.items():
            if v is None:
                st.pop(k, None)
            else:
                st[k] = v
        st["id"] = stage
        return " | ".join(mc.block_problems(st))

    check("anchor must be" in bad("auction_mail", anchor="sale"), "a mistyped anchor is refused")
    check("did you mean min_gap_days" in bad("probate_mail", min_gap_day=14), "a key one letter off a setting is refused as a typo")
    check("send must be staged" in bad("auction_mail", send="auto"), "this stage never sends by itself")
    check("name the records" in bad("auction_mail", scope={"tags": ["FTM"]}), "a stage with no list and no preset is refused")
    check("weekly_batch_day is for the probate table" in bad("auction_mail", weekly_batch_day="Tue"),
          "a countdown cannot wait for a weekly batch")
    check("fallback must name another piece" in bad("auction_mail", pieces=[dict(AUCTION_PIECES[0], fallback="ZZ")]),
          "a fallback must name another piece by its id")
    check("has no price" in bad("auction_mail", prices={}), "a piece kind with no price is refused when the playbook loads")
    check("clock.steps 1 must be at: on_entry" in bad("probate_mail", clock={"steps": [{"piece": "PB1", "after_days": 3}]}),
          "the probate table starts on entry")
    check("not a sale_date setting" in bad("auction_mail", clock={"steps": []}), "a probate clock key on a countdown is refused")

    # ------------------------------------------------------------------ the auction stage, plan then live
    print("  auction stage")
    _clean()
    recs, custom = [], {}
    for u, street, sale, kw in (("a25", "25 Oak Ln", "2026-12-12", {}), ("a12", "12 Oak Ln", "2026-11-29", {}),
                                ("a6", "6 Oak Ln", "2026-11-23", {}), ("a2", "2 Oak Ln", "2026-11-19", {}),
                                ("apast", "9 Past Ln", "2026-11-10", {}), ("agone", "8 Gone Ln", "2026-10-20", {}),
                                ("allc", "7 Firm Ln", "2026-12-12", {"first": "", "last": "", "company": "Sample Holdings LLC"}),
                                ("adead", "5 Late Ln", "2026-12-12", {"deceased": True}),
                                ("alead", "4 Lead Ln", "2026-12-12", {"status": "Warm Lead"}),
                                ("adnm", "3 Stop Ln", "2026-12-12", {"owner_dnm": True})):
        r, cf = _sale(u, street, sale, **kw)
        recs.append(r)
        custom[u] = cf
    crm = CRM(recs, custom)
    plan = sweep.run("auction_mail", crm=crm, now=NOW, pol=pol, plan=True)
    tally = dict(plan.tally)
    check(plan.acted == 2 and plan.head.startswith("Would stage 2 piece(s) to mail Thu Nov 19, landing about Nov 24"),
          "plan: 25 and 12 days out each get their first piece, mailed Thursday to land the next Tuesday (%r)" % plan.head)
    check("Due in the next 7 days: 4 piece(s) to 2 record(s), about $6.24" in plan.head,
          "plan says what falls due over the next seven days and its cost: the opener now, then A2 and the last note (%r)" % plan.head)
    check(tally.get("too close to the sale for a piece to land in time") == 2,
          "6 and 2 days out get nothing: no piece could land 2 days before the sale (%r)" % tally)
    check(any("held 14 days for a new date" in k for k in tally) and any("would hand" in k for k in tally),
          "a passed sale is held 14 days for a new date, then handed to the monthly campaign")
    check(any("people only" in k for k in tally) and any("has died" in k for k in tally),
          "a company owner and an owner who may have died are held at the gate")
    check(any("lead" in k for k in tally) and any("do not mail" in k for k in tally), "a lead and a do-not-mail record are never mailed")
    check(not crm.writes and not ledger.batches() and not ledger.events(action="mail"),
          "a plan writes nothing to the account and stages nothing")
    check(mc.claimed_by("a25", pol, NOW, live_only=False) == "auction_mail" and mc.claimed_by("apast", pol, NOW, live_only=False),
          "the countdown owns a record with a sale ahead, and one passed in the last 14 days")
    check(mc.handoff_step("agone", "ftm_mail") == 0, "a plan hands nothing over")

    live = sweep.run("auction_mail", crm=crm, now=NOW, pol=pol)
    b = ledger.batch(live.batch) if live.batch else {}
    items = (b or {}).get("items") or []
    check(b and b["kind"] == "mail" and b["stage"] == "auction_mail" and sorted(i["record_uuid"] for i in items) == ["a12", "a25"],
          "live: the pieces become one batch of kind mail, staged for a person")
    check(all(set(mail.Piece.__dataclass_fields__) <= set(i) for i in items) and {i["step"] for i in items} == {1}
          and all(i["merge"]["send_date"] == "2026-11-19" and i["merge"]["in_home"] == "2026-11-24" for i in items),
          "each item is a mail piece whose step is its place in the list, with the send date and the day it should land")
    check("Release: python src/agents/cli.py autopilot release %d" % live.batch in live.head, "the head says how to release it")
    check(mc.handoff_step("agone", "ftm_mail") == 2, "live: a sale passed 14+ days ago with no new date hands the record to the "
                                                    "monthly campaign at step 2")
    check(mc.claimed_by("agone", pol, NOW) == "" and mc.claimed_by("a25", pol, NOW) == "auction_mail",
          "the countdown lets go of a handed-over record, and keeps the ones it is timing")
    again = sweep.run("auction_mail", crm=crm, now=NOW + timedelta(hours=1), pol=pol)
    check(again.batch == 0 and again.tally.get("already staged, waiting on release") == 2, "nothing is staged twice")
    rel = mail.release(live.batch, crm=crm, pol=pol, commit=True, now=NOW + timedelta(hours=2))
    check(rel["sent"] == 2 and len(crm.campaigns) == 1 and crm.campaigns[0]["uuids"] == ("a12", "a25")
          and crm.campaigns[0]["touch"]["template"] == "tpl-a1",
          "mail.release sends the batch as it is: one campaign for the piece, never split")
    check([e["detail"].get("step") for e in ledger.events("a25", "mail")] == [1] and ledger.events("a25", "mail")[0]["stage"] == "auction_mail",
          "the ledger remembers which piece went out, under this campaign")
    nxt = sweep.run("auction_mail", crm=crm, now=NOW + timedelta(days=1), pol=pol, plan=True)
    check(nxt.acted == 0 and nxt.tally.get("waiting for the next slot", 0) + nxt.tally.get(
        "waiting: the last mail piece went out too recently", 0) == 2, "a piece is never sent twice: the next one waits for its slot")
    a12 = sweep.run("auction_mail", crm=crm, now=NOW + timedelta(days=3), pol=pol, plan=True)
    check(any("A6" in ln and "12 Oak Ln" in ln for ln in a12.lines),
          "12 days out gets the last note in the last slot that still lands in time (%r)" % a12.lines)

    # ------------------------------------------------------------------ a postponed sale
    print("  postponed")
    custom["a25"]["Foreclosure Date"] = "2027-01-09"
    moved = sweep.run("auction_mail", crm=crm, now=NOW + timedelta(days=6), pol=pol, plan=True)
    check(any("the sale moved from Dec 12 to Jan 9;" in ln for ln in moved.lines) and moved.tally.get(
        "the sale date moved later: the pieces left count back from the new date") == 1,
          "a later sale date is a postponement: the record stays in and the pieces left count back from it (%r)" % moved.lines)
    check(mc.claimed_by("a25", pol, NOW + timedelta(days=6)) == "auction_mail",
          "a postponed record stays in the countdown")
    _clean()
    r_all, cf_all = _sale("aall", "11 Done Ln", "2026-12-20")
    crm_all = CRM([r_all], {"aall": cf_all})
    for n, when in zip(range(1, 7), (_at(2026, 10, 6), _at(2026, 10, 13), _at(2026, 10, 20), _at(2026, 10, 27),
                                      _at(2026, 10, 30), _at(2026, 11, 2))):
        ledger.event("seed", "auction_mail", "aall", "mail", {"piece": "x", "step": n}, now=when, tz=TZ)
    ledger.set_fact("aall", "%s:auction_mail" % mc.SALE_FACT, {"day": "2026-11-12", "prev": "", "moved_at": "2026-10-06T15:00:00+00:00"})
    rerun = sweep.run("auction_mail", crm=crm_all, now=NOW, pol=pol, plan=True)
    check(rerun.tally.get("waiting for the next slot", 0) + rerun.tally.get("waiting for a later slot: more slots than pieces left", 0) >= 1
          and ledger.fact("aall", "%s:auction_mail" % mc.SALE_FACT)["prev"] == "2026-11-12",
          "every piece gone and then the sale moved: the closing pair waits for the new date's last slots")
    run_late = sweep.run("auction_mail", crm=crm_all, now=_at(2026, 12, 8), pol=pol, plan=True)
    check(any("A5" in ln for ln in run_late.lines), "then the closing pair runs once more before the new date, A5 first (%r)" % run_late.lines)

    # ------------------------------------------------------------------ the probate stage
    print("  probate stage")
    _clean()
    pr = [_estate("p3", "3 Elm St", "2026-11-14"), _estate("p20", "20 Elm St", "2026-10-28"),
          _estate("p20b", "21 Elm St", "2026-10-28"), _estate("p70", "70 Elm St", "2026-09-08"),
          _estate("p400", "400 Elm St", "2025-10-13"), _estate("punc", "1 Elm St", "2026-10-01"),
          _estate("pinh", "2 Elm St", "2026-10-20"), _estate("pnodec", "4 Elm St", "2026-10-05"),
          _estate("pold", "5 Elm St", "2026-09-01"), _estate("pwrong", "6 Elm St", "2026-11-12", tags=["Wrong House"]),
          _estate("pkeep", "7 Elm St", "2026-10-02"), _estate("p24", "8 Elm St", "2024-10-01"),
          _estate("pdp", "9 Elm St", "2026-11-13", status="Deep Prospecting")]
    pr[1]["tags"].append("2026-10")
    crm_p = CRM(pr)
    ledger.event("seed", "probate_mail", "p20", "mail", {"step": 1}, now=_at(2026, 11, 3), tz=TZ)
    ledger.event("seed", "probate_mail", "p20b", "mail", {"step": 1}, now=_at(2026, 11, 6), tz=TZ)
    for n, when in ((1, _at(2026, 9, 8)), (2, _at(2026, 9, 22)), (3, _at(2026, 10, 6))):
        ledger.event("seed", "probate_mail", "p70", "mail", {"step": n}, now=when, tz=TZ)
    ledger.event("seed", "probate_mail", "p400", "mail", {"step": 1}, now=_at(2025, 10, 14), tz=TZ)
    ledger.event("seed", "probate_mail", "p400", "mail", {"step": 10}, now=_at(2026, 8, 25), tz=TZ)
    ledger.event("seed", "probate_honest", "pkeep", "probate", {"part": "audit", "action": "keep"}, now=NOW, tz=TZ)
    pplan = sweep.run("probate_mail", crm=crm_p, now=NOW, pol=pol, plan=True)
    ptally = dict(pplan.tally)
    plive = sweep.run("probate_mail", crm=crm_p, now=NOW, pol=pol)
    staged = {i["record_uuid"]: i["merge"]["design"] for i in (ledger.batch(plive.batch) or {}).get("items", [])}
    check(staged == {"p3": "PB1", "p20": "PB2", "p70": "EVG_T4", "p400": "PB8", "pinh": "PB1", "pkeep": "PB1"},
          "filed 3 days ago gets the first letter, 20 days the second, 70 days month 2's card, 400 days the rotation (%r)" % staged)
    check(pplan.acted == 6 and not crm_p.writes, "the probate plan finds the same six, and staging writes nothing to the account")
    check(any("house not confirmed" in k and "has not settled" in k for k in ptally)
          and any("house not confirmed" in k and "no decedent name" in k for k in ptally)
          and any("house not confirmed" in k and "not in the address audit" in k for k in ptally)
          and any("house not confirmed: tagged wrong house" == k for k in ptally),
          "an unconfirmed house is never mailed: flagged and unsettled, no decedent to test, before the guard, or Wrong House (%r)" % ptally)
    check(any("too recently" in k or "14 days" in k for k in ptally), "21 Elm St's second letter waits: never closer than 14 days")
    check(any("month 24" in k for k in ptally), "a probate opened more than 24 months ago is not mailed")
    check(any("in research" in k.lower() or "research" in k for k in ptally),
          "a probate record in Deep Prospecting is held by the gate unless the stage lifts in_research")
    f3 = ledger.fact("p3", mc.FILED_FACT)
    check(f3["day"] == "2026-11-14" and f3["source"] == "the probate open date" and any("from the probate open date" in ln for ln in pplan.lines),
          "the open date is read from the Probate Open Date and the run says which date it used")
    check(mc.claimed_by("p20", pol, NOW, live_only=False) == "probate_mail", "the probate campaign owns a confirmed probate record")

    rec = {"uuid": "x", "tags": ["2026-10", "2026-09"]}
    check(mc.opened_day({"probate_open_date": "2026-10-28", "tags": ["2026-09"]}, None, "Probate Open Date")[1] == "the probate open date"
          and mc.opened_day(rec, {"Probate Open Date": "2026-10-03"}, "Probate Open Date") == ("2026-10-03", "the Probate Open Date field")
          and mc.opened_day(rec, {}, "Probate Open Date") == ("2026-09-01", "the notice month tag 2026-09")
          and mc.opened_day({"tags": []}, {"Date Added": "10/02/2026"}, "Probate Open Date") == ("2026-10-02", "the Date Added field")
          and mc.opened_day({"tags": []}, {}, "Probate Open Date") == ("", ""),
          "the open date falls back from the Probate Open Date to the field, the earliest notice month tag, then Date Added")
    pol_conf = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_conf.stage("probate_mail").params["house_confirmed_tags"] = ["House Confirmed"]
    ok, why = mc.house_check("pold", {"house confirmed"}, "2026-09-01", audit_csv, {}, mc.options(pol_conf.stage("probate_mail")))
    check(ok and "a person confirmed" in why, "a person can confirm a house the audit could not, with a tag")

    # ------------------------------------------------------------------ one campaign per record
    print("  one campaign per record")
    _clean()
    both = _estate("pboth", "30 Both Rd", "2026-11-14")
    both["lists"].append("Foreclosure")
    crm_b = CRM([both], {"pboth": {"Foreclosure Date": "2026-12-20"}})
    sweep.run("auction_mail", crm=crm_b, now=NOW, pol=pol, plan=True)
    pb_both = sweep.run("probate_mail", crm=crm_b, now=NOW, pol=pol, plan=True)
    check(pb_both.acted == 0 and pb_both.tally.get("an auction countdown is timing it: a sale date comes first") == 1
          and mc.claimed_by("pboth", pol, NOW, live_only=False) == "auction_mail",
          "a record on both lists is the countdown's: a sale date comes first")
    pol_plan = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_plan.stage("auction_mail").mode = "plan"
    check(mc.claimed_by("pboth", pol, NOW) == "auction_mail" and mc.claimed_by("pboth", pol_plan, NOW) == "",
          "the monthly lane is held off only by a campaign live on this box, never by one still in plan")
    config.MODE = "dry"
    check(mc.claimed_by("pboth", pol, NOW) == "", "a dry box holds nothing off")
    config.MODE = "live"
    crm_b.recs["pboth"]["lists"] = ["Probate"]
    crm_b.custom["pboth"] = {}
    sweep.run("auction_mail", crm=crm_b, now=NOW, pol=pol, plan=True)
    check(mc.claimed_by("pboth", pol, NOW, live_only=False) == "", "a record that left the countdown's list is let go the same day")
    first_probate = _estate("pfirst", "31 Both Rd", "2026-11-14")
    crm_fp = CRM([first_probate])
    sweep.run("probate_mail", crm=crm_fp, now=NOW, pol=pol, plan=True)
    check(mc.claimed_by("pfirst", pol, NOW, live_only=False) == "probate_mail", "the probate campaign takes a confirmed estate first")
    crm_fp.recs["pfirst"]["lists"].append("Foreclosure")
    crm_fp.custom["pfirst"] = {"Foreclosure Date": "2026-12-20"}
    took = sweep.run("auction_mail", crm=crm_fp, now=NOW, pol=pol, plan=True)
    check(took.tally.get("taken from the probate campaign: a sale date comes first") == 1
          and mc.claimed_by("pfirst", pol, NOW, live_only=False) == "auction_mail",
          "a sale date that appears later takes the record over from the probate campaign, and says so")
    ledger.set_fact("pkept", mc.CLAIM_FACT, {"stage": "auction_mail", "anchor": "sale_date", "until": "2026-12-31", "why": "test"})
    pol_t = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_t.stage("auction_mail").params["scope"] = {"lists": ["Foreclosure"], "tags": ["No Such Tag"]}

    class NoTag(CRM):
        def tag_uuid(self, title):
            return "" if title == "No Such Tag" else super().tag_uuid(title)

    broken = sweep.run("auction_mail", crm=NoTag([]), now=NOW, pol=pol_t, plan=True)
    check(any(p.startswith("The tag No Such Tag does not exist") for p in broken.problems)
          and mc.claimed_by("pkept", pol, NOW, live_only=False) == "auction_mail",
          "a read that failed never lets go of the records it could not see")
    ledger.event("seed", "ftm_mail", "pboth", "mail", {"step": 1}, now=NOW - timedelta(days=5), tz=TZ)
    gap = sweep.run("probate_mail", crm=crm_b, now=NOW, pol=pol, plan=True)
    check(gap.acted == 0 and gap.tally.get("waiting: the last mail piece went out too recently") == 1,
          "a piece from any campaign in the last 14 days keeps the next one off the mailbox")
    _clean()
    r_lm, cf_lm = _sale("alm", "40 Recent Rd", "2026-12-20")
    r_lm["last_direct_mailed"] = "2026-11-18"
    lm = sweep.run("auction_mail", crm=CRM([r_lm], {"alm": cf_lm}), now=NOW, pol=pol, plan=True)
    check(lm.acted == 0 and lm.tally.get("mailed too recently (the record's last mailed date)") == 1,
          "the record's own last mailed date counts too, so mail sent from the app is not stacked on")

    # ------------------------------------------------------------------ the floor, the cap, the weekly batch
    print("  floor")
    _clean()
    pol_f = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_f.stage("auction_mail").params["min_batch"] = 20
    crm_f = CRM(recs[:2], {u: custom[u] for u in ("a25", "a12")})
    crm_f.custom["a25"]["Foreclosure Date"] = "2026-12-12"
    fl = sweep.run("auction_mail", crm=crm_f, now=NOW, pol=pol_f)
    check(fl.batch == 0 and fl.acted == 0 and any(p.startswith("Pieces are waiting on SiftMail's floor of 20 records a campaign")
                                                  for p in fl.problems),
          "under the floor nothing is staged, and the run says so in words that stay the same")
    fl2 = sweep.run("auction_mail", crm=crm_f, now=NOW + timedelta(days=1), pol=pol_f)
    check(any("the longest 1 day(s)" in p for p in fl2.problems) and any("under the floor of 20" in ln for ln in fl2.lines),
          "the run says how many wait and for how long")
    check(ledger.fact("a25", mc.WAIT_FACT).get("since") == "2026-11-17", "the wait is timed from the first day the piece was held")
    pol_f.stage("auction_mail").params["min_batch"] = 1
    fl3 = sweep.run("auction_mail", crm=crm_f, now=NOW + timedelta(days=1), pol=pol_f, plan=True)
    check(fl3.acted == 2 and not ledger.fact("a25", mc.WAIT_FACT),
          "once the floor allows it the pieces go, and their wait timer is cleared, in plan mode too")
    pol_c = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_c.stage("auction_mail").params["daily_pieces"] = 1
    _clean()
    cap = sweep.run("auction_mail", crm=CRM(recs[:2], {u: custom[u] for u in ("a25", "a12")}), now=NOW, pol=pol_c, plan=True)
    check(cap.acted == 1 and cap.tally.get("left for tomorrow (daily cap 1)") == 1, "the daily cap holds; the soonest sale goes first")
    pol_w = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_w.stage("probate_mail").params["weekly_batch_day"] = "Wed"
    _clean()
    wk = sweep.run("probate_mail", crm=CRM([_estate("p3", "3 Elm St", "2026-11-14")]), now=NOW, pol=pol_w, plan=True)
    check(wk.acted == 0 and wk.tally.get("waiting for the weekly batch on Wed") == 1, "a probate piece waits for the weekly batch day")
    wk2 = sweep.run("probate_mail", crm=CRM([_estate("p3", "3 Elm St", "2026-11-14")]), now=NOW + timedelta(days=1), pol=pol_w, plan=True)
    check(wk2.acted == 1, "and goes on it")
    _clean()
    pol_d = policy.parse(playbook(audit_csv), EMPLOYEES)
    for pc in pol_d.stage("auction_mail").params["pieces"]:
        pc["design"] = ""
    nd = sweep.run("auction_mail", crm=CRM(recs[:1], {"a25": {"Foreclosure Date": "2026-12-12"}}), now=NOW, pol=pol_d)
    check(nd.batch == 0 and any(p.startswith("Some pieces have no SiftMail design yet (A1)") for p in nd.problems),
          "a piece with no SiftMail design is not staged live, and the run names it")
    ndp = sweep.run("auction_mail", crm=CRM(recs[:1], {"a25": {"Foreclosure Date": "2026-12-12"}}), now=NOW, pol=pol_d, plan=True)
    check(ndp.acted == 1, "a plan still counts and prices what is due before the designs exist")

    # ------------------------------------------------------------------ at release
    print("  release")
    _clean()
    crm_r = CRM(recs[:2], {"a25": {"Foreclosure Date": "2026-12-12"}, "a12": {"Foreclosure Date": "2026-11-29"}})
    st_r = sweep.run("auction_mail", crm=crm_r, now=NOW, pol=pol)
    pieces = [mail.Piece(**{k: v for k, v in it.items() if k in mail.Piece.__dataclass_fields__}) for it in ledger.batch(st_r.batch)["items"]]
    ctx = stages.Ctx(policy=pol, stage=pol.stage("auction_mail"), crm=crm_r, run_id="rel", now=NOW + timedelta(days=4), live=True,
                     out=stages.Summary("auction_mail", "mailcadence", "ops", "live", True))
    kept, send = mc.at_release(ctx, pieces)
    check(send == "2026-11-24" and [p.record_uuid for p in kept] == ["a25"],
          "released four days late: the send date is set again from the release, and the piece that would now land too close "
          "to the sale is dropped, never sent late (%s %r)" % (send, [p.record_uuid for p in kept]))
    check(any("held back at release" in k for k in ctx.out.tally), "the dropped piece is said")
    ctx_u = stages.Ctx(policy=pol, stage=pol.stage("auction_mail"), crm=crm_r, run_id="rel2", now=NOW + timedelta(days=4), live=True,
                       out=stages.Summary("auction_mail", "mailcadence", "ops", "live", True))
    kept_u, _ = mc.at_release(ctx_u, pieces, uploaded=True)
    check(len(kept_u) == 2 and ctx_u.out.problems, "a file already at the mail house cannot be pulled back, so it is said")
    ctx_n = stages.Ctx(policy=pol, stage=pol.stage("auction_mail"), crm=crm_r, run_id="rel3", now=NOW + timedelta(hours=1), live=True,
                       out=stages.Summary("auction_mail", "mailcadence", "ops", "live", True))
    kept_n, send_n = mc.at_release(ctx_n, pieces)
    check(len(kept_n) == 2 and send_n == "2026-11-19", "released the same day, both go on the planned send date")
    _clean()
    wh = _estate("prel", "50 Elm St", "2026-11-14")
    crm_w = CRM([wh])
    sp = sweep.run("probate_mail", crm=crm_w, now=NOW, pol=pol)
    ppieces = [mail.Piece(**{k: v for k, v in it.items() if k in mail.Piece.__dataclass_fields__}) for it in ledger.batch(sp.batch)["items"]]
    crm_w.recs["prel"]["tags"].append("Wrong House")
    ctx_w = stages.Ctx(policy=pol, stage=pol.stage("probate_mail"), crm=crm_w, run_id="rel4", now=NOW + timedelta(hours=1), live=True,
                       out=stages.Summary("probate_mail", "mailcadence", "ops", "live", True))
    check(mc.at_release(ctx_w, ppieces)[0] == [], "a probate piece whose house was marked Wrong House since it was staged is dropped")
    ctx_m = stages.Ctx(policy=pol, stage=pol.stage("ftm_mail"), crm=crm_w, run_id="rel5", now=NOW, live=True,
                       out=stages.Summary("ftm_mail", "mail", "ops", "live", True))
    check(mc.at_release(ctx_m, ppieces)[0] == ppieces, "at_release leaves the monthly mail stage's own pieces alone")

    # ------------------------------------------------------------------ name routing (SPEC 5.3, 2026-11-17)
    print("  name routing")

    def owner(**kw):
        o = {"first_name": "Pat", "last_name": "Example", "company": ""}
        o.update(kw)
        return o
    check(mail.is_named(owner()), "a clean first name may print: the owner gets the named design")
    check(not mail.is_named(owner(first_name="", last_name="", company="Sample Holdings LLC")),
          "a company never gets the named design: SiftMail would print the company's name")
    check(not mail.is_named(owner(company="Sample Holdings LLC")), "an owner record that carries a company is not greeted by name")
    check(not mail.is_named(owner(first_name="E A")) and not mail.is_named(owner(first_name="C Pat")),
          "initials are not a first name, alone or in front of one")
    check(not mail.is_named(owner(first_name="PAT", last_name="EXAMPLE")) and not mail.is_named(owner(first_name="pat")),
          "a first name SiftMail would print in the wrong case is not used")
    check(not mail.is_named(owner(first_name="Estate")) and not mail.is_named(owner(first_name="Mrs")),
          "a word that is not anyone's first name is never printed as one")
    check(not mail.is_named(owner(last_name="Example Family Trust")), "a trust written into the name fields is not greeted by name")
    check(not mail.is_named({}), "no owner, no name")

    def pc(u, named):
        return mail.Piece(record_uuid=u, piece="handwritten_letter", preset="x", step=1, name="N", street="1 Mail Rd",
                          city="Knoxville", state="TN", zip="37917", named=named)

    def ids(ps):
        return [p.record_uuid for p in ps]
    nm, tw = mail.split_by_name([pc("a", True), pc("b", True), pc("c", False)], 1)
    check(ids(nm) == ["a", "b"] and ids(tw) == ["c"], "with a floor of one, every clean name gets the named design")
    nm, tw = mail.split_by_name([pc(str(i), True) for i in range(5)] + [pc("x", False)], 2)
    check(len(nm) == 4 and len(tw) == 2 and tw[0].record_uuid == "x", "the twin's campaign is topped up to the floor with named pieces")
    nm, tw = mail.split_by_name([pc(str(i), True) for i in range(4)] + [pc("x", False)], 3)
    check(nm == [] and len(tw) == 5, "a split that would leave a campaign under the floor sends everyone the twin")
    nm, tw = mail.split_by_name([pc("a", True), pc("b", True)], 1)
    check(ids(nm) == ["a", "b"] and tw == [], "all clean names: one named campaign and no twin campaign")
    pchk, why = mail.build_piece(_rec("ck1", "3 Check Ln"), "check_letter", "x", 3, pol.stage("ftm_mail"))
    check(pchk is not None and pchk.offer == 0 and pchk.named,
          "the check letter (no amount) needs no offer_pct, and carries the owner's name routing (%s)" % why)

    _clean()
    pol_n = policy.parse(playbook(audit_csv), EMPLOYEES)
    pol_n.stage("auction_mail").params["pieces"][0].update(design="tpl-a1-named", external_identifier="1001",
                                                           design_noname="tpl-a1-hi", external_identifier_noname="1002")
    r_nm, cf_nm = _sale("nm25", "25 Name Ln", "2026-12-12")
    r_nc, cf_nc = _sale("nc25", "26 Name Ln", "2026-12-12", first="PAT", last="EXAMPLE")
    crm_n = CRM([r_nm, r_nc], {"nm25": cf_nm, "nc25": cf_nc})
    pn = sweep.run("auction_mail", crm=crm_n, now=NOW, pol=pol_n, plan=True)
    check(pn.tally.get("greets the owner by first name") == 1 and pn.tally.get("greets without a name (no clean first name)") == 1,
          "the plan says how many owners get their first name and how many get the twin (%r)" % dict(pn.tally))
    st_n = sweep.run("auction_mail", crm=crm_n, now=NOW, pol=pol_n)
    rel_n = mail.release(st_n.batch, crm=crm_n, pol=pol_n, commit=True, now=NOW + timedelta(hours=2))
    by = {c["touch"]["template"]: c for c in crm_n.campaigns}
    check(rel_n["sent"] == 2 and set(by) == {"tpl-a1-named", "tpl-a1-hi"} and by["tpl-a1-named"]["uuids"] == ("nm25",)
          and by["tpl-a1-hi"]["uuids"] == ("nc25",) and by["tpl-a1-named"]["touch"]["external_identifier"] == "1001"
          and by["tpl-a1-hi"]["touch"]["external_identifier"] == "1002",
          "release splits the piece: the named design for the clean name, the twin with its own template number for the rest (%r)"
          % {k: v["uuids"] for k, v in by.items()})
    check({e["detail"].get("design") for e in ledger.events(action="mail")} == {"tpl-a1-named", "tpl-a1-hi"},
          "the ledger records which design each record got, so a response can be credited to it")

    # ------------------------------------------------------------------ house rules
    src = Path(mc.__file__)
    text = src.read_text(encoding="utf-8") + Path(__file__).read_text(encoding="utf-8")
    long = (chr(0x2014), chr(0x2013), chr(8))
    check(not any(ch in text for ch in long), "no long dash and no backspace byte in this module or its test")
