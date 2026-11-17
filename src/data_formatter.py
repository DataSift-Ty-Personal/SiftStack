"""Format parsed notices into REI Sift CRM upload CSV."""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Column order matches the Sift upload template exactly.
# Sift standard columns first, then our extra metadata columns.
SIFT_COLUMNS = [
    "full_name",
    "address",
    "city",
    "state",
    "zip",
    "first_name",
    "last_name",
    "Owner Street",
    "Owner City",
    "Owner State",
    "Owner ZIP Code",
    "Date Added",
    "Notice Publish Date",
    # Extra columns (not in Sift template but useful for filtering)
    "notice_type",
    "county",
    "decedent_name",
    "auction_date",
    # Smarty address standardization fields
    "zip_plus4",
    "latitude",
    "longitude",
    "dpv_match_code",
    "vacant",
    "rdi",
    # Zillow property enrichment fields
    "mls_status",
    "mls_listing_price",
    "mls_last_sold_date",
    "mls_last_sold_price",
    "estimated_value",
    "estimated_equity",
    "equity_percent",
    "property_type",
    "bedrooms",
    "bathrooms",
    "sqft",
    "year_built",
    "lot_size",
    # County assessor / tax fields
    "parcel_id",
    "tax_delinquent_amount",
    "tax_delinquent_years",
    "deceased_indicator",
    "tax_owner_name",
    # Obituary-confirmed deceased owner fields
    "owner_deceased",
    "date_of_death",
    "obituary_url",
    "decision_maker_name",
    "decision_maker_relationship",
    # Deep prospecting — ranked decision-makers + error map
    "decision_maker_status",
    "decision_maker_source",
    "decision_maker_street",
    "decision_maker_city",
    "decision_maker_state",
    "decision_maker_zip",
    "decision_maker_2_name",
    "decision_maker_2_relationship",
    "decision_maker_2_status",
    "decision_maker_3_name",
    "decision_maker_3_relationship",
    "decision_maker_3_status",
    "obituary_source_type",
    "heir_search_depth",
    "heirs_verified_living",
    "heirs_verified_deceased",
    "heirs_unverified",
    "dm_confidence",
    "dm_confidence_reason",
    "missing_data_flags",
    "heir_map_json",
    "mailable",
    # Entity research fields
    "entity_type",
    "entity_person_name",
    "entity_person_role",
    "entity_research_source",
    "entity_research_confidence",
    "source_url",
    # Proof-of-source auction notice screenshot (permanent Dropbox ?raw=1 URL)
    "notice_screenshot_url",
    # Pipeline metadata
    "run_id",
]


def _format_date_sift(iso_date: str) -> str:
    """Convert YYYY-MM-DD to M/D/YYYY for Sift import."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return iso_date


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (first_name, last_name).

    Handles common patterns:
      "John Doe"         → ("John", "Doe")
      "John A. Doe"      → ("John A.", "Doe")
      "John Doe And Jane Doe" → ("John", "Doe And Jane Doe")
    """
    if not full_name:
        return ("", "")
    parts = full_name.strip().split()
    if len(parts) == 1:
        return (parts[0], "")
    first = parts[0]
    rest = parts[1:]
    return (first, " ".join(rest))


def _notice_id_from_url(url: str) -> str:
    """Extract the numeric notice ID from a source URL.

    URLs look like: .../Details.aspx?SID=...&ID=509975
    Returns the ID value, or empty string if not found.
    """
    import re
    m = re.search(r"[?&]ID=(\d+)", url)
    return m.group(1) if m else ""


def deduplicate(notices: list[NoticeData]) -> list[NoticeData]:
    """Collapse duplicates: the same notice twice, and one PROPERTY re-noticed.

    Two passes, and the second one is the point:

      1. by notice ID, which catches the identical notice arriving through two
         saved searches.
      2. by (notice_type, address, city), which catches the same property
         advertised repeatedly. A continued or republished foreclosure sale gets
         a NEW notice id every week, so pass 1 lets every one of them through:
         a single staged month came out 134 rows for 88 distinct properties, 46
         rows of pure repetition. `POST /property/` is upsert by address, so
         those extra rows do not create duplicate CRM records, but they do mean
         the same property is written several times and whichever row happens to
         land last wins, which is not a decision anyone made.

    Keyed on notice_type as well as address on purpose: a house with BOTH a
    probate filing and a foreclosure notice is two real leads on two different
    lists, and collapsing them would silently drop one.

    Tie-break is publication date, most recent wins. date_added is the run date
    and identical across a run, so it cannot order anything.
    """
    seen_ids: set[str] = set()
    seen_parcels: set[str] = set()
    id_pass: list[NoticeData] = []

    for notice in notices:
        nid = _notice_id_from_url(notice.source_url)
        if nid:
            if nid in seen_ids:
                continue
            seen_ids.add(nid)
            id_pass.append(notice)
            continue

        # By parcel_id (PDF imports carry no notice id)
        pid = notice.parcel_id.strip()
        if pid:
            if pid in seen_parcels:
                continue
            seen_parcels.add(pid)
        id_pass.append(notice)

    # ── Pass 2: one row per property per notice type ──────────────────
    best: dict[tuple, NoticeData] = {}
    order: list[tuple] = []
    result: list[NoticeData] = []

    for notice in id_pass:
        addr = notice.address.strip().lower()
        if not addr:
            # No address yet (probate before lookup). Nothing to key on, and
            # collapsing them would merge unrelated estates into one row.
            result.append(notice)
            continue

        key = (notice.notice_type, addr, notice.city.strip().lower())
        existing = best.get(key)
        if existing is None:
            best[key] = notice
            order.append(key)
        elif ((notice.date_published or notice.date_added)
                > (existing.date_published or existing.date_added)):
            best[key] = notice

    result.extend(best[k] for k in order)

    removed = len(notices) - len(result)
    if removed:
        logger.info(
            "Deduplicated: %d -> %d (removed %d; same notice or re-noticed property)",
            len(notices), len(result), removed,
        )
    return result


def write_csv(notices: list[NoticeData], filename: str | None = None) -> Path:
    """Write notices to a Sift-formatted CSV file.

    Args:
        notices: List of parsed and filtered NoticeData objects.
        filename: Optional filename override. Defaults to date-stamped name.

    Returns:
        Path to the written CSV file.
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"tn_notices_{timestamp}.csv"

    output_path = OUTPUT_DIR / filename
    written = 0

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SIFT_COLUMNS)
        writer.writeheader()

        for notice in notices:
            first, last = _split_name(notice.owner_name)
            row = {
                "full_name": notice.owner_name,
                "address": notice.address,
                "city": notice.city,
                "state": notice.state,
                "zip": notice.zip,
                "first_name": first,
                "last_name": last,
                "Owner Street": notice.owner_street,
                "Owner City": notice.owner_city,
                "Owner State": notice.owner_state,
                "Owner ZIP Code": notice.owner_zip,
                "Date Added": _format_date_sift(notice.date_added),
                "Notice Publish Date": _format_date_sift(notice.date_published),
                "notice_type": notice.notice_type,
                "county": notice.county,
                "decedent_name": notice.decedent_name,
                "auction_date": _format_date_sift(notice.auction_date),
                "zip_plus4": notice.zip_plus4,
                "latitude": notice.latitude,
                "longitude": notice.longitude,
                "dpv_match_code": notice.dpv_match_code,
                "vacant": notice.vacant,
                "rdi": notice.rdi,
                "mls_status": notice.mls_status,
                "mls_listing_price": notice.mls_listing_price,
                "mls_last_sold_date": _format_date_sift(notice.mls_last_sold_date),
                "mls_last_sold_price": notice.mls_last_sold_price,
                "estimated_value": notice.estimated_value,
                "estimated_equity": notice.estimated_equity,
                "equity_percent": notice.equity_percent,
                "property_type": notice.property_type,
                "bedrooms": notice.bedrooms,
                "bathrooms": notice.bathrooms,
                "sqft": notice.sqft,
                "year_built": notice.year_built,
                "lot_size": notice.lot_size,
                "parcel_id": notice.parcel_id,
                "tax_delinquent_amount": notice.tax_delinquent_amount,
                "tax_delinquent_years": notice.tax_delinquent_years,
                "deceased_indicator": notice.deceased_indicator,
                "tax_owner_name": notice.tax_owner_name,
                "owner_deceased": notice.owner_deceased,
                "date_of_death": notice.date_of_death,
                "obituary_url": notice.obituary_url,
                "decision_maker_name": notice.decision_maker_name,
                "decision_maker_relationship": notice.decision_maker_relationship,
                "decision_maker_status": notice.decision_maker_status,
                "decision_maker_source": notice.decision_maker_source,
                "decision_maker_street": notice.decision_maker_street,
                "decision_maker_city": notice.decision_maker_city,
                "decision_maker_state": notice.decision_maker_state,
                "decision_maker_zip": notice.decision_maker_zip,
                "decision_maker_2_name": notice.decision_maker_2_name,
                "decision_maker_2_relationship": notice.decision_maker_2_relationship,
                "decision_maker_2_status": notice.decision_maker_2_status,
                "decision_maker_3_name": notice.decision_maker_3_name,
                "decision_maker_3_relationship": notice.decision_maker_3_relationship,
                "decision_maker_3_status": notice.decision_maker_3_status,
                "obituary_source_type": notice.obituary_source_type,
                "heir_search_depth": notice.heir_search_depth,
                "heirs_verified_living": notice.heirs_verified_living,
                "heirs_verified_deceased": notice.heirs_verified_deceased,
                "heirs_unverified": notice.heirs_unverified,
                "dm_confidence": notice.dm_confidence,
                "dm_confidence_reason": notice.dm_confidence_reason,
                "missing_data_flags": notice.missing_data_flags,
                "heir_map_json": notice.heir_map_json,
                "mailable": notice.mailable,
                "entity_type": notice.entity_type,
                "entity_person_name": notice.entity_person_name,
                "entity_person_role": notice.entity_person_role,
                "entity_research_source": notice.entity_research_source,
                "entity_research_confidence": notice.entity_research_confidence,
                "source_url": notice.source_url,
                "notice_screenshot_url": getattr(notice, "notice_screenshot_url", ""),
                "run_id": notice.run_id,
            }
            writer.writerow(row)
            written += 1

    logger.info("Wrote %d notices to %s", written, output_path)
    return output_path


def write_csv_by_type(notices: list[NoticeData]) -> list[Path]:
    """Write separate CSV files per county + notice type.

    Filenames: {county}_{notice_type}_{date}.csv
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # Group by (county, notice_type)
    groups: dict[tuple[str, str], list[NoticeData]] = {}
    for notice in notices:
        key = (notice.county.lower(), notice.notice_type)
        groups.setdefault(key, []).append(notice)

    paths = []
    for (county, ntype), group_notices in sorted(groups.items()):
        filename = f"{county}_{ntype}_{timestamp}.csv"
        path = write_csv(group_notices, filename)
        paths.append(path)

    return paths


# ── CSV Re-Import ────────────────────────────────────────────────────────────

# CSV column → NoticeData field name (where Sift columns differ from field names)
CSV_TO_FIELD = {
    "full_name": "owner_name",
    "Date Added": "date_added",
    "Notice Publish Date": "date_published",
    "Owner Street": "owner_street",
    "Owner City": "owner_city",
    "Owner State": "owner_state",
    "Owner ZIP Code": "owner_zip",
}

# Valid NoticeData field names (for filtering unknown CSV columns)
_NOTICE_FIELDS = {f.name for f in NoticeData.__dataclass_fields__.values()}

# Date columns that use Sift M/D/YYYY format and need conversion back to YYYY-MM-DD
_DATE_FIELDS = {"date_added", "date_published", "auction_date", "mls_last_sold_date"}


def _parse_sift_date(sift_date: str) -> str:
    """Convert M/D/YYYY (Sift format) back to YYYY-MM-DD (internal format).

    Also handles YYYY-MM-DD passthrough and empty strings.
    """
    if not sift_date or not sift_date.strip():
        return ""
    sift_date = sift_date.strip()
    # Already in ISO format?
    if re.match(r"\d{4}-\d{2}-\d{2}", sift_date):
        return sift_date
    # M/D/YYYY → YYYY-MM-DD
    try:
        dt = datetime.strptime(sift_date, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return sift_date


def read_csv(path: str | Path) -> list[NoticeData]:
    """Read a Sift-formatted CSV back into NoticeData objects.

    Handles:
    - Column name mapping (full_name → owner_name, Date Added → date_added, etc.)
    - Date format conversion (M/D/YYYY → YYYY-MM-DD)
    - Graceful handling of missing/extra columns
    - UTF-8-BOM encoding (Excel adds BOM)

    Args:
        path: Path to the CSV file.

    Returns:
        List of NoticeData objects with all available fields populated.
    """
    path = Path(path)
    notices: list[NoticeData] = []

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapped: dict[str, str] = {}
            for csv_col, raw_value in row.items():
                field = CSV_TO_FIELD.get(csv_col, csv_col)
                if field in _NOTICE_FIELDS:
                    val: str = raw_value if raw_value is not None else ""
                    if field in _DATE_FIELDS:
                        val = _parse_sift_date(val)
                    mapped[field] = val  # type: ignore[arg-type]
            notices.append(NoticeData(**mapped))

    logger.info("Read %d records from %s", len(notices), path)
    return notices


def filter_sold(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove properties with mls_status indicating already sold.

    Properties that have sold are no longer actionable — skip them to
    save enrichment API calls and avoid mailing to new owners.
    """
    sold_statuses = {"sold", "closed"}
    before = len(notices)
    result = [
        n for n in notices
        if n.mls_status.strip().lower() not in sold_statuses
    ]
    removed = before - len(result)
    if removed:
        logger.info("Filtered %d sold properties (%d remaining)", removed, len(result))
    return result
