"""Deep-prospecting skip-trace + scoring for a single return-mailer lead.

Target: Mohammed Tarchane, 8946 Maple Ridge Ln, Knoxville, TN 37923-1142.
Source: USPS return mailer (NIXIE — not deliverable as addressed / unable to
forward). Bad mailing address, so we skip trace by name + last-known address to
find current phones, then score them.

Per request this SKIPS the L1-L3 research front-half AND the TruePeopleSearch /
FastPeopleSearch / CyberBackgroundChecks browser waterfall. It runs only the
production automated path:

    Step 1: Tracerfy batch skip trace (living owner)
    Step 2: Trestle phone scoring (activity score + dial tier + litigator risk)

Run from project root:  python src/run_tarchane_skiptrace.py
"""

import json
import logging
import sys
from datetime import datetime

import config
from notice_parser import NoticeData
from tracerfy_skip_tracer import batch_skip_trace
from phone_validator import (
    DEFAULT_TIERS,
    DM_PHONE_FIELDS,
    assign_tier,
    call_trestle,
    clean_phone,
)

# ── Logging to console + file ────────────────────────────────────────
TS = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_PATH = config.LOG_DIR / f"deep_prospect_tarchane_{TS}.log"
config.LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("deep_prospect_tarchane")

OUT_DIR = config.OUTPUT_DIR / "deep_prospect"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Subject (living owner; return-mailer bad address) ─────────────────
notice = NoticeData(
    date_added="2026-06-09",
    address="8946 Maple Ridge Ln",
    city="Knoxville",
    state="TN",
    zip="37923",                 # +4 was 1142; Tracerfy/Trestle want 5-digit
    owner_name="Mohammed Tarchane",
    notice_type="return_mailer",
    county="Knox",
    source_url="USPS return mailer NIXIE 372 — not deliverable as addressed",
)

log.info("=" * 70)
log.info("DEEP PROSPECTING (skip-trace + score only): %s, %s %s %s",
         notice.address, notice.city, notice.state, notice.zip)
log.info("Owner (living): %s | Source: return mailer (bad address)",
         notice.owner_name)
log.info("=" * 70)

# ── Step 1: Tracerfy batch skip trace ─────────────────────────────────
log.info("-" * 70)
log.info("STEP 1: Tracerfy skip trace (living owner, by name + last-known address)")
tracerfy_stats = batch_skip_trace(
    [notice],
    max_signing_traces=1,
    lookup_heir_addresses=False,   # living owner — no heir address backfill
)
log.info("Tracerfy: %s", json.dumps(tracerfy_stats))

# ── Step 2: Trestle scoring of every returned phone ───────────────────
log.info("-" * 70)
log.info("STEP 2: Trestle phone scoring (activity score + dial tier + litigator)")

# Collect all phones returned for the owner (flat DM fields)
raw_phones: list[str] = []
for f in DM_PHONE_FIELDS:
    v = getattr(notice, f, "") or ""
    if v.strip():
        raw_phones.append(v.strip())

emails = [getattr(notice, f"email_{i}", "") for i in range(1, 6)
          if getattr(notice, f"email_{i}", "")]

# Dedup by cleaned 10-digit form
seen: dict[str, str] = {}   # cleaned -> raw
for raw in raw_phones:
    c = clean_phone(raw)
    if c and c not in seen:
        seen[c] = raw

log.info("Scoring %d unique phone(s) for %s...", len(seen), notice.owner_name)

scored_rows: list[dict] = []
key = config.TRESTLE_API_KEY
for c, raw in seen.items():
    data = call_trestle(c, key, add_litigator=True)
    if "error" in data and not data.get("is_valid"):
        log.warning("  %s: Trestle error: %s", c, data.get("error"))
        scored_rows.append({"phone": c, "error": data.get("error")})
        continue
    score = data.get("activity_score")
    tier = assign_tier(score, DEFAULT_TIERS)
    lit = None
    addons = data.get("add_ons") or {}
    if isinstance(addons, dict) and addons.get("litigator_checks"):
        lit = addons["litigator_checks"].get("phone.is_litigator_risk")
    row = {
        "phone": c,
        "activity_score": score,
        "tier": tier,
        "line_type": data.get("line_type"),
        "carrier": data.get("carrier"),
        "is_valid": data.get("is_valid"),
        "is_prepaid": data.get("is_prepaid"),
        "is_litigator_risk": lit,
    }
    scored_rows.append(row)
    log.info("  %s | score=%s tier=%s | %s %s%s",
             c, score, tier, data.get("line_type") or "?",
             data.get("carrier") or "", " | LITIGATOR-RISK" if lit else "")

# Sort dial order: highest activity score first (None last)
def _score_key(r: dict):
    s = r.get("activity_score")
    return (-1 if s is None else -s)

scored_rows.sort(key=_score_key)

# ── Output JSON ───────────────────────────────────────────────────────
summary = {
    "subject": {
        "name": notice.owner_name,
        "last_known_address": f"{notice.address}, {notice.city}, {notice.state} 37923-1142",
        "source": "USPS return mailer (NIXIE 372 — not deliverable as addressed)",
        "owner_status": "living",
    },
    "tracerfy_stats": tracerfy_stats,
    "phones": scored_rows,
    "emails": emails,
}
json_path = OUT_DIR / f"tarchane_skiptrace_{TS}.json"
json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

log.info("=" * 70)
log.info("DONE.  JSON: %s  LOG: %s", json_path, LOG_PATH)
log.info("=" * 70)

print("\n@@RESULT@@" + json.dumps(summary))
