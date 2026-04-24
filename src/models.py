"""Shared data models used across the SiftStack pipeline.

State-agnostic. Source scrapers, enrichment steps, formatters, and CSV
exporters all share the same `NoticeData` shape.
"""

from dataclasses import dataclass


@dataclass
class NoticeData:
    """Structured data extracted from a single notice."""
    date_added: str = ""       # Published / filed date (YYYY-MM-DD)
    auction_date: str = ""     # Scheduled sale/auction date (YYYY-MM-DD)
    address: str = ""
    city: str = ""
    state: str = "OH"
    zip: str = ""
    owner_name: str = ""
    notice_type: str = ""      # foreclosure | tax_sale | tax_lien | probate | eviction | code_violation | divorce
    county: str = ""
    source_url: str = ""
    raw_text: str = ""         # Full notice text for classification
    # Smarty address standardization fields (populated post-scrape)
    zip_plus4: str = ""        # Full ZIP+4
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
    # Obituary/heir metadata
    obituary_source_type: str = ""          # "full_page" or "snippet"
    heir_search_depth: str = ""             # "0" (none), "1" (survivors checked), "2" (2nd gen)
    heirs_verified_living: str = ""         # Count of verified living heirs
    heirs_verified_deceased: str = ""       # Count of verified deceased heirs
    heirs_unverified: str = ""              # Count of unverified heirs
    heir_map_json: str = ""                 # JSON-encoded full ranked heir list (all heirs, not just top 3)
    signing_chain_count: str = ""           # Count of living signing-authority heirs
    signing_chain_names: str = ""           # Comma-separated names of signing-authority heirs
    # Error map (flat fields)
    dm_confidence: str = ""                 # "high", "medium", "low"
    dm_confidence_reason: str = ""          # Brief explanation
    missing_data_flags: str = ""            # Pipe-separated: "no_survivors|snippet_only|common_name"
    # Mailability flag
    mailable: str = ""                      # "yes" or "" (unmailable)
    # Entity research fields
    entity_type: str = ""                   # "llc", "corp", "trust", "estate", "lp", "other"
    entity_person_name: str = ""            # Person found behind entity (full name)
    entity_person_role: str = ""            # "registered_agent", "member", "trustee", "officer", etc.
    entity_research_source: str = ""        # "name_parse", "web_search", "sos_snippet"
    entity_research_confidence: str = ""    # "high", "medium", "low"
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
    run_id: str = ""                        # Unique pipeline run identifier for data lineage
