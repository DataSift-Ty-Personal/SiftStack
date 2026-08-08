"""Format NoticeData records into DataSift.ai (REISift) upload-ready CSV.

DataSift has 60+ built-in fields that auto-map when CSV headers match exactly.
This module maps our enrichment data to those built-in fields, plus 23 custom
fields in the "SiftStack" custom group for deep prospecting/notice-specific data.

For deceased records, the contact (Owner First/Last + Mailing Address) is set
to the decision maker, not the deceased owner. For living records, the contact
is the property owner.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Column order: auto-mapped built-in fields first, then custom fields.
# Headers must match DataSift's exact names for auto-mapping during upload.
DATASIFT_COLUMNS = [
    # ── Core (auto-mapped) ──
    "Property Street Address",
    "Property City",
    "Property State",
    "Property ZIP Code",
    "Owner First Name",
    "Owner Last Name",
    "Mailing Street Address",
    "Mailing City",
    "Mailing State",
    "Mailing ZIP Code",
    # ── Phone/Email (Tracerfy skip trace, mapped to DataSift built-in) ──
    "Phone 1",
    "Phone 2",
    "Phone 3",
    "Phone 4",
    "Phone 5",
    "Phone 6",
    "Phone 7",
    "Phone 8",
    "Phone 9",
    "Email 1",
    "Email 2",
    "Email 3",
    "Email 4",
    "Email 5",
    "Tags",
    "Lists",
    "Notes",
    # ── Built-in fields (auto-mapped by DataSift) ──
    "Estimated Value",
    "MSL Status",               # DataSift spells it "MSL" not "MLS"
    "Last Sale Date",
    "Last Sale Price",
    "Equity Percentage",
    "Tax Deliquent Value",      # DataSift typo — "Deliquent" not "Delinquent"
    "Tax Delinquent Year",
    "Tax Auction Date",
    "Foreclosure Date",
    "Probate Open Date",
    "Personal Representative",
    "Parcel ID",
    "Structure Type",
    "Year Built",
    "Living SqFt",
    "Bedrooms",
    "Bathrooms",
    "Lot (Acres)",
    # ── Custom fields (SiftStack group) ──
    "Notice Type",
    "County",
    "Date Added",
    "Owner Deceased",
    "Date of Death",
    "Decedent Name",
    "Decision Maker",
    "DM Relationship",
    "DM Confidence",
    "DM 2 Name",
    "DM 2 Relationship",
    "DM 3 Name",
    "DM 3 Relationship",
    "Obituary URL",
    "Source URL",
    "Notice Screenshot",
    # ── Deep prospecting fields ──
    "DM 1 Status",
    "DM 1 Source",
    "DM 2 Status",
    "DM 3 Status",
    "Heir Count",
    "Heirs Living",
    "Signing Chain Count",
    "Signing Chain Names",
    "DM Confidence Reason",
    "Data Flags",
    # ── Entity research fields ──
    "Entity Type",
    "Entity Contact",
    "Entity Contact Role",
]


def _format_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to M/D/YYYY."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return iso_date


def _heir_count(notice: NoticeData) -> str:
    """Return total heir count from heir_map_json, or empty string."""
    if not notice.heir_map_json:
        return ""
    try:
        return str(len(json.loads(notice.heir_map_json)))
    except (json.JSONDecodeError, TypeError):
        return ""


# Entity suffixes — expanded to cover LLC, LP, Trust, Realty, etc.
_ENTITY_SUFFIXES = re.compile(
    r"\b(?:LLC|L\.L\.C|Corp(?:oration)?|Inc(?:orporated)?|Trust|LP|LLP|LTD|Limited|"
    r"Co\b|Company|Association|Partners|Partnership|Holdings|Realty|Properties|"
    r"Group|Management|Enterprises|Investments|Development|Associates|Authority|"
    r"Housing|Foundation|Ventures|Services)\b"
    r"|Estate\s+of\b|Family\s+Trust\b|Revocable\s+Trust\b|Irrevocable\s+Trust\b",
    re.IGNORECASE,
)

# Ordered rules for entity-type classification (first match wins)
_ENTITY_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bLLC\b|\bL\.L\.C\b|\bLLP\b", re.IGNORECASE), "LLC"),
    (re.compile(r"\bLP\b|\bL\.P\b", re.IGNORECASE), "LP"),
    (re.compile(r"\bTrust\b|Family\s+Trust|Revocable\s+Trust|Irrevocable\s+Trust", re.IGNORECASE), "TRUST"),
    (re.compile(r"Estate\s+of\b", re.IGNORECASE), "ESTATE"),
    (re.compile(r"\bInc(?:orporated)?\b|\bCorp(?:oration)?\b", re.IGNORECASE), "CORP"),
    (re.compile(r"\bPartners\b|\bPartnership\b", re.IGNORECASE), "PARTNERSHIP"),
    (re.compile(
        r"City\s+of\b|Housing\s+Authority|\bAuthority\b|Redevelopment|"
        r"Commonwealth\s+of\b|Dep(?:artment|t)\.?\s+of\b|Bureau\s+of\b|Office\s+of\b|"
        r"\bPHA\b|School\s+District|United\s+States|\bFederal\b|\bMunicipal\b|"
        r"Pennsylvania\s+Housing",
        re.IGNORECASE,
    ), "GOVT"),
]

# Government entity patterns — used to tag non-marketable tax sale parcels
_GOVT_PATTERNS = re.compile(
    r"City\s+of\b|Housing\s+Authority|\bAuthority\b|Redevelopment|"
    r"Commonwealth\s+of\b|Dep(?:artment|t)\.?\s+of\b|Bureau\s+of\b|Office\s+of\b|"
    r"\bPHA\b|School\s+District|United\s+States|\bFederal\b|\bMunicipal\b|"
    r"Pennsylvania\s+Housing",
    re.IGNORECASE,
)


def _is_entity_name(name: str) -> bool:
    """Return True if name looks like a business entity, not a person."""
    return bool(_ENTITY_SUFFIXES.search(name))


def _detect_entity_type(name: str) -> str:
    """Return DataSift entity type string ('LLC', 'LP', 'TRUST', 'CORP',
    'PARTNERSHIP', 'ESTATE', 'OTHER') for an entity name, or '' for individuals.
    """
    for pattern, etype in _ENTITY_TYPE_RULES:
        if pattern.search(name):
            return etype
    if _is_entity_name(name):
        return "OTHER"
    return ""


def _clean_and_split_name(full_name: str) -> tuple[str, str]:
    """Clean a full name for DataSift upload and split into (first, last).

    Handles patterns that cause DataSift "incomplete" records:
    - Joint names with "&" or "AND": "John & Jane Smith" → ("John", "Smith")
    - Entity names (LLC, Trust, etc.): returns ("", "") — entity goes to Notes
    - Special characters: strips &, @, #, % from name parts
    """
    if not full_name:
        return ("", "")

    name = full_name.strip()

    # Entity names → empty (don't put business names in person fields)
    if _is_entity_name(name):
        return ("", "")

    # Split joint owners on " & " or " AND " — keep first person only
    # "John & Jane Smith" → "John Smith"
    # "John David & Jane Marie Smith" → "John David Smith"
    joint_match = re.split(r"\s+(?:&|AND)\s+", name, maxsplit=1, flags=re.IGNORECASE)
    if len(joint_match) > 1:
        first_person = joint_match[0].strip()
        second_part = joint_match[1].strip()
        # Extract last name from second part (last word(s) after second person's first name)
        second_words = second_part.split()
        if len(second_words) >= 2:
            # "Jane Smith" → last name is "Smith"
            last_name = second_words[-1]
            # Check if first person already has a last name
            first_words = first_person.split()
            if len(first_words) == 1:
                # "John" & "Jane Smith" → "John Smith"
                name = f"{first_person} {last_name}"
            else:
                # "John David" & "Jane Marie Smith" → "John David Smith"
                # But if "John Smith" & "Jane Doe" → keep "John Smith"
                name = first_person
        else:
            # "John & Jane" with no last name → just use first person
            name = first_person

    # Strip remaining special characters that cause incomplete status
    # Comma included so stray "LAST," tokens (from caller pre-splitting) are cleaned.
    name = re.sub(r"[&@#%,]", "", name)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return ("", "")

    parts = name.split()
    if len(parts) == 1:
        return (parts[0], "")
    if len(parts) >= 3:
        # Strip middle initials (single letter + optional period) from between
        # first and last name parts. "Eric J. Yopp" → "Eric Yopp"
        # Keeps multi-char prefixes like "St." in "Richard C. St. Leger"
        middle = parts[1:-1]
        middle = [p for p in middle if not re.match(r"^[A-Za-z]\.?$", p)]
        parts = [parts[0]] + middle + [parts[-1]]
    return (parts[0], " ".join(parts[1:]))


def _split_name_opa(full_name: str) -> tuple[str, str]:
    """Split an OPA-format name: 'LAST FIRST [MIDDLE]' → (first, last).

    OPA owner_1 stores names as LAST FIRST MIDDLE (no comma).
    Examples:
      'Rogers Alvan M'  → first='Alvan M',  last='Rogers'
      'Cooper Keith U'  → first='Keith U',  last='Cooper'
      'Smith John'      → first='John',     last='Smith'
    """
    if not full_name:
        return ("", "")
    name = full_name.strip()
    if _is_entity_name(name):
        return ("", "")
    parts = name.split()
    if len(parts) == 1:
        return ("", parts[0])        # bare last name only
    last  = parts[0]
    first = " ".join(parts[1:])
    return (first, last)


def _split_name(
    full_name: str,
    source_format: str = "notice_space",
) -> tuple[str, str]:
    """Split full name into (first, last) using the correct format for its source.

    source_format:
      'opa'          — OPA owner_1: 'LAST FIRST [MIDDLE]' (token 0 = last, rest = first)
      'notice_comma' — notice field: 'LAST, FIRST' (caller must already strip the comma)
      'notice_space' — notice field: 'FIRST LAST' (default; legacy behaviour)
    """
    if source_format == "opa":
        return _split_name_opa(full_name)
    return _clean_and_split_name(full_name)


# Map notice_type → DataSift list name for niche sequential marketing.
# DataSift auto-creates lists from CSV if they don't exist yet.
NOTICE_TYPE_TO_LIST = {
    # TN (lowercase)
    "foreclosure":    "Foreclosure",
    "probate":        "Probate",
    "tax_sale":       "Tax Sale",
    "tax_delinquent": "Tax Delinquent",
    "eviction":       "Eviction",
    "code_violation": "Code Violation",
    "divorce":        "Divorce",
    # Philadelphia (uppercase)
    "SHERIFF_MORTGAGE_FORECLOSURE": "Foreclosure",
    "PROBATE_ESTATE":               "Probate",
    "TAX_SALE":                     "Tax Sale",
    "EVICTION":                     "Eviction",
    "CODE_VIOLATION":               "Code Enforcement",
    "LIS_PENDENS":                  "Pre-Foreclosure",
    "TAX_DELINQUENT":               "Tax Delinquent",   # Philly uppercase
    "IMMINENTLY_DANGEROUS":         "Code Enforcement",
}


def _build_tags(notice: NoticeData) -> str:
    """Build comma-separated tags string for DataSift upload.

    Tags include:
    - Courthouse Data (all records — for niche sequential filter presets)
    - notice_type (foreclosure, tax_sale, probate, tax_delinquent)
    - county (knox, blount)
    - YYYY-MM date tag
    - deceased/living status
    - DM confidence level (for deceased records)
    - has_auction if auction date is upcoming
    """
    # "Courthouse Data" signals first-to-market county data. "FTM" (First-to-Market /
    # Tier 1) is the tag the ty+2 marketing template routes on: direct-from-county notice
    # data is the FTM upload stream and must carry `FTM` to land in the FTM-CALL/FTM-MAIL
    # folders (SiftMap/aggregated data is `Tier 2` instead). See the data-tier model.
    tags = ["Courthouse Data", "FTM"]

    # Notice type
    if notice.notice_type:
        tags.append(notice.notice_type)

    # County
    if notice.county:
        tags.append(notice.county.lower())

    # Month tag from the notice publication date (the meaningful cohort), falling
    # back to date_added (the run date) when no publication date is available.
    _month_src = notice.date_published or notice.date_added
    if _month_src:
        try:
            dt = datetime.strptime(_month_src, "%Y-%m-%d")
            tags.append(dt.strftime("%Y-%m"))
        except ValueError:
            pass

    # Run-cohort tag: "the records this run uploaded". Anchored on date_added (the
    # run date), NOT date_published (the notice's own date), because downstream
    # phone scoring needs today's upload, not this month's notices. This replaces
    # the old "SiftStack <date>" list — same cohort, no new list per run.
    if notice.date_added:
        try:
            _run_dt = datetime.strptime(notice.date_added, "%Y-%m-%d")
            tags.append(f"siftstack_{_run_dt.strftime('%Y-%m-%d')}")
        except ValueError:
            pass

    # Deceased/living status
    if notice.owner_deceased == "yes":
        tags.append("deceased")
        # DM confidence
        if notice.dm_confidence:
            tags.append(f"{notice.dm_confidence}_confidence")
    else:
        tags.append("living")

    # Upcoming auction. The auction_ month tag is applied whenever the sale date
    # parses (not just future dates) so postponed/past sales stay in their sale
    # cohort; the bare YYYY-M tag above is the NOTICE month, which is a different
    # thing — hence the prefix.
    if notice.auction_date:
        try:
            auction_dt = datetime.strptime(notice.auction_date, "%Y-%m-%d")
            tags.append(f"auction_{auction_dt.strftime('%Y-%m')}")
            if auction_dt >= datetime.now():
                tags.append("has_auction")
        except ValueError:
            pass

    # Tax delinquent flag
    if notice.tax_delinquent_amount:
        try:
            amt = float(notice.tax_delinquent_amount)
            if amt > 0:
                tags.append("tax_delinquent")
        except (ValueError, TypeError):
            pass

    # Deep prospecting tags
    if notice.decision_maker_status == "verified_living":
        tags.append("dm_verified")
    if notice.heir_map_json:
        tags.append("has_heirs")
    elif notice.owner_deceased == "yes":
        tags.append("no_heirs")
    if (notice.owner_deceased == "yes"
            and notice.decision_maker_street
            and notice.decision_maker_street != notice.address):
        tags.append("has_dm_address")

    # Signing chain tags
    if notice.signing_chain_count:
        try:
            sc_count = int(notice.signing_chain_count)
            tags.append(f"signing_chain_{sc_count}")
            # Check if all signing heirs have phone data
            if notice.heir_map_json:
                import json as _json
                try:
                    heirs = _json.loads(notice.heir_map_json)
                    signers = [h for h in heirs
                               if h.get("signing_authority") and h.get("status") != "deceased"]
                    traced = [h for h in signers if h.get("phones")]
                    # DM #1 counts as traced if notice has primary_phone
                    if notice.primary_phone and signers:
                        dm1_name = (notice.decision_maker_name or "").lower()
                        if any(h.get("name", "").lower() == dm1_name for h in signers):
                            traced_names = {h.get("name", "").lower() for h in traced}
                            if dm1_name not in traced_names:
                                traced.append({"name": dm1_name})  # count DM #1
                    if traced and len(traced) >= len(signers):
                        tags.append("signing_chain_complete")
                    elif traced:
                        tags.append("signing_chain_partial")
                except (ValueError, TypeError):
                    pass
        except (ValueError, TypeError):
            pass

    # Entity research tags
    if notice.entity_type:
        tags.append("entity_owned")
        if notice.entity_person_name:
            tags.append("entity_researched")

    # Photo import tag (source_url starts with "photo:")
    if notice.source_url and notice.source_url.startswith("photo:"):
        tags.append("photo_import")

    # Expired permit tag
    if getattr(notice, "expired_permit", "") == "yes":
        tags.append("expired_permit")

    # Owner occupancy + repeat-violation depth. The scrapers compute both into
    # heir_map_json meta but they never reached DataSift, so neither could be
    # filtered on. Violation counts are bucketed (cumulative "N plus") rather
    # than emitted raw, so the tag namespace stays bounded and "2+ violations"
    # is one filter instead of an open-ended OR list.
    try:
        _meta = json.loads(notice.heir_map_json) if notice.heir_map_json else {}
    except (json.JSONDecodeError, TypeError):
        _meta = {}

    _owner_status = str(_meta.get("owner_status") or "").upper()
    if _owner_status == "ABSENTEE":
        tags.append("absentee")
    elif _owner_status == "OWNER_OCCUPIED":
        tags.append("owner_occupied")

    try:
        _vcount = int(_meta.get("violation_count_for_parcel") or 0)
    except (ValueError, TypeError):
        _vcount = 0
    for _threshold in (2, 3, 5):
        if _vcount >= _threshold:
            tags.append(f"violations_{_threshold}plus")

    # Distress tier scoring.
    # Words, not numbers: every other tier in the business reads 1 = best
    # (Tier 1 ZIPs, Priority 1, DataSift's pyramid) while this one is derived
    # from a signal count, so 4 = worst-off owner = best record. Naming it
    # cold/warm/hot/critical removes the collision instead of picking a side.
    try:
        from philly_pipeline import compute_distress_tier
        tier, tier_signals = compute_distress_tier(notice)
        tier_names = {
            0: "distress_none",
            1: "distress_cold",
            2: "distress_warm",
            3: "distress_hot",
            4: "distress_critical",
        }
        tags.append(tier_names[tier])
        tags.extend(tier_signals)
    except Exception:
        pass  # Non-fatal — tier scoring failure should not block CSV write

    return ",".join(tags)


def _get_contact_info(notice: NoticeData) -> dict:
    """Determine the contact person, mailing address, and entity classification.

    Name-splitting rules (Bug 1 fix):
      • OPA-sourced (tax_owner_name):  'LAST FIRST [MIDDLE]' → source_format='opa'
      • Comma-joined owner_name:       'LAST, FIRST' → split on comma, swap
      • Plain owner_name (non-OPA):    'FIRST LAST' → source_format='notice_space'

    Entity handling (Bug 2 fix):
      Corporate entities (LLC, LP, Trust, …) get entity_type / entity_name in the
      returned dict rather than first/last names.  Caller populates Entity Type and
      Entity Contact CSV columns.

    Returns dict keys: first, last, street, city, state, zip,
                       entity_type, entity_name  (both '' for individuals)
    """
    _empty_entity = {"entity_type": "", "entity_name": ""}

    if notice.owner_deceased == "yes" and notice.decision_maker_name:
        first, last = _split_name(notice.decision_maker_name, "notice_space")
        street = notice.decision_maker_street or notice.address
        city   = notice.decision_maker_city   or notice.city
        state  = notice.decision_maker_state  or notice.state
        zip_c  = notice.decision_maker_zip    or notice.zip
        return {"first": first, "last": last,
                "street": street, "city": city, "state": state, "zip": zip_c,
                **_empty_entity}

    # ── Resolve raw name and its format ───────────────────────────────────
    raw_owner  = notice.owner_name or ""
    name_src   = ""   # the canonical single-owner string
    src_format = "notice_space"
    first = last = ""   # guaranteed initialisation — branches below may override

    if notice.tax_owner_name:
        # OPA owner_1: LAST FIRST [MIDDLE] — always single owner, no comma
        name_src   = notice.tax_owner_name
        src_format = "opa"
    elif "," in raw_owner:
        # "LAST, FIRST [MIDDLE]" convention or comma-joined dual owners
        # Take part before first comma (= last name in OPA/notice_comma format)
        before, after = raw_owner.split(",", 1)
        # Reconstruct as a clean single name: OPA part before comma is the last name
        name_src   = before.strip()
        src_format = "opa"    # pre-comma token is the last name in both conventions
        # Re-prepend the after-comma part as first name if it looks like a person
        if after.strip() and not _is_entity_name(after.strip()):
            # Treat as "LAST, FIRST" — swap to produce (first=after, last=before)
            first = after.strip().split()[0] if after.strip() else ""
            last  = before.strip()
            # Skip further splitting below
            name_src = ""
    else:
        # No OPA match, no comma — non-OPA source: "FIRST LAST"
        name_src   = raw_owner
        src_format = "notice_space"

    if name_src:
        # Skip splitting for entity/government names — detection block below handles them
        if _detect_entity_type(name_src):
            first = last = ""
        else:
            first, last = _split_name(name_src, src_format)
    # else first/last already set by the comma-split branch above

    # ── Entity detection ─────────────────────────────────────────────────
    entity_type = ""
    entity_name = ""

    if not first and not last:
        # Primary name resolved to an entity — populate entity fields
        raw_entity = notice.tax_owner_name or raw_owner
        entity_type = _detect_entity_type(raw_entity)
        if entity_type:
            entity_name = raw_entity

        # Fallback chain: entity research → tax owner (individual) → DM → owner_name
        if notice.entity_person_name:
            first, last = _split_name(notice.entity_person_name, "notice_space")
        if not first and not last and notice.tax_owner_name and not _is_entity_name(notice.tax_owner_name):
            first, last = _split_name(notice.tax_owner_name, "opa")
        if not first and not last and notice.decision_maker_name:
            first, last = _split_name(notice.decision_maker_name, "notice_space")
        # If still blank but entity_name is set, leave first/last blank —
        # DataSift will use Entity Contact column instead.

    street = notice.owner_street or notice.address
    city   = notice.owner_city   or notice.city
    state  = notice.owner_state  or notice.state
    zip_c  = notice.owner_zip    or notice.zip
    return {
        "first": first, "last": last,
        "street": street, "city": city, "state": state, "zip": zip_c,
        "entity_type": entity_type, "entity_name": entity_name,
    }


def _build_heir_summary(notice: NoticeData) -> str:
    """Build signing chain + family summary from heir_map_json.

    Two sections:
    1. SIGNING CHAIN — heirs with signing_authority who must sign to sell property.
       Includes phone + address for each.
    2. OTHER FAMILY — everyone else (in-laws, step-children, etc.) in compact format.
    """
    if not notice.heir_map_json:
        return ""

    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not heirs:
        return ""

    # Split into signing chain vs others
    signers = [h for h in heirs
                if h.get("signing_authority") and h.get("status") != "deceased"]
    non_signers = [h for h in heirs if not h.get("signing_authority") or h.get("status") == "deceased"]

    lines = []

    # ── Signing chain section ──
    if signers:
        lines.append(f"=== SIGNING CHAIN ({len(signers)} heir{'s' if len(signers) != 1 else ''} must sign) ===")
        for i, h in enumerate(signers, 1):
            name = h.get("name", "?")
            rel = h.get("relationship", "unknown")
            status = h.get("status", "unverified")
            status_label = "ALIVE" if status == "verified_living" else status.upper()

            # Phone info
            phones = h.get("phones", [])
            # DM #1 phones are on flat NoticeData fields, not in heir_map_json
            if not phones and notice.primary_phone:
                dm1_name = (notice.decision_maker_name or "").strip().lower()
                if name.lower() == dm1_name:
                    phones = [notice.primary_phone]

            phone_str = phones[0] if phones else "no phone yet"
            lines.append(f"{i}. {name} ({rel}) — {status_label} — {phone_str}")

            # Address
            street = h.get("street", "")
            if street:
                city = h.get("city", "")
                state = h.get("state", "TN")
                zip_code = h.get("zip", "")
                addr_parts = [street]
                if city:
                    addr_parts.append(city)
                addr_parts.append(f"{state} {zip_code}".strip())
                lines.append(f"   Mail: {', '.join(addr_parts)}")
    else:
        lines.append("=== NO SIGNING CHAIN IDENTIFIED ===")

    # ── Non-signing family section (compact) ──
    if non_signers:
        entries = []
        for h in non_signers[:6]:
            name = h.get("name", "?")
            rel = h.get("relationship", "")
            status = h.get("status", "unverified")
            tag = "living" if status == "verified_living" else "deceased" if status == "deceased" else status
            entries.append(f"{name} ({rel}) [{tag}]")
        lines.append("")
        lines.append("=== OTHER FAMILY (no signing authority) ===")
        lines.append(", ".join(entries))
        remaining = len(non_signers) - 6
        if remaining > 0:
            lines.append(f"(+{remaining} more)")

    return "\n".join(lines)


def _build_dm_section(notice: NoticeData) -> str:
    """Build ranked decision maker section with status and address."""
    dms = []

    for i, (name_attr, rel_attr, status_attr) in enumerate([
        ("decision_maker_name", "decision_maker_relationship", "decision_maker_status"),
        ("decision_maker_2_name", "decision_maker_2_relationship", "decision_maker_2_status"),
        ("decision_maker_3_name", "decision_maker_3_relationship", "decision_maker_3_status"),
    ], 1):
        name = getattr(notice, name_attr, "")
        if not name:
            continue
        rel = getattr(notice, rel_attr, "") or "unknown"
        status = getattr(notice, status_attr, "") or "unverified"

        status_label = "VERIFIED LIVING" if status == "verified_living" else status
        line = f"{i}. {name} ({rel}) — {status_label}"

        # Include DM1 mailing address if available
        if i == 1 and notice.decision_maker_street:
            addr_parts = [notice.decision_maker_street]
            if notice.decision_maker_city:
                addr_parts.append(notice.decision_maker_city)
            if notice.decision_maker_state:
                addr_parts.append(notice.decision_maker_state)
            if notice.decision_maker_zip:
                addr_parts[-1] = addr_parts[-1] + " " + notice.decision_maker_zip
            line += f"\n   Mail: {', '.join(addr_parts)}"

        dms.append(line)

    if not dms:
        return ""

    return "=== DECISION MAKERS ===\n" + "\n".join(dms)


def _build_property_section(notice: NoticeData) -> str:
    """Build the property/notice details section for Notes."""
    parts = []

    # Include entity name when owner is LLC/Trust (name stripped from contact fields)
    if notice.owner_name and _is_entity_name(notice.owner_name):
        parts.append(f"Entity: {notice.owner_name}")

    # Include entity research contact if found
    if notice.entity_person_name:
        role = notice.entity_person_role.replace("_", " ").title() if notice.entity_person_role else "Unknown"
        parts.append(f"Entity Contact: {notice.entity_person_name} ({role})")

    if notice.notice_type:
        parts.append(notice.notice_type.replace("_", " ").title())

    if notice.auction_date:
        parts.append(f"Auction: {_format_date(notice.auction_date)}")

    if notice.tax_delinquent_amount:
        tax_str = f"Tax Due: ${notice.tax_delinquent_amount}"
        if notice.tax_delinquent_years:
            tax_str += f" ({notice.tax_delinquent_years} yrs)"
        parts.append(tax_str)

    if notice.source_url:
        parts.append(f"Source: {notice.source_url}")

    # Proof-of-source: link to the screenshot of the actual published notice
    if notice.notice_screenshot_url:
        parts.append(f"Notice Screenshot: {notice.notice_screenshot_url}")

    return " | ".join(parts)


def _build_notes(notice: NoticeData) -> str:
    """Build a structured notes string for DataSift records.

    Deceased records get a multi-section format with heir map and DM summary.
    Living records get a simpler single-section format.
    """
    if notice.owner_deceased == "yes":
        sections = []

        # Section 1: Deceased owner header
        deceased_parts = []
        if notice.decedent_name:
            deceased_parts.append(f"Decedent: {notice.decedent_name}")
        if notice.date_of_death:
            deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
        if notice.obituary_url:
            deceased_parts.append(f"Obituary: {notice.obituary_url}")

        confidence_line = ""
        if notice.dm_confidence:
            confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
            if notice.dm_confidence_reason:
                confidence_line += f" — {notice.dm_confidence_reason}"

        if deceased_parts or confidence_line:
            header = "=== DECEASED OWNER ==="
            body = " | ".join(deceased_parts)
            if confidence_line:
                body += f"\n{confidence_line}" if body else confidence_line
            sections.append(f"{header}\n{body}")

        # Section 2: Decision makers
        dm_section = _build_dm_section(notice)
        if dm_section:
            sections.append(dm_section)

        # Section 3: Heir map
        heir_section = _build_heir_summary(notice)
        if heir_section:
            sections.append(heir_section)

        # Section 4: Property/notice details
        prop_section = _build_property_section(notice)
        if prop_section:
            sections.append(f"=== PROPERTY ===\n{prop_section}")

        if notice.report_url:
            sections.append(f"=== REPORT ===\n{notice.report_url}")

        return "\n\n".join(sections)

    # Living owner — simple format
    return _build_property_section(notice)


def _build_dm_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 1: deceased owner header + DM breakdown + property.

    For living records, returns the simple property section.
    Used by write_datasift_split_csvs() for the DMs upload.
    """
    if notice.owner_deceased != "yes":
        return _build_property_section(notice)

    sections = []

    # Deceased owner header
    deceased_parts = []
    if notice.decedent_name:
        deceased_parts.append(f"Decedent: {notice.decedent_name}")
    if notice.date_of_death:
        deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
    if notice.obituary_url:
        deceased_parts.append(f"Obituary: {notice.obituary_url}")

    confidence_line = ""
    if notice.dm_confidence:
        confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
        if notice.dm_confidence_reason:
            confidence_line += f" — {notice.dm_confidence_reason}"

    if deceased_parts or confidence_line:
        header = "=== DECEASED OWNER ==="
        body = " | ".join(deceased_parts)
        if confidence_line:
            body += f"\n{confidence_line}" if body else confidence_line
        sections.append(f"{header}\n{body}")

    # Decision makers
    dm_section = _build_dm_section(notice)
    if dm_section:
        sections.append(dm_section)

    # Property details
    prop_section = _build_property_section(notice)
    if prop_section:
        sections.append(f"=== PROPERTY ===\n{prop_section}")

    return "\n\n".join(sections)


def _build_heir_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 2: full heir map only.

    Used by write_datasift_split_csvs() for the Heirs upload.
    Returns empty string if no heir data.
    """
    return _build_heir_summary(notice)


def _validate_row(row: dict) -> tuple[bool, list[str]]:
    """Check a row dict for DataSift completeness.

    DataSift marks records incomplete when missing owner first/last name,
    mailing address, or property address.

    Returns:
        (is_complete, issues) — True if record will be "clean" in DataSift.
    """
    issues = []
    if not row.get("Owner First Name"):
        issues.append("no_first_name")
    if not row.get("Owner Last Name"):
        issues.append("no_last_name")
    if not row.get("Property Street Address"):
        issues.append("no_property_address")
    if not row.get("Mailing Street Address"):
        issues.append("no_mailing_address")
    return (len(issues) == 0, issues)


def _build_row(notice: NoticeData, notes_override: str | None = None) -> dict:
    """Build a single CSV row dict for a NoticeData record.

    Args:
        notice: The notice to format.
        notes_override: If provided, use this as the Notes value instead of
            calling _build_notes(). Used by write_datasift_split_csvs().

    Returns:
        Dict keyed by DATASIFT_COLUMNS headers.
    """
    contact = _get_contact_info(notice)
    tags = _build_tags(notice)
    list_name = NOTICE_TYPE_TO_LIST.get(notice.notice_type, "")
    notes = notes_override if notes_override is not None else _build_notes(notice)

    # Conditionally map auction_date to the right built-in field
    tax_auction = ""
    foreclosure_date = ""
    probate_open = ""
    if notice.notice_type in ("TAX_SALE", "tax_sale"):
        tax_auction = _format_date(notice.auction_date)
    elif notice.notice_type in ("SHERIFF_MORTGAGE_FORECLOSURE", "foreclosure"):
        foreclosure_date = _format_date(notice.auction_date)
    elif notice.notice_type in ("PROBATE_ESTATE", "probate"):
        # Probate notices are published when the estate opens — use the
        # publication date, not the run date.
        probate_open = _format_date(notice.date_published or notice.date_added)

    # Personal Representative only for probate notices. Prefer the resolved
    # decision maker (deep prospecting), else the court-named PR that the parser
    # puts in owner_name — so the field is populated even when deep prospecting
    # is off (lean daily run).
    personal_rep = ""
    if notice.notice_type in ("PROBATE_ESTATE", "probate"):
        personal_rep = notice.decision_maker_name or notice.owner_name or ""

    # ── Bug 2: Entity type/contact from contact resolution ─────────────
    # _get_contact_info returns entity_type and entity_name when the owner
    # is a corporate entity.  These override the notice.entity_* fields so
    # eviction landlords (LLCs, LPs) populate Entity Type/Contact correctly.
    entity_type = contact.get("entity_type") or notice.entity_type or ""
    entity_contact = contact.get("entity_name") or notice.entity_person_name or ""

    # ── Bug 4: Government-owned tax sale flagging ──────────────────────
    # Government parcels are non-marketable; keep in CSV but flag them.
    govt_tags = ""
    if notice.notice_type in ("TAX_SALE", "tax_sale"):
        owner_check = notice.tax_owner_name or notice.owner_name or ""
        if _GOVT_PATTERNS.search(owner_check):
            govt_tags = ",government_owned,excluded_from_marketing"

    return {
        # ── Core auto-mapped ──
        "Property Street Address": notice.address,
        "Property City": notice.city,
        "Property State": notice.state or "PA",
        "Property ZIP Code": notice.zip,
        "Owner First Name": contact["first"],
        "Owner Last Name": contact["last"],
        "Mailing Street Address": contact["street"],
        "Mailing City": contact["city"],
        "Mailing State": contact["state"],
        "Mailing ZIP Code": contact["zip"],
        # ── Phone/Email (Tracerfy → DataSift generic Phone N format) ──
        "Phone 1": notice.primary_phone,
        "Phone 2": notice.mobile_1,
        "Phone 3": notice.mobile_2,
        "Phone 4": notice.mobile_3,
        "Phone 5": notice.mobile_4,
        "Phone 6": notice.mobile_5,
        "Phone 7": notice.landline_1,
        "Phone 8": notice.landline_2,
        "Phone 9": notice.landline_3,
        "Email 1": notice.email_1,
        "Email 2": notice.email_2,
        "Email 3": notice.email_3,
        "Email 4": notice.email_4,
        "Email 5": notice.email_5,
        "Tags": tags + govt_tags,
        "Lists": list_name,
        "Notes": notes,
        # ── Built-in fields ──
        "Estimated Value": notice.estimated_value,
        # BLANKED (bug 2026-06): the upload wizard auto-maps "MSL Status" to the reisift
        # lead STATUS field, so a Zillow "sold"/"listed" AUCTION listing here wrongly stamped
        # active foreclosures as sold and suppressed them from the cadence. MLS status is not
        # needed for FTM cold outreach. Leave blank so the lead status is never set on import.
        "MSL Status": "",
        "Last Sale Date": _format_date(notice.mls_last_sold_date),
        "Last Sale Price": notice.mls_last_sold_price,
        "Equity Percentage": notice.equity_percent,
        "Tax Deliquent Value": notice.tax_delinquent_amount,
        "Tax Delinquent Year": notice.tax_delinquent_years,
        "Tax Auction Date": tax_auction,
        "Foreclosure Date": foreclosure_date,
        "Probate Open Date": probate_open,
        "Personal Representative": personal_rep,
        "Parcel ID": notice.parcel_id,
        "Structure Type": notice.property_type,
        "Year Built": notice.year_built,
        "Living SqFt": notice.sqft,
        "Bedrooms": notice.bedrooms,
        "Bathrooms": notice.bathrooms,
        "Lot (Acres)": notice.lot_size,
        # ── Custom fields (SiftStack group) ──
        "Notice Type": notice.notice_type,
        "County": notice.county,
        "Date Added": _format_date(notice.date_added),
        "Owner Deceased": notice.owner_deceased,
        "Date of Death": notice.date_of_death,
        "Decedent Name": notice.decedent_name,
        "Decision Maker": notice.decision_maker_name,
        "DM Relationship": notice.decision_maker_relationship,
        "DM Confidence": notice.dm_confidence,
        "DM 2 Name": notice.decision_maker_2_name,
        "DM 2 Relationship": notice.decision_maker_2_relationship,
        "DM 3 Name": notice.decision_maker_3_name,
        "DM 3 Relationship": notice.decision_maker_3_relationship,
        "Obituary URL": notice.obituary_url,
        "Source URL": notice.source_url,
        "Notice Screenshot": notice.notice_screenshot_url,
        # ── Deep prospecting fields ──
        "DM 1 Status": notice.decision_maker_status,
        "DM 1 Source": notice.decision_maker_source,
        "DM 2 Status": notice.decision_maker_2_status,
        "DM 3 Status": notice.decision_maker_3_status,
        "Heir Count": _heir_count(notice),
        "Heirs Living": notice.heirs_verified_living,
        "Signing Chain Count": notice.signing_chain_count,
        "Signing Chain Names": notice.signing_chain_names,
        "DM Confidence Reason": notice.dm_confidence_reason,
        "Data Flags": notice.missing_data_flags,
        # ── Entity research fields (Bug 2) ──
        "Entity Type": entity_type,
        "Entity Contact": entity_contact,
        "Entity Contact Role": notice.entity_person_role,
    }


def write_datasift_csv(
    notices: list[NoticeData],
    filename: str | None = None,
) -> Path:
    """Write notices to a DataSift-formatted CSV file.

    Args:
        notices: List of enriched NoticeData objects.
        filename: Optional filename override.

    Returns:
        Path to the written CSV file.
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        filename = f"datasift_upload_{timestamp}.csv"

    output_path = OUTPUT_DIR / filename
    written = 0
    incomplete = 0
    probate_dropped = 0
    issue_counts: dict[str, int] = {}

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()

        for notice in notices:
            # ── Bug 3: Drop probate records with no property address ─────────
            # A probate record with no OPA match AND no property address has no
            # Philadelphia property to market against — drop it entirely.
            # Only applied to inquirer_probate; all other sources keep their records.
            if notice.notice_type == "PROBATE_ESTATE":
                try:
                    meta = json.loads(notice.heir_map_json) if notice.heir_map_json else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if not meta.get("opa_match") and not notice.address:
                    probate_dropped += 1
                    continue

            row = _build_row(notice)
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
                logger.debug("Incomplete record %s: %s", notice.address, issues)
            writer.writerow(row)
            written += 1

    if probate_dropped:
        logger.info("Probate drop (no address + no OPA match): %d records dropped", probate_dropped)
    logger.info("Wrote %d records to DataSift CSV: %s", written, output_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        written - incomplete, written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", written, written)
    return output_path


def write_datasift_split_csvs(
    notices: list[NoticeData],
    date_str: str | None = None,
) -> list[dict]:
    """Generate separate DM and Heir Map CSVs for two-upload Message Board flow.

    CSV 1 ("DMs"): All records. Deceased get DM breakdown as Notes, living get
    property details. Creates/updates all records in DataSift.

    CSV 2 ("Heirs"): Only deceased records with heir data. Notes = full heir map.
    DataSift merges by address, adding a second Message Board comment.

    Args:
        notices: List of enriched NoticeData objects.
        date_str: Optional date string for filenames/list names (default: today).

    Returns:
        List of dicts: [{"path": Path, "label": str, "list_name": str}, ...]
        Returns 1 item if no deceased-with-heirs, 2 items otherwise.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    results = []

    # CSV 1: DMs — all records
    dm_path = OUTPUT_DIR / f"datasift_upload_DMs_{timestamp}.csv"
    dm_written = 0
    incomplete = 0
    issue_counts: dict[str, int] = {}
    with open(dm_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for notice in notices:
            # A notice can carry its own pre-built note (e.g. priority_skiptrace's
            # identity/trace note) — that beats the generic DM/property notes.
            custom_note = getattr(notice, "_notes_override", None)
            row = _build_row(notice, notes_override=custom_note or _build_dm_notes(notice))
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
            writer.writerow(row)
            dm_written += 1

    logger.info("DMs CSV: %d records → %s", dm_written, dm_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        dm_written - incomplete, dm_written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", dm_written, dm_written)
    results.append({
        "path": dm_path,
        "label": "DMs",
        # Stable list, NOT "SiftStack <date> - DMs". The dated form minted two
        # fresh lists every single run and buried the niche lists. The run
        # cohort is a tag now (`siftstack_YYYY-MM-DD`, see build_tags), which is
        # what phone scoring filters on.
        "list_name": "SiftStack DMs",
    })

    # CSV 2: Heirs — only deceased with heir data
    deceased_with_heirs = [
        n for n in notices
        if n.owner_deceased == "yes" and n.heir_map_json
    ]

    if deceased_with_heirs:
        heir_path = OUTPUT_DIR / f"datasift_upload_Heirs_{timestamp}.csv"
        heir_written = 0
        with open(heir_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for notice in deceased_with_heirs:
                row = _build_row(notice, notes_override=_build_heir_notes(notice))
                writer.writerow(row)
                heir_written += 1

        logger.info("Heirs CSV: %d records → %s", heir_written, heir_path)
        results.append({
            "path": heir_path,
            "label": "Heirs",
            "list_name": "SiftStack Heirs",
        })
    else:
        logger.info("No deceased records with heir data — skipping Heirs CSV")

    return results
