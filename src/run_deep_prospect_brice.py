"""One-off deep prospecting driver for 2106 Brice St, Knoxville, TN 37917.

Property is tax-delinquent with a deceased owner (heir filing, DOD 2026-03-21).
Runs the full research → Tracerfy skip trace → Trestle scoring chain on a single
record and emits a PDF + JSON + console summary.

Run from project root:  python src/run_deep_prospect_brice.py
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import config
from notice_parser import NoticeData
from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
from tracerfy_skip_tracer import batch_skip_trace
from phone_validator import (
    DEFAULT_TIERS,
    DM_PHONE_FIELDS,
    assign_tier,
    call_trestle,
    clean_phone,
)
from report_generator import generate_record_pdf

# ── Logging to console + file ────────────────────────────────────────
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH = config.LOG_DIR / f"deep_prospect_brice_{TS}.log"
config.LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("deep_prospect_brice")

OUT_DIR = config.OUTPUT_DIR / "deep_prospect"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Known facts about the subject property ───────────────────────────
# DOD given by user. Use a recent filing/reference date so the obituary
# DOD sanity check (MAX_DOD_GAP_YEARS = 3) treats the match as plausible.
notice = NoticeData(
    date_added="2026-06-03",
    address="2106 Brice St",
    city="Knoxville",
    state="TN",
    zip="37917",
    owner_name="Norman Willis",
    tax_owner_name="WILLIS NORMAN",     # LAST FIRST helps Knox tax + name parsing
    notice_type="tax_delinquent",
    county="Knox",
    parcel_id="082ad025",               # from prior REISift export
    date_of_death="2026-03-21",         # known fact (heir filing)
)

log.info("=" * 70)
log.info("DEEP PROSPECTING: %s, %s %s %s",
         notice.address, notice.city, notice.state, notice.zip)
log.info("Owner: %s | DOD (known): %s | Type: %s | County: %s",
         notice.owner_name, notice.date_of_death, notice.notice_type, notice.county)
log.info("=" * 70)

# ── Step 1: Full research (tax + Smarty + Zillow + obituary/heir) ─────
opts = PipelineOptions(
    skip_filter_sold=True,
    skip_vacant_filter=True,        # single, known-good address
    skip_commercial_filter=True,
    skip_entity_filter=True,
    skip_entity_research=True,
    skip_parcel_lookup=True,        # already have address + parcel
    skip_tax=False,                 # confirm tax delinquency via Knox API
    skip_smarty=False,              # standardize property + DM addresses
    skip_zillow=False,              # value / equity
    skip_obituary=False,            # <-- heir / decision-maker research
    skip_ancestry=False,            # Ancestry fallback if obit search misses
    skip_heir_verification=False,
    max_heir_depth=2,
    skip_dm_address=False,          # look up mailing address for every signing heir
    tracerfy_tier1=False,
    source_label="Deep Prospect - 2106 Brice St",
)
notices = run_enrichment_pipeline([notice], opts)

if not notices:
    log.error("Record was filtered out by the pipeline - aborting.")
    sys.exit(1)
notice = notices[0]

# If the obituary search could not confirm death on its own, fall back to the
# user-provided ground truth so downstream skip-trace still targets the estate.
if notice.owner_deceased != "yes":
    log.warning("Obituary search did not auto-confirm death. Applying known DOD.")
    notice.owner_deceased = "yes"
    if not notice.date_of_death:
        notice.date_of_death = "2026-03-21"

# ── Step 2: Tracerfy batch skip trace (DM #1 + all signing heirs) ─────
log.info("-" * 70)
log.info("STEP 2: Tracerfy skip trace (DM + signing-authority heirs)")
tracerfy_stats = batch_skip_trace(
    [notice],
    max_signing_traces=5,
    lookup_heir_addresses=True,
    address_lookup_api_key=config.ANTHROPIC_API_KEY or None,
)
log.info("Tracerfy: %s", json.dumps(tracerfy_stats))

# ── Step 3: Trestle scoring of every phone tied to a signing party ────
log.info("-" * 70)
log.info("STEP 3: Trestle phone scoring (activity score + dial tier + litigator)")


def _collect_labeled_phones(n: NoticeData) -> list[tuple[str, str, str]]:
    """Return (party_name, party_role, raw_phone) for every signing-party phone."""
    out: list[tuple[str, str, str]] = []
    dm_label = n.decision_maker_name or n.owner_name or "Owner/Estate"
    dm_role = n.decision_maker_relationship or "owner"
    for f in DM_PHONE_FIELDS:
        v = getattr(n, f, "") or ""
        if v.strip():
            out.append((dm_label, dm_role, v.strip()))
    if n.heir_map_json:
        try:
            heirs = json.loads(n.heir_map_json)
        except (ValueError, TypeError):
            heirs = []
        for h in heirs if isinstance(heirs, list) else []:
            if not isinstance(h, dict):
                continue
            hname = h.get("name", "") or "(heir)"
            hrole = h.get("relationship", "") or "heir"
            for ph in h.get("phones", []) or []:
                if ph and str(ph).strip():
                    out.append((hname, hrole, str(ph).strip()))
    return out


labeled = _collect_labeled_phones(notice)
# Deduplicate by cleaned phone, keep first label
seen: dict[str, tuple[str, str]] = {}
for name, role, raw in labeled:
    c = clean_phone(raw)
    if c and c not in seen:
        seen[c] = (name, role)

log.info("Scoring %d unique phone(s) across signing parties...", len(seen))
phone_tiers: dict[str, dict] = {}      # cleaned phone -> {score, tier, line_type} for PDF
scored_rows: list[dict] = []
key = config.TRESTLE_API_KEY
for c, (name, role) in seen.items():
    data = call_trestle(c, key, add_litigator=True)
    if "error" in data and not data.get("is_valid"):
        log.warning("  %s (%s): Trestle error: %s", c, name, data.get("error"))
        scored_rows.append({"phone": c, "party": name, "role": role,
                            "error": data.get("error")})
        continue
    score = data.get("activity_score")
    tier = assign_tier(score, DEFAULT_TIERS)
    lit = None
    addons = data.get("add_ons") or {}
    if isinstance(addons, dict) and addons.get("litigator_checks"):
        lit = addons["litigator_checks"].get("phone.is_litigator_risk")
    phone_tiers[c] = {"score": score, "tier": tier, "line_type": data.get("line_type")}
    row = {
        "phone": c,
        "party": name,
        "role": role,
        "activity_score": score,
        "tier": tier,
        "line_type": data.get("line_type"),
        "carrier": data.get("carrier"),
        "is_valid": data.get("is_valid"),
        "is_prepaid": data.get("is_prepaid"),
        "is_litigator_risk": lit,
    }
    scored_rows.append(row)
    log.info("  %s | %s (%s) | score=%s tier=%s | %s %s%s",
             c, name, role, score, tier, data.get("line_type") or "?",
             data.get("carrier") or "", " | LITIGATOR-RISK" if lit else "")

# ── Step 4: PDF + JSON outputs ───────────────────────────────────────
log.info("-" * 70)
log.info("STEP 4: Generating PDF + JSON")
pdf_path = generate_record_pdf(notice, output_dir=OUT_DIR, phone_tiers=phone_tiers)

# Parse heir map for the JSON dump
try:
    heir_map = json.loads(notice.heir_map_json) if notice.heir_map_json else []
except (ValueError, TypeError):
    heir_map = []

summary = {
    "property": {
        "address": notice.address, "city": notice.city,
        "state": notice.state, "zip": notice.zip,
        "county": notice.county, "parcel_id": notice.parcel_id,
        "property_type": notice.property_type, "bedrooms": notice.bedrooms,
        "bathrooms": notice.bathrooms, "sqft": notice.sqft,
        "year_built": notice.year_built,
        "estimated_value": notice.estimated_value,
        "estimated_equity": notice.estimated_equity,
        "equity_percent": notice.equity_percent,
        "mls_status": notice.mls_status,
    },
    "tax": {
        "tax_delinquent_amount": notice.tax_delinquent_amount,
        "tax_delinquent_years": notice.tax_delinquent_years,
    },
    "deceased": {
        "owner_name": notice.owner_name,
        "owner_deceased": notice.owner_deceased,
        "date_of_death": notice.date_of_death,
        "obituary_url": notice.obituary_url,
        "obituary_source_type": notice.obituary_source_type,
    },
    "decision_maker": {
        "name": notice.decision_maker_name,
        "relationship": notice.decision_maker_relationship,
        "status": notice.decision_maker_status,
        "source": notice.decision_maker_source,
        "street": notice.decision_maker_street,
        "city": notice.decision_maker_city,
        "state": notice.decision_maker_state,
        "zip": notice.decision_maker_zip,
        "confidence": notice.dm_confidence,
        "confidence_reason": notice.dm_confidence_reason,
        "dm2_name": notice.decision_maker_2_name,
        "dm2_relationship": notice.decision_maker_2_relationship,
        "dm2_status": notice.decision_maker_2_status,
        "dm3_name": notice.decision_maker_3_name,
        "dm3_relationship": notice.decision_maker_3_relationship,
        "dm3_status": notice.decision_maker_3_status,
    },
    "heirs": {
        "verified_living": notice.heirs_verified_living,
        "verified_deceased": notice.heirs_verified_deceased,
        "unverified": notice.heirs_unverified,
        "signing_chain_count": notice.signing_chain_count,
        "signing_chain_names": notice.signing_chain_names,
        "heir_map": heir_map,
    },
    "emails_on_dm": [getattr(notice, f"email_{i}", "") for i in range(1, 6)
                     if getattr(notice, f"email_{i}", "")],
    "tracerfy_stats": tracerfy_stats,
    "trestle_scores": scored_rows,
    "pdf_path": str(pdf_path),
}

json_path = OUT_DIR / f"2106_brice_st_deep_prospect_{TS}.json"
json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

log.info("=" * 70)
log.info("DONE.")
log.info("  PDF:  %s", pdf_path)
log.info("  JSON: %s", json_path)
log.info("  LOG:  %s", LOG_PATH)
log.info("=" * 70)

# Machine-readable marker for the orchestrator to parse the key result
print("\n@@RESULT@@" + json.dumps({
    "owner_deceased": notice.owner_deceased,
    "dod": notice.date_of_death,
    "dm": notice.decision_maker_name,
    "dm_status": notice.decision_maker_status,
    "dm_confidence": notice.dm_confidence,
    "signing_chain_names": notice.signing_chain_names,
    "tax_delinquent_amount": notice.tax_delinquent_amount,
    "tax_delinquent_years": notice.tax_delinquent_years,
    "estimated_value": notice.estimated_value,
    "equity_percent": notice.equity_percent,
    "tracerfy": tracerfy_stats,
    "phones_scored": len([r for r in scored_rows if "activity_score" in r]),
    "pdf": str(pdf_path),
    "json": str(json_path),
}))
