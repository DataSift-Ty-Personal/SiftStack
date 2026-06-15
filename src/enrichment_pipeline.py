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
    skip_ownership_verification: bool = False

    # Obituary sub-options
    skip_heir_verification: bool = False
    max_heir_depth: int = 2
    skip_dm_address: bool = False
    tracerfy_tier1: bool = False

    # Smart detection flags (set by detect_existing_enrichment)
    has_smarty: bool = False
    has_zillow: bool = False
    has_tax: bool = False
    has_obituary: bool = False

    # Context label for summary logging
    source_label: str = ""


# ── Enrichment Health Monitoring ─────────────────────────────────────
#
# Per-field fill rates surface in Slack at the end of every run. If a
# field drops below its hard floor, the Slack message gets prefixed
# with a HEALTH WARNING so operators see the regression immediately
# instead of via spot-checking the CSV. Soft floor breach is an
# informational note; hard floor breach is a flag-it-now signal.
#
# Floors are starting points — tune after observing 2-3 weeks of real
# data. Tighten when a field is consistently above its current soft;
# loosen when a known external API is rate-limited or noisy.

# {field_name: (soft_floor_pct, hard_floor_pct)}
ENRICHMENT_FLOORS: dict[str, tuple[int, int]] = {
    "smarty_usps_confirmed":     (80, 60),
    "zillow_enriched":           (60, 40),
    "estimated_value":           (60, 40),
    "mls_status":                (50, 30),
    "decision_maker_identified": (90, 70),
    "mailable":                  (90, 80),
    # Probate ownership: verified / (verified + mismatch). Soft 55%, hard 40%
    # — Week 25 baseline was ~53% real owners among checkable records.
    "ownership_verified":        (55, 40),
}


def compute_enrichment_health(notices: list["NoticeData"]) -> dict:
    """Compute per-field fill rates for monitoring.

    Returns {field: {count, total, pct}}. Fields that don't apply to
    the batch (e.g. decision_maker for a sheriff-only run) are omitted
    entirely — better to skip the row than alarm on a structural 0%.

    This is read-only: it only inspects already-enriched notices and
    never mutates them. Safe to call from any caller without risking
    pipeline behavior change.
    """
    total = len(notices)
    if total == 0:
        return {}

    def _stat(count: int, denom: int) -> dict:
        return {
            "count": count,
            "total": denom,
            "pct": round(count / denom * 100, 1) if denom else 0.0,
        }

    health: dict = {}

    # Universal fields — apply to every record in the batch.
    smarty_hit = sum(1 for n in notices if n.dpv_match_code == "Y")
    health["smarty_usps_confirmed"] = _stat(smarty_hit, total)

    zillow_hit = sum(1 for n in notices if n.estimated_value)
    health["zillow_enriched"] = _stat(zillow_hit, total)
    health["estimated_value"] = _stat(zillow_hit, total)

    mls_hit = sum(1 for n in notices if n.mls_status)
    health["mls_status"] = _stat(mls_hit, total)

    mailable_hit = sum(1 for n in notices if n.mailable == "yes")
    health["mailable"] = _stat(mailable_hit, total)

    # Conditional field: DM identification only fires for probate or
    # obit-confirmed-deceased records. A pure sheriff-sale batch would
    # show 0/N and trigger a false hard-floor alarm — exclude instead.
    dm_eligible = sum(
        1
        for n in notices
        if n.notice_type == "probate" or n.owner_deceased == "yes"
    )
    if dm_eligible > 0:
        dm_hit = sum(1 for n in notices if n.decision_maker_name)
        health["decision_maker_identified"] = _stat(dm_hit, dm_eligible)

    # Conditional field: probate ownership pass rate (verified of checkable).
    # ownership_health() returns None for batches with nothing determinable
    # (sheriff-only, or all-Essex probate), so the row is omitted then.
    try:
        from ownership_verifier import ownership_health
        oh = ownership_health(notices)
        if oh is not None:
            health["ownership_verified"] = oh
    except Exception:  # pragma: no cover — never let monitoring break the run
        pass

    return health


def evaluate_enrichment_health(
    health: dict,
    floors: dict[str, tuple[int, int]] | None = None,
) -> tuple[list[str], bool, bool]:
    """Compare health stats against floors.

    Returns:
        (lines, has_hard_breach, has_soft_breach)

        lines: Slack-ready strings, e.g.
            "  smarty_usps_confirmed: 42/47 (89.4%) ✓"
            "  zillow_enriched: 27/47 (57.4%) — below 60% soft floor ⚠️"
        has_hard_breach: any field below its hard floor
        has_soft_breach: any field below its soft floor (independent
                          of hard — both can be True simultaneously)

    Fields present in `floors` but missing from `health` are skipped
    (e.g. sheriff-only run won't have decision_maker_identified).
    """
    if floors is None:
        floors = ENRICHMENT_FLOORS

    lines: list[str] = []
    has_hard = False
    has_soft = False

    for field, (soft, hard) in floors.items():
        stats = health.get(field)
        if not stats:
            continue
        pct = stats["pct"]
        count = stats["count"]
        total = stats["total"]

        if pct < hard:
            marker = f"— below {hard}% hard floor 🔴"
            has_hard = True
        elif pct < soft:
            marker = f"— below {soft}% soft floor ⚠️"
            has_soft = True
        else:
            marker = "✓"

        lines.append(f"  {field}: {count}/{total} ({pct}%) {marker}")

    return lines, has_hard, has_soft


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

    Records flagged `needs_manual_address` are kept regardless — those
    are block/lot tax-sale addresses we intentionally preserve for
    manual downstream lookup.
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
    result = [
        n for n in notices
        if n.needs_manual_address == "yes" or _has_house_number(n.address)
    ]
    removed = before - len(result)
    if removed:
        logger.info("  Removed %d vacant land records (no house number)", removed)
    return result


# Patterns that signal a non-street-address (block/lot, parcel-id-ish,
# range descriptions). Matched case-insensitively against the raw
# address string before Smarty.
_BLOCK_LOT_PATTERNS = (
    re.compile(r"\bblock\s*\d+", re.IGNORECASE),
    re.compile(r"\blot\s*\d+", re.IGNORECASE),
    re.compile(r"\bb\s*\d+\s*[\s,/-]?\s*l\s*\d+", re.IGNORECASE),
    re.compile(r"\bparcel\s*(id|#|no\.?|number)?\s*\d+", re.IGNORECASE),
)
_HAS_LEADING_STREET_NUMBER = re.compile(r"^\s*\d+\s+\S")


def _flag_block_lot_addresses(notices: list[NoticeData]) -> int:
    """Pre-filter: flag block/lot-style addresses before Smarty + vacant-land.

    Tax-sale notices and similar court records often reference properties
    by "Block N Lot M" or parcel id rather than a deliverable street
    address. Smarty can't standardize those (wasted credits), and the
    vacant-land filter would drop them outright. Flagging them lets the
    pipeline preserve the original text in `address_raw` and route the
    record to the HELD CSV with `needs_manual_address="yes"`.

    Trigger conditions (any one):
      - matches a block/lot/parcel pattern in _BLOCK_LOT_PATTERNS
      - non-empty address with no leading street number

    Returns the count of records flagged.
    """
    flagged = 0
    for n in notices:
        addr = n.address.strip()
        if not addr:
            continue  # blank addresses handled elsewhere
        if n.needs_manual_address == "yes":
            continue  # already flagged
        is_block_lot = any(p.search(addr) for p in _BLOCK_LOT_PATTERNS)
        has_street_number = bool(_HAS_LEADING_STREET_NUMBER.match(addr))
        if is_block_lot or not has_street_number:
            n.address_raw = addr
            n.needs_manual_address = "yes"
            flagged += 1
    return flagged


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
        for date_field in ("date_added", "auction_date"):
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
    *,
    return_health: bool = False,
) -> list[NoticeData] | tuple[list[NoticeData], dict]:
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

    When `return_health=True`, returns a tuple `(notices, health)`
    where `health` is the per-field fill-rate dict from
    `compute_enrichment_health()`. Default is the historical
    list-only return so existing callers (main.py, nj_scraper,
    nj_middlesex_probate, dropbox_watcher, scripts, tests) keep
    working without modification.
    """
    # Closure that wraps each return point — `return_health` is a
    # keyword-only arg, so this stays in scope for all 5 returns
    # without threading it through.
    def _finalize(ns: list[NoticeData]):
        if return_health:
            return ns, compute_enrichment_health(ns)
        return ns

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

    # ── Stamp run_id on all records ─────────────────────────────────
    run_id = _generate_run_id()
    for n in notices:
        n.run_id = run_id
    logger.info("Pipeline run_id: %s (%d records)", run_id, len(notices))

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

    # ── Step 2b: Block/Lot Address Flag ──────────────────────────────
    # Detect non-street addresses (tax-sale block/lot, parcel-id-style)
    # BEFORE vacant-land filter + Smarty. Flagged records preserve the
    # original address in `address_raw`, set needs_manual_address=yes,
    # skip Smarty, survive vacant-land, and reach the HELD CSV intact.
    logger.info("── Step 2b: Block/Lot Address Flag ──")
    flagged = _flag_block_lot_addresses(notices)
    if flagged:
        logger.info("  Flagged %d records as needs_manual_address (block/lot or no street #)", flagged)
    else:
        logger.info("  No block/lot addresses detected")

    # ── Step 3: Vacant Land Filter ───────────────────────────────────
    if not opts.skip_vacant_filter:
        logger.info("── Step 3: Vacant Land Filter ──")
        before = len(notices)
        notices = _filter_vacant_land(notices)
        logger.info("  %d records after filter", len(notices))
    if not notices:
        logger.warning("No records remaining after filtering")
        return _finalize(notices)

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
        return _finalize(notices)

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
            return _finalize(notices)
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

    # ── Step 8b: Probate Ownership Verification ─────────────────────
    # Cross-check probate decedents against the MOD-IV owner of record
    # (taxrecords-nj, Middlesex/Somerset/Union) and flag verified /
    # mismatch / unknown. Flag only — never drops. No-op for batches with
    # no eligible probate records (sheriff/NOD runs).
    if not getattr(opts, "skip_ownership_verification", False):
        probate_eligible = [
            n for n in notices
            if "probate" in (n.notice_type or "").lower() and n.address.strip()
        ]
        if probate_eligible:
            logger.info(
                "── Step 8b: Ownership Verification (%d probate candidates) ──",
                len(probate_eligible),
            )
            try:
                from ownership_verifier import enrich_ownership
                enrich_ownership(notices)
            except ImportError:
                logger.warning("  ownership_verifier not available — skipping")
            except Exception as e:
                logger.warning("  Ownership verification failed: %s", e)

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
        return _finalize(notices)

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

    return _finalize(notices)


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
