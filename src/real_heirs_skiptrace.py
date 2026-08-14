"""Skip trace + Trestle score the REAL heirs of Norman Willis (2106 Brice St).

The first deep-prospect run hallucinated a spouse/children chain. The actual
Unity Mortuary obituary shows Norman Willis was a never-married, childless man
survived only by siblings, nieces, and nephews. Under TN intestacy (no spouse,
no descendants), the estate passes to his siblings - they are the true signing
parties / decision makers.

Real survivors (signing-authority heirs = siblings):
  - Luther Willis Jr.   brother   Knoxville, TN
  - Kenneth Willis      brother   Knoxville, TN
  - Letitia Willis      sister    Knoxville, TN
  - Rosa Judge          sister    Birmingham, AL  (husband Kenneth Judge)
  - Elizabeth A. Holland sister   Bowie, MD       (married name; "Rosher")
Next of kin (inherit only by representation if a sibling predeceased):
  - Taja J. Nix (niece), Taurean Holland (nephew), Darrell Willis Jr. (nephew)

Run from project root:  python src/real_heirs_skiptrace.py
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
LOG_PATH = config.LOG_DIR / f"real_heirs_{TS}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("real_heirs")
OUT_DIR = config.OUTPUT_DIR / "deep_prospect"
OUT_DIR.mkdir(parents=True, exist_ok=True)
API = config.ANTHROPIC_API_KEY

# (name, relationship, city, state) - priority order (local siblings first)
HEIRS = [
    ("Luther Willis Jr.", "brother", "Knoxville", "TN"),
    ("Kenneth Willis",    "brother", "Knoxville", "TN"),
    ("Letitia Willis",    "sister",  "Knoxville", "TN"),
    ("Rosa Judge",        "sister",  "Birmingham", "AL"),
    ("Elizabeth Holland", "sister",  "Bowie",     "MD"),
]

results = []

# ── 1. Address lookup per heir (Knox Tax / people search / Tracerfy) ──
log.info("=" * 72)
log.info("STEP 1: Locate each real heir (address waterfall)")
notices = []
for name, rel, city, st in HEIRS:
    log.info("--- %s (%s, %s %s) ---", name, rel, city, st)
    addr = {"street": "", "city": city, "state": st, "zip": "", "source": ""}
    try:
        # Knox Tax only meaningful for Knox; people-search/tracerfy for all
        found = oe._lookup_dm_address(name, city, API, tracerfy_tier1=True)
        if found.get("street"):
            addr.update(found)
            log.info("  address: %s, %s %s (%s)", addr["street"],
                     addr.get("city"), addr.get("state"), addr.get("source"))
        else:
            log.info("  no confirmed address (will trace by name+city)")
    except Exception as e:
        log.warning("  address lookup error: %s", e)
    n = NoticeData(owner_name=name, address=addr.get("street", ""),
                   city=addr.get("city") or city, state=st,
                   zip=addr.get("zip", ""), county="Knox" if st == "TN" else "")
    notices.append((n, name, rel, addr))

# ── 2. Tracerfy batch skip trace (all heirs in one batch) ────────────
log.info("=" * 72)
log.info("STEP 2: Tracerfy batch skip trace (all real heirs)")
tf_stats = batch_skip_trace([n for n, *_ in notices], max_signing_traces=1,
                            lookup_heir_addresses=False)
log.info("Tracerfy: %s", json.dumps(tf_stats))

# ── 3. Collect phones/emails per heir + Trestle score ────────────────
log.info("=" * 72)
log.info("STEP 3: Trestle scoring per heir")
PHONE_FIELDS = ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
                "mobile_5", "landline_1", "landline_2", "landline_3"]
scored_all = []
for n, name, rel, addr in notices:
    phones, emails = [], []
    for f in PHONE_FIELDS:
        v = getattr(n, f, "") or ""
        if v.strip():
            phones.append(v.strip())
    for i in range(1, 6):
        e = getattr(n, f"email_{i}", "")
        if e:
            emails.append(e)
    heir_scores = []
    seen = set()
    for raw in phones:
        c = clean_phone(raw)
        if not c or c in seen:
            continue
        seen.add(c)
        d = call_trestle(c, config.TRESTLE_API_KEY, add_litigator=True)
        if "error" in d and not d.get("is_valid"):
            continue
        score = d.get("activity_score")
        tier = assign_tier(score, DEFAULT_TIERS)
        ad = d.get("add_ons") or {}
        lit = ad.get("litigator_checks", {}).get("phone.is_litigator_risk") if isinstance(ad, dict) else None
        row = {"phone": c, "activity_score": score, "tier": tier,
               "line_type": d.get("line_type"), "carrier": d.get("carrier"),
               "is_litigator_risk": lit}
        heir_scores.append(row)
        scored_all.append({"heir": name, **row})
    rec = {"name": name, "relationship": rel,
           "address": addr.get("street", ""), "city": addr.get("city") or addr.get("city"),
           "state": addr.get("state"), "address_source": addr.get("source", ""),
           "phones": phones, "emails": emails, "trestle": heir_scores}
    results.append(rec)
    log.info("  %s (%s): addr=%s | %d phone(s), %d email(s)",
             name, rel, rec["address"] or "(none)", len(phones), len(emails))
    for s in heir_scores:
        log.info("      %s | score=%s tier=%s | %s %s%s", s["phone"], s["activity_score"],
                 s["tier"], s["line_type"] or "?", s["carrier"] or "",
                 " | LITIGATOR-RISK" if s["is_litigator_risk"] else "")

out = OUT_DIR / f"norman_willis_real_heirs_{TS}.json"
out.write_text(json.dumps({"heirs": results, "tracerfy_stats": tf_stats},
                          indent=2, ensure_ascii=False), encoding="utf-8")
log.info("=" * 72)
log.info("DONE. JSON: %s | LOG: %s", out, LOG_PATH)
print("\n@@RESULT@@" + json.dumps({"heirs": [
    {"name": r["name"], "rel": r["relationship"], "address": r["address"],
     "source": r["address_source"], "phones": r["phones"], "emails": r["emails"],
     "trestle": r["trestle"]} for r in results],
    "tracerfy": tf_stats, "json": str(out)}))
