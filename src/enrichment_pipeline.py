"""Unified enrichment pipeline for all data sources.

Provides a single canonical pipeline that all entry points call:
  - Apify Actor (daily/historical web scrape)
  - CLI daily/historical (web scrape)
  - PDF import (OCR tax sale PDFs)
  - CSV re-import (re-enrich existing data)

Each caller acquires data, builds PipelineOptions, and calls
run_enrichment_pipeline(). The pipeline handles dedup, filtering,
and all enrichment steps in a fixed canonical order.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────


@dataclass
class PipelineOptions:
    """Controls which enrichment steps run and passes sub-options."""

    # Step skip flags (default: run everything)
    skip_filter_sold: bool = True  # only CSV re-import sets False
    skip_vacant_filter: bool = False
    skip_entity_filter: bool = False
    skip_entity_research: bool = True   # opt-in via --research-entities
    skip_commercial_filter: bool = False
    skip_parcel_lookup: bool = False
    skip_tax: bool = False
    skip_smarty: bool = False
    skip_geocode: bool = False
    skip_zillow: bool = False
    skip_obituary: bool = False
    skip_ancestry: bool = False

    # Obituary sub-options
    skip_heir_verification: bool = False
    max_heir_depth: int = 2
    skip_dm_address: bool = False
    tracerfy_tier1: bool = False
    deep_heirs: bool = False  # opt-in: resolve heirs via Enformion Person Search

    # Smart detection flags (set by detect_existing_enrichment)
    has_smarty: bool = False
    has_zillow: bool = False
    has_tax: bool = False
    has_obituary: bool = False

    # Context label for summary logging
    source_label: str = ""


# ── Smart detection ──────────────────────────────────────────────────


def detect_existing_enrichment(
    notices: list[NoticeData], opts: PipelineOptions
) -> None:
    """Scan notices for pre-populated enrichment data and set has_* flags.

    Call this only for CSV re-import — fresh scrapes and PDF imports should
    always run all steps.
    """
    opts.has_smarty = any(n.dpv_match_code for n in notices)
    opts.has_zillow = any(n.estimated_value for n in notices)
    opts.has_tax = any(n.parcel_id or n.tax_delinquent_amount for n in notices)
    # Obituary: only skip if >50% of records already have data (not just any())
    deceased_count = sum(1 for n in notices if n.owner_deceased)
    total = len(notices) if notices else 1
    opts.has_obituary = deceased_count > total * 0.5

    if opts.has_smarty:
        logger.info("Smarty data detected — will preserve existing data")
    if opts.has_zillow:
        logger.info("Zillow data detected — will preserve existing data")
    if opts.has_tax:
        logger.info("Tax data detected — will preserve existing data")
    if opts.has_obituary:
        logger.info("Obituary data detected (%d/%d = %.0f%%) — will preserve existing data",
                     deceased_count, total, deceased_count / total * 100)


# ── Filters ──────────────────────────────────────────────────────────


def _filter_vacant_land(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove records where the property address has no real house number.

    Vacant land parcels (e.g., "0 Andersonville Pike", "0000 Old Rd",
    or just "Andersonville Pike") are not actionable for marketing.
    """

    def _has_house_number(addr: str) -> bool:
        addr = addr.strip()
        if not addr:
            return False
        m = re.match(r"^(\d+)", addr)
        if not m:
            return False
        return int(m.group(1)) > 0

    before = len(notices)
    result = [n for n in notices if _has_house_number(n.address)]
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d vacant land records (no house number)", removed)
    return result


def _filter_entity_owners(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove records owned by business entities (LLC, INC, CORP, etc.).

    Personal trusts and estates are NOT filtered — "JOHN DOE TRUST" is a
    person, while "FIRST TENNESSEE BANK TRUST" is a business entity.
    """

    def _is_entity(n: NoticeData) -> bool:
        # Check both tax_owner_name (preferred) and owner_name
        name = (n.tax_owner_name or n.owner_name or "").strip()
        if not name:
            return False
        if not config.BUSINESS_RE.search(name):
            return False
        # Exempt personal trusts/estates (have extractable personal name)
        if config.TRUST_NAME_RE.match(name):
            return False
        if config.ESTATE_OF_RE.match(name):
            return False
        # Entity research found a real person — keep the record
        if n.entity_person_name:
            return False
        return True

    before = len(notices)
    removed_names = []
    result = []
    for n in notices:
        if _is_entity(n):
            removed_names.append(n.tax_owner_name or n.owner_name)
        else:
            result.append(n)
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d entity-owned records", removed)
        for name in removed_names[:10]:
            logger.info("    - %s", name)
        if len(removed_names) > 10:
            logger.info("    ... and %d more", len(removed_names) - 10)
    return result


def _filter_commercial(notices: list[NoticeData]) -> list[NoticeData]:
    """Remove records with Smarty RDI = 'Commercial'.

    Only filters when rdi is explicitly 'Commercial' — empty rdi
    (no Smarty data) passes through.
    """
    before = len(notices)
    result = [n for n in notices if n.rdi.lower() != "commercial"]
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d commercial properties", removed)
    return result


def _compute_mailable(notices: list[NoticeData]) -> None:
    """Set mailable flag: 'yes' if address + city + zip all present."""
    for n in notices:
        if n.address.strip() and n.city.strip() and n.zip.strip():
            n.mailable = "yes"
        else:
            n.mailable = ""


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Generate a timestamped run ID for data lineage tracking."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{ts}_{short_uuid}"


# ── Data Validation ──────────────────────────────────────────────────

# Regex for garbage OCR: mostly non-alphanumeric characters
_GARBAGE_RE = re.compile(r"^[^a-zA-Z0-9]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_records(notices: list[NoticeData]) -> list[NoticeData]:
    """Validate records before export. Removes invalid records and logs issues.

    Checks:
      - address, city, zip must be non-empty
      - address must contain at least one letter (not pure garbage OCR)
      - date fields must be valid YYYY-MM-DD format if present
    """
    valid = []
    invalid_count = 0

    for n in notices:
        issues = []

        # Required fields
        if not n.address.strip():
            issues.append("missing address")
        elif _GARBAGE_RE.match(n.address):
            issues.append(f"garbage address: {n.address!r}")

        if not n.city.strip():
            issues.append("missing city")

        if not n.zip.strip():
            issues.append("missing zip")

        # Date format validation (only if populated)
        for date_field in ("date_added", "date_published", "auction_date"):
            val = getattr(n, date_field, "")
            if val and not _DATE_RE.match(val):
                issues.append(f"bad {date_field}: {val!r}")

        if issues:
            invalid_count += 1
            if invalid_count <= 10:
                label = n.address or n.owner_name or "(unknown)"
                logger.warning("  Validation failed [%s]: %s", label, "; ".join(issues))
            continue

        valid.append(n)

    if invalid_count:
        logger.info("  Removed %d invalid records (validation)", invalid_count)
        if invalid_count > 10:
            logger.info("  ... (showing first 10 of %d)", invalid_count)

    return valid


# ── Pipeline ─────────────────────────────────────────────────────────


def run_enrichment_pipeline(
    notices: list[NoticeData],
    opts: PipelineOptions,
) -> list[NoticeData]:
    """Run the full enrichment pipeline on a list of notices.

    Steps (canonical order):
      1. Filter sold properties
      2. Deduplicate
      3. Vacant land filter
      4. Parcel address lookup
      5. Tax delinquency enrichment
      6. Smarty address standardization
      7. Reverse geocode + Smarty retry
      8. Zillow property enrichment
      9. Obituary deceased owner detection
     10. Compute mailable flag
     11. Log summary

    Returns the (possibly filtered) list, modified in-place.
    """
    from data_formatter import deduplicate

    # ── Step 1: Filter Sold ──────────────────────────────────────────
    if not opts.skip_filter_sold:
        logger.info("── Step 1: Filter Sold Properties ──")
        try:
            from data_formatter import filter_sold

            before = len(notices)
            notices = filter_sold(notices)
            removed = before - len(notices)
            if removed:
                logger.info("  Removed %d sold properties", removed)
            else:
                logger.info("  No sold properties found")
        except Exception as e:
            logger.warning("  Filter sold failed: %s", e)

    # ── Stamp run_id + date_added on all records ────────────────────
    # date_added = when WE added the record (the run date), not the notice's
    # publication date (that lives in date_published). Only stamp records that
    # don't already carry one — PDF/photo imports and CSV re-imports set it
    # explicitly and must be preserved.
    run_id = _generate_run_id()
    today = config.run_date()  # business-tz date, not naive UTC (Apify runs in UTC)
    stamped = 0
    for n in notices:
        n.run_id = run_id
        if not n.date_added.strip():
            n.date_added = today
            stamped += 1
    logger.info(
        "Pipeline run_id: %s (%d records, %d date_added=%s)",
        run_id, len(notices), stamped, today,
    )

    # ── Step 2: Deduplicate ──────────────────────────────────────────
    logger.info("── Step 2: Deduplicate ──")
    before = len(notices)
    notices = deduplicate(notices)
    removed = before - len(notices)
    logger.info(
        "  %d records after dedup%s",
        len(notices),
        f" (removed {removed})" if removed else "",
    )

    # ── Step 3: Vacant Land Filter ───────────────────────────────────
    if not opts.skip_vacant_filter:
        logger.info("── Step 3: Vacant Land Filter ──")
        before = len(notices)
        notices = _filter_vacant_land(notices)
        logger.info("  %d records after filter", len(notices))
    if not notices:
        logger.warning("No records remaining after filtering")
        return notices

    # ── Step 3a: Entity Research ──────────────────────────────────
    if not opts.skip_entity_research:
        if config.ANTHROPIC_API_KEY:
            try:
                from entity_researcher import enrich_entity_data

                enrich_entity_data(notices, config.ANTHROPIC_API_KEY)
            except ImportError:
                logger.warning("  entity_researcher not available — skipping")
            except Exception as e:
                logger.warning("  Entity research failed: %s", e)
        else:
            logger.info("── Step 3a: Entity Research (no API key) ──")
    else:
        logger.info("── Step 3a: Entity Research (skipped) ──")

    # ── Step 3b: Entity Owner Filter ──────────────────────────────
    if not opts.skip_entity_filter:
        logger.info("── Step 3b: Entity Owner Filter ──")
        before = len(notices)
        notices = _filter_entity_owners(notices)
        logger.info("  %d records after filter", len(notices))
    else:
        logger.info("── Step 3b: Entity Owner Filter (skipped) ──")
    if not notices:
        logger.warning("No records remaining after filtering")
        return notices

    # ── Step 3c: Probate Property Lookup ────────────────────────────
    # For probate records without a property address, search Knox Tax API
    # by the decedent's name to find their property.
    probate_no_addr = [
        n for n in notices
        if n.notice_type == "probate"
        and not n.address.strip()
        and n.decedent_name.strip()
        and n.county.lower() == "knox"
    ]
    if probate_no_addr:
        logger.info("── Step 3c: Probate Property Lookup (%d candidates) ──", len(probate_no_addr))
        try:
            from tax_enricher import _probate_property_lookup
            _probate_property_lookup(probate_no_addr)
            found = sum(1 for n in probate_no_addr if n.address.strip())
            logger.info("  Property address found: %d/%d", found, len(probate_no_addr))
        except ImportError:
            logger.warning("  _probate_property_lookup not available — skipping")
        except Exception as e:
            logger.warning("  Probate property lookup failed: %s", e)

    # ── Step 4: Parcel Address Lookup ────────────────────────────────
    if not opts.skip_parcel_lookup and not opts.skip_tax:
        candidates = [
            n
            for n in notices
            if n.parcel_id.strip() and n.county.lower() == "knox"
        ]
        if candidates:
            logger.info(
                "── Step 4: Parcel Address Lookup (%d candidates) ──",
                len(candidates),
            )
            try:
                from tax_enricher import lookup_parcel_addresses

                lookup_parcel_addresses(notices)
            except ImportError:
                logger.warning("  tax_enricher not available — skipping")
            except Exception as e:
                logger.warning("  Parcel address lookup failed: %s", e)
        else:
            logger.info("── Step 4: Parcel Address Lookup (no candidates) ──")
    elif opts.skip_parcel_lookup:
        logger.info("── Step 4: Parcel Address Lookup (skipped) ──")

    # ── Step 5: Tax Delinquency ──────────────────────────────────────
    if not opts.skip_tax and not opts.has_tax:
        logger.info("── Step 5: Tax Delinquency Enrichment ──")
        try:
            from tax_enricher import enrich_tax_delinquency

            enrich_tax_delinquency(notices)
            enriched = sum(1 for n in notices if n.tax_delinquent_years)
            logger.info("  Tax-delinquent: %d/%d", enriched, len(notices))
        except ImportError:
            logger.warning("  tax_enricher not available — skipping")
        except Exception as e:
            logger.warning("  Tax enrichment failed: %s", e)
    elif opts.has_tax:
        logger.info("── Step 5: Tax Delinquency (preserved — data already present) ──")
    elif opts.skip_tax:
        logger.info("── Step 5: Tax Delinquency (skipped) ──")

    # ── Step 6: Smarty Address Standardization ───────────────────────
    if not opts.skip_smarty and not opts.has_smarty:
        if config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
            logger.info("── Step 6: Smarty Address Standardization ──")
            try:
                from address_standardizer import standardize_addresses

                standardize_addresses(
                    notices, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN
                )
                confirmed = sum(
                    1 for n in notices if n.dpv_match_code == "Y"
                )
                logger.info(
                    "  USPS-confirmed: %d/%d", confirmed, len(notices)
                )
            except ImportError:
                logger.warning(
                    "  smartystreets-python-sdk not installed — skipping"
                )
            except Exception as e:
                logger.warning("  Smarty standardization failed: %s", e)
        else:
            logger.info("── Step 6: Smarty (no API keys configured) ──")
    elif opts.has_smarty:
        logger.info(
            "── Step 6: Smarty (preserved — data already present) ──"
        )
    elif opts.skip_smarty:
        logger.info("── Step 6: Smarty (skipped) ──")

    # ── Step 6a: Commercial Property Filter ─────────────────────────
    if not opts.skip_commercial_filter:
        logger.info("── Step 6a: Commercial Property Filter ──")
        before = len(notices)
        notices = _filter_commercial(notices)
        logger.info("  %d records after filter", len(notices))
        if not notices:
            logger.warning("No records remaining after filtering")
            return notices
    else:
        logger.info("── Step 6a: Commercial Property Filter (skipped) ──")

    # ── Step 7: Reverse Geocode Retry ────────────────────────────────
    if (
        not opts.skip_geocode
        and not opts.skip_smarty
        and not opts.has_smarty
    ):
        if config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN:
            logger.info("── Step 7: Reverse Geocode + Smarty Retry ──")
            try:
                from address_standardizer import retry_with_geocoded_city

                retry_with_geocoded_city(
                    notices,
                    config.SMARTY_AUTH_ID,
                    config.SMARTY_AUTH_TOKEN,
                )
            except ImportError:
                pass  # Function may not exist in older builds
            except Exception as e:
                logger.warning("  Reverse geocode retry failed: %s", e)
    else:
        skip_reason = (
            "skipped"
            if opts.skip_geocode
            else "Smarty skipped/preserved"
        )
        logger.info("── Step 7: Reverse Geocode (%s) ──", skip_reason)

    # ── Step 8: Zillow Property Enrichment ───────────────────────────
    if not opts.skip_zillow and not opts.has_zillow:
        if config.OPENWEBNINJA_API_KEY:
            logger.info("── Step 8: Zillow Property Enrichment ──")
            try:
                from property_enricher import enrich_properties

                enrich_properties(notices, config.OPENWEBNINJA_API_KEY)
                enriched = sum(1 for n in notices if n.estimated_value)
                logger.info(
                    "  Zillow-enriched: %d/%d", enriched, len(notices)
                )
            except ImportError:
                logger.warning(
                    "  property_enricher not available — skipping"
                )
            except Exception as e:
                logger.warning("  Zillow enrichment failed: %s", e)
        else:
            logger.info("── Step 8: Zillow (no API key configured) ──")
    elif opts.has_zillow:
        logger.info(
            "── Step 8: Zillow (preserved — data already present) ──"
        )
    elif opts.skip_zillow:
        logger.info("── Step 8: Zillow (skipped) ──")

    # ── Step 9: Obituary Enrichment ──────────────────────────────────
    if not opts.skip_obituary and not opts.has_obituary:
        if config.ANTHROPIC_API_KEY:
            logger.info("── Step 9: Obituary Deceased Owner Detection ──")
            try:
                from obituary_enricher import enrich_obituary_data

                enrich_obituary_data(
                    notices,
                    config.ANTHROPIC_API_KEY,
                    skip_heir_verification=opts.skip_heir_verification,
                    max_heir_depth=opts.max_heir_depth,
                    skip_dm_address=opts.skip_dm_address,
                    tracerfy_tier1=getattr(opts, "tracerfy_tier1", False),
                    skip_ancestry=opts.skip_ancestry,
                    deep_heirs=getattr(opts, "deep_heirs", False),
                )
                confirmed = sum(1 for n in notices if n.owner_deceased)
                logger.info(
                    "  Obituary-confirmed deceased: %d/%d",
                    confirmed,
                    len(notices),
                )
            except ImportError:
                logger.warning(
                    "  obituary_enricher not available — skipping"
                )
            except Exception as e:
                logger.warning("  Obituary enrichment failed: %s", e)
        else:
            logger.info(
                "── Step 9: Obituary (no Anthropic API key configured) ──"
            )
    elif opts.has_obituary:
        logger.info(
            "── Step 9: Obituary (preserved — data already present) ──"
        )
    elif opts.skip_obituary:
        logger.info("── Step 9: Obituary (skipped) ──")

    # ── Step 9b: Data Validation ────────────────────────────────────
    logger.info("── Step 9b: Data Validation ──")
    before = len(notices)
    notices = _validate_records(notices)
    logger.info("  %d records after validation", len(notices))
    if not notices:
        logger.warning("No records remaining after validation")
        return notices

    # ── Step 10: Compute Mailable Flag ───────────────────────────────
    logger.info("── Step 10: Compute Mailable Flag ──")
    _compute_mailable(notices)
    mailable = sum(1 for n in notices if n.mailable)
    logger.info(
        "  Mailable: %d/%d (%.0f%%)",
        mailable,
        len(notices),
        100 * mailable / len(notices) if notices else 0,
    )

    # ── Step 11: Summary ─────────────────────────────────────────────
    _log_summary(notices, opts)

    return notices


# ── Summary ──────────────────────────────────────────────────────────


def _log_summary(notices: list[NoticeData], opts: PipelineOptions) -> None:
    """Log comprehensive summary stats after pipeline completes."""
    total = len(notices)
    if not total:
        return

    logger.info("══ Pipeline Summary (%s) ══", opts.source_label or "unknown")
    logger.info("Total records: %d", total)

    # By type / county
    by_type: dict[str, int] = {}
    by_county: dict[str, int] = {}
    for n in notices:
        by_type[n.notice_type] = by_type.get(n.notice_type, 0) + 1
        by_county[n.county] = by_county.get(n.county, 0) + 1
    for ntype, count in sorted(by_type.items()):
        logger.info("  %s: %d", ntype, count)
    for county, count in sorted(by_county.items()):
        logger.info("  %s county: %d", county, count)

    # Smarty
    smarty_confirmed = sum(1 for n in notices if n.dpv_match_code == "Y")
    if smarty_confirmed:
        logger.info(
            "  Smarty USPS-confirmed: %d/%d (%.0f%%)",
            smarty_confirmed,
            total,
            100 * smarty_confirmed / total,
        )

    # Mailable
    mailable = sum(1 for n in notices if n.mailable)
    logger.info(
        "  Mailable: %d/%d (%.0f%%)",
        mailable,
        total,
        100 * mailable / total,
    )

    # Zillow
    zillow_enriched = sum(1 for n in notices if n.estimated_value)
    if zillow_enriched:
        logger.info("  Zillow-enriched: %d/%d", zillow_enriched, total)
        equity_values = [
            float(n.estimated_equity)
            for n in notices
            if n.estimated_equity
        ]
        if equity_values:
            avg_equity = sum(equity_values) / len(equity_values)
            logger.info("  Avg estimated equity: $%s", f"{avg_equity:,.0f}")

    # Tax
    tax_enriched = sum(1 for n in notices if n.tax_delinquent_years)
    if tax_enriched:
        logger.info("  Tax-delinquent: %d/%d", tax_enriched, total)

    # Deceased indicators
    deceased_count = sum(1 for n in notices if n.deceased_indicator)
    if deceased_count:
        from collections import Counter

        by_indicator = Counter(
            n.deceased_indicator for n in notices if n.deceased_indicator
        )
        breakdown = ", ".join(
            f"{k}: {v}" for k, v in by_indicator.most_common()
        )
        logger.info(
            "  Likely deceased: %d/%d (%s)", deceased_count, total, breakdown
        )

    # Obituary-confirmed
    obit_confirmed = sum(1 for n in notices if n.owner_deceased)
    if obit_confirmed:
        logger.info(
            "  Obituary-confirmed deceased: %d/%d", obit_confirmed, total
        )
        with_dm = sum(1 for n in notices if n.decision_maker_name)
        if with_dm:
            dm_verified = sum(
                1
                for n in notices
                if n.decision_maker_status == "verified_living"
            )
            dm_from_tax = sum(
                1
                for n in notices
                if n.decision_maker_source == "tax_record_joint_owner"
            )
            logger.info(
                "  Decision-maker ID'd: %d/%d (%.0f%%)",
                with_dm,
                obit_confirmed,
                100 * with_dm / obit_confirmed,
            )
            if dm_verified:
                logger.info("    Verified living: %d", dm_verified)
            if dm_from_tax:
                logger.info("    From tax record: %d", dm_from_tax)

    # Probate
    probate_total = sum(1 for n in notices if n.notice_type == "probate")
    if probate_total:
        probate_with_addr = sum(
            1
            for n in notices
            if n.notice_type == "probate" and n.address
        )
        logger.info(
            "  Probate with address: %d/%d",
            probate_with_addr,
            probate_total,
        )
