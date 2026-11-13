"""Substitute trustee appointments: the foreclosure signal that comes before the sale notice
(build plan 1.9). Knox only: Blount has no online recorder.

WHY. By the time a trustee's sale runs in the newspaper the owner has usually been marketed for
months and the sale is weeks away. In a non-judicial state the first public sign is earlier: the
lender records an APPOINTMENT OF SUBSTITUTE TRUSTEE at the Register of Deeds. This pulls those
filings each morning as one more document type in the daily recorder pull.

THE TRAP THIS MODULE IS BUILT AROUND. The appointment's index row names the LENDER (grantor) and
the SUBSTITUTE TRUSTEE (grantee). It never names the borrower. So "the same name match as liens"
cannot be run on either indexed party: matching the lender finds the bank's own houses, and
matching the trustee finds an attorney's home. Either one uploads a stranger's house as a
pre-foreclosure, which is the worst thing this pipeline can do.

The borrower is on the DEED OF TRUST the appointment refers to. The recorder cross-references
that instrument in the appointment's marginal column (the same column a lien release uses to name
the lien it clears), and an instrument number carries its own recording date (YYYYMMDD plus a
sequence). So the route is:

    appointment  ->  the instrument it refers to  ->  that one day's deeds of trust
                 ->  the grantor on that deed of trust (the borrower)  ->  the tax roll

Every step uses the search contract knox_rod already proved (a doc type and a date window), so
nothing here hand-builds a request the page never sent.

FOUR GUARDS, each one a stranger's house avoided:
  * A lender-shaped or trustee-shaped name is never matched to a parcel, whatever role it holds.
  * A parcel id counts only when the tax roll answers with THAT parcel. The county's search is
    fuzzy and ranks by relevance, so asked for one id it can put a different parcel first.
  * A parcel id on the deed of trust is used only when the owner of record TODAY is still the
    borrower. Otherwise the house changed hands and the filing is not a lead.
  * A borrower matched by name must own exactly ONE parcel. A lien attaches to everything its
    debtor owns; a deed of trust secures one house, and the index does not say which.

A MISS IS SETTLED, A FAILURE IS NOT. An appointment that names no instrument, or whose borrower
owns three parcels, is marked handled once the county stage has run, so it is not looked up again
every morning. A lookup that errored, or that the day's request cap pushed to tomorrow, stays open.

UNVERIFIED, AND SAID OUT LOUD. Three facts could not be checked against the live recorder when
this was written: that the appointment's marginal column carries the deed of trust's instrument
number, that the deed of trust type is labelled TD in the county's tree, and that the parcel id
the recorder prints is the tax roll's account number. The first two are settings and fail
loudly: when several appointments are read and none yields a deed of trust, the stage FAILS with
the count instead of reporting a quiet zero. The third fails safe: an id the roll does not
answer exactly is ignored and the borrower's name is used instead.

A WINDOW WITH NO APPOINTMENT AT ALL IS A FAULT, not a quiet stretch. Knox records these most
business days, so once the pull window covers KNOX_APPT_QUIET_DAYS days and holds none, the
label is wrong or the search came back empty, and the stage fails and says so.

    KNOX_ROD_APPT_LABEL=APPT        the appointment's label in the recorder's doc-type tree
    KNOX_ROD_DOT_LABEL=TD           the deed of trust's label
    KNOX_APPT_MAX_LOOKUPS=12        deed of trust day lookups per run (the county asked for few requests)
    KNOX_APPT_MAX_PARCELS=1         parcels a name-matched borrower may own before it is ambiguous
    KNOX_APPT_QUIET_DAYS=10         days the window may cover with no appointment before that fails (0 = never)

    python src/knox_appointments.py --scratch output/knox      # prepare only, uploads nothing
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ftm_staging  # noqa: E402

FEED = "substitute_trustee"            # the staging ledger's name for this feed
FEED_KEY = "knox_substitute_trustee"   # its row in the feed table, which names the DataSift list
NOTICE_TYPE = "substitute_trustee"
SOURCE_URL = "https://paxsub.knoxrod.org/views/search"
# Fewer appointments than this in the window is too few to judge the index by.
MIN_FOR_VERDICT = 3

_TAGS = re.compile(r"<[^>]+>")
# Instrument numbers are 15 digits: YYYYMMDD plus a 7 digit sequence (see knox_lien_index).
_INSTRUMENT = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})\d{7}(?!\d)")
_RECORDED = re.compile(r"Recorded:\s*(\d{1,2})/(\d{1,2})/(\d{4})")
_PRP = re.compile(r"PrpId:\s*([0-9A-Za-z]+)")
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "ETAL", "ETUX", "ETVIR"}
# A party shaped like one of these is the lender, the servicer, the nominee or the trustee. It is
# never the homeowner, and it is never looked up on the tax roll.
# Deliberately no bare LAW, NA or PC: Law and Na are surnames, and "P C" is a pair of initials.
_NOT_A_BORROWER = re.compile(
    r"\b(BANK|BANCORP|MORTGAGE|MTG|LOAN|LOANS|LENDING|LENDER|FUNDING|FINANCIAL|FINANCE|SERVICING|SERVICER|"
    r"CREDIT UNION|SAVINGS|FEDERAL|NATIONAL ASSOCIATION|MERS|ELECTRONIC REGISTRATION|TRUSTEE|TRUSTEES|"
    r"TRUST COMPANY|TITLE|ESCROW|ATTORNEY|ATTORNEYS|LAW FIRM|LAW OFFICE|LAW OFFICES|LAW GROUP|AT LAW|PLLC|LLP|"
    r"SECRETARY OF|HOUSING|VETERANS AFFAIRS|FANNIE|FREDDIE|FNMA|FHLMC|HUD|USDA)\b", re.I)


class AppointmentsUnusable(RuntimeError):
    """Several appointments were read and none could be followed to a deed of trust. The index
    does not carry what this feed needs, or a label is wrong. No retry fixes it."""


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def quiet_days() -> int:
    """Days the pull window may cover with no appointment at all before that is a fault. 0 = never."""
    try:
        return max(0, int(os.environ.get("KNOX_APPT_QUIET_DAYS", "10") or 0))
    except ValueError:
        return 10


def appt_label() -> str:
    return (os.environ.get("KNOX_ROD_APPT_LABEL") or "APPT").strip().upper()


def dot_label() -> str:
    return (os.environ.get("KNOX_ROD_DOT_LABEL") or "TD").strip().upper()


def _txt(cell) -> str:
    return " ".join(_TAGS.sub(" ", str(cell or "")).split())


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^A-Z ]", " ", (name or "").upper()).split())


def _money(cell) -> float | None:
    raw = _txt(cell).replace("$", "").replace(",", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _recorded_iso(detail: str) -> str:
    m = _RECORDED.search(detail or "")
    return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else ""


def instrument_day(instrument: str) -> str:
    """The recording day an instrument number carries, as MM/DD/YYYY, or "" when it is not one."""
    m = _INSTRUMENT.fullmatch((instrument or "").strip())
    if not m:
        return ""
    y, mth, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(y, mth, d)
    except ValueError:
        return ""
    return "%02d/%02d/%04d" % (mth, d, y)


def looks_like_lender(name: str) -> bool:
    return bool(_NOT_A_BORROWER.search(_norm(name)))


def _parties(rows: list[list]) -> dict:
    """One record per instrument from raw grid rows, keeping WHO held which role.

    knox_rod.collapse merges both sides into one name list, which is fine for a lien and useless
    here: the whole question is which name is on which side."""
    out: dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, list) or len(r) < 15:
            continue
        instrument = _txt(r[10])
        if not instrument:
            continue
        rec = out.setdefault(instrument, {
            "instrument": instrument, "grantors": [], "grantees": [], "refs": [], "prp_ids": [],
            "recorded": "", "consideration": None, "legal": ""})
        name = (_txt(r[4]) + " " + _txt(r[5])).strip()
        role = _txt(r[6]).lower()
        side = "grantors" if role.startswith("grantor") else ("grantees" if role.startswith("grantee") else "")
        if name and side and name not in rec[side]:
            rec[side].append(name)
        rec["recorded"] = rec["recorded"] or _recorded_iso(_txt(r[3]))
        legal = _txt(r[9])
        rec["legal"] = rec["legal"] or legal
        for pin in _PRP.findall(legal):
            if pin.upper() not in rec["prp_ids"]:
                rec["prp_ids"].append(pin.upper())
        for m in _INSTRUMENT.finditer(_txt(r[13])):
            if m.group(0) != instrument and m.group(0) not in rec["refs"]:
                rec["refs"].append(m.group(0))
        if rec["consideration"] is None:
            rec["consideration"] = _money(r[14])
    return out


def borrowers_of(deed: dict) -> list[str]:
    """The people who signed the deed of trust: its grantors, minus anything lender-shaped."""
    out = []
    for name in deed.get("grantors") or []:
        if looks_like_lender(name):
            continue
        if len(_norm(name).split()) < 2:
            continue                      # a bare surname matches far too much
        out.append(name)
    return out


def _pin_key(pin) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(pin or "").upper())


def _roll_search(pin: str) -> list:
    """The county tax roll's rows for one parcel id query. Raises on a failed request, so a
    lookup that could not be made is never read as "no such parcel"."""
    from urllib.parse import quote

    import requests

    from tax_enricher import KNOX_API_BASE, REQUEST_TIMEOUT, _normalize_parcel_id

    url = "%s/parcels/%s?detail_level=public&start=0&length=10" % (KNOX_API_BASE, quote(_normalize_parcel_id(str(pin))))
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json().get("parcels") or []


def parcel_by_id(pin: str, search=None) -> dict | None:
    """The tax roll's row for EXACTLY this parcel id, or None when the roll does not hold it.

    The county's parcel search is fuzzy and ranked by relevance: asked for one id it can answer
    with a different parcel first. Taking the first row is how one house's tax bill was once
    written onto another. Here a wrong first row would be worse than a wrong figure: its owner
    is not the borrower, so the filing would be settled as "the house changed hands" and a real
    lead would be thrown away with nothing to show for it. So a row counts only when its OWN
    account number is this id (a Knox tax account is the parcel id), compared with punctuation
    and case removed."""
    want = _pin_key(pin)
    if not want:
        return None
    for p in (search or _roll_search)(pin) or []:
        address = str(p.get("parcel_address") or "").strip()
        if address and _pin_key(p.get("account_number")) == want:
            return {"address": address, "owner": str(p.get("owner") or "").strip(),
                    "account": p.get("account_number")}
    return None


def name_in_owner(name: str, owner: str) -> bool:
    """True when the owner of record carries this person's name: every name token of two letters
    or more must appear, and there must be at least two of them. The roll prints names in its own
    order and the recorder in another, so order is ignored and nothing is guessed."""
    toks = [t for t in _norm(name).split() if len(t) >= 2 and t not in _SUFFIXES]
    return len(toks) >= 2 and set(toks).issubset(set(_norm(owner).split()))


def resolve_parcel(deed: dict, appt: dict, parcel_lookup, name_resolver, max_parcels: int = 1) -> dict:
    """One parcel for this deed of trust, or the plain reason there is none.

    Returns {"outcome": "matched", "address", "owner", "account", "borrowers", "via"} or
    {"outcome": <why not>, "borrowers": [...]}. Raises nothing: a lookup error is an outcome
    ("lookup failed") that leaves the appointment open for tomorrow."""
    borrowers = borrowers_of(deed)
    if not borrowers:
        return {"outcome": "the deed of trust names no borrower", "borrowers": []}

    for pin in (deed.get("prp_ids") or []) + (appt.get("prp_ids") or []):
        try:
            got = parcel_lookup(pin)
        except Exception as exc:  # noqa: BLE001
            return {"outcome": "lookup failed", "borrowers": borrowers, "error": "%s: %s" % (type(exc).__name__, str(exc)[:80])}
        if not got or not got.get("address"):
            continue
        owner = got.get("owner") or ""
        if any(name_in_owner(b, owner) for b in borrowers):
            # The roll's own account number when it gave one: that is the form the tax bill
            # lookup downstream is keyed on.
            return {"outcome": "matched", "address": got["address"], "owner": owner,
                    "account": got.get("account") or pin,
                    "borrowers": borrowers, "via": "parcel id on the deed of trust"}
        return {"outcome": "the house has a different owner today", "borrowers": borrowers}

    hits: dict[str, dict] = {}
    for b in borrowers:
        got = name_resolver({"name": b})
        if got.get("error"):
            return {"outcome": "lookup failed", "borrowers": borrowers, "error": got["error"]}
        if got.get("institutional"):
            return {"outcome": "the borrower holds too many parcels to be a homeowner", "borrowers": borrowers}
        for p in got.get("parcels") or []:
            if p.get("address") and name_in_owner(b, p.get("owner") or ""):
                hits.setdefault(str(p.get("account") or p.get("address")), p)
    if not hits:
        return {"outcome": "no parcel on the tax roll in the borrower's name", "borrowers": borrowers}
    if len(hits) > max_parcels:
        return {"outcome": "the borrower owns %d parcels and the deed of trust does not say which" % len(hits),
                "borrowers": borrowers}
    account, p = next(iter(hits.items()))
    return {"outcome": "matched", "address": p["address"], "owner": p.get("owner") or "", "account": account,
            "borrowers": borrowers, "via": "the borrower's name on the tax roll"}


def _note(appt: dict, deed: dict, found: dict) -> str:
    amount = deed.get("consideration")
    return ("Substitute trustee appointed %s (instrument %s) on the deed of trust %s recorded %s. Borrower: %s. "
            "Lender or trustee on the deed of trust: %s. %sAppointing party: %s. Substitute trustee: %s. "
            "Matched by %s."
            % (appt.get("recorded") or "date not read", appt["instrument"], deed["instrument"],
               deed.get("recorded") or "date not read", "; ".join(found["borrowers"]),
               "; ".join(deed.get("grantees") or []) or "not named",
               ("Original loan $%s. " % format(amount, ",.2f")) if amount else "",
               "; ".join(appt.get("grantors") or []) or "not named",
               "; ".join(appt.get("grantees") or []) or "not named", found["via"]))[:880]


def _row(appt: dict, deed: dict, found: dict) -> dict:
    """One staged record, in the shape knox_ftm_pull's loaders hand to the buy box."""
    return {
        "source": "register_of_deeds_appointment",
        "feed": FEED_KEY,
        "notice_type": NOTICE_TYPE,
        "address": found["address"],
        "owner_raw": found.get("owner") or "",
        "case_number": appt["instrument"],
        "event_date": appt.get("recorded") or "",
        "filed_date": appt.get("recorded") or "",
        "tax_account": found.get("account") or "",
        "loan_amount": deed.get("consideration"),
        "notes": _note(appt, deed, found),
        "source_url": SOURCE_URL,
    }


def prepare(scratch: str, months: int | None = None, fetch_rows=None, parcel_lookup=None,
            name_resolver=None, sleep=time.sleep, pause: float = 2.0, today: date | None = None) -> dict:
    """Pull the window's appointments, follow the new ones to a house, and stage them.

    fetch_rows(label, d_from, d_to) -> raw grid rows. The live default replays the recorder
    session knox_rod already has open (logging in only when there is none)."""
    import knox_liens_daily
    import knox_rod

    os.makedirs(scratch, exist_ok=True)
    if fetch_rows is None:
        fields, headers = knox_rod.capture_cached()
        for label in (appt_label(), dot_label()):
            if knox_rod.doc_code(label) is None:
                raise AppointmentsUnusable(
                    "the recorder's doc-type tree has no %r type (it lists %d). Set KNOX_ROD_APPT_LABEL "
                    "or KNOX_ROD_DOT_LABEL to the label the tree shows." % (label, len(knox_rod.DOC_TREE)))

        def fetch_rows(label, d_from, d_to):
            return knox_rod.fetch_type(fields, headers, label, d_from, d_to, sleep=pause)
    if parcel_lookup is None:
        parcel_lookup = parcel_by_id
    if name_resolver is None:
        from knox_lien_resolve import resolve as name_resolver

    today = today or date.today()
    windows = knox_rod._month_windows(months or knox_liens_daily.months_to_pull(today), today)
    rows: list = []
    for d_from, d_to in windows:
        rows.extend(fetch_rows(appt_label(), d_from, d_to))
        sleep(pause)
    appts = _parties(rows)

    # Proof of life. Nothing below can tell "no appointment was recorded" from "the search read
    # nothing", so that is settled here, on the one fact that separates them: how long a stretch
    # the pull just covered. A short stretch with none is a quiet week. A long one is a fault.
    if not appts and quiet_days():
        m, d, y = (int(x) for x in windows[0][0].split("/"))
        covered = (today - date(y, m, d)).days + 1
        if covered >= quiet_days():
            raise AppointmentsUnusable(
                "no %r document was recorded between %s and today (%d days). Knox records substitute "
                "trustee appointments most business days, so the type label is wrong or the search came "
                "back empty. Check KNOX_ROD_APPT_LABEL against the recorder's doc-type tree."
                % (appt_label(), windows[0][0], covered))

    fresh = [a for a in appts.values() if ftm_staging.is_new(scratch, FEED, a["instrument"])]
    max_lookups = _env_int("KNOX_APPT_MAX_LOOKUPS", 12)
    max_parcels = _env_int("KNOX_APPT_MAX_PARCELS", 1)

    day_cache: dict[str, dict] = {}
    lookups = 0
    staged_rows, settled, outcomes = [], [], {}
    with_ref = looked = found_deed = 0

    def count(why: str) -> None:
        outcomes[why] = outcomes.get(why, 0) + 1

    for appt in sorted(fresh, key=lambda a: a["instrument"]):
        refs = [r for r in appt["refs"] if instrument_day(r)]
        if not refs:
            count("names no deed of trust the index can follow")
            settled.append(appt["instrument"])
            continue
        with_ref += 1
        deed, deferred = None, False
        for ref in refs:
            day = instrument_day(ref)
            if day not in day_cache:
                if lookups >= max_lookups:
                    deferred = True
                    break
                lookups += 1
                day_cache[day] = _parties(fetch_rows(dot_label(), day, day))
                sleep(pause)
            deed = day_cache[day].get(ref)
            if deed:
                break
        if deferred:
            count("left for tomorrow (the day's lookup cap was reached)")
            continue
        looked += 1
        if not deed:
            count("its deed of trust was not found on the day the number names")
            settled.append(appt["instrument"])
            continue
        found_deed += 1
        found = resolve_parcel(deed, appt, parcel_lookup, name_resolver, max_parcels)
        if found["outcome"] == "lookup failed":
            count("left for tomorrow (the tax roll lookup failed)")
            continue
        settled.append(appt["instrument"])
        if found["outcome"] != "matched":
            count(found["outcome"])
            continue
        count("matched to a house")
        staged_rows.append(_row(appt, deed, found))

    # A feed that cannot work must not look like a quiet month.
    judged = len(fresh) - outcomes.get("left for tomorrow (the day's lookup cap was reached)", 0)
    if judged >= MIN_FOR_VERDICT and with_ref == 0:
        raise AppointmentsUnusable(
            "%d substitute trustee appointments were read and none names the deed of trust it belongs to, "
            "so no borrower can be identified from the index. This feed cannot produce a lead as built."
            % judged)
    if looked >= MIN_FOR_VERDICT and found_deed == 0:
        raise AppointmentsUnusable(
            "%d appointments name a deed of trust and none was found under the %r type on its recording "
            "day. Check KNOX_ROD_DOT_LABEL against the recorder's doc-type tree." % (looked, dot_label()))

    ftm_staging.stage(scratch, FEED, staged_rows, settled)
    return {
        "window": "%s .. %s" % (windows[0][0], windows[-1][1]),
        "in_window": len(appts), "new": len(fresh), "staged": len(staged_rows), "settled": len(settled),
        "lookups": lookups, "outcomes": outcomes,
        "subscription_days": (getattr(knox_rod, "SUBSCRIPTION", {}) or {}).get("days"),
    }


def describe(stats: dict) -> str:
    """One plain sentence for the run report."""
    if not stats["new"]:
        return "no new substitute trustee appointments (%d in the window, all handled)" % stats["in_window"]
    parts = ["%d %s" % (n, why) for why, n in sorted(stats["outcomes"].items(), key=lambda kv: -kv[1])]
    return "%d new substitute trustee appointment(s) (%s): %s" % (stats["new"], stats["window"], ", ".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--months", type=int, default=0)
    a = ap.parse_args()
    stats = prepare(a.scratch, a.months or None)
    print(describe(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
