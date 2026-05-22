"""Parse individual notice pages and extract structured data.

After reCAPTCHA is solved and "View Notice" is clicked, the detail page shows:
  1. Structured metadata labels: Publication Name, Publication City and State,
     Publication County, Notice Publish Date
  2. A "Notice Content" section with the raw legal text body

We extract the metadata labels directly, then regex-parse address/owner/etc.
from the Notice Content body.

IMPORTANT: For address parsing, we ONLY extract addresses that appear after
high-confidence property-indicator phrases like "commonly known as" or
"property address". We never fall back to a generic address regex — it's better
to leave the address empty than to grab a courthouse, auction location, or
instrument number by mistake.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)


@dataclass
class NoticeData:
    """Structured data extracted from a single notice."""
    date_added: str = ""       # Published date (YYYY-MM-DD)
    auction_date: str = ""     # Scheduled sale/auction date (YYYY-MM-DD)
    address: str = ""
    city: str = ""
    state: str = "TN"
    zip: str = ""
    owner_name: str = ""
    notice_type: str = ""      # foreclosure | tax_sale | tax_lien | probate
    county: str = ""
    source_url: str = ""
    raw_text: str = ""         # Full notice text for classification
    # Smarty address standardization fields (populated post-scrape)
    zip_plus4: str = ""        # Full ZIP+4 (e.g. "37918-1234")
    latitude: str = ""         # Decimal latitude from Smarty geocode
    longitude: str = ""        # Decimal longitude from Smarty geocode
    dpv_match_code: str = ""   # Delivery Point Validation: Y=confirmed, S=secondary missing, N=no match
    vacant: str = ""           # "Y" if address is vacant
    rdi: str = ""              # "Residential" or "Commercial"
    # Zillow property enrichment fields (populated post-scrape)
    mls_status: str = ""           # "Active", "Pending", "Sold", "Off Market"
    mls_listing_price: str = ""    # Current list price or last sold price
    mls_last_sold_date: str = ""   # Most recent sale date (YYYY-MM-DD)
    mls_last_sold_price: str = ""  # Most recent sale price
    estimated_value: str = ""      # Zestimate
    estimated_equity: str = ""     # zestimate - estimated remaining mortgage
    equity_percent: str = ""       # (equity / zestimate) * 100
    property_type: str = ""        # "Single Family", "Condo", etc.
    bedrooms: str = ""
    bathrooms: str = ""
    sqft: str = ""
    year_built: str = ""
    lot_size: str = ""             # Lot size in sqft
    # Probate-specific fields
    decedent_name: str = ""        # Deceased person's name (probate only)
    owner_street: str = ""         # PR/contact mailing street address
    owner_city: str = ""           # PR/contact mailing city
    owner_state: str = ""          # PR/contact mailing state
    owner_zip: str = ""            # PR/contact mailing zip
    # County assessor / tax fields
    parcel_id: str = ""                # County assessor parcel ID
    tax_delinquent_amount: str = ""    # Total delinquent tax owed ($)
    tax_delinquent_years: str = ""     # Number of years delinquent
    # Deceased owner detection
    deceased_indicator: str = ""       # "life_estate", "personal_rep", "trustee", "care_of", "et_al", or ""
    tax_owner_name: str = ""           # Raw owner name from county tax API
    # Obituary-confirmed deceased owner
    owner_deceased: str = ""                # "yes" or "" — confirmed via obituary search
    date_of_death: str = ""                 # YYYY-MM-DD from obituary
    obituary_url: str = ""                  # URL of confirmed obituary
    decision_maker_name: str = ""           # Heir/executor full name
    decision_maker_relationship: str = ""   # "spouse", "son", "daughter", "executor", etc.
    # Deep prospecting — ranked decision-makers (flat columns)
    decision_maker_status: str = ""         # "verified_living", "unverified"
    decision_maker_source: str = ""         # "obituary_survivors", "tax_record_joint_owner", "snippet"
    decision_maker_street: str = ""         # DM residential mailing address
    decision_maker_city: str = ""
    decision_maker_state: str = ""
    decision_maker_zip: str = ""
    decision_maker_2_name: str = ""
    decision_maker_2_relationship: str = ""
    decision_maker_2_status: str = ""       # "verified_living", "unverified"
    decision_maker_3_name: str = ""
    decision_maker_3_relationship: str = ""
    decision_maker_3_status: str = ""       # "verified_living", "unverified"
    # Obituary-extracted name context (NAME-03) — feeds the name-variant
    # resolver so PVA/deeds search the maiden/prior-married forms a property
    # may be titled under (e.g. Jackson -> GREATHOUSE returns 0 rows under the
    # married surname). Set by obituary_enricher._apply_obituary_match when the
    # LLM extracts them; read downstream via getattr so they degrade to "" when
    # obituary didn't run. Stored as flat strings for CSV serialization;
    # prior surnames are semicolon-delimited.
    decedent_obit_maiden_name: str = ""     # Obituary-confirmed maiden surname
    decedent_obit_prior_surnames: str = ""  # Semicolon-delimited prior/aka surnames
    # Obituary/heir metadata
    obituary_source_type: str = ""          # "full_page" or "snippet"
    heir_search_depth: str = ""             # "0" (none), "1" (survivors checked), "2" (2nd gen)
    heirs_verified_living: str = ""         # Count of verified living heirs
    heirs_verified_deceased: str = ""       # Count of verified deceased heirs
    heirs_unverified: str = ""              # Count of unverified heirs
    heir_map_json: str = ""                 # JSON-encoded full ranked heir list (all heirs, not just top 3)
    signing_chain_count: str = ""            # Count of living signing-authority heirs
    signing_chain_names: str = ""            # Comma-separated names of signing-authority heirs
    # Error map (flat fields)
    dm_confidence: str = ""                 # "high", "medium", "low"
    dm_confidence_reason: str = ""          # Brief explanation
    missing_data_flags: str = ""            # Pipe-separated: "no_survivors|snippet_only|common_name"
    # Mailability flag
    mailable: str = ""                 # "yes" or "" (unmailable)
    # Entity research fields
    entity_type: str = ""                  # "llc", "corp", "trust", "estate", "lp", "other"
    entity_person_name: str = ""           # Person found behind entity (full name)
    entity_person_role: str = ""           # "registered_agent", "member", "trustee", "officer", etc.
    entity_research_source: str = ""       # "name_parse", "web_search", "sos_snippet"
    entity_research_confidence: str = ""   # "high", "medium", "low"
    # PDF report link (Google Drive URL, populated by report_generator)
    report_url: str = ""
    # Tracerfy skip trace — phones + emails (populated by tracerfy_skip_tracer)
    primary_phone: str = ""
    mobile_1: str = ""
    mobile_2: str = ""
    mobile_3: str = ""
    mobile_4: str = ""
    mobile_5: str = ""
    landline_1: str = ""
    landline_2: str = ""
    landline_3: str = ""
    email_1: str = ""
    email_2: str = ""
    email_3: str = ""
    email_4: str = ""
    email_5: str = ""
    # Pipeline metadata (set by enrichment_pipeline)
    run_id: str = ""                   # Unique pipeline run identifier for data lineage
    # Court case metadata (populated by court docket scrapers — e.g. KCOJ, Jefferson Deeds)
    case_number: str = ""              # Court case number (e.g. "26-P-001544" for KY probate)
    # Deed-history enrichment (KY probate; Phase 2b)
    mortgage_balance_estimate: str = ""  # Remaining mortgage balance ($) estimated from origination + elapsed years
    mortgage_origination_date: str = ""  # YYYY-MM-DD of the most recent unreleased mortgage
    mortgage_original_amount: str = ""   # Original loan amount ($) from the mortgage document
    # Heir-property detection (KY probate; Phase 2b.5)
    property_owner_status: str = ""      # "direct" | "estate" | "trust" | "heir_recent" | "" (not confirmed)
    heir_transferred_to: str = ""        # Grantee name from decedent's most recent transfer deed (within 24mo)
    heir_transfer_date: str = ""         # YYYY-MM-DD of that transfer
    heir_same_surname: str = ""          # "yes" if grantee surname matches decedent's (family transfer heuristic)
    # Current-title-holder discovered by walking the decedent's deed chain (Phase 2b)
    # Populated whether or not the holder differs from the decedent — used by
    # Phase 2a as the primary PVA search target for the property-confirmation step.
    current_property_holder: str = ""        # Name to search PVA with (decedent / estate / trust / heir)
    current_holder_relationship: str = ""    # "self" | "trust" | "heir_recent" | "" (no deed chain)
    # Street address extracted from the active mortgage PDF (Phase 2b OCR).
    # Used as a fallback PVA-search input when name-based PVA search misses.
    # Solves cases like married-name PVA records or middle-initial mismatches.
    deed_discovered_address: str = ""        # e.g. "7802 RIVA RIDGE PT" — no city/state/zip
    deed_discovered_parcel_id: str = ""      # 12-char Jefferson PIDN if extracted from deed
    # CourtNet case-detail enrichment (KY probate; Phase 2c)
    estate_attorney_name: str = ""       # Attorney representing the estate (party-type AP)
    courtnet_party_types: str = ""       # Pipe-separated party-type codes observed on the case (AP|FI|AD|PE|HE|...)


# ── Known cities in target markets ────────────────────────────────────
# TN: Knox & Blount counties. KY: Jefferson County (Louisville metro).
# Sorted longest-first so "Lenoir City" matches before "City".
# Louisville is the tricky one — it's both a small TN town (in Blount) and
# the Jefferson KY seat. Disambiguation happens downstream via state/zip,
# not here.
KNOWN_CITIES: list[str] = sorted(
    [
        # TN — Knox & Blount metro
        "Knoxville", "Maryville", "Alcoa", "Farragut", "Powell",
        "Lenoir City", "Loudon", "Oak Ridge", "Clinton", "Sevierville",
        "Pigeon Forge", "Gatlinburg", "Karns", "Halls", "Concord",
        "Friendsville", "Louisville", "Townsend", "Walland", "Rockford",
        "Corryton", "Mascot", "Strawberry Plains", "New Market",
        "Kodak", "Dandridge", "Bean Station", "Jefferson City",
        "Morristown", "Madisonville", "Vonore", "Greenback",
        # KY — Jefferson County / Louisville metro
        "Anchorage", "Audubon Park", "Bancroft", "Barbourmeade",
        "Bellemeade", "Bellewood", "Beechwood Village", "Blue Ridge Manor",
        "Briarwood", "Broeck Pointe", "Brownsboro Farm", "Brownsboro Village",
        "Cambridge", "Coldstream", "Creekside", "Crossgate", "Douglass Hills",
        "Fincastle", "Forest Hills", "Glenview", "Glenview Hills",
        "Glenview Manor", "Goose Creek", "Graymoor-Devondale", "Green Spring",
        "Heritage Creek", "Hickory Hill", "Hills and Dales", "Hollow Creek",
        "Hollyvilla", "Houston Acres", "Hurstbourne", "Hurstbourne Acres",
        "Indian Hills", "Jeffersontown", "Kingsley", "Langdon Place",
        "Lincolnshire", "Lyndon", "Lynnview", "Manor Creek", "Maryhill Estates",
        "Meadow Vale", "Meadowbrook Farm", "Meadowview Estates", "Middletown",
        "Minor Lane Heights", "Mockingbird Valley", "Moorland", "Murray Hill",
        "Northfield", "Norwood", "Old Brownsboro Place", "Parkway Village",
        "Plantation", "Poplar Hills", "Prospect", "Richlawn", "Riverwood",
        "Rolling Fields", "Rolling Hills", "Saint Matthews", "St Matthews",
        "Saint Regis Park", "St Regis Park", "Seneca Gardens", "Shively",
        "South Park View", "Spring Mill", "Spring Valley", "Strathmoor Manor",
        "Strathmoor Village", "Sycamore", "Ten Broeck", "Thornhill",
        "Watterson Park", "Wellington", "West Buechel", "Westwood",
        "Wildwood", "Windy Hills", "Woodland Hills", "Woodlawn Park",
        "Worthington Hills", "Buechel", "Fern Creek", "Fairdale", "Highview",
        "Newburg", "Okolona", "Pleasure Ridge Park", "Valley Station",
    ],
    key=len,
    reverse=True,
)

# Back-compat alias — some external callers may still import TN_CITIES.
TN_CITIES = KNOWN_CITIES

# Set version for O(1) membership tests in standalone address validation
_KNOWN_CITIES_SET: set[str] = {c.title() for c in KNOWN_CITIES}

# ── Reusable suffix pattern ──────────────────────────────────────────
# Word-boundary at the end prevents matching "Cir" inside "Circuit", etc.
_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|"
    r"Pike|Highway|Hwy|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
    r"Cove|Cv|Loop|Run|Path|Ridge|Rdg|Crossing|Xing|"
    r"Bend|Point|Pt|Pass|Hollow|Holw|Glen|Glenn|View|"
    r"Landing|Lndg|Row|Trace|Walk|Knoll|Overlook|Crest|Spur|Commons)\b"
)

# House number (1-5 digits) + optional direction + street words + suffix
# Uses (?:\w+\s+)+? to match one or more street name words (each followed by
# a space), then the suffix. This is much safer than [\w\s]+? which grabs junk.
_ADDR_PART = (
    r"(\d{1,5}\s+"                 # house number
    r"(?:[NSEW]\.?\s+)?"           # optional direction prefix
    r"(?:[\w'-]+\s+)+?"            # street name words (1+)
    + _SUFFIX +
    r"\.?)"                        # optional trailing period
)

# ── Property address indicator phrases (high confidence) ─────────────
# These phrases appear in legal notices right before the actual property address.
_PROP_INDICATOR = (
    r"(?:"
    r"commonly\s+known\s+as"
    r"|property\s+known\s+as"
    r"|property\s+address\s*(?:is|of|:)"
    r"|(?:real\s+)?property\s+(?:located|situated)\s+at"
    r"|said\s+property\s+(?:being|is)"
    r"|hereinafter\s+(?:known|described)\s+as"
    r"|also\s+known\s+as"
    r"|a/?k/?a"
    r"|known\s+as"
    r"|bearing\s+the\s+address\s+(?:of\s+)?"
    r"|having\s+(?:the\s+)?address\s+(?:of\s+)?"
    r"|street\s+address\s*(?:is|of|:)?"
    r"|civic\s+address\s*(?:is|of|:)?"
    r"|property\s+at"
    r"|being\s+the\s+(?:same\s+)?property\s+(?:located\s+)?at"
    r"|with\s+(?:the|an)\s+address\s+(?:of\s+)?"
    r"|the\s+address\s+of\s+(?:which|said\s+property|the\s+property)\s+(?:is|being)"
    r"|referred\s+to\s+as"
    r"|(?:property\s+)?identified\s+as"
    r"|address/?description\s*:"
    r")"
)

# Optional ", Knox County" / ", Jefferson County" etc. between city and state
_OPTIONAL_COUNTY = r"(?:\s*[,.]\s*\w+\s+County)?"

# Reusable state fragment — covers TN and KY (the two states we currently target).
# Tennessee | Tenn. | TN | Kentucky | Ky. | KY
_STATE = r"(?:Tennessee|Tenn\.?|TN|Kentucky|Ky\.?|KY)"

# FULL match: indicator + address + city + [county] + state + zip
# Captures (address, city, zip) all from the same context.
FULL_PROPERTY_RE = re.compile(
    _PROP_INDICATOR
    + r"\s*[:.,\s]*"
    + _ADDR_PART
    + r"(?:\s*[,.]?\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?"
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"           # city name
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + _STATE
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",     # optional zip
    re.IGNORECASE,
)

# Address-only match: indicator + address (no city/state/zip in same line)
PROPERTY_ADDR_RE = re.compile(
    _PROP_INDICATOR + r"\s*[:.,\s]*" + _ADDR_PART,
    re.IGNORECASE,
)

# "located at ADDRESS, CITY, STATE ZIP" — secondary, used for tax sales.
# We validate the result against the blacklist to filter auction locations.
LOCATED_AT_FULL_RE = re.compile(
    r"located\s+at\s+"
    + _ADDR_PART
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + _STATE
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)

LOCATED_AT_ADDR_RE = re.compile(
    r"located\s+at\s+" + _ADDR_PART,
    re.IGNORECASE,
)

# Standalone "ADDRESS, CITY, STATE ZIP" — no indicator phrase required.
# Only used for tax_sale / tax_lien notices as a last resort before giving up.
STANDALONE_ADDR_RE = re.compile(
    _ADDR_PART
    + r"\s*[,.]\s*"
    + r"([\w][\w\s]*?)"           # city name
    + _OPTIONAL_COUNTY
    + r"\s*[,.]\s*"
    + _STATE
    + r"\s*[,.\s]*"
    + r"(\d{5}(?:-\d{4})?)?",     # optional zip
    re.IGNORECASE,
)

# ── Address validation ───────────────────────────────────────────────

# Words that indicate the address is a courthouse / auction location / office
_BAD_ADDR_WORDS = [
    "courthouse", "court house", "county building", "city building",
    "city county", "register", "office of", "entrance",
    "county court", "usual and customary", "main entrance",
]

# Known government / courthouse addresses (normalized lowercase)
_KNOWN_BAD_ADDRS = [
    "400 main street",      # Knox County City-County Building
    "400 main avenue",
    "400 main ave",
    "400 w main",
    "345 court street",     # Blount County courthouse area
    "345 court st",
    "800 s gay st",         # Downtown Knoxville (law offices)
    "800 s. gay st",
    "800 south gay",
    "300 main street",      # Blount County courthouse
    "300 main st",
    # Jefferson County KY (Louisville)
    "700 w jefferson",      # Louis D. Brandeis Hall of Justice
    "700 west jefferson",
    "600 w jefferson",      # Jefferson Judicial Center
    "600 west jefferson",
    "527 w jefferson",      # Louisville Metro Hall
    "527 west jefferson",
    "514 w liberty",        # Jefferson County Clerk
    "514 west liberty",
]


def _is_valid_address(addr: str) -> bool:
    """Reject addresses that are clearly not property addresses."""
    if not addr or len(addr.strip()) < 5:
        return False

    lower = addr.lower()

    # Reject if contains courthouse/office keywords
    for bad in _BAD_ADDR_WORDS:
        if bad in lower:
            return False

    # Reject if matches known government/courthouse addresses
    normalized = re.sub(r"\s+", " ", lower.strip())
    for bad_addr in _KNOWN_BAD_ADDRS:
        if normalized.startswith(bad_addr):
            return False

    # House number sanity: must be 1-99999
    m = re.match(r"(\d+)", addr)
    if m:
        num = int(m.group(1))
        if num < 1 or num > 99999:
            return False

    return True


# ── Zip code ─────────────────────────────────────────────────────────
# TN:  37xxx / 38xxx (Knox, Blount, surrounding East TN counties)
# KY:  40xxx / 41xxx / 42xxx — Louisville/Jefferson is 402xx specifically,
#      but keep the whole KY range so we don't silently drop valid PR/DM
#      mailing addresses from neighboring counties.
ZIP_RE = re.compile(r"\b(3[78]\d{3}|4[012]\d{3})(?:-\d{4})?\b")

# Zips to reject when found via fallback (no address context):
# Courthouse / auction / law-office zips that commonly appear in notice text
_COURTHOUSE_ZIPS = {
    "37902",  # Downtown Knoxville (courthouse, City-County Building)
    "37901",  # Knoxville PO Box area
    "38103",  # Memphis (law firms often referenced)
    "38101",  # Memphis PO Box area
    "37219",  # Nashville (state offices)
    "40202",  # Downtown Louisville (Jefferson Judicial Center, Metro Hall)
    "40601",  # Frankfort (KY state offices)
}

# Expected zip prefixes by county (for fallback validation)
_COUNTY_ZIP_PREFIXES: dict[str, list[str]] = {
    "Knox":      ["377", "378", "379"],
    "Blount":    ["377", "378"],
    "Jefferson": ["402"],  # Louisville metro (KY). 402xx is Jefferson County.
}


# ── Owner name patterns ──────────────────────────────────────────────

# "executed by JOHN DOE AND JANE DOE, conveying..."
# Stop words expanded to catch "conveying", "wife", "husband", etc.
EXECUTED_BY_RE = re.compile(
    r"executed\s+(?:on\s+\w+\s+\d+,?\s+\d{4},?\s+)?by\s+"
    r"([A-Z][A-Za-z\s.,]+?)"
    r"(?:"
    r"\s*,\s*(?:conveying|a\s|an\s|as\s|her\s|his\s|to\s|who\s|wife|husband|"
    r"being|unmarried|single|granting|transferring|said|the\s|for\s+the\s+benefit)"
    r"|\s+conveying\b"
    r"|\s+granting\b"
    r"|\s+transferring\b"
    r"|\s+for\s+the\s+benefit\b"
    r"|\s+to\s+[\w\s,]+?(?:trustee|trust\b)"
    r"|\s*\("
    r"|\.\s+(?:The|Said|This|Such)"
    r")",
    re.IGNORECASE,
)

# "made by NAME" / "given by NAME" — common in deed of trust references
MADE_BY_RE = re.compile(
    r"(?:made|given)\s+by\s+"
    r"([A-Z][A-Za-z\s.,]+?)"
    r"(?:\s*,\s*(?:dated|to\s|conveying|a\s|an\s|as\s|her\s|his\s|who\s|wife|husband|"
    r"being|unmarried|single|granting|transferring|said|the\s)"
    r"|\s+(?:dated|to\s+[\w\s,]+?(?:trustee|trust\b))"
    r"|\s*\("
    r"|\.\s+(?:The|Said|This|Such))",
    re.IGNORECASE,
)

# "from NAME to TRUSTEE" — deed of trust transfer language
FROM_TO_RE = re.compile(
    r"from\s+([A-Z][A-Za-z\s.,]+?)\s*,?\s+to\s+[\w\s,]+?(?:trustee|trust\b)",
    re.IGNORECASE,
)

# "Grantor(s): NAME" / "the grantor, NAME"
GRANTOR_RE = re.compile(
    r"grantor\(?s?\)?\s*(?:herein)?[:\s,]+([A-Z][A-Za-z\s.,]+?)"
    r"(?:\s*,\s*(?:conveying|to\s|a\s|an\s|dated)|"
    r"\s+to\s+[\w\s,]+?(?:trustee|trust\b)|"
    r"\s*\(|\.\s+)",
    re.IGNORECASE,
)

# "borrower(s): NAME" / "the borrower, NAME"
BORROWER_RE = re.compile(
    r"borrower\(?s?\)?\s*[,:\s]+(?:being\s+)?([A-Z][A-Za-z\s.,]+?)"
    r"(?:\s*,|\s+at\b|\s+of\b|\s+in\b|\s*\(|\.\s+)",
    re.IGNORECASE,
)

# "WHEREAS, NAME, as borrower(s), executed" — Vylla/Brock & Scott format
WHEREAS_BORROWER_RE = re.compile(
    r"WHEREAS,\s+([A-Z][A-Za-z\s.,]+?)"
    r"\s*,\s*(?:as\s+borrower|an?\s+unmarried|husband\s+and\s+wife|"
    r"a\s+(?:single|married)|wife\s+and\s+husband)",
    re.IGNORECASE,
)

# "Whereas, NAME by Deed of Trust" / "NAME executed a Deed of Trust" — Nestor format
WHEREAS_DEED_RE = re.compile(
    r"WHEREAS,\s+([A-Z][A-Za-z\s.,]+?)\s+(?:by\s+Deed|executed\s+a\s+Deed)",
    re.IGNORECASE,
)

# "Current Owner(s): NAME" — structured label in some notice formats
CURRENT_OWNER_RE = re.compile(
    r"Current\s+Owner\(?s?\)?\s*:\s*([A-Z][A-Za-z\s.,]+?)(?:\s*\n|\s*$|\s+Other)",
    re.IGNORECASE | re.MULTILINE,
)

# Fallback owner patterns
OWNER_PATTERNS = [
    MADE_BY_RE,
    FROM_TO_RE,
    GRANTOR_RE,
    BORROWER_RE,
    WHEREAS_BORROWER_RE,
    WHEREAS_DEED_RE,
    CURRENT_OWNER_RE,
    re.compile(r"default\s+(?:of|by)\s+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s*\(|\s+in\b)", re.IGNORECASE),
    re.compile(r"property\s+of\s+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s*\(|\s+in\b)", re.IGNORECASE),
    re.compile(r"against\s+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s+for\b|\s+at\b)", re.IGNORECASE),
]

# Probate — personal representative / executor / administrator
PROBATE_NAME_RE = re.compile(
    r"(?:Personal\s+Representative(?:\(S\))?|Executor|Executrix|Administrator|Administratrix)"
    r"[:\s]+([A-Z][A-Za-z\s.]+?)(?:\s*,|\s*\(|\s+of\b|\s+for\b|\s+\d|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# Probate — decedent name from "Estate of [NAME], Deceased"
DECEDENT_NAME_RE = re.compile(
    r"Estate\s+of\s+([A-Z][A-Za-z\s.,'\-]+?)"
    r"(?:\s*,?\s*(?:Deceased|Dec['\u2019.]?\s*d|who\s+died))",
    re.IGNORECASE,
)

# Probate — PR mailing address (street + city + state + zip after the PR title)
# Anchors from the PR title keyword, skips over name/title (non-digit chars),
# then captures: (1) street address, (2) city, (3) state, (4) zip
PR_ADDRESS_RE = re.compile(
    r"(?:Personal\s+Representative(?:\(S\))?|Executor|Executrix|Administrator|Administratrix)"
    r"[^0-9]{3,80}"                   # skip PR name + optional title suffix
    r"(\d{1,5}\s+"                     # house number
    r"[\w\s.,'#-]+?"                   # street name words (non-greedy)
    + _SUFFIX +
    r"\.?"
    r"(?:\s*[,.]?\s*(?:Suite|Ste\.?|Apt\.?|Unit|#)\s*\.?\s*[\w.]+)?)"  # optional unit
    r"\s*[,.\s]+\s*"
    r"([A-Za-z][\w\s]*?)"             # city
    r"\s*[,.]\s*"
    r"(" + _STATE + r")"              # state (captured)
    r"\s*[,.\s]*"
    r"(\d{5})",                        # zip
    re.IGNORECASE,
)


def _normalize_state(raw: str) -> str:
    """Collapse a matched state fragment to the 2-letter abbreviation."""
    if not raw:
        return ""
    s = raw.strip().upper().rstrip(".")
    if s in ("TN", "TENNESSEE", "TENN"):
        return "TN"
    if s in ("KY", "KENTUCKY"):
        return "KY"
    return s

# Names that are clearly not real person names
_INVALID_NAMES = {
    "said property", "the grantor", "the grantors", "the creditor",
    "the creditors", "the respondent", "respondent", "the defendant",
    "defendant", "the borrower", "the mortgagor", "the debtor",
    "the estate", "the above", "the property", "the court",
    "all persons", "unknown heirs", "you in the", "you and",
    "the cause", "the following", "the undersigned",
    "executed a deed", "executed a d", "default having",
}


def _is_valid_name(name: str) -> bool:
    """Reject names that are obviously not real person/entity names."""
    lower = name.strip().lower()
    if lower in _INVALID_NAMES:
        return False
    for bad in _INVALID_NAMES:
        if lower.startswith(bad):
            return False
    if len(name) > 80 or len(name) < 3:
        return False
    return True


# Structured metadata from the detail page (labeled fields)
PUBLISH_DATE_RE = re.compile(
    r"Notice Publish Date:\s*\n?\s*(?:\w+day,\s*)?(\w+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


# ── Auction / sale date patterns ─────────────────────────────────────
# These phrases appear in foreclosure notices before the scheduled sale date.
# The date may be "Month DD, YYYY", "MM/DD/YYYY", or with a day-of-week prefix.

# Reusable date fragment: matches "March 18, 2026", "MARCH 27, 2026",
# "04/17/2025", optional day-of-week prefix like "Wednesday, " or "FRIDAY, "
_DATE_FRAGMENT = (
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*)?"
    r"("
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}\s*,?\s*\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")"
)

AUCTION_DATE_PATTERNS = [
    # "Sale at public auction will be on March 18, 2026"
    re.compile(
        r"(?:sale\s+at\s+public\s+auction|public\s+auction\s+sale)\s+will\s+be\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "will, on March 5, 2026" / "will on March 5, 2026"
    re.compile(
        r"will\s*,?\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "Sale Date and Location: MARCH 6, 2026" / "Sale Date: March 6, 2026"
    re.compile(
        r"Sale\s+Date\s*(?:and\s+\w+)?\s*:\s*" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "will be sold ... on March 5, 2026" (within 60 chars)
    re.compile(
        r"will\s+be\s+sold\b.{0,60}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # "sale will be on March 5, 2026" / "sale will be held on ..."
    re.compile(
        r"sale\s+will\s+be\s+(?:held\s+)?on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # "sell at public auction ... on March 5, 2026" (within 80 chars)
    re.compile(
        r"sell\s+at\s+public\s+auction\b.{0,80}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # "proceed to sell ... on 3/5/2026" (within 80 chars)
    re.compile(
        r"proceed\s+to\s+sell\b.{0,80}?\bon\s+" + _DATE_FRAGMENT,
        re.IGNORECASE | re.DOTALL,
    ),
    # "the 12th day of February, 2026" — ordinal date format
    re.compile(
        r"the\s+(\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s*,?\s*\d{4})",
        re.IGNORECASE,
    ),
    # "sell the property described on: Friday, February 20, 2026"
    re.compile(
        r"(?:sell|advertise)\s+the\s+property\s+described\s+on\s*:\s*" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
    # ", on DATE[,] at/on or about HH:MM" — sale scheduled with specific time
    re.compile(
        r",\s+on\s+" + _DATE_FRAGMENT + r"\s*,?\s+(?:at|on)\s+(?:or\s+about\s+)?\d{1,2}:\d{2}",
        re.IGNORECASE,
    ),
    # "notice is hereby given that on DATE" — HUD foreclosure notices
    re.compile(
        r"notice\s+is\s+hereby\s+given\s+that\s+on\s+" + _DATE_FRAGMENT,
        re.IGNORECASE,
    ),
]


# ── County validation ────────────────────────────────────────────────
# These patterns detect when a notice's actual property is in a different
# county than the saved search that returned it (false positive from keyword match).

# "Register's Office for {County} County" — property deed is recorded there
_REGISTER_COUNTY_RE = re.compile(
    r"Register'?s\s+Office\s+(?:for|of)\s+(\w+)\s+County",
    re.IGNORECASE,
)

# "{County} County Courthouse" — sale location / property county
_COURTHOUSE_COUNTY_RE = re.compile(
    r"(\w+)\s+County\s+Courthouse",
    re.IGNORECASE,
)

# Counties we care about — notices for other counties are false positives.
# TN: Knox, Blount.  KY: Jefferson (Louisville metro).
_TARGET_COUNTIES = {"knox", "blount", "jefferson"}


def is_target_county(text: str, search_county: str) -> bool:
    """Check if the notice's actual property county matches our target counties.

    The search may return notices that merely *mention* Knox County (e.g. the
    trustee is from Knox County) but the actual property is in Hamilton, Hardeman,
    Union, etc.  We detect this by looking at Register's Office and Courthouse
    references which indicate where the property actually is.

    Returns True if the property appears to be in one of our target counties
    (Knox, Blount, Jefferson) or if we can't determine the county — benefit of
    the doubt.
    """
    # Find all Register's Office mentions — the first one is typically the
    # property's recording county (later mentions may be trustee appointments)
    register_matches = _REGISTER_COUNTY_RE.findall(text)
    courthouse_matches = _COURTHOUSE_COUNTY_RE.findall(text)

    # Collect unique county names from both patterns
    mentioned_counties = set()
    for c in register_matches:
        mentioned_counties.add(c.lower())
    for c in courthouse_matches:
        mentioned_counties.add(c.lower())

    if not mentioned_counties:
        return True  # Can't determine — keep it

    # If ANY of our target counties appear, keep the notice
    if mentioned_counties & _TARGET_COUNTIES:
        return True

    # Only non-target counties found — this is a false positive
    logger.info(
        "County mismatch: search='%s' but property in %s — filtering out",
        search_county, ", ".join(sorted(mentioned_counties)).title(),
    )
    return False


# ── Main parser ───────────────────────────────────────────────────────


async def _try_extract_pdf_text(page: Page) -> str:
    """Try to extract full text from the PDF embedded on the notice detail page.

    The site embeds a PDF viewer above the web text.  When the web display is
    truncated to 1,000 characters, the full text may only be available in the PDF.
    We look for an <iframe> or <embed>/<object> with a PDF src and download it.
    """
    try:
        # Look for PDF iframe, embed, or object element
        pdf_url = await page.evaluate("""() => {
            // iframe with PDF src
            const iframe = document.querySelector('iframe[src*=".pdf"], iframe[src*="PDF"]');
            if (iframe) return iframe.src;
            // embed element
            const embed = document.querySelector('embed[src*=".pdf"], embed[type="application/pdf"]');
            if (embed) return embed.src;
            // object element
            const obj = document.querySelector('object[data*=".pdf"], object[type="application/pdf"]');
            if (obj) return obj.data;
            // Link to PDF
            const link = document.querySelector('a[href*=".pdf"]');
            if (link) return link.href;
            return null;
        }""")

        if not pdf_url:
            return ""

        logger.info("Found PDF URL: %s", pdf_url[:120])

        # Download the PDF using the page's browser context (inherits cookies/session)
        response = await page.context.request.get(pdf_url)
        if response.status != 200:
            logger.warning("PDF download failed: HTTP %d", response.status)
            return ""

        pdf_bytes = await response.body()

        # Extract text using pdfminer (lightweight, included in base image)
        try:
            from io import BytesIO
            from pdfminer.high_level import extract_text as pdfminer_extract
            text = pdfminer_extract(BytesIO(pdf_bytes))
            if text and len(text.strip()) > 100:
                logger.info("PDF text extracted: %d chars", len(text))
                return text.strip()
        except ImportError:
            logger.debug("pdfminer not available — skipping PDF extraction")
        except Exception as e:
            logger.warning("PDF text extraction failed: %s", e)

    except Exception as e:
        logger.debug("PDF URL detection failed: %s", e)

    return ""


async def parse_notice_page(
    page: Page, county: str, notice_type: str, llm_api_key: str | None = None,
) -> NoticeData:
    """Extract structured fields from a notice detail page (after CAPTCHA solve).

    Uses both the structured metadata labels on the page and regex parsing
    of the notice body text.  When llm_api_key is provided, falls back to
    Claude Haiku for any fields the regex parser couldn't extract.
    """
    notice = NoticeData(
        county=county,
        notice_type=notice_type,
        source_url=page.url,
    )

    # Get the full page text (includes both metadata labels and notice body)
    full_text = await page.inner_text("body")

    # Normalize non-breaking spaces → regular spaces (breaks \s+ regex matching)
    full_text = full_text.replace("\xa0", " ")

    # Extract the notice body content (after "Notice Content" header)
    notice_content = _extract_notice_content(full_text)

    # If web text is truncated, try extracting full text from the embedded PDF
    if notice_content and "Web display limited to" in notice_content:
        pdf_text = await _try_extract_pdf_text(page)
        if pdf_text:
            notice_content = pdf_text

    notice.raw_text = notice_content if notice_content else full_text

    if not notice.raw_text.strip():
        logger.warning("No notice text found on %s", page.url)
        return notice

    # ── Extract structured metadata from labels ────────────────────
    notice.date_added = _extract_publish_date(full_text)

    # ── Extract fields from the notice body text ───────────────────
    _parse_address(notice)
    _parse_name(notice)
    _parse_pr_address(notice)
    if notice_type != "probate":
        _parse_auction_date(notice)

    # ── LLM fallback for missing fields ──────────────────────────
    needs_llm = (
        (notice_type == "probate" and (not notice.owner_name or not notice.decedent_name or not notice.owner_street))
        or (notice_type != "probate" and (not notice.address or not notice.owner_name or not notice.auction_date))
    )
    if llm_api_key and needs_llm:
        from llm_parser import extract_with_llm

        llm_result = await extract_with_llm(
            notice.raw_text, notice_type, county, llm_api_key,
        )

        if notice_type == "probate":
            # Probate: fill decedent name, PR name, and PR mailing address
            if not notice.decedent_name and llm_result.get("decedent_name"):
                notice.decedent_name = llm_result["decedent_name"]
                logger.info("LLM filled decedent: %s", notice.decedent_name)
            if not notice.owner_name and llm_result.get("owner_name"):
                notice.owner_name = llm_result["owner_name"]
                logger.info("LLM filled PR: %s", notice.owner_name)
            if not notice.owner_street and llm_result.get("owner_street"):
                notice.owner_street = llm_result["owner_street"]
                notice.owner_city = llm_result.get("owner_city") or notice.owner_city
                notice.owner_state = (
                    llm_result.get("owner_state")
                    or notice.owner_state
                    or notice.state
                    or "TN"
                )
                notice.owner_zip = llm_result.get("owner_zip") or notice.owner_zip
                logger.info("LLM filled PR address: %s", notice.owner_street)
        else:
            # Foreclosure / tax sale / tax lien
            if not notice.address and llm_result.get("address"):
                notice.address = llm_result["address"]
                notice.city = llm_result.get("city") or notice.city
                notice.zip = llm_result.get("zip") or notice.zip
                logger.info("LLM filled address: %s", notice.address)
            if not notice.owner_name and llm_result.get("owner_name"):
                notice.owner_name = llm_result["owner_name"]
                logger.info("LLM filled owner: %s", notice.owner_name)
            if not notice.auction_date and llm_result.get("auction_date"):
                notice.auction_date = llm_result["auction_date"]
                logger.info("LLM filled auction_date: %s", notice.auction_date)

    return notice


# ── Metadata extractors ──────────────────────────────────────────────


def _extract_notice_content(full_text: str) -> str:
    """Pull just the notice body from the full page text.

    The page has a "Notice Content" label followed by the actual legal text,
    then "Back" and footer content.
    """
    # Find "Notice Content" section
    marker = "Notice Content"
    idx = full_text.find(marker)
    if idx == -1:
        return ""

    body = full_text[idx + len(marker):]

    # Trim at the footer / "Back" link / language selector
    for end_marker in ["\nBack\n", "\nIf you have any questions", "\nSelect Language"]:
        end_idx = body.find(end_marker)
        if end_idx != -1:
            body = body[:end_idx]
            break

    return body.strip()


def _extract_publish_date(full_text: str) -> str:
    """Extract the Notice Publish Date from the structured metadata labels."""
    m = PUBLISH_DATE_RE.search(full_text)
    if m:
        return _normalize_date(m.group(1))
    return ""


def _normalize_date(raw: str) -> str:
    """Convert various date formats to YYYY-MM-DD."""
    raw = raw.strip().rstrip(".")

    # Handle ordinal format: "12th day of February, 2026" → "February 12, 2026"
    ordinal_m = re.match(
        r"(\d{1,2})(?:st|nd|rd|th)\s+day\s+of\s+(\w+)\s*,?\s*(\d{4})",
        raw, re.IGNORECASE,
    )
    if ordinal_m:
        raw = f"{ordinal_m.group(2)} {ordinal_m.group(1)}, {ordinal_m.group(3)}"

    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _parse_auction_date(notice: NoticeData) -> None:
    """Extract the scheduled sale/auction date from the notice body text."""
    text = notice.raw_text
    for pattern in AUCTION_DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            raw_date = m.group(1).strip()
            normalized = _normalize_date(raw_date)
            if normalized and len(normalized) >= 8:
                notice.auction_date = normalized
                return


# ── Address extraction ───────────────────────────────────────────────


def _parse_address(notice: NoticeData) -> None:
    """Extract property address, city, and zip from the notice body text.

    Strategy (in priority order):
      1. Full contextual match: "commonly known as ADDRESS, CITY, TN ZIP"
         → extracts address + city + zip from the same phrase
      2. Address-only contextual: "commonly known as ADDRESS"
         → extracts address, then finds city/zip nearby
      3. "located at" pattern: "located at ADDRESS, CITY, TN ZIP"
         → secondary, used for tax sales (validated against blacklist)
      4. Give up — leave fields empty (better than grabbing wrong address)
    """
    text = notice.raw_text.replace("\xa0", " ")

    # ── Strategy 1: Full context — indicator + address + city + TN + zip ──
    m = FULL_PROPERTY_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        if _is_valid_address(addr):
            notice.address = addr
            notice.city = _clean_city(m.group(2))
            if m.group(3):
                notice.zip = m.group(3)
            return

    # ── Strategy 2: Address-only context — indicator + address ──
    m = PROPERTY_ADDR_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        if _is_valid_address(addr):
            notice.address = addr
            # Try to find city and zip near the matched address
            _extract_city_zip_near(notice, text, m.end())
            return

    # ── Strategy 3: "located at" (for tax sales, etc.) ──
    m = LOCATED_AT_FULL_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        # Extra validation: reject if near "sale" or "auction" context
        context_before = _get_context_before(text, m.start(), 80)
        is_sale_location = any(w in context_before for w in [
            "sale", "auction", "held", "entrance", "courthouse",
        ])
        if not is_sale_location and _is_valid_address(addr):
            notice.address = addr
            notice.city = _clean_city(m.group(2))
            if m.group(3):
                notice.zip = m.group(3)
            return

    m = LOCATED_AT_ADDR_RE.search(text)
    if m:
        addr = _clean_address(m.group(1))
        context_before = _get_context_before(text, m.start(), 80)
        is_sale_location = any(w in context_before for w in [
            "sale", "auction", "held", "entrance", "courthouse",
        ])
        if not is_sale_location and _is_valid_address(addr):
            notice.address = addr
            _extract_city_zip_near(notice, text, m.end())
            return

    # ── Strategy 4: Standalone "ADDRESS, CITY, TN ZIP" for tax types ──
    # Tax sale / tax lien notices sometimes list the address without an
    # indicator phrase. We only try this for those types and validate
    # against known bad addresses and auction context.
    if notice.notice_type in ("tax_sale", "tax_lien"):
        m = STANDALONE_ADDR_RE.search(text)
        if m:
            addr = _clean_address(m.group(1))
            city = _clean_city(m.group(2))
            # Reject if near sale/auction context
            context_before = _get_context_before(text, m.start(), 100)
            is_sale_ctx = any(w in context_before for w in [
                "sale", "auction", "held at", "entrance", "courthouse",
                "conducted at", "front door",
            ])
            if not is_sale_ctx and _is_valid_address(addr) and city in _KNOWN_CITIES_SET:
                notice.address = addr
                notice.city = city
                if m.group(3):
                    notice.zip = m.group(3)
                return

    # ── Strategy 5: No confident match → leave address empty ──
    # Still try to extract city/zip if they appear in context
    _extract_city_zip_fallback(notice, text)


def _get_context_before(text: str, pos: int, chars: int) -> str:
    """Get lowercase text in the window before a position."""
    s: int = pos - chars
    if s < 0:
        s = 0
    return text[s:pos].lower()


def _extract_city_zip_near(notice: NoticeData, text: str, addr_end: int) -> None:
    """Extract city and zip from the text near the end of the address match.

    Looks in the 200 characters after the address for "City, STATE ZIP".
    STATE covers TN and KY full names + abbreviations.
    """
    window = text[addr_end:addr_end + 200]

    # Try "CITY, [County,] STATE ZIP" (TN or KY, full or abbreviated)
    city_state_re = re.compile(
        r"[,.\s]+([\w][\w\s]*?)"
        r"(?:\s*[,.]\s*\w+\s+County)?"   # optional county
        r"\s*[,.]\s*" + _STATE +
        r"\s*[,.\s]*(\d{5}(?:-\d{4})?)?",
        re.IGNORECASE,
    )
    m = city_state_re.search(window)
    if m:
        notice.city = _clean_city(m.group(1))
        if m.group(2):
            notice.zip = m.group(2)
        return

    # Fallback: find a known city in the window
    window_upper = window.upper()
    for city in KNOWN_CITIES:
        if city.upper() in window_upper:
            notice.city = city
            break

    # Find a zip near the address
    zip_match = ZIP_RE.search(window)
    if zip_match:
        notice.zip = zip_match.group(1)


def _is_valid_fallback_zip(zip_code: str, county: str) -> bool:
    """Check if a zip found via fallback (no address context) is plausible."""
    if zip_code in _COURTHOUSE_ZIPS:
        return False
    prefixes = _COUNTY_ZIP_PREFIXES.get(county)
    if prefixes and not any(zip_code.startswith(p) for p in prefixes):
        return False
    return True


def _extract_city_zip_fallback(notice: NoticeData, text: str) -> None:
    """Last resort: find city/zip anywhere in the notice text.

    Only used when no address was found. Finds the first known city and first
    in-state zip code, but rejects courthouse/out-of-county zips.
    """
    if not notice.city:
        text_upper = text.upper()
        for city in KNOWN_CITIES:
            if city.upper() in text_upper:
                notice.city = city
                break

    if not notice.zip:
        for zip_match in ZIP_RE.finditer(text):
            candidate = zip_match.group(1)
            if not _is_valid_fallback_zip(candidate, notice.county):
                continue
            notice.zip = candidate
            break


def _clean_address(raw: str) -> str:
    """Normalize whitespace and trailing punctuation in an extracted address."""
    addr = re.sub(r"\s+", " ", raw).strip()
    addr = addr.rstrip(",. ")
    return addr


def _clean_city(raw: str) -> str:
    """Clean up an extracted city name."""
    city = re.sub(r"\s+", " ", raw).strip()
    city = city.rstrip(",. ")
    # Title-case if all uppercase
    if city.isupper():
        city = city.title()
    return city


# ── Name extraction ──────────────────────────────────────────────────


def _parse_name(notice: NoticeData) -> None:
    """Extract owner/party name based on notice type."""
    text = notice.raw_text.replace("\xa0", " ")

    if notice.notice_type == "probate":
        # Extract decedent name from "Estate of [NAME], Deceased"
        dec_match = DECEDENT_NAME_RE.search(text)
        if dec_match:
            dec_name = _clean_name(dec_match.group(1))
            if _is_valid_name(dec_name):
                notice.decedent_name = dec_name

        # Extract PR/Executor name
        match = PROBATE_NAME_RE.search(text)
        if match:
            name = _clean_name(match.group(1))
            if _is_valid_name(name):
                notice.owner_name = name
        return

    # Foreclosure / tax sale / tax lien — "executed by" is the most common
    match = EXECUTED_BY_RE.search(text)
    if match:
        name = _clean_name(match.group(1))
        if _is_valid_name(name):
            notice.owner_name = name
            return

    # Fallback patterns
    for pattern in OWNER_PATTERNS:
        match = pattern.search(text)
        if match:
            name = _clean_name(match.group(1))
            if _is_valid_name(name):
                notice.owner_name = name
                return


def _parse_pr_address(notice: NoticeData) -> None:
    """Extract the PR's mailing address from probate notice text.

    Probate notices contain the PR/Executor's mailing address (where creditors
    send claims), but NOT the decedent's property address. This extracts the
    PR's street, city, state, and zip into the owner_* fields.
    """
    if notice.notice_type != "probate":
        return

    text = notice.raw_text.replace("\xa0", " ")
    match = PR_ADDRESS_RE.search(text)
    if match:
        street = _clean_address(match.group(1))
        # Title-case — PR addresses in notices are usually ALL CAPS
        if street.isupper():
            street = street.title()
        notice.owner_street = street
        notice.owner_city = _clean_city(match.group(2))
        notice.owner_state = _normalize_state(match.group(3)) or notice.state or "TN"
        notice.owner_zip = match.group(4)
        logger.debug(
            "PR address: %s, %s, %s %s",
            notice.owner_street, notice.owner_city,
            notice.owner_state, notice.owner_zip,
        )


def _clean_name(raw: str) -> str:
    """Normalize a name: trim, title-case, remove trailing conjunctions."""
    name = re.sub(r"\s+", " ", raw).strip()
    # Remove trailing "And" / "and" (word-level — don't strip from "Bolland" etc.)
    name = re.sub(r"\s+,?\s*(?:AND|and)\s*$", "", name)
    # Remove trailing commas, periods
    name = name.rstrip(",. ")
    # Title-case
    name = name.title()
    return name
