"""Fit and test the deal-odds model (src/deal_odds.py) on what actually sold in Knox and Blount.

Workstation only: it reads the staff OpenSearch MCP through src/opensearch_mcp.py (read-only, the
same guarded door Claude uses) and never touches an account. The box only ever reads the fitted
weights in data/deal_odds_model.json.

    python src/deal_odds_train.py --phase pull --work C:/pp/_work/deal_odds     # every SFR house, 8 streams, resumable
    python src/deal_odds_train.py --phase history --work ...                     # sale history for houses sold since snapshot A
    python src/deal_odds_train.py --phase build --work ...                       # two snapshots: facts as of each date, labels after
    python src/deal_odds_train.py --phase fit --work ...                         # fit on A, test on B, write the model
    python src/deal_odds_train.py --phase score --work ...                       # every house today, ranked by owner
    python src/deal_odds_train.py --phase all --work ...

THE TIME MACHINE. Each SiftMap flag carries the date it was added and the date it was removed, so a
house's flags on any past day can be rebuilt: on day S a flag was on if it was added on or before S
and removed after S (or never). Flag history starts in April 2024 and removal dates only from mid
2025, so the earliest honest snapshot is A = 2025-11-14. The owner on day S is the buyer of the last
sale on or before S (sales-history), which gives how long they had held it, whether they paid cash
and whether they were a company. Value on day S comes from the county assessment scaled to market
by the ZIP's own ratio, because the AVM re-anchors to a sale and would leak the answer.

THE LABEL. A deal is an investor acquisition: a sale whose buyer is an investor. Read, in order: the
property's own last-sale label (Pending, Wholesale, Wholetail, Rental), the sale before a Flip exit,
a company buyer that is not a bank, a government body, a church or a family trust, a cash buyer who
never moved in and paid under 85% of the area's value, and a sale resold within 18 months for 25%
more. Family transfers and sales under $10,000 never count.

THE TEST. Snapshot A (facts on 2025-11-14, deals to 2026-05-14) trains; snapshot B (facts on
2026-05-14, deals to 2026-11-13) tests, so every reported number is a prediction made before the
outcome it is scored on. The weights shipped to the box are then refitted on A and B together.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import deal_odds as do  # noqa: E402

DAY = 86_400_000
FIPS = ["47093", "47009"]
SNAP_A = dt.date(2025, 11, 14)
SNAP_B = dt.date(2026, 5, 14)
DATA_END = dt.date(2026, 11, 13)          # the property index snapshot the pull reads (properties--20261113)
WINDOW_DAYS = 182
PRIOR_DAYS = 365
BUY_BOX_MAX = 700_000
HOLD_TYPES = {"Pending", "Wholesale", "Wholetail", "Rental"}
# How often the number we dial is the owner's, by its Trestle tier, from the account's own call
# log (autopilot calibrate, 45 days to 2026-11-12: 1,556 numbers marked; Dial First was the owner's
# 46% of the time across 552 settled marks, an unscored number 37%). The other tiers had 78 marks
# between them, too few to read, so they carry no lift until measured.
REACH = {"Dial First": 0.46, "untiered": 0.37, "measured": "autopilot calibrate, 45 days to 2026-11-12"}
FIELDS = ["dataflik_id", "fips", "property_zip", "location", "owner_id", "property_value", "assessed_value",
          "equity_percentage_est", "living_area_sqft", "bedroom_count", "bath_count", "area_lot_acres", "year_built",
          "is_new_construction", "is_builder_owned", "is_corporate_owned", "is_trust_owned", "owner_occupied",
          "is_cash_buyer", "is_reo", "last_sale_date", "last_sale_price", "last_sale_investor_transaction_type",
          "is_last_sale_intrafamily", "distressor_data", "last_obituary_date", "rent_value", "investor_score",
          "investor_off_market_score", "investor_on_market_score", "realtor_score", "model_score", "is_active_listing",
          "mls_data.status"]
SALES_FIELDS = ["dataflik_id", "sale_date", "sale_amount", "is_cash_sale", "buyer_full_name", "buyer_2_full_name",
                "seller_full_name", "seller_2_full_name", "transaction_id"]
ENTITY_RX = re.compile(r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|LP|LLP|LTD|LIMITED|COMPANY|PROPERTIES|PROPERTY|"
                       r"HOLDINGS?|INVESTMENTS?|INVESTORS?|INVESTING|REALTY|HOMES|HOUSING|GROUP|CAPITAL|VENTURES?|PARTNERS|"
                       r"PARTNERSHIP|ENTERPRISES?|FUND|ASSETS|ACQUISITIONS?|DEVELOPMENT|BUILDERS?|CONSTRUCTION|RENTALS?|"
                       r"MANAGEMENT|SOLUTIONS|REAL ESTATE|LAND TRUST)\b")
NOT_INVESTOR_RX = re.compile(r"\b(BANK|MORTGAGE|LENDING|LOAN|LOANS|FINANCIAL|CREDIT UNION|FEDERAL|NATIONAL ASSOCIATION|"
                             r"SECRETARY|HOUSING AND URBAN|HUD|VETERANS|FANNIE|FREDDIE|COUNTY|CITY OF|STATE OF|TOWN OF|"
                             r"GOVERNMENT|AUTHORITY|CHURCH|MINISTR\w*|HABITAT|UNIVERSITY|COLLEGE|SCHOOL|RELOCATION|CARTUS|"
                             r"SIRVA|LIVING TRUST|REVOCABLE|FAMILY TRUST|IRREVOCABLE|ESTATE OF|HOSPITAL|FOUNDATION)\b")
NAME_STOP = {"LLC", "INC", "THE", "AND", "TRUST", "TRUSTEE", "ETAL", "ETUX", "JR", "SR", "III", "LIVING", "REVOCABLE",
             "FAMILY", "ESTATE", "HOMES", "PROPERTIES"}


# ---------------------------------------------------------------- small helpers
def ms(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def to_ms(v):
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00")[:25])
    except ValueError:
        try:
            t = dt.datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp() * 1000)


def num(v):
    try:
        f = float(str(v).replace(",", "")) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return None if f is None or f != f else f


WKT_RX = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)", re.I)


def geo(v):
    """(lat, lon) from a geo point in any of its shapes. The property index answers WKT,
    "POINT (lon lat)", longitude first (seen live 2026-11-14)."""
    if isinstance(v, dict):
        return num(v.get("lat")), num(v.get("lon"))
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return num(v[1]), num(v[0])
    if isinstance(v, str):
        m = WKT_RX.search(v)
        if m:
            return num(m.group(2)), num(m.group(1))
        if "," in v:
            a, b = v.split(",", 1)
            return num(a), num(b)
    return None, None


def investor_entity(name: str) -> bool:
    n = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())
    return bool(ENTITY_RX.search(n)) and not NOT_INVESTOR_RX.search(n)


def company(name: str) -> bool:
    n = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())
    return bool(ENTITY_RX.search(n)) or bool(re.search(r"\b(BANK|MORTGAGE|FEDERAL|NATIONAL ASSOCIATION)\b", n))


def _tokens(*names) -> set:
    out = set()
    for n in names:
        for t in re.sub(r"[^A-Z ]", " ", str(n or "").upper()).split():
            if len(t) >= 3 and t not in NAME_STOP:
                out.add(t)
    return out


def same_family(sale: dict) -> bool:
    """A buyer and seller who share a name token: a family transfer, never a deal."""
    b, s = _tokens(sale.get("buyer"), sale.get("buyer2")), _tokens(sale.get("seller"), sale.get("seller2"))
    return bool(b and s and b & s)


INITIAL_LOAD = ms(dt.date(2024, 4, 1))   # flag history begins here: the first load stamped every flag then
# Flags about the PERSON, not the house. They clear months after a sale (seen live: a senior flag
# removed 5.5 months after the house sold), so a trait older than the current owner's purchase is
# the previous owner's.
OWNER_TRAITS = {"is_senior_homeowners", "is_high_equity", "is_free_and_clear", "is_absentee_owners", "is_low_income",
                "is_bad_credit", "is_tired_landlord", "is_criminal_felony", "is_oos_owners"}


# How long after a sale an old owner's flags clear, measured 2026-11-14 on the 8,106 houses that
# sold once after snapshot A: 98% of a person's traits clear within nine months of the sale (most
# within two) and 92% of event flags (vacancy, tax) within six. A flag cleared inside the window
# was the seller's; one cleared later is the buyer's own.
SELLER_WINDOW_DAYS = {"trait": 274, "event": 183}


def _draw(*parts) -> float:
    """A fixed draw in [0, 1) from the parts: the same house, flag and day always draw the same."""
    h = hashlib.sha256("|".join(str(x) for x in parts).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / 2.0 ** 64


def flag_on(entry, at: int, q: float = 0.0, owner_since=None, trait: bool = False, sales_after=(), key="") -> float:
    """Was this flag on at `at` (epoch ms)? 1 or 0, never in between. entry = (added, removed, active now).

    The index keeps the two halves of a flag's life in different shapes (verified 2026-11-14): an
    ACTIVE flag carries its added date; a REMOVED flag drops the added date and keeps only its
    removal date; a flag with neither was removed before removals were recorded (mid 2025).

    A sale is what clears most flags, so every rule here has to read the same whether or not the
    house sold later, or the model learns the rule instead of the house (found the same day: a
    fractional value for a removed flag, and removed flags sitting six times as often on houses that
    sold, made a boosted model look three times better than it was).
      - active: on from its added date (the first load, April 2024, when none is given);
      - a person's trait (OWNER_TRAITS) older than the owner's purchase is the previous owner's;
      - removed after `at`: on, except a trait on an owner who bought within the year before `at`
        (the seller's clearing late), a flag cleared at or after a SECOND sale (the middle owner's
        could be in it), or one cleared later than SELLER_WINDOW_DAYS after the sale (the buyer's);
        and since its start date is gone, it is dropped by a fixed draw with chance q, the share of
        that flag's live stock younger than six months.
    """
    added, removed, active = entry
    if active:
        start = added if added is not None else INITIAL_LOAD
        if start > at:
            return 0.0
        if trait and owner_since is not None and start < owner_since:
            return 0.0
        return 1.0
    if removed is None or removed <= at:
        return 0.0
    if trait and owner_since is not None and at - owner_since < 365 * DAY:
        return 0.0
    if sales_after:
        if len(sales_after) >= 2 and removed >= sales_after[1]:
            return 0.0
        if removed > sales_after[0] + SELLER_WINDOW_DAYS["trait" if trait else "event"] * DAY:
            return 0.0
    return 1.0 if _draw(key, at) >= float(q) else 0.0


def compact(d: dict) -> dict:
    dd = d.get("distressor_data") or {}
    fl = {}
    for k, e in dd.items():
        if isinstance(e, dict) and (e.get("date_added") or e.get("date_removed") or e.get("is_active")):
            fl[k] = (to_ms(e.get("date_added")), to_ms(e.get("date_removed")), bool(e.get("is_active")))
    lat, lon = geo(d.get("location"))
    mls = d.get("mls_data") or {}
    return {"id": d.get("dataflik_id"), "fips": d.get("fips"), "zip": d.get("property_zip"), "lat": lat, "lon": lon,
            "owner": d.get("owner_id"), "v_now": num(d.get("property_value")), "assessed": num(d.get("assessed_value")),
            "eq_now": num(d.get("equity_percentage_est")), "sqft": num(d.get("living_area_sqft")),
            "beds": num(d.get("bedroom_count")), "baths": num(d.get("bath_count")), "lot": num(d.get("area_lot_acres")),
            "yb": num(d.get("year_built")), "newc": bool(d.get("is_new_construction")), "builder": bool(d.get("is_builder_owned")),
            "corp": bool(d.get("is_corporate_owned")), "trust": bool(d.get("is_trust_owned")), "occ": d.get("owner_occupied"),
            "cash": bool(d.get("is_cash_buyer")), "reo": bool(d.get("is_reo")), "ls": to_ms(d.get("last_sale_date")),
            "lp": num(d.get("last_sale_price")), "lt": d.get("last_sale_investor_transaction_type"),
            "intra": bool(d.get("is_last_sale_intrafamily")), "flags": fl, "rent": num(d.get("rent_value")),
            "ai": [num(d.get(k)) for k in ("investor_score", "investor_off_market_score", "investor_on_market_score",
                                            "realtor_score", "model_score")],
            "listed": bool(d.get("is_active_listing")) or str(mls.get("status") or "").lower() in ("active", "pending")}


def log_to(work: Path):
    path = work / "run.log"

    def log(msg: str) -> None:
        line = "%s %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return log


def base_query() -> dict:
    return {"bool": {"filter": [{"terms": {"fips": FIPS}}, {"term": {"property_type_group": "single_family_residence"}},
                                {"term": {"has_postal_address": True}}], "must_not": []}}


# ---------------------------------------------------------------- phase: pull
def _pull_stream(work: Path, i: int, base: dict, lo: int, hi: int, log) -> int:
    from builder_land import _resume_id
    from opensearch_mcp import OpenSearchMCP
    path, done = work / ("props_%d.jsonl" % i), work / ("props_%d.done" % i)
    if done.exists():
        return 0
    last = _resume_id(path) if path.exists() else None
    start = lo if last is None else last + 1
    qy = {"bool": {"filter": base["bool"]["filter"] + [{"range": {"dataflik_id": {"gte": start, "lt": hi}}}]}}
    c, n, t = OpenSearchMCP(), 0, time.time()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for row in c.page(qy, [{"dataflik_id": "asc"}], FIELDS, 10 ** 8):
                r = compact(row)
                r["dataflik_id"] = r["id"]          # _resume_id reads this key
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
                n += 1
                if n % 10_000 == 0:
                    log("pull stream %d: %d rows %.0fs" % (i, n, time.time() - t))
    finally:
        c.close()
    done.write_text(str(n))
    log("pull stream %d done: %d rows %.0fs" % (i, n, time.time() - t))
    return n


def phase_pull(args) -> int:
    from builder_land import balanced_edges, id_buckets
    from opensearch_mcp import OpenSearchMCP
    work, log = args.work, log_to(args.work)
    meta_path = work / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    base = base_query()
    c = OpenSearchMCP()
    try:
        total = c.count(base)

        def edge(order):
            return c.search({"query": base, "sort": [{"dataflik_id": order}], "_source": ["dataflik_id"]}, 1)["hits"]["hits"][0]["_source"]["dataflik_id"]
        lo, hi = edge("asc"), edge("desc") + 1
        edges = meta.get("edges") if list(work.glob("props_*.jsonl")) else None
        if not edges:
            edges = balanced_edges(id_buckets(c, base, lo, hi, total, args.streams), args.streams, lo, hi)
    finally:
        c.close()
    meta.update({"houses": total, "edges": edges, "pulled": dt.date.today().isoformat(), "fips": FIPS})
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    n = len(edges) - 1
    log("pull: %d houses in %d streams" % (total, n))
    with cf.ThreadPoolExecutor(n) as ex:
        list(ex.map(lambda i: _pull_stream(work, i, base, edges[i], edges[i + 1], log), range(n)))
    have = sum(1 for _ in iter_props(work))
    log("pull done: %d houses on disk of %d counted" % (have, total))
    return 0 if have >= 0.98 * total else 1


def iter_props(work: Path):
    seen = set()
    for p in sorted(work.glob("props_*.jsonl")):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") in seen:
                    continue
                seen.add(r.get("id"))
                yield r


# ---------------------------------------------------------------- phase: history
def _sales_stream(work: Path, batches, ids, log) -> int:
    from opensearch_mcp import OpenSearchMCP
    sort = [{"dataflik_id": "asc"}, {"sale_date": "asc"}, {"transaction_id": "asc"}]
    c, n = OpenSearchMCP(), 0
    try:
        for b in batches:
            dest = work / "sales" / ("batch_%05d.jsonl" % b)
            if dest.exists():
                continue
            qy = {"bool": {"filter": [{"terms": {"dataflik_id": ids[b * 500:(b + 1) * 500]}}],
                           "must_not": [{"term": {"is_deleted": True}}]}}
            tmp = Path(str(dest) + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for row in c.page(qy, sort, SALES_FIELDS, 10 ** 7, "sales-history"):
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                    n += 1
            os.replace(tmp, dest)
    finally:
        c.close()
    return n


def phase_history(args) -> int:
    work, log = args.work, log_to(args.work)
    # Every house with a sale on record: the owner on a snapshot day must be read from the same
    # source (its purchase row) whether or not the house sold later, or the two read differently
    # and the model learns the difference (found 2026-11-14: company and cash read off DataFlik's
    # fields for unsold houses and off sale rows for sold ones split deals from the rest).
    cut = 0
    ids = sorted({r["id"] for r in iter_props(work) if r.get("ls")})
    (work / "sales").mkdir(exist_ok=True)
    nb = (len(ids) + 499) // 500
    k = max(1, args.streams)
    log("history: %d houses sold since %s, %d batches over %d streams" % (len(ids), dt.date.fromtimestamp(cut / 1000), nb, k))
    t = time.time()
    with cf.ThreadPoolExecutor(k) as ex:
        got = sum(ex.map(lambda i: _sales_stream(work, list(range(i, nb, k)), ids, log), range(k)))
    have = len(list((work / "sales").glob("batch_*.jsonl")))
    log("history done: %d sale rows, %d of %d batches, %.0fs" % (got, have, nb, time.time() - t))
    return 0 if have >= nb else 1


MLS_FIELDS = ["dataflik_id", "listing_date", "recording_date", "status", "days_listed", "listing_id", "listing_update_id"]
MORTGAGE_FIELDS = ["dataflik_id", "first_loan_amount", "first_loan_recording_date", "first_loan_due_date",
                   "second_loan_amount", "second_loan_recording_date", "second_loan_due_date", "transaction_id"]


def _id_stream(work: Path, sub: str, index: str, fields: list, batches, ids, deleted_filter: bool) -> int:
    """One stream of a by-id history pull (listings or mortgages), 500 ids a batch, resumable."""
    from opensearch_mcp import OpenSearchMCP
    # search_after needs a unique sort; the internal _id cannot be sorted on, so a listing row is
    # told apart by its listing and its update id, a mortgage row by its transaction.
    sort = ([{"dataflik_id": "asc"}, {"listing_id": "asc"}, {"listing_update_id": "asc"}] if index == "mls-history"
            else [{"dataflik_id": "asc"}, {"transaction_id": "asc"}])
    c, n = OpenSearchMCP(), 0
    try:
        for b in batches:
            dest = work / sub / ("batch_%05d.jsonl" % b)
            if dest.exists():
                continue
            filt = [{"terms": {"dataflik_id": ids[b * 500:(b + 1) * 500]}}]
            qy = {"bool": {"filter": filt, "must_not": [{"term": {"is_deleted": True}}] if deleted_filter else []}}
            tmp = Path(str(dest) + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for row in c.page(qy, sort, fields, 10 ** 7, index):
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                    n += 1
            os.replace(tmp, dest)
    finally:
        c.close()
    return n


def phase_listings(args) -> int:
    """MLS listing history and mortgage history for every house: a failed listing and the real debt on a
    snapshot day, read from records DataFlik does not rewrite when a house sells."""
    work, log = args.work, log_to(args.work)
    ids = sorted(r["id"] for r in iter_props(work))
    nb = (len(ids) + 499) // 500
    k = max(1, args.streams)
    for sub, index, fields, deleted in (("mls", "mls-history", MLS_FIELDS, False),
                                        ("mortgages", "mortgage-history", MORTGAGE_FIELDS, True)):
        (work / sub).mkdir(exist_ok=True)
        t = time.time()
        log("%s: %d houses, %d batches over %d streams" % (index, len(ids), nb, k))
        with cf.ThreadPoolExecutor(k) as ex:
            got = sum(ex.map(lambda i: _id_stream(work, sub, index, fields, list(range(i, nb, k)), ids, deleted), range(k)))
        have = len(list((work / sub).glob("batch_*.jsonl")))
        log("%s done: %d rows, %d of %d batches, %.0fs" % (index, got, have, nb, time.time() - t))
        if have < nb:
            return 1
    return 0


AI_COLS = ["investor_score", "investor_off_market_score", "investor_on_market_score", "realtor_score", "model_score"]
AI_REFRESH_INDEX = "properties-changes--20261030"   # DataFlik rewrote every house's AI scores that day (verified 2026-11-14)
AI_TEST_FROM = dt.date(2026, 10, 16)                # the first logged day: the old scores were set before it


def _ai_stream(work: Path, batches, ids) -> int:
    from opensearch_mcp import OpenSearchMCP
    c, n = OpenSearchMCP(), 0
    try:
        for b in batches:
            dest = work / "ai" / ("batch_%05d.jsonl" % b)
            if dest.exists():
                continue
            qy = {"bool": {"filter": [{"terms": {"dataflik_id": ids[b * 500:(b + 1) * 500]}},
                                      {"nested": {"path": "changes", "query": {"terms": {"changes.col": AI_COLS}}}}]}}
            tmp = Path(str(dest) + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for row in c.page(qy, [{"dataflik_id": "asc"}, {"at": "asc"}], ["dataflik_id", "at", "changes"], 10 ** 6,
                                  AI_REFRESH_INDEX):
                    old = {ch["col"]: ch.get("old") for ch in row.get("changes") or [] if ch.get("col") in AI_COLS}
                    fh.write(json.dumps({"id": row["dataflik_id"], "at": row.get("at"), "old": old}, separators=(",", ":")) + "\n")
                    n += 1
            os.replace(tmp, dest)
    finally:
        c.close()
    return n


def phase_aiscores(args) -> int:
    """Each house's AI scores as they stood BEFORE DataFlik's 2026-10-30 refresh, from that day's change
    log (old values). They were set before the logs begin (2026-10-16), so they are a fair test of
    the AI scores against the deals since: the one honest look at them this data allows."""
    work, log = args.work, log_to(args.work)
    ids = sorted(r["id"] for r in iter_props(work))
    nb = (len(ids) + 499) // 500
    k = max(1, args.streams)
    (work / "ai").mkdir(exist_ok=True)
    t = time.time()
    log("AI scores before the %s refresh: %d houses, %d batches over %d streams" % (AI_REFRESH_INDEX[-8:], len(ids), nb, k))
    with cf.ThreadPoolExecutor(k) as ex:
        got = sum(ex.map(lambda i: _ai_stream(work, list(range(i, nb, k)), ids), range(k)))
    have = len(list((work / "ai").glob("batch_*.jsonl")))
    log("AI scores done: %d rows, %d of %d batches, %.0fs" % (got, have, nb, time.time() - t))
    return 0 if have >= nb else 1


def _load_rows(work: Path, sub: str) -> dict:
    out = defaultdict(list)
    for p in sorted((work / sub).glob("batch_*.jsonl")):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                out[r.get("dataflik_id")].append(r)
    return out


def load_listings(work: Path) -> dict:
    """id -> [(start, end, status)] from the MLS listing history, oldest first. A listing still open has
    no end. Several rows of one listing are its status updates; the last one is what it came to."""
    out = {}
    for i, rows in _load_rows(work, "mls").items():
        by_listing = {}
        for r in rows:
            key = r.get("listing_id") or (r.get("listing_date"), r.get("listing_update_id"))
            start, end = to_ms(r.get("listing_date")), to_ms(r.get("recording_date"))
            have = by_listing.get(key)
            if have is None or (end or 0) >= (have[1] or 0):
                by_listing[key] = (start, end, str(r.get("status") or ""))
        out[i] = sorted((v for v in by_listing.values() if v[0]), key=lambda v: v[0])
    return out


def load_mortgages(work: Path) -> dict:
    """id -> [(recorded, amount, due)] for every first and second loan, oldest first."""
    out = {}
    for i, rows in _load_rows(work, "mortgages").items():
        loans = []
        for r in rows:
            for n in ("first", "second"):
                rec, amt = to_ms(r.get("%s_loan_recording_date" % n)), num(r.get("%s_loan_amount" % n))
                if rec and amt and amt > 0:
                    loans.append((rec, amt, to_ms(r.get("%s_loan_due_date" % n)), n))
        # Two loans can share a day and an amount with one due date missing: sort on a key that
        # never compares a date with nothing.
        out[i] = sorted(loans, key=lambda l: (l[0], l[1], l[2] or 0, l[3]))
    return out


ON_MARKET = {"Active", "Pending", "Contingent"}


def listing_on(listings: list, at: int, owner_since) -> tuple[bool, bool]:
    """(on the market on day `at`, the owner's last listing before it failed within two years). A
    listing by the previous owner is not this owner's failure."""
    listed = failed = False
    last = None
    for start, end, status in listings or []:
        if start > at:
            continue
        if end is None or end > at:
            if status in ON_MARKET or end is None or status in ("Sold", "Fail"):
                listed = True
            continue
        if owner_since is not None and start < owner_since:
            continue
        last = (start, end, status)
    if last and last[2] == "Fail" and at - last[1] <= 730 * DAY:
        failed = True
    return listed, failed


def debt_on(loans: list, at: int, owner_since) -> float | None:
    """What the owner owed on day `at`: the latest first loan recorded since the purchase (a refinance
    replaces the one before it) and any second loan recorded after it, each paid down in a straight
    line to its due date (30 years when none is given). An owner from before the records is read off
    every loan on record, as DataFlik's own equity figure (what the box sees) would be."""
    since = owner_since - 30 * DAY if owner_since is not None else -(10 ** 18)
    firsts = [l for l in loans or [] if l[3] == "first" and since <= l[0] <= at]
    if not firsts:
        return 0.0
    first = firsts[-1]
    seconds = [l for l in loans or [] if l[3] == "second" and first[0] <= l[0] <= at]

    def left(loan):
        rec, amt, due = loan[0], loan[1], loan[2] or loan[0] + int(30 * 365.25 * DAY)
        if due <= at:
            return 0.0
        return amt * (due - at) / max(1, due - rec)
    return left(first) + (left(seconds[-1]) if seconds else 0.0)


def load_sales(work: Path) -> dict:
    out = defaultdict(list)
    for p in sorted((work / "sales").glob("batch_*.jsonl")):
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                out[s.get("dataflik_id")].append({
                    "date": to_ms(s.get("sale_date")), "amt": num(s.get("sale_amount")), "cash": bool(s.get("is_cash_sale")),
                    "buyer": s.get("buyer_full_name") or "", "buyer2": s.get("buyer_2_full_name") or "",
                    "seller": s.get("seller_full_name") or "", "seller2": s.get("seller_2_full_name") or "",
                    "tx": s.get("transaction_id")})
    for rows in out.values():
        rows.sort(key=lambda r: (r["date"] or 0, r["tx"] or 0))
    return out


# ---------------------------------------------------------------- the label and the owner on a day
def acquisitions(p: dict, hist: list, start: int, end: int, value_s) -> list[int]:
    """Investor acquisitions of house `p` in (start, end], epoch ms. See the module docstring."""
    out = set()
    ls, lt = p.get("ls"), p.get("lt")

    def in_win(t):
        return t is not None and start < t <= end

    def price_ok(a):
        return a is None or a >= 10_000
    if in_win(ls) and lt in HOLD_TYPES and not p.get("intra") and price_ok(p.get("lp")):
        out.add(ls)
    rows = [r for r in hist or [] if r.get("date") is not None]
    if lt == "Flip" and ls is not None:
        prior = [r for r in rows if r["date"] < ls]
        if prior and in_win(prior[-1]["date"]) and price_ok(prior[-1]["amt"]) and not same_family(prior[-1]):
            out.add(prior[-1]["date"])
    for r in rows:
        if in_win(r["date"]) and investor_entity(r["buyer"]) and price_ok(r["amt"]) and not same_family(r):
            out.add(r["date"])
    lp = p.get("lp")
    if (in_win(ls) and p.get("cash") and p.get("occ") == "No" and not p.get("intra") and value_s and lp
            and 10_000 <= lp <= 0.85 * value_s):
        out.add(ls)
    for a, b in zip(rows, rows[1:]):
        if (in_win(a["date"]) and a["amt"] and b["amt"] and a["amt"] >= 10_000 and b["date"] - a["date"] <= 548 * DAY
                and b["amt"] >= 1.25 * a["amt"] and not same_family(a)):
            out.add(a["date"])
    return sorted(out)


def deal_in(p: dict, hist: list, start: int, end: int, value) -> bool:
    """A deal WE could have had in (start, end]: an investor acquisition whose seller was not itself an
    investor passing the house on. A company or a cash buyer that bought under two years before
    reselling is a wholesale chain or a flip; it trades among investors and never sells to us at a
    discount. A landlord who held longer (a tired landlord) still counts."""
    o = owner_at(p, hist, start)
    for d in acquisitions(p, hist, start, end, value):
        if o["date"] is not None and d - o["date"] < 2 * 365 * DAY and (o["corp"] or o["cash"]):
            continue
        return True
    return False


def owner_at(p: dict, hist: list, at: int) -> dict:
    """The owner on day `at`: the buyer of the last sale on or before it, read off the sale rows for
    EVERY house alike. DataFlik's own owner fields (is_corporate_owned, is_cash_buyer) describe
    today's owner and read differently from a sale row, so using them for the houses that never sold
    again and the sale rows for the ones that did split deals from the rest by source, not by owner
    (found 2026-11-14). A house with no sale on record has an owner from before the records."""
    prior = [r for r in hist or [] if r.get("date") is not None and r["date"] <= at]
    if prior:
        r = prior[-1]
        return {"date": r["date"], "cash": bool(r.get("cash")), "corp": company(r.get("buyer"))}
    return {"date": None, "cash": False, "corp": False}


def zip_ratios(props: list) -> dict:
    """ZIP -> median(market value / county assessment) over houses not sold since snapshot A, so a
    house's value on a past day can be read off its assessment without the post-sale AVM."""
    by_zip, allr = defaultdict(list), []
    cut = ms(SNAP_A)
    for p in props:
        if p.get("assessed") and p["assessed"] > 0 and p.get("v_now") and p["v_now"] > 0 and (p.get("ls") or 0) <= cut:
            r = p["v_now"] / p["assessed"]
            if 0.5 <= r <= 20:
                by_zip[p.get("zip")].append(r)
                allr.append(r)
    county = statistics.median(allr) if allr else None
    return {z: statistics.median(v) if len(v) >= 30 else county for z, v in by_zip.items()} | {"_county": county}


def value_on(p: dict, ratios: dict, at: int):
    if p.get("assessed") and p["assessed"] > 0:
        r = ratios.get(p.get("zip")) or ratios.get("_county")
        if r:
            return p["assessed"] * r
    if (p.get("ls") or 0) <= at:
        return p.get("v_now")
    return None


def eligible(p: dict, at: int, value) -> bool:
    year = dt.datetime.fromtimestamp(at / 1000, dt.timezone.utc).year
    yb = p.get("yb")
    if yb and yb > year:
        return False
    if yb and yb == year and p.get("newc"):
        return False
    if value is not None and value > BUY_BOX_MAX:
        return False
    return True


def facts_on(p: dict, hist: list, at: int, value, q: dict | None = None, listings=None, loans=None) -> dict:
    o = owner_at(p, hist, at)
    listed, failed = listing_on(listings, at, o["date"])
    debt = debt_on(loans, at, o["date"])
    after = {r["date"] for r in hist or [] if r.get("date") is not None and r["date"] > at}
    if p.get("ls") and p["ls"] > at:
        after.add(p["ls"])                       # a sale the history missed still clears flags
    after = sorted(after)
    flags = {}
    for k, e in (p.get("flags") or {}).items():
        if k in do.FLAG_KEYS and flag_on(tuple(e), at, (q or {}).get(k, 0.0), o["date"], k in OWNER_TRAITS, after,
                                         "%s|%s" % (p.get("id"), k)):
            flags[k] = 1.0
    return {"flags": flags,
            "corporate": o["corp"], "cash": o["cash"], "value": value, "sqft": p.get("sqft"), "beds": p.get("beds"),
            "baths": p.get("baths"), "lot_acres": p.get("lot"),
            "tenure_years": (at - o["date"]) / (365.25 * DAY) if o["date"] else None, "year_built": p.get("yb"),
            "zip": p.get("zip"), "lat": p.get("lat"), "lon": p.get("lon"), "fips": p.get("fips"),
            "ltv": (debt / value) if (debt is not None and value) else None, "failed_listing": failed, "_listed": listed}


def area_tables(props: list, sales: dict, ratios: dict, at: int, value_mode: str = "assessed") -> dict:
    """The area rates a snapshot (or the box, for today) may know: investor acquisitions in the year
    BEFORE `at`, by ZIP (shrunk toward the two-county rate) and by neighborhood cell (shrunk toward its
    ZIP), and each ZIP's median value per square foot."""
    start = at - PRIOR_DAYS * DAY
    n_zip, pos_zip, n_cell, pos_cell, ppsf = Counter(), Counter(), Counter(), Counter(), defaultdict(list)
    n = pos = 0
    for p in props:
        v = value_on(p, ratios, at) if value_mode == "assessed" else p.get("v_now")
        if not eligible(p, at, v):
            continue
        hit = deal_in(p, sales.get(p["id"]), start, at, v)
        z, c = p.get("zip") or "", do.cell_of(p.get("lat"), p.get("lon"))
        n += 1
        pos += hit
        n_zip[z] += 1
        pos_zip[z] += hit
        if c:
            n_cell[(z, c)] += 1
            pos_cell[(z, c)] += hit
        if v and p.get("sqft") and p["sqft"] >= 300:
            ppsf[z].append(v / p["sqft"])
    base = (pos + 1) / (n + 2)
    zrate = {z: (pos_zip[z] + 300 * base) / (n_zip[z] + 300) for z in n_zip}
    zip_lo = {z: round(do.logit(r) - do.logit(base), 4) for z, r in zrate.items() if z}
    cell_lo = {}
    for (z, c), k in n_cell.items():
        zr = zrate.get(z, base)
        rc = (pos_cell[(z, c)] + 60 * zr) / (k + 60)
        lo = round(do.logit(rc) - do.logit(zr), 4)
        if abs(lo) >= 0.01:
            cell_lo[c] = lo
    return {"base": base, "zip": zip_lo, "cell": cell_lo,
            "zip_ppsf": {z: round(statistics.median(v), 2) for z, v in ppsf.items() if z and len(v) >= 20},
            "n": n, "pos": pos}


# ---------------------------------------------------------------- phase: build
def phase_build(args) -> int:
    work, log = args.work, log_to(args.work)
    t = time.time()
    props = list(iter_props(work))
    sales = load_sales(work)
    listings, mortgages = load_listings(work), load_mortgages(work)
    ratios = zip_ratios(props)
    log("build: %d houses, sale history for %d, listings for %d, loans for %d, assessment-to-market ratio %.2f county" % (
        len(props), len(sales), len(listings), len(mortgages), ratios.get("_county") or 0))
    if not listings or not mortgages:
        log("WARNING: no listing or loan history on disk (run --phase listings); failed listings and debt read as none")
    flag_start, live, young, removed = {}, Counter(), Counter(), Counter()
    recent = ms(DATA_END) - WINDOW_DAYS * DAY
    for p in props:
        for k, e in (p.get("flags") or {}).items():
            if e[0] and (k not in flag_start or e[0] < flag_start[k]):
                flag_start[k] = e[0]
            if e[2]:
                live[k] += 1
                young[k] += bool(e[0] and e[0] > recent)
            elif e[1]:
                removed[k] += 1
    # q: the share of each flag's live stock younger than one window. A removed flag lost its added
    # date, so this is the chance it was added after a snapshot instead of before.
    q = {k: round(young[k] / live[k], 4) for k in live if live[k]}
    out = {"ratios_county": ratios.get("_county"), "flag_q": q, "flag_live": dict(live), "flag_removed": dict(removed),
           "flag_first_added": {k: dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).date().isoformat()
                                for k, v in sorted(flag_start.items())}}
    log("flags: %d live entries, %d removed with a date; q (share under six months old): %s" % (
        sum(live.values()), sum(removed.values()),
        ", ".join("%s %.2f" % (k.replace("is_", ""), v) for k, v in sorted(q.items(), key=lambda kv: -live[kv[0]])[:12])))
    for name, snap in (("A", SNAP_A), ("B", SNAP_B)):
        at = ms(snap)
        end = at + WINDOW_DAYS * DAY
        tables = area_tables(props, sales, ratios, at)
        rows = []
        for p in props:
            v = value_on(p, ratios, at)
            if not eligible(p, at, v):
                continue
            hist = sales.get(p["id"])
            f = facts_on(p, hist, at, v, q, listings.get(p["id"]), mortgages.get(p["id"]))
            if f.pop("_listed"):
                continue                     # on the market that day: the box leaves a listed house out too
            y = 1 if deal_in(p, hist, at, end, v) else 0
            rows.append({"id": p["id"], "y": y, "x": do.encode(f, tables), "owner": p.get("owner"), "zip": p.get("zip"),
                         "fips": p.get("fips"), "value": v, "nflags": len(f["flags"]), "flags": sorted(f["flags"]),
                         "ai": p.get("ai")})
        pos = sum(r["y"] for r in rows)
        log("snapshot %s (%s): %d houses, %d investor deals in the next %d days (%.2f%%), prior-year base %.2f%%" % (
            name, snap, len(rows), pos, WINDOW_DAYS, 100.0 * pos / max(1, len(rows)), 100 * tables["base"]))
        with gzip.open(work / ("snap_%s.jsonl.gz" % name), "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        out[name] = {"date": snap.isoformat(), "houses": len(rows), "deals": pos, "base_prior": tables["base"]}
    now = ms(DATA_END)
    tables_now = area_tables(props, sales, ratios, now, value_mode="avm")
    (work / "tables_now.json").write_text(json.dumps(tables_now), encoding="utf-8")
    (work / "build.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    log("build done in %.0fs; today's area tables: %d ZIPs, %d cells" % (time.time() - t, len(tables_now["zip"]), len(tables_now["cell"])))
    return 0


def read_snap(work: Path, name: str) -> list:
    with gzip.open(work / ("snap_%s.jsonl.gz" % name), "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


# ---------------------------------------------------------------- phase: fit
def capture(y, score, ks=(1000, 2500, 5000, 10000)) -> list[dict]:
    y = [int(v) for v in y]                 # numpy integers do not survive json.dumps
    score = [float(v) for v in score]
    order = sorted(range(len(y)), key=lambda i: -score[i])
    total, n = sum(y), len(y)
    out = []
    for k in ks:
        k = min(k, n)
        hit = sum(y[i] for i in order[:k])
        out.append({"top": k, "deals": hit, "rate": hit / k if k else 0, "share_of_all": hit / total if total else 0,
                    "lift": (hit / k) / (total / n) if total and k else 0})
    return out


def phase_fit(args) -> int:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.model_selection import StratifiedKFold
    work, log = args.work, log_to(args.work)
    A, B = read_snap(work, "A"), read_snap(work, "B")
    vec = DictVectorizer(sparse=True)
    XA = vec.fit_transform([r["x"] for r in A])
    XB = vec.transform([r["x"] for r in B])
    yA, yB = np.array([r["y"] for r in A]), np.array([r["y"] for r in B])
    names = vec.get_feature_names_out()
    log("fit: %d features; A %d houses / %d deals, B %d houses / %d deals" % (len(names), len(yA), yA.sum(), len(yB), yB.sum()))
    grid = [0.003, 0.01, 0.03, 0.1, 0.3, 1.0]
    best, cv = None, {}
    skf = StratifiedKFold(n_splits=4, shuffle=True, random_state=7)
    for C in grid:
        aps = []
        for tr, va in skf.split(XA, yA):
            m = LogisticRegression(C=C, max_iter=3000, solver="lbfgs")
            m.fit(XA[tr], yA[tr])
            aps.append(average_precision_score(yA[va], m.predict_proba(XA[va])[:, 1]))
        cv[C] = float(np.mean(aps))
        if best is None or cv[C] > cv[best]:
            best = C
    log("C chosen by 4-fold cross-validation inside A: %s (average precision %s)" % (
        best, ", ".join("%s=%.4f" % (c, v) for c, v in cv.items())))
    lr = LogisticRegression(C=best, max_iter=5000, solver="lbfgs").fit(XA, yA)
    pB = lr.predict_proba(XB)[:, 1]
    gb = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31, random_state=7)
    gb.fit(XA.toarray(), yA)
    gB = gb.predict_proba(XB.toarray())[:, 1]
    test = {"auc": float(roc_auc_score(yB, pB)), "ap": float(average_precision_score(yB, pB)),
            "capture": capture(list(yB), list(pB)), "gbm_auc": float(roc_auc_score(yB, gB)),
            "gbm_capture": capture(list(yB), list(gB))}
    dec = []
    order = np.argsort(-pB)
    for i in range(10):
        part = order[i * len(order) // 10:(i + 1) * len(order) // 10]
        dec.append({"decile": i + 1, "predicted": float(pB[part].mean()), "actual": float(yB[part].mean())})
    test["calibration"] = dec
    base_flags = {
        "priority1_combos": [1.0 if (("flag:is_absentee_owners" in r["x"] and "flag:is_low_income" in r["x"])
                                     or ("flag:is_bad_credit" in r["x"] and "flag:is_free_and_clear" in r["x"])
                                     or ("flag:is_absentee_owners" in r["x"] and "flag:is_bad_credit" in r["x"])) else 0.0
                             for r in B],
        "distress_flag_count": [float(r["nflags"]) for r in B],
        # DataFlik's investor score as it reads TODAY, after the sales it is scored on: it can know the
        # answer, so it is shown for scale and never counted as a fair comparison.
        "investor_score_today_leaky": [float((r.get("ai") or [None])[0] or 0) for r in B],
    }
    try:
        import dream100_build as dbuild
        from agents.autopilot.top1000 import FLAG_LISTS

        def dream_proxy(r):
            """The old Dream 100 points that can be rebuilt for a past day: deal size (equity guessed from
            the free-and-clear and high-equity flags) and the distress evidence. Its investor, condition
            and reach points cannot be rebuilt, so this is the old score's skeleton, not the old score."""
            v = float(r.get("value") or 0)
            fl = set(r.get("flags") or [])
            eq = 1.0 if "is_free_and_clear" in fl else 0.6 if "is_high_equity" in fl else 0.3
            room = min(eq * v, 0.30 * v)
            ev = sum(dbuild.LIST_PTS.get(FLAG_LISTS.get(f, ""), 0) for f in fl) + (1 if "is_absentee_owners" in fl else 0)
            return dbuild.W_DEAL * dbuild.ramp(room, dbuild.DEAL_LO, dbuild.DEAL_HI) + dbuild.W_EVID * dbuild.ramp(ev, 0, dbuild.EVID_CAP)
        base_flags["old_dream_skeleton"] = [dream_proxy(r) for r in B]
    except Exception as exc:  # noqa: BLE001  (a baseline that cannot be built is said, the fit goes on)
        log("old Dream skeleton baseline skipped: %s" % str(exc)[:120])
    rng = np.random.default_rng(7)
    baselines = {k: capture(list(yB), [v + 1e-6 * rng.random() for v in vals]) for k, vals in base_flags.items()}
    baselines["priority1_combos_size"] = int(sum(base_flags["priority1_combos"]))
    final = LogisticRegression(C=best, max_iter=5000, solver="lbfgs")
    from scipy.sparse import vstack
    final.fit(vstack([XA, XB]), np.concatenate([yA, yB]))
    weights = {str(n): round(float(w), 5) for n, w in zip(names, final.coef_[0])}
    freq = Counter()
    groups = defaultdict(float)
    tot = len(A) + len(B)
    for r in A + B:
        for k in r["x"]:
            if k.split(":")[0] in ("value", "tenure", "built", "lot", "beds", "baths", "county"):
                freq[k] += 1
    for k, c in freq.items():
        groups[k.split(":")[0]] += weights.get(k, 0.0) * c / tot
    tables = json.loads((work / "tables_now.json").read_text(encoding="utf-8"))
    built = json.loads((work / "build.json").read_text(encoding="utf-8"))
    model = {"version": 1, "fitted": dt.date.today().isoformat(), "window_days": WINDOW_DAYS,
             "snapshots": {"A": built["A"], "B": built["B"]}, "data_through": DATA_END.isoformat(),
             "intercept": round(float(final.intercept_[0]), 5), "weights": weights,
             "band_ref": {k: round(v, 5) for k, v in groups.items()},
             "tables": {"base": tables["base"], "zip": tables["zip"], "cell": tables["cell"], "zip_ppsf": tables["zip_ppsf"]},
             "test": test, "baselines": baselines, "C": best, "flag_first_added": built.get("flag_first_added"),
             "reach": REACH, "untestable_flags": list(do.UNTESTABLE_FLAGS),
             "label": "an investor acquisition within %d days (see src/deal_odds_train.py)" % WINDOW_DAYS}
    out = Path(args.model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(model, indent=1, sort_keys=True), encoding="utf-8")
    log("test on B (fitted on A): AUC %.3f, top 1,000 houses held %d deals (%.1fx the base), top 5,000 held %d" % (
        test["auc"], test["capture"][0]["deals"], test["capture"][0]["lift"], test["capture"][2]["deals"]))
    log("gradient boosting for comparison: AUC %.3f, top 1,000 held %d" % (test["gbm_auc"], test["gbm_capture"][0]["deals"]))
    for k, v in baselines.items():
        if isinstance(v, list):
            log("baseline %s: top 1,000 held %d, top 5,000 held %d" % (k, v[0]["deals"], v[2]["deals"]))
    log("model written: %s" % out)
    return 0


# ---------------------------------------------------------------- phase: forward (the AI scores, fairly)
def phase_forward(args) -> int:
    """The one fair look at DataFlik's AI scores this data allows: each house's scores as they stood
    before the 2026-10-30 refresh (set before the logs begin, 2026-10-16) against the deals since,
    beside this model refitted on snapshot A alone (labels end 2026-05-14, so it never saw the
    window). The result is written into the model file, so a reader sees why the AI scores are out."""
    import numpy as np
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    work, log = args.work, log_to(args.work)
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))
    props = list(iter_props(work))
    sales, listings, loans = load_sales(work), load_listings(work), load_mortgages(work)
    ratios = zip_ratios(props)
    q = json.loads((work / "build.json").read_text(encoding="utf-8")).get("flag_q") or {}
    ai = {}
    for f in sorted((work / "ai").glob("batch_*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                ai[r["id"]] = r.get("old") or {}
    if not ai:
        log("forward: no AI scores on disk (run --phase aiscores); nothing to test")
        return 1
    at, end = ms(AI_TEST_FROM), ms(DATA_END)
    tables = area_tables(props, sales, ratios, at)
    rows = []
    for p in props:
        v = value_on(p, ratios, at)
        if not eligible(p, at, v):
            continue
        hist = sales.get(p["id"])
        f = facts_on(p, hist, at, v, q, listings.get(p["id"]), loans.get(p["id"]))
        if f.pop("_listed"):
            continue
        rows.append((do.encode(f, tables), 1 if deal_in(p, hist, at, end, v) else 0, ai.get(p["id"]) or {}))
    y = np.array([r[1] for r in rows])
    A = read_snap(work, "A")
    vec = DictVectorizer(sparse=True)
    lr = LogisticRegression(C=float(model.get("C") or 0.1), max_iter=5000).fit(
        vec.fit_transform([r["x"] for r in A]), np.array([r["y"] for r in A]))
    ours = lr.predict_proba(vec.transform([r[0] for r in rows]))[:, 1]

    def grade(scores):
        s = np.array([np.nan if v is None else float(v) for v in scores], dtype=float)
        have = np.isfinite(s)
        if not have.any():
            return {"scored": 0.0}
        s = np.where(have, s, np.nanmin(s) - 1)
        order = np.argsort(-s, kind="stable")
        k = max(1, len(y) // 10)
        return {"scored": round(float(have.mean()), 3), "auc": round(float(roc_auc_score(y, s)), 3),
                "top10pct_deals": int(y[order[:k]].sum())}

    def col(c):
        out = []
        for r in rows:
            try:
                out.append(float(r[2].get(c)))
            except (TypeError, ValueError):
                out.append(None)
        return out
    result = {"window": "%s to %s" % (AI_TEST_FROM, DATA_END), "houses": len(rows), "deals": int(y.sum()),
              "deal_odds": grade(list(ours)), "chance_top10pct": round(0.1 * float(y.sum()), 1)}
    for c in AI_COLS:
        result["ai_" + c] = grade(col(c))
    model["forward_ai_test"] = result
    Path(args.model).write_text(json.dumps(model, indent=1, sort_keys=True), encoding="utf-8")
    log("forward test %s: %d deals; deal odds AUC %s, top tenth %s; AI off-market AUC %s, on-market %s, realtor %s" % (
        result["window"], result["deals"], result["deal_odds"].get("auc"), result["deal_odds"].get("top10pct_deals"),
        result["ai_investor_off_market_score"].get("auc"), result["ai_investor_on_market_score"].get("auc"),
        result["ai_realtor_score"].get("auc")))
    return 0


# ---------------------------------------------------------------- phase: score (today)
def phase_score(args) -> int:
    work, log = args.work, log_to(args.work)
    model = do.load(args.model)
    props = list(iter_props(work))
    sales = load_sales(work)
    listings, mortgages = load_listings(work), load_mortgages(work)
    now = ms(DATA_END)
    houses = []
    for p in props:
        v = p.get("v_now")
        if not eligible(p, now, v) or p.get("listed"):
            continue
        f = facts_on(p, sales.get(p["id"]), now, v, None, listings.get(p["id"]), mortgages.get(p["id"]))
        if f.pop("_listed"):
            continue
        f["ltv"] = do.ltv_from_equity(p.get("eq_now"))      # today, as the box reads it: SiftMap's own equity figure
        houses.append({"id": p["id"], "owner": p.get("owner") or "h%s" % p["id"], "p": do.prob(model, f),
                       "zip": p.get("zip"), "fips": p.get("fips"), "value": v, "why": do.reasons(model, f),
                       "ai": (p.get("ai") or [None])[0]})
    by_owner = defaultdict(list)
    for h in houses:
        by_owner[h["owner"]].append(h)
    owners = sorted(({"owner": o, "p": do.owner_prob(x["p"] for x in hs), "houses": len(hs),
                      "ids": [x["id"] for x in sorted(hs, key=lambda x: -x["p"])],
                      "why": max(hs, key=lambda x: x["p"])["why"]} for o, hs in by_owner.items()), key=lambda o: -o["p"])
    (work / "scored_owners.json").write_text(json.dumps(owners[:20000]), encoding="utf-8")
    top = owners[:1000]
    log("score: %d houses, %d owners; the top 1,000 owners hold %d houses and are expected to sell %.0f to investors "
        "in six months (the average owner: %.2f%%)" % (len(houses), len(owners), sum(o["houses"] for o in top),
                                                       sum(o["p"] for o in top), 100 * sum(o["p"] for o in owners) / len(owners)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["pull", "history", "listings", "aiscores", "build", "fit", "forward", "score", "all"])
    ap.add_argument("--work", default=str(ROOT / "output" / "deal_odds" / "work"))
    ap.add_argument("--model", default=str(do.MODEL_PATH))
    ap.add_argument("--streams", type=int, default=8)
    args = ap.parse_args(argv)
    args.work = Path(args.work)
    args.work.mkdir(parents=True, exist_ok=True)
    phases = ["pull", "history", "listings", "aiscores", "build", "fit", "forward", "score"] if args.phase == "all" else [args.phase]
    for ph in phases:
        rc = globals()["phase_" + ph](args)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
