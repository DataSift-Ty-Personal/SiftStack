"""Deep-prospecting skip-trace + scoring for a single return-mailer lead.

Return mailers (USPS NIXIE: not deliverable / attempted-not-known / unable to
forward) signal a bad mailing address. We skip trace by name + last-known
address to find current phones, then score them.

Per the request this SKIPS the L1-L3 research front-half AND the
TruePeopleSearch / FastPeopleSearch / CyberBackgroundChecks browser waterfall.
It runs only the production automated path:

    Step 1: Tracerfy batch skip trace (living owner)
    Step 2: Trestle phone scoring (activity score + dial tier + litigator risk)

Usage (from project root):
    python src/run_return_mailer_skiptrace.py \
        --name "Randall Robinson" \
        --address "9128 Woodpark Ln" --city Knoxville --state TN --zip 37923 \
        --source "USPS return mailer NIXIE 372 CE — attempted, not known"
"""

import argparse
import json
import logging
import re
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Return-mailer skip trace + scoring")
    ap.add_argument("--name", required=True, help="Owner full name")
    ap.add_argument("--address", required=True, help="Last-known street address")
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", default="TN")
    ap.add_argument("--zip", required=True, help="5-digit ZIP")
    ap.add_argument("--county", default="Knox")
    ap.add_argument("--source", default="USPS return mailer (bad address)")
    args = ap.parse_args()

    slug = re.sub(r"[^a-z0-9]+", "_", args.name.lower()).strip("_") or "lead"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / f"deep_prospect_{slug}_{ts}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    log = logging.getLogger("deep_prospect")

    out_dir = config.OUTPUT_DIR / "deep_prospect"
    out_dir.mkdir(parents=True, exist_ok=True)

    notice = NoticeData(
        date_added=datetime.now().strftime("%Y-%m-%d"),
        address=args.address,
        city=args.city,
        state=args.state,
        zip=args.zip,
        owner_name=args.name,
        notice_type="return_mailer",
        county=args.county,
        source_url=args.source,
    )

    log.info("=" * 70)
    log.info("DEEP PROSPECTING (skip-trace + score only): %s, %s %s %s",
             notice.address, notice.city, notice.state, notice.zip)
    log.info("Owner (living): %s | Source: %s", notice.owner_name, args.source)
    log.info("=" * 70)

    # ── Step 1: Tracerfy batch skip trace ─────────────────────────────
    log.info("-" * 70)
    log.info("STEP 1: Tracerfy skip trace (living owner, by name + last-known address)")
    tracerfy_stats = batch_skip_trace(
        [notice],
        max_signing_traces=1,
        lookup_heir_addresses=False,
    )
    log.info("Tracerfy: %s", json.dumps(tracerfy_stats))

    # ── Step 2: Trestle scoring ───────────────────────────────────────
    log.info("-" * 70)
    log.info("STEP 2: Trestle phone scoring (activity score + dial tier + litigator)")

    raw_phones: list[str] = []
    for f in DM_PHONE_FIELDS:
        v = getattr(notice, f, "") or ""
        if v.strip():
            raw_phones.append(v.strip())

    emails = [getattr(notice, f"email_{i}", "") for i in range(1, 6)
              if getattr(notice, f"email_{i}", "")]

    seen: dict[str, str] = {}
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

    scored_rows.sort(key=lambda r: (-1 if r.get("activity_score") is None
                                    else -r["activity_score"]))

    summary = {
        "subject": {
            "name": notice.owner_name,
            "last_known_address": f"{notice.address}, {notice.city}, {notice.state} {notice.zip}",
            "source": args.source,
            "owner_status": "living",
        },
        "tracerfy_stats": tracerfy_stats,
        "phones": scored_rows,
        "emails": emails,
    }
    json_path = out_dir / f"{slug}_skiptrace_{ts}.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("=" * 70)
    log.info("DONE.  JSON: %s  LOG: %s", json_path, log_path)
    log.info("=" * 70)

    print("\n@@RESULT@@" + json.dumps(summary))


if __name__ == "__main__":
    main()
