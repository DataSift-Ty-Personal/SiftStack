"""In-depth locator for the 2106 Brice St decision maker: Benjamin L. Holland.

The batch deep-prospect run identified Benjamin L. Holland (surviving spouse of
Norman Willis) as DM #1 but could NOT locate him: Tracerfy returned no phones and
his address defaulted to the property-address fallback. This script does a focused,
multi-source locate:

  1. Re-fetch + print the obituary (verify relationship + family context)
  2. verify_heir_status (living/deceased)
  3. Full tiered address waterfall WITH Tracerfy tier-0 (instant lookup)
  4. Direct Knox Tax owner search
  5. Direct Tracerfy instant lookup (property address as hint)
  6. People-search dump (Serper + CyberBackgroundChecks via Firecrawl)
  7. Tracerfy batch skip trace for phones at any located address
  8. Trestle score of any phones found

Run from project root:  python src/find_benjamin_holland.py
"""

import json
import logging
import sys
from datetime import datetime

import config
from notice_parser import NoticeData
import obituary_enricher as oe
from tracerfy_skip_tracer import batch_skip_trace
from phone_validator import DEFAULT_TIERS, assign_tier, call_trestle, clean_phone

TS = datetime.now().strftime("%Y%m%d_%H%M%S")
config.LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = config.LOG_DIR / f"find_benjamin_{TS}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("find_benjamin")

OUT_DIR = config.OUTPUT_DIR / "deep_prospect"
OUT_DIR.mkdir(parents=True, exist_ok=True)

API = config.ANTHROPIC_API_KEY
CITY = "Knoxville"
OBIT_URL = "https://www.articobits.com/obituaries/unity-mortuary/norman-willis-obituary"
PROP_ADDR, PROP_ZIP = "2106 Brice St", "37917"
NAME_VARIANTS = ["Benjamin L. Holland", "Benjamin Holland", "Ben Holland"]

findings: dict = {"variants": {}, "addresses_found": [], "phones": []}

# ── 1. Obituary text (verify relationship + context) ─────────────────
log.info("=" * 72)
log.info("STEP 1: Re-fetch obituary for relationship/context verification")
obit_text = oe._fetch_page_text(OBIT_URL)
if obit_text:
    log.info("Obituary text (%d chars):\n%s", len(obit_text), obit_text[:5000])
    findings["obituary_excerpt"] = obit_text[:5000]
else:
    log.warning("Could not fetch obituary text from %s", OBIT_URL)
    findings["obituary_excerpt"] = ""

# ── 2. Living/deceased verification ──────────────────────────────────
log.info("=" * 72)
log.info("STEP 2: verify_heir_status for Benjamin")
vstatus = oe.verify_heir_status(heir_name="Benjamin L. Holland", city=CITY, api_key=API)
log.info("Status: %s (%s) | obit_url=%s",
         vstatus.get("status"), vstatus.get("confidence"), vstatus.get("obituary_url"))
findings["verify_status"] = {k: vstatus.get(k) for k in
                             ("status", "confidence", "obituary_url", "date_of_death")}

# ── 3. Tiered address waterfall WITH Tracerfy tier-0 ─────────────────
log.info("=" * 72)
log.info("STEP 3: Tiered DM address waterfall (Tracerfy tier-0 + Knox Tax + people search)")
for nm in NAME_VARIANTS:
    log.info("--- waterfall: %s ---", nm)
    addr = oe._lookup_dm_address(nm, CITY, API, tracerfy_tier1=True)
    findings["variants"][nm] = {"waterfall": addr}
    if addr.get("street"):
        log.info("  HIT (%s): %s, %s %s %s",
                 addr.get("source"), addr["street"], addr.get("city"),
                 addr.get("state"), addr.get("zip"))
        findings["addresses_found"].append({"name": nm, **addr})
    else:
        log.info("  no address from waterfall")

# ── 4. Direct Knox Tax owner search ──────────────────────────────────
log.info("=" * 72)
log.info("STEP 4: Direct Knox Tax owner search")
for tax_name in ["Holland Benjamin", "Holland Benjamin L"]:
    kt = oe._lookup_dm_address_knox_tax(tax_name)
    log.info("  Knox Tax '%s' -> %s", tax_name, kt)
    if kt and kt.get("street"):
        findings["addresses_found"].append({"name": tax_name, "source": "knox_tax_direct", **kt})

# ── 5. Direct Tracerfy instant lookup ────────────────────────────────
log.info("=" * 72)
log.info("STEP 5: Direct Tracerfy instant lookup (property addr as hint)")
for nm in ["Benjamin Holland", "Benjamin L Holland"]:
    tf = oe._lookup_dm_address_tracerfy(nm, CITY, address=PROP_ADDR, zip_code=PROP_ZIP)
    log.info("  Tracerfy instant '%s' -> %s", nm, tf)
    if tf and tf.get("street"):
        findings["addresses_found"].append({"name": nm, "source": "tracerfy_instant", **tf})

# ── 6. People-search dump (Serper + CBC via Firecrawl) ───────────────
log.info("=" * 72)
log.info("STEP 6: People-search dump (Serper URLs + CyberBackgroundChecks)")
serper_urls = oe._search_serper("Benjamin Holland", CITY)
log.info("  Serper URLs: %s", serper_urls)
cbc_urls = oe._build_people_search_urls("Benjamin Holland", CITY)
log.info("  CBC direct URLs: %s", cbc_urls)
for url in (cbc_urls + serper_urls)[:3]:
    txt = oe._fetch_firecrawl(url, max_text=oe.MAX_ADDRESS_TEXT, priority="high")
    if not txt:
        txt = oe._fetch_page_text(url)
    if txt:
        log.info("  --- %s (%d chars) ---\n%s", url, len(txt), txt[:3500])
        findings.setdefault("people_search", []).append({"url": url, "excerpt": txt[:3500]})

# ── 7. Tracerfy batch skip trace for phones at located address ───────
log.info("=" * 72)
log.info("STEP 7: Tracerfy skip trace for Benjamin at located/property address")
best = findings["addresses_found"][0] if findings["addresses_found"] else {
    "street": PROP_ADDR, "city": CITY, "state": "TN", "zip": PROP_ZIP, "source": "property"}
log.info("  Using address: %s", best)
bn = NoticeData(
    owner_name="Benjamin L. Holland",
    address=best.get("street", PROP_ADDR),
    city=best.get("city") or CITY,
    state="TN",
    zip=best.get("zip") or PROP_ZIP,
    county="Knox",
)
tf_stats = batch_skip_trace([bn], max_signing_traces=1, lookup_heir_addresses=False)
log.info("  Tracerfy: %s", json.dumps(tf_stats))
phones = []
for f in ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
          "mobile_5", "landline_1", "landline_2", "landline_3"]:
    v = getattr(bn, f, "") or ""
    if v.strip():
        phones.append(v.strip())
emails = [getattr(bn, f"email_{i}", "") for i in range(1, 6) if getattr(bn, f"email_{i}", "")]
log.info("  Benjamin phones: %s | emails: %s", phones, emails)

# ── 8. Trestle score any phones ──────────────────────────────────────
log.info("=" * 72)
log.info("STEP 8: Trestle scoring of Benjamin's phones")
scored = []
seen = set()
for raw in phones:
    c = clean_phone(raw)
    if not c or c in seen:
        continue
    seen.add(c)
    d = call_trestle(c, config.TRESTLE_API_KEY, add_litigator=True)
    if "error" in d and not d.get("is_valid"):
        log.warning("  %s: %s", c, d.get("error"))
        continue
    score = d.get("activity_score")
    tier = assign_tier(score, DEFAULT_TIERS)
    addons = d.get("add_ons") or {}
    lit = addons.get("litigator_checks", {}).get("phone.is_litigator_risk") if isinstance(addons, dict) else None
    row = {"phone": c, "activity_score": score, "tier": tier,
           "line_type": d.get("line_type"), "carrier": d.get("carrier"),
           "is_litigator_risk": lit}
    scored.append(row)
    log.info("  %s | score=%s tier=%s | %s %s%s", c, score, tier,
             d.get("line_type") or "?", d.get("carrier") or "",
             " | LITIGATOR-RISK" if lit else "")

findings["phones"] = phones
findings["emails"] = emails
findings["trestle"] = scored
findings["tracerfy_stats"] = tf_stats

out = OUT_DIR / f"benjamin_holland_locate_{TS}.json"
out.write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
log.info("=" * 72)
log.info("DONE. JSON: %s | LOG: %s", out, LOG_PATH)

print("\n@@RESULT@@" + json.dumps({
    "verify_status": findings.get("verify_status"),
    "addresses_found": findings["addresses_found"],
    "phones": phones,
    "emails": emails,
    "trestle": scored,
    "json": str(out),
}))
