"""Weekly probate-list cleanup — bring Smarty + MOD-IV + Tracerfy to 31 records
that the upstream pipeline couldn't auto-match for Week 21.

Stitches together existing building blocks; no new modules:
  - src/address_standardizer.standardize_addresses
  - src/nj_taxrecords.lookup_by_address  (Middlesex / Somerset / Union)
  - deep_prospecting/sources/tracerfy.search  (paid fallback / Essex)

Run with:
  PYTHONPATH=src python deep_prospecting/run_week21_not_found.py

Output: deep_prospecting/outputs/{date}/week-21-not-found-enriched.csv
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# src/ for address_standardizer + nj_taxrecords; ROOT for the
# deep_prospecting package (Tracerfy wrapper imports from its own pkg).
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

INPUT_CSV = ROOT / "deep_prospecting" / "inputs" / "week-21-not-found.csv"
OUTPUT_DIR = ROOT / "deep_prospecting" / "outputs" / datetime.now().strftime("%Y-%m-%d")
OUTPUT_CSV = OUTPUT_DIR / "week-21-not-found-enriched.csv"

# Smarty: $0.0025/lookup (Smarty US-Street rate). Tracerfy: cost_usd on result.
SMARTY_PER_LOOKUP_USD = 0.0025

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────

# Common misspellings in source data. nj_taxrecords.COUNTY_CODES is the
# canonical set; we map the input county to this casing so MOD-IV lookups
# don't silently miss on a typo.
_COUNTY_CANONICAL = {
    "essex": "Essex",
    "middlesex": "Middlesex",
    "somerset": "Somerset",
    "sommerset": "Somerset",  # observed typo in Week 21 input
    "union": "Union",
}


def _canonical_county(raw: str) -> str:
    return _COUNTY_CANONICAL.get((raw or "").strip().lower(), (raw or "").strip())


def _name_tokens(name: str) -> set[str]:
    """Lowercased word tokens, with common suffixes stripped, for fuzzy match."""
    if not name:
        return set()
    bad = {"jr", "sr", "ii", "iii", "iv", "esq", "estate", "deceased", "the", "of"}
    return {t for t in name.lower().replace(",", " ").replace(".", " ").split() if t and t not in bad}


def _classify_owner(owner: str, decedent: str, rep_first: str, rep_last: str) -> str:
    """Bucket the owner against decedent + rep names. Looks for last-name +
    one other token overlap rather than full-string equality (MOD-IV often
    stores "LAST, FIRST" / "LAST FIRST" / "LAST FIRST & SPOUSE")."""
    if not owner:
        return "NO_MATCH"
    owner_t = _name_tokens(owner)
    decedent_t = _name_tokens(decedent)
    rep_t = _name_tokens(f"{rep_first} {rep_last}")
    if decedent_t and len(owner_t & decedent_t) >= 2:
        return "DECEDENT_ON_TITLE"
    if rep_t and len(owner_t & rep_t) >= 2:
        return "POST_TRANSFER_TO_REP"
    return "THIRD_PARTY"


@dataclass
class EnrichedRow:
    """Result of one record's pass through Smarty + owner lookup."""
    original: dict
    smarty_status: str = ""
    smarty_corrected_address: str = ""
    smarty_corrected_city: str = ""
    smarty_corrected_state: str = ""
    smarty_corrected_zip: str = ""
    smarty_county_match: str = ""
    owner_of_record: str = ""
    owner_source: str = ""
    owner_mailing_address: str = ""
    last_sale_date: str = ""
    parcel_id: str = ""
    match_status: str = "NO_MATCH"
    notes: str = ""
    tracerfy_cost_usd: float = 0.0


# ── Pipeline ─────────────────────────────────────────────────────────────

def _read_input() -> list[dict]:
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"missing {INPUT_CSV}")
    with INPUT_CSV.open() as f:
        return list(csv.DictReader(f))


def _smarty_pass(rows: list[dict]) -> list[EnrichedRow]:
    """Run all 31 addresses through Smarty in one batch and capture the
    corrected components + DPV match code per row."""
    from address_standardizer import standardize_addresses
    from notice_parser import NoticeData
    from config import SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN

    notices = [
        NoticeData(
            address=r["Address"], city=r["City"], state=r["State"], zip=r["Zip"],
            owner_name=r["Deceased Full Name"], notice_type="probate",
            county=_canonical_county(r["County"]),
        )
        for r in rows
    ]
    standardize_addresses(notices, SMARTY_AUTH_ID, SMARTY_AUTH_TOKEN)

    results: list[EnrichedRow] = []
    for raw, n in zip(rows, notices):
        e = EnrichedRow(original=raw)
        e.smarty_corrected_address = n.address or ""
        e.smarty_corrected_city = n.city or ""
        e.smarty_corrected_state = n.state or ""
        e.smarty_corrected_zip = n.zip_plus4 or n.zip or ""
        dpv = (n.dpv_match_code or "").upper()
        # DPV "Y" = confirmed deliverable; "N" / "" = no match / unparseable.
        if dpv == "Y":
            e.smarty_status = "Y"
        elif dpv:
            e.smarty_status = f"N ({dpv})"
        else:
            e.smarty_status = "N (no_dpv_code)"
        # County cross-check: Smarty returns the county for the corrected
        # address. Mismatches usually mean a wrong city/zip in the source.
        input_county = _canonical_county(raw["County"])
        smarty_county = (n.county or "").strip()
        if not smarty_county:
            e.smarty_county_match = "unknown"
        elif smarty_county.lower() == input_county.lower():
            e.smarty_county_match = "match"
        else:
            e.smarty_county_match = f"mismatch (Smarty={smarty_county})"
        results.append(e)
    return results


async def _owner_pass(rows: list[EnrichedRow]) -> None:
    """For each row, look up owner of record. MOD-IV first if county is
    supported (Middlesex/Somerset/Union — free); Tracerfy fallback otherwise
    or on MOD-IV miss (paid, ~$0.10/hit)."""
    from nj_taxrecords import lookup_by_address, COUNTY_CODES
    from deep_prospecting.sources.tracerfy import search as tracerfy_search

    for e in rows:
        # Use Smarty-corrected address if available, else fall back to input.
        addr = e.smarty_corrected_address or e.original["Address"]
        city = e.smarty_corrected_city or e.original["City"]
        state = e.smarty_corrected_state or e.original["State"]
        # MOD-IV expects 5-digit ZIP, not ZIP+4. Smarty stores zip_plus4 as
        # "12345-6789"; carry the corrected 5-digit version separately.
        zip5 = (e.smarty_corrected_zip or e.original["Zip"]).split("-")[0]
        county = _canonical_county(e.original["County"])

        # MOD-IV first
        mod_iv_tried = False
        if county in COUNTY_CODES:
            mod_iv_tried = True
            try:
                parcels = lookup_by_address(addr, county)
            except Exception as ex:
                logger.warning("MOD-IV %s '%s' raised: %s", county, addr, ex)
                parcels = []
            if parcels:
                p = parcels[0]
                e.owner_of_record = p.owner_name or ""
                e.owner_source = "MOD-IV"
                e.owner_mailing_address = " / ".join(
                    s for s in [p.mailing_street, p.mailing_city_state] if s
                )
                e.last_sale_date = p.last_sale_date or ""
                e.parcel_id = p.parcel_id or ""
                if len(parcels) > 1:
                    e.notes = (e.notes + "; " if e.notes else "") + f"MOD-IV: {len(parcels)} parcels at address (took first)"

        # Tracerfy fallback — when MOD-IV is unsupported (Essex) or missed.
        if not e.owner_of_record:
            try:
                tr = await tracerfy_search(
                    address=addr, city=city, state=state, zip=zip5, find_owner=True,
                )
            except Exception as ex:
                logger.warning("Tracerfy '%s' raised: %s", addr, ex)
                tr = None

            e.tracerfy_cost_usd = getattr(tr, "cost_usd", 0.0) or 0.0
            if tr and tr.hit and tr.persons:
                # Prefer person flagged property_owner; fall back to first.
                owner = next((p for p in tr.persons if p.property_owner), tr.persons[0])
                e.owner_of_record = f"{owner.first_name} {owner.last_name}".strip()
                e.owner_source = "Tracerfy"
                ma = owner.mailing_address
                e.owner_mailing_address = " / ".join(
                    s for s in [
                        ma.street, f"{ma.city}, {ma.state} {ma.zip}".strip(", ")
                    ] if s.strip(", ")
                )
                # Tracerfy doesn't return parcel_id / last_sale_date.
                if not mod_iv_tried:
                    e.notes = (e.notes + "; " if e.notes else "") + "Essex skipped MOD-IV (unsupported)"
            else:
                if mod_iv_tried:
                    e.notes = (e.notes + "; " if e.notes else "") + "MOD-IV miss; Tracerfy miss"
                else:
                    e.notes = (e.notes + "; " if e.notes else "") + "Essex Tracerfy miss"

        # Classify regardless of owner source.
        e.match_status = _classify_owner(
            e.owner_of_record,
            e.original["Deceased Full Name"],
            e.original["Rep First Name"],
            e.original["Rep Last Name"],
        )


def _write_output(rows: list[EnrichedRow], path: Path | None = None) -> Path:
    out = path or OUTPUT_CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "Address", "City", "State", "Zip",
        "Deceased Full Name", "Rep First Name", "Rep Last Name", "County",
        "Smarty_Status", "Smarty_Corrected_Address", "Smarty_Corrected_City",
        "Smarty_Corrected_State", "Smarty_Corrected_Zip", "Smarty_County_Match",
        "Owner_of_Record", "Owner_Source", "Owner_Mailing_Address",
        "Last_Sale_Date", "Parcel_ID", "Match_Status", "Notes",
    ]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in rows:
            w.writerow({
                **e.original,
                "Smarty_Status": e.smarty_status,
                "Smarty_Corrected_Address": e.smarty_corrected_address,
                "Smarty_Corrected_City": e.smarty_corrected_city,
                "Smarty_Corrected_State": e.smarty_corrected_state,
                "Smarty_Corrected_Zip": e.smarty_corrected_zip,
                "Smarty_County_Match": e.smarty_county_match,
                "Owner_of_Record": e.owner_of_record,
                "Owner_Source": e.owner_source,
                "Owner_Mailing_Address": e.owner_mailing_address,
                "Last_Sale_Date": e.last_sale_date,
                "Parcel_ID": e.parcel_id,
                "Match_Status": e.match_status,
                "Notes": e.notes,
            })
    return out


def _load_retry_rows(prior_csv: Path) -> list[EnrichedRow]:
    """Build EnrichedRow objects from a prior enriched CSV, filtered to
    rows still flagged NO_MATCH. Carries the Smarty-corrected fields
    forward so the retry only re-runs the owner-lookup phase."""
    if not prior_csv.exists():
        raise FileNotFoundError(f"prior enriched CSV not found: {prior_csv}")
    out: list[EnrichedRow] = []
    with prior_csv.open() as f:
        for row in csv.DictReader(f):
            if (row.get("Match_Status") or "").strip() != "NO_MATCH":
                continue
            # The original 8-column input survives verbatim under the
            # first 8 fieldnames; isolate them so _write_output's
            # `**e.original` spread doesn't double-write enrichment fields.
            original = {k: row[k] for k in (
                "Address", "City", "State", "Zip",
                "Deceased Full Name", "Rep First Name", "Rep Last Name", "County",
            )}
            e = EnrichedRow(original=original)
            e.smarty_status = row.get("Smarty_Status", "") or ""
            e.smarty_corrected_address = row.get("Smarty_Corrected_Address", "") or ""
            e.smarty_corrected_city = row.get("Smarty_Corrected_City", "") or ""
            e.smarty_corrected_state = row.get("Smarty_Corrected_State", "") or ""
            e.smarty_corrected_zip = row.get("Smarty_Corrected_Zip", "") or ""
            e.smarty_county_match = row.get("Smarty_County_Match", "") or ""
            # Owner fields intentionally cleared — the retry overwrites them.
            # Notes carries forward so we don't lose the prior diagnostic.
            e.notes = row.get("Notes", "") or ""
            out.append(e)
    return out


def _report(rows: list[EnrichedRow], out_path: Path, *, smarty_called: bool) -> None:
    total = len(rows)
    smarty_y = sum(1 for r in rows if r.smarty_status == "Y")
    mod_iv = sum(1 for r in rows if r.owner_source == "MOD-IV")
    tracerfy = sum(1 for r in rows if r.owner_source == "Tracerfy")
    no_owner = sum(1 for r in rows if not r.owner_source)
    buckets = {"DECEDENT_ON_TITLE": 0, "POST_TRANSFER_TO_REP": 0, "THIRD_PARTY": 0, "NO_MATCH": 0}
    for r in rows:
        buckets[r.match_status] += 1
    # Smarty cost only accrues when we actually called Smarty this run; in
    # retry mode the prior CSV's Smarty_Status carries forward unbilled.
    smarty_cost = total * SMARTY_PER_LOOKUP_USD if smarty_called else 0.0
    tracerfy_cost = sum(r.tracerfy_cost_usd for r in rows)
    total_cost = smarty_cost + tracerfy_cost

    print()
    print("=" * 60)
    title_mode = "fresh" if smarty_called else "retry (NO_MATCH rows only)"
    print(f"Week 21 probate cleanup — {title_mode}")
    print("=" * 60)
    print(f"Total rows: {total}")
    smarty_label = "Smarty DPV-confirmed" if smarty_called else "Smarty DPV-confirmed (from prior run)"
    print(f"{smarty_label}: {smarty_y}/{total}")
    print(f"Owner lookups: MOD-IV {mod_iv} · Tracerfy {tracerfy} · no-owner {no_owner}")
    print(f"Match buckets:")
    for k, v in buckets.items():
        print(f"  {k}: {v}")
    print(f"Cost: ${total_cost:.2f}  (Smarty ${smarty_cost:.2f} · Tracerfy ${tracerfy_cost:.2f})")
    print(f"Output: {out_path}")
    print()


async def _amain(retry_from: Path | None) -> int:
    # config import here triggers dotenv load so TRACERFY_API_KEY etc.
    # are available to the analytics probe below.
    import config  # noqa: F401
    from deep_prospecting.sources.tracerfy import preflight_check

    if retry_from is not None:
        enriched = _load_retry_rows(retry_from)
        logger.info(
            "Retry mode: %d NO_MATCH rows loaded from %s",
            len(enriched), retry_from.name,
        )
        if not enriched:
            print("Nothing to retry — prior CSV has 0 NO_MATCH rows.")
            return 0
        out_path = OUTPUT_DIR / "week-21-not-found-retry-enriched.csv"
    else:
        rows = _read_input()
        logger.info("Loaded %d rows from %s", len(rows), INPUT_CSV.name)
        enriched = _smarty_pass(rows)
        logger.info("Smarty pass complete — running owner lookups")
        out_path = OUTPUT_CSV

    # Tracerfy credit pre-flight. The fresh batch sizes batch_size to the
    # full 31 (any row could fall through to Tracerfy); the retry path
    # sizes to the actual NO_MATCH count we just loaded.
    ok, msg = preflight_check(batch_size=len(enriched))
    if not ok:
        print(msg, file=sys.stderr)
        return 1
    logger.info("%s", msg)

    await _owner_pass(enriched)
    path = _write_output(enriched, out_path)
    logger.info("Wrote %s", path)
    _report(enriched, path, smarty_called=(retry_from is None))
    return 0


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Week 21 probate cleanup batch")
    p.add_argument(
        "--retry-no-match", type=Path, metavar="PATH", default=None,
        help=(
            "Re-process only the NO_MATCH rows from a prior enriched CSV "
            "(skips Smarty; just retries owner lookup). "
            "Output: outputs/{date}/week-21-not-found-retry-enriched.csv"
        ),
    )
    args = p.parse_args()
    return asyncio.run(_amain(retry_from=args.retry_no_match))


if __name__ == "__main__":
    sys.exit(main())
