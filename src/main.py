"""Entry point for SiftStack — full-stack REI operations platform.

Runs as either:
  - Apify Actor (when APIFY_IS_AT_HOME is set — reads input from Actor.get_input())
  - Standalone CLI (e.g. ``python src/main.py daily --counties Knox,Jefferson``)

Supports both target markets:
  - TN: Knox, Blount counties (Knoxville/Maryville metro)
  - KY: Jefferson county (Louisville metro)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import config
from config import (
    LOG_DIR,
    NOTICE_TYPES,
    OUTPUT_DIR,
    SAVED_SEARCHES,
    SavedSearch,
)
from data_formatter import deduplicate, write_csv, write_csv_by_type
from scraper import scrape_all

logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────


def _filter_searches(
    counties: list[str] | None,
    types: list[str] | None,
) -> list[SavedSearch]:
    """Filter SAVED_SEARCHES by county and/or notice type."""
    searches = list(SAVED_SEARCHES)

    if counties:
        county_set = {c.lower() for c in counties}
        searches = [s for s in searches if s.county.lower() in county_set]

    if types:
        type_set = {t.lower() for t in types}
        searches = [s for s in searches if s.notice_type.lower() in type_set]

    return searches


# ── Preflight health checks ─────────────────────────────────────────


def _preflight_check(mode: str, active_searches: list | None = None) -> list[str]:
    """Verify required API keys and service connectivity before running.

    Returns a list of failure descriptions. Empty list = all checks passed.
    active_searches: the filtered search list for this run. If None, all
                     SAVED_SEARCHES are used (conservative — may over-require creds).
    """
    failures: list[str] = []

    # ── Credential checks (mode-dependent) ──────────────────────────
    scrape_modes = {"daily", "historical"}
    enrichment_modes = scrape_modes | {"pdf-import", "photo-import", "dropbox-watch", "csv-import"}
    datasift_modes = {"manage-presets", "manage-sold", "phone-validate"}

    if mode in scrape_modes:
        # Only TNPN searches require TNPN creds + 2Captcha.
        # JCD (Jefferson County Deeds) uses plain HTTP; KCOJ (Kentucky Court of
        # Justice dockets) uses Playwright but needs no login/CAPTCHA.
        searches_to_check = active_searches if active_searches is not None else list(config.SAVED_SEARCHES)
        has_tnpn = any(getattr(s, "source", "tnpn") == "tnpn" for s in searches_to_check)
        if has_tnpn:
            if not config.TNPN_EMAIL or not config.TNPN_PASSWORD:
                failures.append("TNPN_EMAIL / TNPN_PASSWORD not set (required for scraping)")
            if not config.CAPTCHA_API_KEY:
                failures.append("CAPTCHA_API_KEY not set (CAPTCHA solving will fail)")

    if mode in enrichment_modes:
        # These are warnings, not blockers — pipeline degrades gracefully
        if not config.SMARTY_AUTH_ID or not config.SMARTY_AUTH_TOKEN:
            logger.warning("Preflight: SMARTY credentials missing — address standardization will be skipped")
        if not config.OPENWEBNINJA_API_KEY:
            logger.warning("Preflight: OPENWEBNINJA_API_KEY missing — Zillow enrichment will be skipped")
        if not config.ANTHROPIC_API_KEY:
            logger.warning("Preflight: ANTHROPIC_API_KEY missing — obituary search and LLM parsing will be skipped")

    if mode in datasift_modes:
        if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
            failures.append("DATASIFT_EMAIL / DATASIFT_PASSWORD not set (required for DataSift operations)")

    if mode == "dropbox-watch":
        if not config.DROPBOX_APP_KEY or not config.DROPBOX_APP_SECRET or not config.DROPBOX_REFRESH_TOKEN:
            failures.append("DROPBOX credentials incomplete (need APP_KEY, APP_SECRET, REFRESH_TOKEN)")

    if mode == "phone-validate":
        if not config.TRESTLE_API_KEY:
            failures.append("TRESTLE_API_KEY not set (required for phone validation)")

    # ── Connectivity checks (only for TNPN scrape modes) ────────────
    # Reuse `has_tnpn` from the credential block above. Only run if that block
    # executed (i.e., mode is a scrape mode); otherwise fall back to False.
    has_tnpn = mode in scrape_modes and any(
        getattr(s, "source", "tnpn") == "tnpn"
        for s in (active_searches if active_searches is not None else list(config.SAVED_SEARCHES))
    )
    if mode in scrape_modes and has_tnpn:
        import requests as _requests
        try:
            resp = _requests.head(config.BASE_URL, timeout=10, allow_redirects=True)
            if resp.status_code >= 500:
                failures.append(f"tnpublicnotice.com returned {resp.status_code} — site may be down")
        except Exception as e:
            failures.append(f"Cannot reach tnpublicnotice.com: {e}")

    # ── 2Captcha balance check ──────────────────────────────────────
    if mode in scrape_modes and has_tnpn and config.CAPTCHA_API_KEY:
        import requests as _requests
        try:
            resp = _requests.get(
                f"https://2captcha.com/res.php?key={config.CAPTCHA_API_KEY}&action=getbalance",
                timeout=10,
            )
            balance_text = resp.text.strip()
            try:
                balance = float(balance_text)
                if balance < 0.50:
                    failures.append(f"2Captcha balance too low: ${balance:.2f} (need at least $0.50)")
                else:
                    logger.info("Preflight: 2Captcha balance: $%.2f", balance)
            except ValueError:
                if "ERROR" in balance_text:
                    failures.append(f"2Captcha API key invalid: {balance_text}")
        except Exception as e:
            logger.warning("Preflight: Could not check 2Captcha balance: %s", e)

    return failures


# ── Apify Actor mode ─────────────────────────────────────────────────


async def actor_main() -> None:
    """Run as an Apify Actor — full automated pipeline.

    Scrape → Enrich → Tracerfy → DataSift Upload → Slack Notification.
    """
    from apify import Actor
    from time import time as _time

    # Set up Python logging so all modules output at INFO level
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async with Actor:
        pipeline_start = _time()
        actor_input = await Actor.get_input() or {}

        # Override config credentials from Actor input.
        # Set both config.* AND os.environ so downstream modules that read
        # from either source (e.g., datasift_uploader uses os.environ) pick them up.
        _cred_map = {
            "TNPN_EMAIL": actor_input.get("tn_username", ""),
            "TNPN_PASSWORD": actor_input.get("tn_password", ""),
            "CAPTCHA_API_KEY": actor_input.get("captcha_api_key", ""),
            "ANTHROPIC_API_KEY": actor_input.get("anthropic_api_key", ""),
            "SMARTY_AUTH_ID": actor_input.get("smarty_auth_id", ""),
            "SMARTY_AUTH_TOKEN": actor_input.get("smarty_auth_token", ""),
            "OPENWEBNINJA_API_KEY": actor_input.get("openwebninja_api_key", ""),
            "SERPER_API_KEY": actor_input.get("serper_api_key", ""),
            "FIRECRAWL_API_KEY": actor_input.get("firecrawl_api_key", ""),
            "TRACERFY_API_KEY": actor_input.get("tracerfy_api_key", ""),
            "DATASIFT_EMAIL": actor_input.get("datasift_email", ""),
            "DATASIFT_PASSWORD": actor_input.get("datasift_password", ""),
            "SLACK_WEBHOOK_URL": actor_input.get("slack_webhook_url", ""),
            "TRESTLE_API_KEY": actor_input.get("trestle_api_key", ""),
        }
        for key, val in _cred_map.items():
            setattr(config, key, val)
            if val:
                os.environ[key] = val

        mode = actor_input.get("mode", "daily")
        counties = actor_input.get("counties") or None
        types = actor_input.get("types") or None
        since_date_override = actor_input.get("since_date", "").strip()
        start_page = int(actor_input.get("start_page", 1) or 1)
        drive_folder_id = actor_input.get("google_drive_folder_id", "")
        drive_key_b64 = actor_input.get("google_service_account_key", "")

        # Pipeline toggles
        do_tracerfy = actor_input.get("run_tracerfy", True)
        do_notify_slack = actor_input.get("notify_slack", True)

        # Buy box / filter toggles
        include_vacant = actor_input.get("include_vacant", False)
        include_commercial = actor_input.get("include_commercial", False)
        include_entities = actor_input.get("include_entities", False)

        # Validate
        if not config.TNPN_EMAIL or not config.TNPN_PASSWORD:
            Actor.log.error("tn_username and tn_password are required")
            try:
                from slack_notifier import notify_preflight_failure
                notify_preflight_failure(["TNPN credentials missing"])
            except Exception:
                pass
            await Actor.fail(status_message="Missing SiftStack credentials")
            return
        if not config.CAPTCHA_API_KEY:
            Actor.log.warning("captcha_api_key not set — CAPTCHA solving will fail")

        # Filter searches
        searches = _filter_searches(counties, types)
        if not searches:
            Actor.log.error("No saved searches match the given counties/types filters")
            await Actor.fail(status_message="No matching saved searches")
            return

        Actor.log.info(
            "Running %d saved searches: %s",
            len(searches),
            ", ".join(s.saved_search_name for s in searches),
        )

        # Set up residential proxy if requested
        proxy_url: str | None = None
        use_proxy = actor_input.get("use_residential_proxy", True)
        if use_proxy:
            try:
                proxy_config = await Actor.create_proxy_configuration(
                    groups=["RESIDENTIAL"]
                )
                proxy_url = await proxy_config.new_url()
                Actor.log.info("Residential proxy configured")
            except Exception:
                Actor.log.warning("Could not configure residential proxy — running without proxy")

        # Track seen notice IDs for incremental dedup
        seen_ids: set[str] = set()

        def _notice_id(url: str) -> str:
            import re
            m = re.search(r"[?&]ID=(\d+)", url)
            return m.group(1) if m else ""

        async def push_batch(batch_notices):
            """Push new unique notices to dataset immediately after each search."""
            unique = []
            for n in batch_notices:
                nid = _notice_id(n.source_url)
                if nid and nid in seen_ids:
                    continue
                if nid:
                    seen_ids.add(nid)
                unique.append(n)
            if unique:
                await Actor.push_data([
                    {
                        "date_added": n.date_added,
                        "address": n.address,
                        "city": n.city,
                        "state": n.state,
                        "zip": n.zip,
                        "owner_name": n.owner_name,
                        "notice_type": n.notice_type,
                        "county": n.county,
                        "decedent_name": n.decedent_name,
                        "owner_street": n.owner_street,
                        "owner_city": n.owner_city,
                        "owner_state": n.owner_state,
                        "owner_zip": n.owner_zip,
                        "auction_date": n.auction_date,
                        "zip_plus4": n.zip_plus4,
                        "latitude": n.latitude,
                        "longitude": n.longitude,
                        "dpv_match_code": n.dpv_match_code,
                        "vacant": n.vacant,
                        "rdi": n.rdi,
                        "mls_status": n.mls_status,
                        "mls_listing_price": n.mls_listing_price,
                        "mls_last_sold_date": n.mls_last_sold_date,
                        "mls_last_sold_price": n.mls_last_sold_price,
                        "estimated_value": n.estimated_value,
                        "estimated_equity": n.estimated_equity,
                        "equity_percent": n.equity_percent,
                        "property_type": n.property_type,
                        "bedrooms": n.bedrooms,
                        "bathrooms": n.bathrooms,
                        "sqft": n.sqft,
                        "year_built": n.year_built,
                        "lot_size": n.lot_size,
                        "source_url": n.source_url,
                        "raw_text": n.raw_text[:5000] if n.raw_text else "",
                    }
                    for n in unique
                ])
                Actor.log.info("Pushed %d records to dataset (incremental)", len(unique))

        # Log LLM parser status
        if config.ANTHROPIC_API_KEY:
            Actor.log.info("LLM fallback enabled (Claude Haiku) for missing fields")
        else:
            Actor.log.info("LLM fallback disabled — set anthropic_api_key to enable")

        if start_page > 1:
            Actor.log.info("Starting from page %d (skipping earlier pages)", start_page)

        try:
            kvs = await Actor.open_key_value_store()

            # ── Load last_run_date from Apify KVS (persists between runs) ──
            if mode == "daily" and not since_date_override:
                stored = await kvs.get_value("last_run_date")
                if stored:
                    since_date_override = stored
                    Actor.log.info("Daily mode: using stored last_run_date = %s", stored)
                else:
                    Actor.log.info("Daily mode: no stored last_run_date, defaulting to 7 days")

            # ── Load cross-run seen-ID cache from KVS (makes daily re-runs idempotent) ──
            seen_ids = await kvs.get_value("seen_notice_ids") or {}
            Actor.log.info("Loaded %d previously-seen notice IDs from KVS", len(seen_ids))

            # ── Load KCOJ seen-case cache from KVS (independent from TNPN seen_ids) ──
            # KCOJ dockets recur probate cases across many days; without this,
            # the daily scheduled Apify run would resend every still-open case
            # to DataSift every morning.
            kcoj_seen_cases = await kvs.get_value("kcoj_seen_cases") or {}
            Actor.log.info("Loaded %d previously-seen KCOJ case numbers from KVS", len(kcoj_seen_cases))

            async def persist_seen_ids(ids: dict) -> None:
                """Mid-run persistence — if a later search crashes, progress is kept."""
                try:
                    await kvs.set_value("seen_notice_ids", ids)
                    await kvs.set_value(
                        "last_run_date",
                        datetime.now().strftime("%Y-%m-%d"),
                    )
                except Exception as e:
                    Actor.log.warning("Failed to persist seen_notice_ids to KVS: %s", e)

            async def persist_kcoj_seen_cases(cases: dict) -> None:
                try:
                    await kvs.set_value("kcoj_seen_cases", cases)
                except Exception as e:
                    Actor.log.warning("Failed to persist kcoj_seen_cases to KVS: %s", e)

            # ── Scrape ────────────────────────────────────────────────
            notices = await scrape_all(
                mode=mode, searches=searches, proxy_url=proxy_url, on_batch=push_batch,
                since_date_override=since_date_override or None,
                llm_api_key=config.ANTHROPIC_API_KEY or None,
                start_page=start_page,
                seen_ids=seen_ids,
                kcoj_seen_cases=kcoj_seen_cases,
                on_search_complete=persist_seen_ids,
                on_kcoj_search_complete=persist_kcoj_seen_cases,
            )
            # Handle async probate lookup before pipeline (requires await)
            probate_notices = [n for n in notices if n.notice_type == "probate" and n.decedent_name and not n.address]
            if probate_notices:
                try:
                    from property_lookup import lookup_decedent_properties
                    Actor.log.info("Looking up property addresses for %d probate notices...", len(probate_notices))
                    await lookup_decedent_properties(probate_notices)
                except ImportError:
                    Actor.log.warning("property_lookup module not found -- skipping property lookup")
                except Exception as e:
                    Actor.log.warning("Property lookup failed: %s -- continuing without lookups", e)

            # ── Enrichment ────────────────────────────────────────────
            from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

            opts = PipelineOptions(
                skip_parcel_lookup=True,  # web scrape notices don't have parcel IDs
                skip_vacant_filter=include_vacant,
                skip_commercial_filter=include_commercial,
                skip_entity_filter=include_entities,
                source_label="Apify Actor",
            )
            notices = run_enrichment_pipeline(notices, opts)

            if not notices:
                Actor.log.warning("No notices found")
                return

            total = len(notices)

            # ── Tracerfy Skip Trace (DP candidates only) ────────────
            # Only run Tracerfy on records that need deep prospecting
            # (deceased owners, heir maps, decision makers). Basic records
            # get skip traced for free inside DataSift's unlimited plan.
            tracerfy_stats = None
            if do_tracerfy and config.TRACERFY_API_KEY:
                # Fit gate (Phase 4): paid Tracerfy runs only on records at/above
                # SKIP_TRACE_MIN_FIT, keeping the deceased/DM condition as a
                # SECONDARY requirement. Parse wholesale_fit_score defensively so an
                # unscored/blank record fails CLOSED (scores 0 → excluded), never
                # crashing the gate.
                dp_for_tracerfy = [
                    n for n in notices
                    if (int(n.wholesale_fit_score or 0) >= config.SKIP_TRACE_MIN_FIT)
                    and (n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name)
                ]
                if dp_for_tracerfy:
                    Actor.log.info("Running Tracerfy on %d fit DP candidates (>= SKIP_TRACE_MIN_FIT %d; %d records skipped)...",
                                   len(dp_for_tracerfy), config.SKIP_TRACE_MIN_FIT, total - len(dp_for_tracerfy))
                    try:
                        from tracerfy_skip_tracer import batch_skip_trace
                        tracerfy_stats = batch_skip_trace(dp_for_tracerfy)
                        Actor.log.info(
                            "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                            tracerfy_stats["matched"], tracerfy_stats["submitted"],
                            tracerfy_stats["phones_found"], tracerfy_stats["emails_found"],
                            tracerfy_stats["cost"],
                        )
                    except Exception as e:
                        Actor.log.warning("Tracerfy skip trace failed: %s — continuing", e)
                else:
                    Actor.log.info("No DP candidates — Tracerfy skipped (0 deceased/DM records)")
            elif do_tracerfy:
                Actor.log.info("Tracerfy skipped — no API key configured")

            # ── Generate Deep Prospecting PDFs ────────────────────────
            # Only generate PDFs for records that have deep prospecting data:
            # deceased owners with heir/DM info, or records with signing chains.
            # Basic records (just address + owner) don't need a PDF.
            pdf_urls = []
            dp_candidates = [
                n for n in notices
                if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
            ]

            # Score every phone (DM #1 + all heirs) with Trestle before rendering,
            # so signing-chain phones get tier badges — not just DM #1's.
            phone_tiers: dict = {}
            if dp_candidates and config.TRESTLE_API_KEY:
                try:
                    from phone_validator import score_record_phones
                    phone_tiers = score_record_phones(dp_candidates, config.TRESTLE_API_KEY)
                    Actor.log.info("Trestle scored %d unique phones across DP candidates",
                                   len(phone_tiers))
                except Exception as e:
                    Actor.log.warning("Per-record Trestle scoring failed: %s — continuing", e)

            if dp_candidates:
                try:
                    from report_generator import generate_record_pdf
                    kvs = await Actor.open_key_value_store()
                    kvs_id = kvs._id if hasattr(kvs, '_id') else ''
                    report_dir = Path("output/reports")

                    for n in dp_candidates:
                        pdf_path = generate_record_pdf(
                            n, output_dir=report_dir, phone_tiers=phone_tiers,
                        )
                        key = pdf_path.name
                        with open(pdf_path, "rb") as f:
                            await kvs.set_value(key, f.read(), content_type="application/pdf")
                        url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                        pdf_urls.append({"address": n.address, "url": url})

                    Actor.log.info("Generated %d deep prospecting PDFs (%d records skipped — no DP data)",
                                   len(pdf_urls), total - len(dp_candidates))
                except Exception as e:
                    Actor.log.warning("PDF generation failed: %s — continuing", e)
            else:
                Actor.log.info("No records need deep prospecting PDFs")

            # ── Write CSV ─────────────────────────────────────────────
            csv_path = write_csv(notices)
            if not kvs:
                kvs = await Actor.open_key_value_store()
            with open(csv_path, "rb") as f:
                await kvs.set_value("output.csv", f.read(), content_type="text/csv")
            Actor.log.info("CSV saved to key-value store as 'output.csv'")

            # ── Google Drive Upload ───────────────────────────────────
            if drive_folder_id and drive_key_b64:
                Actor.log.info("Uploading to Google Drive...")
                from drive_uploader import upload_csv, upload_summary

                by_type: dict[str, int] = {}
                by_county: dict[str, int] = {}
                for n in notices:
                    by_type[n.notice_type] = by_type.get(n.notice_type, 0) + 1
                    by_county[n.county] = by_county.get(n.county, 0) + 1

                file_id = upload_csv(csv_path, drive_folder_id, drive_key_b64, total)
                if file_id:
                    Actor.log.info("CSV uploaded to Drive (file ID: %s)", file_id)
                else:
                    Actor.log.error("CSV upload to Drive failed — CSV still in key-value store")

                upload_summary(by_type, by_county, total, drive_folder_id, drive_key_b64)
            elif drive_folder_id:
                Actor.log.warning("google_drive_folder_id set but google_service_account_key missing — skipping Drive upload")

            # ── DataSift CSVs → KVS (manual upload) ─────────────────
            # Generate DataSift-formatted CSVs and save to Apify KVS
            # for manual download + upload to DataSift (more reliable than
            # automated Playwright upload in headless cloud containers).
            datasift_csv_urls = []
            try:
                from datasift_formatter import write_datasift_split_csvs

                csv_infos = write_datasift_split_csvs(notices)
                kvs = await Actor.open_key_value_store()
                for info in csv_infos:
                    key = f"datasift_{info['label'].lower().replace(' ', '_')}.csv"
                    with open(info["path"], "rb") as f:
                        await kvs.set_value(key, f.read(), content_type="text/csv")
                    # Build public download URL
                    kvs_id = kvs._id if hasattr(kvs, '_id') else ''
                    url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                    datasift_csv_urls.append({"label": info["label"], "url": url, "records": info.get("count", "?")})
                    Actor.log.info("DataSift CSV (%s) saved to KVS: %s", info["label"], key)
            except Exception as e:
                Actor.log.error("DataSift CSV generation failed: %s", e)

            # ── Slack Notification ────────────────────────────────────
            elapsed_min = (_time() - pipeline_start) / 60

            # Compute estimated run cost
            cost_breakdown = {}
            # 2Captcha: $0.003 per solve, ~1 solve per notice scraped
            captcha_count = total  # each notice detail page requires a CAPTCHA
            cost_breakdown["2Captcha"] = round(captcha_count * 0.003, 2)
            # Anthropic Haiku: ~$0.001 per record (LLM parsing + obituary search)
            if config.ANTHROPIC_API_KEY:
                cost_breakdown["Anthropic (Haiku)"] = round(total * 0.001, 3)
            # Tracerfy: actual cost from batch stats
            if tracerfy_stats and tracerfy_stats.get("cost", 0) > 0:
                cost_breakdown["Tracerfy"] = round(tracerfy_stats["cost"], 2)
            # Smarty: free tier 250/month, $0.01 after
            smarty_count = sum(1 for n in notices if n.dpv_match_code)
            if smarty_count > 0:
                cost_breakdown["Smarty"] = round(max(0, smarty_count - 250) * 0.01, 2) if smarty_count > 250 else 0.0
            # Zillow (OpenWeb Ninja): free tier 100/month, $0.01 after
            zillow_count = sum(1 for n in notices if n.estimated_value)
            if zillow_count > 0:
                cost_breakdown["Zillow"] = round(max(0, zillow_count - 100) * 0.01, 2) if zillow_count > 100 else 0.0
            # Remove zero-cost entries for cleaner display
            cost_breakdown = {k: v for k, v in cost_breakdown.items() if v > 0}

            if do_notify_slack and config.SLACK_WEBHOOK_URL:
                try:
                    from slack_notifier import send_slack_notification, _send_webhook

                    # Send standard run summary with cost breakdown
                    send_slack_notification(
                        notices,
                        elapsed_min=elapsed_min,
                        cost_breakdown=cost_breakdown,
                    )

                    # Send DataSift CSV download links as a follow-up message
                    if datasift_csv_urls:
                        csv_lines = [
                            "*DataSift CSVs ready for manual upload:*",
                        ]
                        for csv_info in datasift_csv_urls:
                            csv_lines.append(f"  <{csv_info['url']}|{csv_info['label']}> ({csv_info['records']} records)")
                        csv_lines.append("_Upload at app.reisift.io → Upload File → Add Data_")
                        _send_webhook("\n".join(csv_lines))

                    # Send PDF download links
                    if pdf_urls:
                        pdf_lines = [
                            f"*Deep Prospecting PDFs ({len(pdf_urls)} records):*",
                        ]
                        for pdf_info in pdf_urls:
                            pdf_lines.append(f"  <{pdf_info['url']}|{pdf_info['address']}>")
                        pdf_lines.append("_Attach to DataSift record → Notes or Files_")
                        _send_webhook("\n".join(pdf_lines))

                    Actor.log.info("Slack notification sent")
                except Exception as e:
                    Actor.log.warning("Slack notification failed: %s", e)

            # ── Save last_run_date + seen_notice_ids + kcoj_seen_cases to KVS ─────
            await kvs.set_value("last_run_date", datetime.now().strftime("%Y-%m-%d"))
            await kvs.set_value("seen_notice_ids", seen_ids)
            await kvs.set_value("kcoj_seen_cases", kcoj_seen_cases)
            Actor.log.info(
                "Saved last_run_date + %d seen_notice_ids + %d kcoj_seen_cases to KVS for next run",
                len(seen_ids), len(kcoj_seen_cases),
            )

            Actor.log.info("Done — %d notices exported (%.1f min)", total, elapsed_min)

        except Exception as e:
            Actor.log.error("Pipeline failed: %s", e, exc_info=True)
            try:
                from slack_notifier import notify_error
                notify_error("Apify Actor Pipeline", e, context=f"mode={mode}")
            except Exception:
                pass
            await Actor.fail(status_message=f"Pipeline error: {e}")


# ── CLI mode ──────────────────────────────────────────────────────────


def setup_logging(verbose: bool = False) -> None:
    """Configure logging to both console and date-stamped log file."""
    level = logging.DEBUG if verbose else logging.INFO
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"scrape_{timestamp}.log"

    # Force UTF-8 on console output to avoid cp1252 encoding errors on Windows
    console = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    handlers: list[logging.Handler] = [
        console,
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.info("Logging to %s", log_file)


def _run_pdf_import(args) -> None:
    """Run the PDF import pipeline: OCR → parse → enrich → CSV."""
    from pdf_importer import process_pdf
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.pdf_path:
        logging.error("--pdf-path is required for pdf-import mode")
        sys.exit(1)
    if not args.pdf_county:
        logging.error("--pdf-county is required for pdf-import mode")
        sys.exit(1)

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logging.error("PDF file not found: %s", pdf_path)
        sys.exit(1)

    county = args.pdf_county.strip().title()  # "knox" → "Knox"

    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_pdf(
        pdf_path=pdf_path,
        county=county,
        api_key=api_key,
        date_added=args.pdf_date,
        regex_only=args.regex_only,
    )

    if not notices:
        logging.warning("No records extracted from PDF")
        sys.exit(0)

    # Run unified enrichment pipeline
    opts = PipelineOptions(
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"PDF import ({pdf_path.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_tax_sale_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_photo_import(args) -> None:
    """Run the photo import pipeline: preprocess → OCR → parse → enrich → CSV."""
    from photo_importer import process_photos
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.folder:
        logging.error("--folder is required for photo-import mode")
        sys.exit(1)
    if not args.photo_county:
        logging.error("--photo-county is required for photo-import mode")
        sys.exit(1)
    if not args.photo_type:
        logging.error("--photo-type is required for photo-import mode")
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.exists() or not folder.is_dir():
        logging.error("Folder not found: %s", folder)
        sys.exit(1)

    county = args.photo_county.strip().title()

    notice_type = args.photo_type.strip().lower()
    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_photos(
        folder=folder,
        county=county,
        notice_type=notice_type,
        date_added=args.photo_date,
        api_key=api_key,
        correct_perspective=not getattr(args, "no_perspective_correct", False),
    )

    if not notices:
        logging.warning("No records extracted from photos")
        sys.exit(0)

    # Run unified enrichment pipeline
    # Skip vacant land filter for notice types without property addresses
    # (probate from court terminals never has property address — would filter everything)
    no_address_types = {"probate", "divorce"}
    opts = PipelineOptions(
        skip_vacant_filter=getattr(args, "include_vacant", False) or notice_type in no_address_types,
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"Photo import ({folder.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_{notice_type}_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_csv_import(args) -> None:
    """Run the CSV re-import pipeline: read CSV → enrich → write new CSV.

    Supports multiple CSV paths (comma-separated) for merging datasets.
    Supports --upload-datasift to format and upload to DataSift after enrichment.
    """
    from data_formatter import read_csv
    from enrichment_pipeline import (
        PipelineOptions,
        detect_existing_enrichment,
        run_enrichment_pipeline,
    )

    # Validate required args
    if not args.csv_path:
        logging.error("--csv-path is required for csv-import mode")
        sys.exit(1)

    # Support multiple CSV paths (comma-separated)
    csv_paths = [Path(p.strip()) for p in args.csv_path.split(",")]
    for cp in csv_paths:
        if not cp.exists():
            logging.error("CSV file not found: %s", cp)
            sys.exit(1)

    county = None
    if args.csv_county:
        county = args.csv_county.strip().title()

    # Read all CSVs → NoticeData, merge
    all_notices = []
    for cp in csv_paths:
        batch = read_csv(cp)
        logging.info("Loaded %d records from %s", len(batch), cp.name)
        all_notices.extend(batch)

    if not all_notices:
        logging.warning("No records found in CSV(s)")
        sys.exit(0)

    # Deduplicate by source_url (notice ID) — keeps most recent
    seen_urls = {}
    for n in all_notices:
        url = getattr(n, "source_url", "") or ""
        if url and url in seen_urls:
            # Keep the one with more enrichment data
            existing = seen_urls[url]
            if (getattr(n, "estimated_value", "") or "") and not (getattr(existing, "estimated_value", "") or ""):
                seen_urls[url] = n
        elif url:
            seen_urls[url] = n
        else:
            # No source_url — keep all (dedup by address later)
            seen_urls[id(n)] = n
    notices = list(seen_urls.values())
    if len(notices) < len(all_notices):
        logging.info("Deduped %d → %d records (by source_url)", len(all_notices), len(notices))

    # Override county if provided (for CSVs without county column)
    if county:
        for n in notices:
            if not n.county.strip():
                n.county = county

    logging.info("Total: %d records from %d CSV(s)", len(notices), len(csv_paths))

    # Build pipeline options
    primary_name = csv_paths[0].name
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"CSV import ({primary_name})",
    )
    detect_existing_enrichment(notices, opts)
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{csv_paths[0].stem}_reimport_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    # DataSift upload (same logic as daily/historical mode)
    if getattr(args, "upload_datasift", False):
        from datasift_formatter import write_datasift_split_csvs
        from datasift_uploader import upload_datasift_split, upload_to_datasift

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        csv_infos = write_datasift_split_csvs(notices)
        for info in csv_infos:
            logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

        if len(csv_infos) > 1:
            upload_result = asyncio.run(
                upload_datasift_split(
                    csv_infos,
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )
        else:
            upload_result = asyncio.run(
                upload_to_datasift(
                    csv_infos[0]["path"],
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

    logging.info("Done — %d records exported", len(notices))


def _run_phone_validate(args) -> None:
    """Run phone validation via Trestle API with DataSift export/upload."""
    import json as _json

    csv_path = getattr(args, "csv_path", None)
    list_name = getattr(args, "list_name", None)
    preset_folder = getattr(args, "preset_folder", None)
    all_records = getattr(args, "all_records", False)

    # Must specify at least one targeting mode
    if not csv_path and not list_name and not preset_folder and not all_records:
        logging.error(
            "phone-validate requires one of: --csv-path, --list-name, --preset-folder, or --all-records"
        )
        sys.exit(1)

    # Parse custom tiers if provided
    tiers = None
    custom_tiers_str = getattr(args, "custom_tiers", None)
    if custom_tiers_str:
        try:
            raw = _json.loads(custom_tiers_str)
            tiers = {k: tuple(v) for k, v in raw.items()}
            logging.info("Using custom tiers: %s", tiers)
        except (_json.JSONDecodeError, ValueError) as e:
            logging.error("Invalid --custom-tiers JSON: %s", e)
            sys.exit(1)

    # Estimate-only mode
    if getattr(args, "estimate", False):
        from phone_validator import estimate_cost, print_estimate

        if csv_path:
            est = estimate_cost(csv_path)
            print_estimate(est)
        else:
            logging.error("--estimate requires --csv-path (export from DataSift first, then estimate)")
            sys.exit(1)
        return

    # Full validation workflow
    from datasift_uploader import run_phone_validation_workflow

    result = asyncio.run(run_phone_validation_workflow(
        list_name=list_name,
        preset_folder=preset_folder,
        all_records=all_records,
        csv_path=csv_path,
        upload_tags=not getattr(args, "no_upload", False),
        api_key=config.TRESTLE_API_KEY or None,
        tiers=tiers,
        add_litigator=getattr(args, "add_litigator", False),
        batch_size=getattr(args, "batch_size", 10),
    ))

    if result.get("success"):
        logging.info("Phone validation: %s", result.get("message", "OK"))
        if result.get("validation_result"):
            vr = result["validation_result"]
            logging.info("  Results: %d scored, %d errors", vr.get("results_count", 0), vr.get("errors_count", 0))
            for tag, count in vr.get("tier_counts", {}).items():
                logging.info("    %s: %d", tag, count)
        if result.get("upload_result"):
            logging.info("  Tag upload: %s", result["upload_result"].get("message", ""))
    else:
        logging.error("Phone validation failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_presets(args) -> None:
    """Run the DataSift filter preset management workflow."""
    from datasift_uploader import run_manage_presets_workflow

    discover = getattr(args, "discover", False)
    add_sold = getattr(args, "add_sold_exclusion", False)
    create_seq = getattr(args, "create_sold_sequence", False)

    # Default to discover if no flags specified
    if not (discover or add_sold or create_seq):
        discover = True

    preset_folders = None
    if getattr(args, "preset_folders", None):
        preset_folders = [f.strip() for f in args.preset_folders.split(",")]

    result = asyncio.run(run_manage_presets_workflow(
        discover=discover,
        add_sold_exclusion=add_sold,
        create_sequence=create_seq,
        preset_folders=preset_folders,
    ))

    if result.get("success"):
        logging.info("Manage presets: %s", result.get("message", "OK"))
        if result.get("discovery"):
            disc = result["discovery"]
            for folder, presets in disc.get("preset_folders", {}).items():
                logging.info("  Folder '%s': %s", folder, presets)
            logging.info("  Sequences: %s", disc.get("sequences", []))
        if result.get("presets"):
            p = result["presets"]
            logging.info("  Updated: %s", p.get("updated", []))
            logging.info("  Failed: %s", p.get("failed", []))
        if result.get("sequence"):
            logging.info("  Sequence: %s", result["sequence"].get("message"))
    else:
        logging.error("Manage presets failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_sold(args) -> None:
    """Run the SiftMap sold properties management workflow."""
    from datasift_uploader import run_manage_sold_workflow

    # Parse counties if provided, otherwise the workflow uses its built-in
    # default (Knox + Blount + Jefferson).
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip().title() for c in args.counties.split(",")]

    result = asyncio.run(run_manage_sold_workflow(
        counties=counties,
        months_back=getattr(args, "months_back", 1),
        min_sale_price=getattr(args, "min_sale_price", 1000),
        sold_tag_date=getattr(args, "sold_tag_date", None),
    ))

    if result.get("success"):
        logging.info("Manage sold: %s", result.get("message", "OK"))
        logging.info("  Counties: %s", ", ".join(result.get("counties_processed", [])))
        logging.info("  Total records: %d", result.get("total_records", 0))
    else:
        logging.error("Manage sold failed: %s", result.get("message"))
        sys.exit(1)


def cli_main() -> None:
    """Run as standalone CLI."""
    parser = argparse.ArgumentParser(
        description="SiftStack — full-stack REI operations platform"
    )
    parser.add_argument(
        "mode",
        choices=[
            "daily", "historical", "pdf-import", "photo-import", "dropbox-watch",
            "csv-import", "phone-validate", "manage-sold", "manage-presets",
            # New analysis & workflow modes
            "comp", "rehab", "analyze-deal", "market-analysis", "buyer-prospect",
            "buyer-prospect-jefferson",
            "deep-prospect", "lead-manage", "setup-sequences", "niche-sequential",
            "playbook", "disposition",
        ],
        help=(
            "daily/historical = scrape notices; pdf-import/photo-import = import from files; "
            "dropbox-watch = poll Dropbox; csv-import = re-enrich CSV; "
            "phone-validate = Trestle scoring; manage-sold/manage-presets = DataSift ops; "
            "comp = comparable sales ARV; rehab = rehab cost estimate; "
            "analyze-deal = full deal analysis; market-analysis = zip code scoring; "
            "buyer-prospect = cash buyer lists; deep-prospect = 4-level research; "
            "lead-manage = 4 Pillars qualification; setup-sequences = CRM automation; "
            "niche-sequential = marketing cycle; playbook = SOP generator; "
            "disposition = 1-page wholesale flyer (PVA + Drive photos)"
        ),
    )
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help='Comma-separated counties to scrape (e.g. "Knox,Blount,Jefferson" or "all"). '
             'TN: Knox, Blount. KY: Jefferson.',
    )
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help='Comma-separated notice types (e.g. "foreclosure,probate" or "all")',
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Output separate CSV files per notice type",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override date cutoff (YYYY-MM-DD). Overrides daily/historical mode logic.",
    )
    parser.add_argument(
        "--max-notices",
        type=int,
        default=0,
        help="Stop after scraping this many notices (0 = no limit)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    # PDF import arguments
    parser.add_argument(
        "--pdf-path",
        type=str,
        default=None,
        help="Path to scanned tax sale PDF (required for pdf-import mode)",
    )
    parser.add_argument(
        "--pdf-county",
        type=str,
        default=None,
        help='County name for PDF import, e.g. "Knox" (required for pdf-import mode)',
    )
    parser.add_argument(
        "--pdf-date",
        type=str,
        default=None,
        help="Date for PDF records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--regex-only",
        action="store_true",
        help="Skip LLM parsing and use regex only (pdf-import mode)",
    )
    # Photo import arguments
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Path to folder of phone photos (required for photo-import mode)",
    )
    parser.add_argument(
        "--photo-county",
        type=str,
        default=None,
        dest="photo_county",
        help='County name for photo import, e.g. "Knox" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-type",
        type=str,
        default=None,
        dest="photo_type",
        help='Notice type for photo import, e.g. "eviction" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-date",
        type=str,
        default=None,
        help="Date for photo records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--no-perspective-correct",
        action="store_true",
        dest="no_perspective_correct",
        help="Skip perspective correction in photo preprocessing (photo-import mode)",
    )
    # Dropbox watcher arguments
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        dest="poll_interval",
        help="Seconds between Dropbox polls (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        dest="max_polls",
        help="Maximum number of poll cycles (default: infinite)",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        dest="no_delete",
        help="Don't delete photos from Dropbox after processing",
    )
    # CSV import arguments
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help="Path to existing CSV file to re-enrich (required for csv-import mode)",
    )
    parser.add_argument(
        "--csv-county",
        type=str,
        default=None,
        help='County name for CSV import, e.g. "Knox" (sets county for records missing it)',
    )

    parser.add_argument(
        "--skip-smarty",
        action="store_true",
        help="Skip Smarty address standardization",
    )
    parser.add_argument(
        "--skip-zillow",
        action="store_true",
        help="Skip Zillow property enrichment",
    )
    parser.add_argument(
        "--skip-tax",
        action="store_true",
        help="Skip tax delinquency enrichment",
    )
    parser.add_argument(
        "--skip-obituary",
        action="store_true",
        help="Skip obituary search for deceased owner detection",
    )
    parser.add_argument(
        "--skip-ancestry",
        action="store_true",
        help="Skip Ancestry.com lookup (SSDI + obituary collection)",
    )
    parser.add_argument(
        "--skip-geocode",
        action="store_true",
        help="Skip reverse geocode retry for failed Smarty lookups",
    )
    parser.add_argument(
        "--skip-dm-address",
        action="store_true",
        help="Skip decision-maker mailing address lookup",
    )
    parser.add_argument(
        "--skip-heir-verification",
        action="store_true",
        help="Skip heir alive/dead verification loop (still runs obituary search)",
    )
    parser.add_argument(
        "--max-heir-depth",
        type=int,
        default=2,
        help="Max recursion depth for heir verification (default: 2)",
    )
    parser.add_argument(
        "--tracerfy-tier1",
        action="store_true",
        help="Use Tracerfy as primary DM address lookup ($0.02/record)",
    )
    parser.add_argument(
        "--skip-tracerfy",
        action="store_true",
        help="Skip Tracerfy batch skip trace (phones + emails) before DataSift upload",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama", "openrouter"],
        default=os.getenv("LLM_BACKEND", "anthropic"),
        help="LLM backend: 'anthropic' (Claude Haiku, paid) or 'ollama' (local, free)",
    )
    parser.add_argument(
        "--research-entities",
        action="store_true",
        help="Research entity-owned properties to find the person behind LLCs/Corps (web search + LLM)",
    )
    # Buy box / filter toggles — control which property types pass through
    parser.add_argument(
        "--include-vacant",
        action="store_true",
        help="Keep vacant land parcels (default: filtered out). Use if your buy box includes land deals.",
    )
    parser.add_argument(
        "--include-commercial",
        action="store_true",
        help="Keep commercial properties (default: filtered out). Use if your buy box includes commercial.",
    )
    parser.add_argument(
        "--include-entities",
        action="store_true",
        help="Keep entity-owned records (LLC, Corp, etc.) without filtering. Default: removed unless --research-entities finds a person.",
    )
    parser.add_argument(
        "--upload-datasift",
        action="store_true",
        help="Upload results to DataSift.ai via Playwright (requires DATASIFT_EMAIL/PASSWORD)",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip DataSift property enrichment after upload",
    )
    parser.add_argument(
        "--no-skip-trace",
        action="store_true",
        help="Skip DataSift skip trace after upload (also suppresses Tracerfy in deep-prospect)",
    )
    parser.add_argument(
        "--notify-slack",
        action="store_true",
        help="Send run summary to Slack/Discord webhook (requires SLACK_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--audit-records",
        action="store_true",
        help="Audit DataSift for incomplete records (future: daily check via Playwright)",
    )

    # Phone validation arguments
    parser.add_argument(
        "--list-name",
        type=str,
        default=None,
        help="DataSift list name to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--preset-folder",
        type=str,
        default=None,
        help="DataSift preset folder to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--all-records",
        action="store_true",
        help="Export all DataSift records for phone validation (phone-validate mode)",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Show phone validation cost estimate only, no API calls (phone-validate mode)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip upload step — phone-validate: don't push tags back to DataSift; "
             "disposition: build PDF locally without uploading to Drive",
    )
    parser.add_argument(
        "--custom-tiers",
        type=str,
        default=None,
        help='JSON custom tier boundaries, e.g. \'{"Hot": [80,100], "Cold": [0,79]}\' (phone-validate mode)',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Concurrent Trestle API requests per batch (phone-validate mode, default: 10)",
    )
    parser.add_argument(
        "--add-litigator",
        action="store_true",
        help="Include litigator risk check in phone validation (phone-validate mode)",
    )

    # Manage sold arguments
    parser.add_argument(
        "--months-back",
        type=int,
        default=1,
        help="Months of sales to pull from SiftMap (manage-sold mode, default: 1)",
    )
    parser.add_argument(
        "--min-sale-price",
        type=int,
        default=1000,
        help="Min sale price to exclude deed transfers (manage-sold mode, default: 1000)",
    )
    parser.add_argument(
        "--sold-tag-date",
        type=str,
        default=None,
        help="Tag date in YYYY-MM format (manage-sold mode, default: current month)",
    )

    # Manage presets arguments
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and list all preset folders, presets, and sequences (manage-presets mode)",
    )
    parser.add_argument(
        "--add-sold-exclusion",
        action="store_true",
        help="Update existing presets to exclude Sold status/tag (manage-presets mode)",
    )
    parser.add_argument(
        "--create-sold-sequence",
        action="store_true",
        help="Create Sold Property Cleanup sequence (manage-presets mode)",
    )
    parser.add_argument(
        "--preset-folders",
        type=str,
        default=None,
        help='Comma-separated preset folder names to target (manage-presets mode, default: all)',
    )

    # ── New analysis & workflow mode arguments ────────────────────────
    # Comp analysis
    parser.add_argument("--address", type=str, default=None,
                        help="Property address (comp/rehab/analyze-deal modes)")
    parser.add_argument("--city", type=str, default=None,
                        help="Property city (comp/rehab/analyze-deal modes)")
    parser.add_argument("--state", type=str, default=None,
                        help="Property state, 2-letter (comp/rehab/analyze-deal modes, default: TN)")
    parser.add_argument("--zip-code", type=str, default=None,
                        help="Property ZIP code (comp/rehab/analyze-deal modes)")
    parser.add_argument("--radius", type=float, default=0.5,
                        help="Comp search radius in miles (comp mode, default: 0.5)")
    parser.add_argument("--months", type=int, default=6,
                        help="Comp lookback months (comp mode, default: 6)")
    # Subject overrides for comp mode — use when Zillow's reso facts are stale
    parser.add_argument("--subject-beds", type=int, default=None,
                        help="Override Zillow's bedroom count for subject (comp mode)")
    parser.add_argument("--subject-baths", type=float, default=None,
                        help="Override Zillow's bathroom count for subject (comp mode)")
    parser.add_argument("--subject-sqft", type=int, default=None,
                        help="Override Zillow's total sqft for subject (comp mode)")
    parser.add_argument("--subject-ag", type=int, default=None,
                        help="Subject above-grade sqft (comp mode) — needed if different from total")
    parser.add_argument("--subject-bg", type=int, default=None,
                        help="Subject below-grade finished sqft (comp mode) — often missing from Zillow")
    parser.add_argument("--subject-year", type=int, default=None,
                        help="Override Zillow's year built for subject (comp mode)")
    parser.add_argument("--subject-lot", type=int, default=None,
                        help="Override Zillow's lot sqft for subject (comp mode)")
    parser.add_argument("--subject-garage", type=int, default=None,
                        help="Override Zillow's garage stall count for subject (comp mode)")
    parser.add_argument("--target-condition", type=str, default="full",
                        help="Subject's target post-rehab condition — as-is/light/full (comp mode)")
    parser.add_argument("--condition-file", type=str, default="data/condition_overrides.csv",
                        help="CSV of zpid/address → condition labels (comp mode)")

    # Rehab estimation
    parser.add_argument("--tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Finish tier 1-4 (rehab mode, default: 2)")
    parser.add_argument("--scope", type=str, default="full", choices=["full", "wholetail"],
                        help="Rehab scope (rehab mode, default: full)")
    parser.add_argument("--region", type=str, default="knoxville",
                        help="Regional pricing (rehab mode, default: knoxville)")
    parser.add_argument("--sqft", type=int, default=0,
                        help="Property sqft override (rehab mode)")
    parser.add_argument("--bedrooms", type=int, default=0,
                        help="Bedrooms override (rehab mode)")
    parser.add_argument("--bathrooms", type=float, default=0,
                        help="Bathrooms override (rehab mode)")

    # Deal analysis
    parser.add_argument("--purchase-price", type=float, default=0,
                        help="Purchase price (analyze-deal mode, default: auto-calculate MAO)")
    parser.add_argument("--rehab-tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Rehab tier for deal analysis (default: 2)")
    parser.add_argument("--exit-strategy", type=str, default="flip",
                        choices=["flip", "wholesale", "hold"],
                        help="Exit strategy (analyze-deal mode, default: flip)")

    # Market analysis
    parser.add_argument("--zip-codes", type=str, default=None,
                        help="Comma-separated ZIP codes to analyze (market-analysis mode)")
    parser.add_argument("--monthly-budget", type=float, default=5000,
                        help="Monthly marketing budget for allocation (market-analysis mode)")

    # Buyer prospecting
    parser.add_argument("--min-transactions", type=int, default=2,
                        help="Min transactions to qualify as investor (buyer-prospect mode)")

    # Buyer prospect — Jefferson KY (DataSift Sold Properties scrape)
    parser.add_argument("--start", type=str, default=None,
                        help="Start month YYYY-MM (buyer-prospect-jefferson mode)")
    parser.add_argument("--end", type=str, default=None,
                        help="End month YYYY-MM (buyer-prospect-jefferson mode). "
                             "If --start/--end omitted, uses --months-back (default 12).")
    parser.add_argument("--include-deeds", action="store_true",
                        help="Cross-reference DataSift buyers against Jefferson County "
                             "Clerk deed records (Phase 1B). Adds 'Deeds Found' column "
                             "to scorecard + 'Deed-Only Buyers' tab. Adds ~5 min to the "
                             "scrape for a 12-month window.")

    # Deep prospecting
    parser.add_argument("--depth", type=int, default=3, choices=[1, 2, 3, 4],
                        help="Research depth level 1-4 (deep-prospect mode, default: 3)")

    # Lead management
    parser.add_argument("--lead-action", type=str, default="qualify",
                        choices=["qualify", "report"],
                        help="Lead management action (lead-manage mode)")

    # Sequence setup
    parser.add_argument("--seq-folder", type=str, default="all",
                        choices=["lead-management", "acquisitions", "transactions",
                                 "deep-prospecting", "default", "all"],
                        help="Sequence folder to create (setup-sequences mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without creating (setup-sequences/niche-sequential)")

    # Niche sequential
    parser.add_argument("--channel", type=str, default="sms",
                        choices=["sms", "call", "mail", "dp"],
                        help="Marketing channel (niche-sequential mode)")
    parser.add_argument("--day", type=int, default=1, choices=[1, 2, 3],
                        help="Cycle day 1-3 (niche-sequential mode)")
    parser.add_argument("--ns-action", type=str, default="execute",
                        choices=["execute", "setup-presets", "status"],
                        help="Niche sequential action (niche-sequential mode)")

    # Playbook
    parser.add_argument("--blueprint", type=str, default="wholesale",
                        choices=["wholesale", "flip", "hold", "hybrid"],
                        help="Investment blueprint (playbook mode)")
    parser.add_argument("--market", type=str, default="knoxville",
                        help="Target market (playbook mode)")
    parser.add_argument("--team-size", type=int, default=1,
                        help="Team size 1/2/5 (playbook mode)")

    # Disposition flyer
    parser.add_argument("--asking", type=str, default="",
                        help="Asking price (number or text like 'Make Offer'; disposition mode)")
    parser.add_argument("--arv", type=str, default="",
                        help="ARV (number or text like '$360,000+'; disposition mode)")
    parser.add_argument("--year-built", type=str, default="",
                        help="Year built (disposition mode — overrides PVA value)")
    parser.add_argument("--acreage", type=float, default=0.0,
                        help="Acreage (disposition mode — overrides PVA value)")
    parser.add_argument("--additional-info", type=str, default="",
                        help="Bullet items for the Additional Info card; "
                             "separate with ';' (e.g. 'Vacant; Cash close; Sold AS-IS')")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Skip interactive prompts (disposition mode — fail if PVA misses fields)")

    args = parser.parse_args()

    # Apply LLM backend override from CLI flag
    if hasattr(args, "llm_backend") and args.llm_backend:
        import config as cfg
        cfg.LLM_BACKEND = args.llm_backend
        if args.llm_backend == "ollama":
            logging.info("LLM backend: Ollama (%s)", cfg.OLLAMA_MODEL)
        elif args.llm_backend == "openrouter":
            logging.info("LLM backend: OpenRouter (%s)", cfg.OPENROUTER_MODEL)

    setup_logging(args.verbose)

    # ── Preflight health checks ──────────────────────────────────────
    _counties = [c.strip() for c in args.counties.split(",")] if args.counties else None
    _types = [t.strip() for t in args.types.split(",")] if getattr(args, "types", None) else None
    _active_searches = _filter_searches(_counties, _types)

    # Auto-enable Slack notification whenever the resolved run includes the
    # lis pendens scrape so the JCD scrape always pings on completion without
    # the operator having to remember --notify-slack. Covers explicit
    # --types lis_pendens, unfiltered runs, and --counties-only filters.
    # Both the empty-results and full-results paths at the bottom of
    # run_pipeline gate on args.notify_slack.
    if (
        any(s.notice_type == "lis_pendens" for s in _active_searches)
        and not getattr(args, "notify_slack", False)
    ):
        args.notify_slack = True
        logging.info("Auto-enabled Slack notification for lis_pendens run")

    preflight_failures = _preflight_check(args.mode, active_searches=_active_searches)
    if preflight_failures:
        for f in preflight_failures:
            logging.error("Preflight FAILED: %s", f)
        # Send Slack alert so unattended runs are visible
        try:
            from slack_notifier import notify_preflight_failure
            notify_preflight_failure(preflight_failures)
        except Exception:
            pass  # Don't fail on notification failure
        sys.exit(1)
    logging.info("Preflight checks passed")

    # ── New analysis & workflow modes ─────────────────────────────────

    if args.mode == "comp":
        if not args.address:
            print("ERROR: --address is required for comp mode")
            return
        from comp_analyzer import run_comp_analysis
        overrides = {}
        if args.subject_beds is not None: overrides["beds"] = args.subject_beds
        if args.subject_baths is not None: overrides["baths"] = args.subject_baths
        if args.subject_sqft is not None: overrides["sqft"] = args.subject_sqft
        if args.subject_ag is not None: overrides["ag_sqft"] = args.subject_ag
        if args.subject_bg is not None: overrides["bg_sqft"] = args.subject_bg
        if args.subject_year is not None: overrides["year_built"] = args.subject_year
        if args.subject_lot is not None: overrides["lot_sqft"] = args.subject_lot
        if args.subject_garage is not None: overrides["garage"] = args.subject_garage
        result = run_comp_analysis(
            address=args.address, city=args.city or "", state=args.state or "TN",
            zip_code=args.zip_code or "", radius=args.radius, months=args.months,
            subject_overrides=overrides or None,
            target_condition=args.target_condition,
            condition_file=args.condition_file,
        )
        if "error" in result:
            logger.error("Comp analysis failed: %s", result["error"])
        else:
            print(f"Comp report (Excel): {result['report_path']}")
            if result.get("pdf_path"):
                print(f"Comp report (PDF):   {result['pdf_path']}")
            arv = result["arv"]
            print(f"ARV: ${arv.arv_low:,.0f} (low) / ${arv.arv_mid:,.0f} (mid) / ${arv.arv_high:,.0f} (high)")
            if arv.sentiment_adj_pct:
                print(f"Market phase: {arv.market_phase} (DOM {arv.market_dom_avg}d) → {arv.sentiment_adj_pct:+.0%} sentiment adj")
            print(f"Confidence: {arv.confidence} — {arv.confidence_reason}")
        return

    if args.mode == "rehab":
        if not args.address:
            print("ERROR: --address is required for rehab mode")
            return
        from rehab_estimator import run_rehab_estimate
        result = run_rehab_estimate(
            address=args.address, sqft=args.sqft, bedrooms=args.bedrooms or 3,
            bathrooms=args.bathrooms or 2.0, tier=args.tier, scope=args.scope,
            region=args.region,
        )
        full = result["full_estimate"]
        wt = result["wholetail_estimate"]
        print(f"Rehab report: {result['report_path']}")
        print(f"Full rehab: ${full.grand_total:,.0f} ({full.total_weeks:.0f} weeks)")
        print(f"Wholetail:  ${wt.grand_total:,.0f} ({wt.total_weeks:.0f} weeks)")
        return

    if args.mode == "analyze-deal":
        if not args.address:
            print("ERROR: --address is required for analyze-deal mode")
            return
        from deal_analyzer import run_deal_analysis
        result = run_deal_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            purchase_price=args.purchase_price, rehab_tier=args.rehab_tier,
            exit_strategy=args.exit_strategy, region=args.region,
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Deal analysis failed: %s", result["error"])
        else:
            pkg = result["package"]
            print(f"Deal report: {result['report_path']}")
            print(f"Recommendation: {pkg.recommendation}")
            print(f"ARV: ${pkg.arv.arv_mid:,.0f} | Rehab: ${pkg.rehab_full.grand_total:,.0f}")
            print(f"Flip MAO: ${pkg.mao.flip_mao:,.0f} | Profit: ${pkg.flip.net_profit:,.0f} ({pkg.flip.roi_pct:.0f}% ROI)")
        return

    if args.mode == "market-analysis":
        from market_analyzer import run_market_analysis
        counties = args.counties.split(",") if args.counties else None
        zip_codes = args.zip_codes.split(",") if args.zip_codes else None
        result = run_market_analysis(
            counties=counties, zip_codes=zip_codes,
            monthly_budget=args.monthly_budget,
        )
        if "error" in result:
            logger.error("Market analysis failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Market report: {result['report_path']}")
            print(f"Analyzed {report.total_zips} zips, {report.total_notices} total notices")
            if report.top_zips:
                top = report.top_zips[0]
                print(f"Top zip: {top.zip_code} (score {top.score:.1f}, grade {top.grade})")
        return

    if args.mode == "buyer-prospect":
        from buyer_prospector import run_buyer_prospecting
        counties = args.counties.split(",") if args.counties else None
        result = run_buyer_prospecting(
            counties=counties,
            months_back=args.months_back,
            min_transactions=args.min_transactions,
        )
        if "error" in result:
            logger.error("Buyer prospecting failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Buyer report: {result['report_path']}")
            print(f"Found {report.total_investors} investors")
            print(f"CSV: {result.get('csv_path', 'N/A')}")
        return

    if args.mode == "buyer-prospect-jefferson":
        import asyncio
        from jefferson_buyer_prospector import run_jefferson_buyer_prospecting
        # If user didn't pass --start/--end, default --months-back to 12
        # for this mode (the generic flag defaults to 1, which is wrong here).
        months_back = args.months_back if args.months_back != 1 else 12
        result = asyncio.run(run_jefferson_buyer_prospecting(
            start_month=args.start,
            end_month=args.end,
            months_back=months_back,
            include_deeds=getattr(args, "include_deeds", False),
        ))
        if "error" in result:
            logger.error("Jefferson buyer prospecting failed: %s", result["error"])
        else:
            print(f"Excel:        {result['excel']}")
            print(f"Raw CSV:      {result['raw_csv']}")
            print(f"DataSift CSV: {result['datasift_csv']}")
            main_top = result["main_buyers"][:10]
            n_builders = len(result["builder_buyers"])
            print(f"\nTop 10 wholesale-target buyers "
                  f"({result['start_month']} -> {result['end_month']}, "
                  f"{n_builders} builders/bulk separated):")
            for r in main_top:
                tag = "[Entity]" if r.is_entity else "[Indiv]"
                print(f"  #{r.rank:2d}  {tag} {r.transaction_count:3d}x  ${r.total_invested:>11,}  {r.buyer_name}")
            if n_builders:
                print(f"\nTop builders/bulk acquirers (excluded from main rank):")
                for r in result["builder_buyers"][:5]:
                    print(f"  #{r.rank:2d}  {r.transaction_count:3d}x  ${r.total_invested:>11,}  "
                          f"{r.buyer_name}  [{r.bulk_signal}]")
            cr = result.get("cross_ref")
            if cr is not None:
                print(f"\nJCD deed cross-reference:")
                print(f"  DataSift verified: {cr.verified_count} / unverified: {cr.unverified_count}")
                print(f"  Total deeds scanned: {cr.total_deeds:,} ({cr.total_entity_grantees:,} entity grantees)")
                print(f"  Deed-only buyers DataSift missed: {cr.deed_only_count}")
                if cr.deed_only_buyers:
                    print(f"\nTop 10 deed-only entity buyers (NOT in DataSift):")
                    for b in cr.deed_only_buyers[:10]:
                        print(f"  #{b.rank:2d}  {b.deed_count:3d}x deeds  {b.first_filing}..{b.last_filing}  {b.buyer_name}")
        return

    if args.mode == "deep-prospect":
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        if not csv_path:
            csvs = sorted(config.OUTPUT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            csv_path = str(csvs[0]) if csvs else ""
        if not csv_path:
            print("ERROR: --csv-path required or place CSVs in output/")
            return
        import asyncio
        from deep_prospector import run_deep_prospecting
        result = asyncio.run(run_deep_prospecting(
            csv_path=csv_path, depth=args.depth,
            max_records=args.max_notices if hasattr(args, "max_notices") else 0,
            # Phase 5: --no-skip-trace also suppresses Tracerfy in deep-prospect.
            skip_trace=not getattr(args, "no_skip_trace", False),
        ))
        if "error" in result:
            logger.error("Deep prospecting failed: %s", result["error"])
        else:
            stats = result["stats"]
            print(f"Report: {result['report_path']}")
            print(f"Processed {stats['total']} records at depth {args.depth}")
            print(f"Phones: {stats['phones_found']} | Deceased: {stats['deceased_confirmed']} | DMs: {stats['dms_identified']}")
        return

    if args.mode == "lead-manage":
        from lead_manager import run_lead_management
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        result = run_lead_management(
            action=args.lead_action, csv_path=csv_path,
        )
        if "error" in result:
            logger.error("Lead management failed: %s", result["error"])
        else:
            print(f"STABM report: {result['report_path']}")
            print(f"Total: {result['total']} | Hot: {result['hot']} | Warm: {result['warm']} | Cold: {result['cold']}")
        return

    if args.mode == "setup-sequences":
        from sequence_templates import get_templates, list_templates, preview_sequence
        templates = get_templates(args.seq_folder)
        if args.dry_run:
            print(f"DRY RUN — Would create {len(templates)} sequences in DataSift:")
            for t in templates:
                preview = preview_sequence(t)
                print(f"  [{preview['folder']}] {preview['name']}")
                print(f"    Trigger: {preview['trigger']}")
                print(f"    Actions: {len(preview['actions'])}")
        else:
            print(f"Sequence creation requires Playwright — {len(templates)} templates ready")
            print("Templates defined. DataSift Playwright creation coming in next build.")
            print("\nTemplate list:")
            print(list_templates())
        return

    if args.mode == "niche-sequential":
        from niche_sequential import run_niche_sequential
        result = run_niche_sequential(
            list_name=args.list_name or "",
            channel=args.channel, day=args.day,
            csv_path=args.csv_path if hasattr(args, "csv_path") and args.csv_path else "",
            action=args.ns_action,
        )
        if "error" in result:
            logger.error("Niche sequential failed: %s", result["error"])
        elif "output" in result:
            print(f"Exported: {result['output']}")
            print(f"Channel: {result['channel']}, Day {result['day']}, {result['records']} records")
        elif "presets" in result:
            for p in result["presets"]:
                print(f"  {p['name']}: {p['description']}")
        return

    if args.mode == "playbook":
        from playbook_generator import run_playbook_generator
        result = run_playbook_generator(
            blueprint=args.blueprint, market=args.market,
            team_size=args.team_size,
        )
        print(f"Playbook: {result['playbook_path']}")
        print(f"Blueprint: {result['blueprint'].title()} | Market: {result['market'].title()} | Team: {result['team_size']}")
        return

    if args.mode == "disposition":
        if not args.address:
            print("ERROR: --address is required for disposition mode")
            return
        if not args.asking or not args.arv:
            print("ERROR: --asking and --arv are required for disposition mode")
            return
        from disposition_flyer import run_disposition_flyer
        pdf_path = run_disposition_flyer(
            address=args.address,
            city=args.city or "Louisville",
            state=args.state or "KY",
            zip_code=args.zip_code or "",
            asking_price=args.asking,
            arv=args.arv,
            bedrooms=args.bedrooms or 0,
            bathrooms=args.bathrooms or 0.0,
            sqft=args.sqft or 0,
            year_built=args.year_built or "",
            acreage=args.acreage or 0.0,
            additional_info=args.additional_info or "",
            interactive=not args.non_interactive,
            skip_upload=args.no_upload,
        )
        if pdf_path:
            print(f"\nFlyer ready: {pdf_path}")
        else:
            print("\nFlyer generation failed — check errors above.")
        return

    # Phone validation mode — separate pipeline
    if args.mode == "phone-validate":
        _run_phone_validate(args)
        return

    # Manage presets mode — filter preset + sequence management
    if args.mode == "manage-presets":
        _run_manage_presets(args)
        return

    # Manage sold properties mode — SiftMap workflow
    if args.mode == "manage-sold":
        _run_manage_sold(args)
        return

    # PDF import mode — separate pipeline
    if args.mode == "pdf-import":
        _run_pdf_import(args)
        return

    # Photo import mode — separate pipeline
    if args.mode == "photo-import":
        _run_photo_import(args)
        return

    # Dropbox watcher mode — polls for new photos
    if args.mode == "dropbox-watch":
        from dropbox_watcher import run_watcher
        run_watcher(
            poll_interval=args.poll_interval,
            delete_after=not getattr(args, "no_delete", False),
            max_polls=args.max_polls,
        )
        return

    # CSV re-import mode — separate pipeline
    if args.mode == "csv-import":
        _run_csv_import(args)
        return

    # Filter saved searches
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip() for c in args.counties.split(",")]

    types = None
    if args.types and args.types.lower() != "all":
        types = [t.strip() for t in args.types.split(",")]

    searches = _filter_searches(counties, types)
    if not searches:
        logging.error("No saved searches match the given --counties / --types filters")
        sys.exit(1)

    logging.info(
        "Running %d saved searches: %s",
        len(searches),
        ", ".join(s.saved_search_name for s in searches),
    )

    try:
        _run_scrape_pipeline(args, searches)
    except Exception as e:
        logging.exception("Pipeline failed with unhandled error")
        try:
            from slack_notifier import notify_error
            notify_error("Pipeline (top-level)", e, context=f"mode={args.mode}")
        except Exception:
            pass
        sys.exit(1)


def _run_scrape_pipeline(args, searches) -> None:
    """Run the daily/historical scrape → enrich → export → upload pipeline."""
    # Scrape
    notices = asyncio.run(scrape_all(
        mode=args.mode, searches=searches,
        llm_api_key=config.ANTHROPIC_API_KEY or None,
        since_date_override=args.since,
        max_notices=args.max_notices,
    ))
    # Handle async probate lookup before pipeline (requires asyncio.run)
    probate_notices = [n for n in notices if n.notice_type == "probate" and n.decedent_name and not n.address]
    if probate_notices:
        try:
            from property_lookup import lookup_decedent_properties
            logging.info("Looking up property addresses for %d probate notices...", len(probate_notices))
            asyncio.run(lookup_decedent_properties(probate_notices))
        except ImportError:
            logging.warning("property_lookup module not found -- skipping property lookup")
        except Exception as e:
            logging.warning("Property lookup failed: %s -- continuing without lookups", e)

    # Run unified enrichment pipeline
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    opts = PipelineOptions(
        skip_parcel_lookup=True,  # web scrape notices don't have parcel IDs
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"CLI {args.mode}",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No notices found")
        # Send Slack ping even on empty runs so operators know the job
        # ran successfully (vs silently dying). Previously sys.exit(0)
        # fired before the Slack block at the bottom of this function.
        if getattr(args, "notify_slack", False):
            try:
                from slack_notifier import send_slack_notification
                send_slack_notification([])
            except Exception:
                logging.exception("Slack notification for empty run failed")
        sys.exit(0)

    # Tracerfy batch skip trace (phones + emails for all records)
    tiers_map: dict = {}
    tracerfy_stats: dict = {}
    if not getattr(args, "skip_tracerfy", False):
        import config as cfg
        if cfg.TRACERFY_API_KEY:
            from tracerfy_skip_tracer import batch_skip_trace
            # Fit gate (Phase 4): below-fit leads are not submitted to paid
            # Tracerfy. Parse wholesale_fit_score defensively so an unscored/blank
            # record fails CLOSED (scores 0 → excluded), never crashing the gate.
            trace_candidates = [
                n for n in notices
                if int(n.wholesale_fit_score or 0) >= cfg.SKIP_TRACE_MIN_FIT
            ]
            logging.info("Tracerfy fit-gate: %d/%d records >= SKIP_TRACE_MIN_FIT (%d)",
                         len(trace_candidates), len(notices), cfg.SKIP_TRACE_MIN_FIT)
            tracerfy_stats = batch_skip_trace(trace_candidates)
            if tracerfy_stats.get("credits_exhausted"):
                logging.error(
                    "TRACERFY OUT OF CREDITS — skip trace disabled for this run. "
                    "Add credits at https://tracerfy.com/billing to resume phone/email lookups."
                )
            logging.info(
                "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
                tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
                tracerfy_stats.get("cost", 0.0),
            )
            # Score every phone (DM #1 + all heirs) — writes per-heir phone_scores
            # into heir_map_json so DataSift Notes and PDFs can surface tier badges.
            if cfg.TRESTLE_API_KEY:
                from phone_validator import score_record_phones
                dp_cands = [
                    n for n in notices
                    if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                ]
                if dp_cands:
                    try:
                        tiers_map = score_record_phones(dp_cands, cfg.TRESTLE_API_KEY)
                        logging.info("Trestle scored %d unique phones across %d DP records",
                                     len(tiers_map), len(dp_cands))
                    except Exception as e:
                        logging.warning("Per-record Trestle scoring failed: %s", e)

    # Write output
    if args.split:
        paths = write_csv_by_type(notices)
        for p in paths:
            logging.info("Output: %s", p)
    else:
        path = write_csv(notices)
        logging.info("Output: %s", path)

    # Generate deep-prospecting PDFs for deceased/DM/heir records.
    # Matches the Apify branch behavior so CLI runs get the same reports —
    # includes the Case Summary section added for deceased-owner records.
    dp_candidates = [
        n for n in notices
        if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
    ]
    if dp_candidates:
        try:
            from report_generator import generate_record_pdf
            report_dir = Path("output/reports")
            generated = 0
            for n in dp_candidates:
                try:
                    pdf_path = generate_record_pdf(
                        n, output_dir=report_dir, phone_tiers=tiers_map,
                    )
                    logging.info("Report generated: %s", pdf_path)
                    generated += 1
                except Exception:
                    logging.exception("PDF generation failed for %s", n.address)
            logging.info(
                "Generated %d/%d deep-prospecting PDFs in %s",
                generated, len(dp_candidates), report_dir,
            )
        except Exception:
            logging.exception("Report generator import failed")

    # DataSift upload
    upload_result = None
    if getattr(args, "upload_datasift", False):
        from datasift_formatter import write_datasift_csv, write_datasift_split_csvs
        from datasift_uploader import upload_to_datasift, upload_datasift_split

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        # Use split flow (separate DM + Heir Map Message Board entries)
        csv_infos = write_datasift_split_csvs(notices)
        for info in csv_infos:
            logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

        if len(csv_infos) > 1:
            upload_result = asyncio.run(
                upload_datasift_split(
                    csv_infos,
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )
        else:
            # No deceased-with-heirs — single CSV upload
            upload_result = asyncio.run(
                upload_to_datasift(
                    csv_infos[0]["path"],
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
            if upload_result.get("enrich_result"):
                logging.info("  Enrich: %s", upload_result["enrich_result"].get("message", ""))
            if upload_result.get("skip_trace_result"):
                logging.info("  Skip trace: %s", upload_result["skip_trace_result"].get("message", ""))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

        # Phonebook CSV — write whenever any record has Tracerfy phone data.
        # Gives DataSift phones for records its own skip trace provider skips.
        records_with_phones = [n for n in notices if n.primary_phone or n.mobile_1 or n.landline_1]
        if records_with_phones:
            from datasift_phonebook_formatter import write_phonebook_csv
            pb_path = write_phonebook_csv(notices)
            logging.info("Phonebook CSV (%d records): %s", len(records_with_phones), pb_path)

    # Slack/Discord notification
    if getattr(args, "notify_slack", False):
        from slack_notifier import send_slack_notification

        send_slack_notification(notices, upload_result=upload_result)

    # Audit DataSift for incomplete records (future daily check)
    if getattr(args, "audit_records", False):
        logging.info("--audit-records: Not yet implemented. "
                      "Will check DataSift Incomplete tab via Playwright in a future build.")

    logging.info("Done — %d notices exported", len(notices))


# ── Entry point ───────────────────────────────────────────────────────


if __name__ == "__main__":
    if os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"):
        # Running inside Apify platform or with apify run
        asyncio.run(actor_main())
    else:
        # Standalone CLI
        cli_main()
