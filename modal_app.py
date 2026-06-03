"""SiftStack Modal deployment — scheduled serverless NJ scraper.

Runs every Wednesday at 6 AM Eastern:
  - Logs into NJLisPendens
  - Searches for new NOD/probate filings in Essex, Middlesex, Somerset, Union
  - Enriches via Smarty + Zillow
  - Uploads to DataSift + skip trace
  - Sends Slack summary

Deploy:  modal deploy modal_app.py
Test:    modal run modal_app.py
"""

import modal

app = modal.App("siftstack")

# Container image with all SiftStack dependencies + Playwright browsers
# add_local_dir must be last (Modal mounts it at startup, no build steps after)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "tesseract-ocr",
        "libtesseract-dev",
        "libgl1",
        "libglib2.0-0",
        # Xvfb provides a virtual X display so ancestry_enricher can run a
        # *headed* Chromium in Modal — Ancestry flags accounts that connect
        # via headless mode, so we cannot use --headless here.
        "xvfb",
    )
    .pip_install_from_requirements("requirements.txt")
    .run_commands("playwright install chromium && playwright install-deps chromium")
    .env({"PYTHONPATH": "/app/src"})
    .add_local_dir("src", remote_path="/app/src")
)

secrets = modal.Secret.from_name("siftstack-secrets")

# Persistent cross-run state — dedup index of processed record IDs so a
# weekly cron + any ad-hoc manual run only enrich/upload new filings.
tracking_volume = modal.Volume.from_name(
    "siftstack-tracking", create_if_missing=True,
)
TRACKING_MOUNT = "/tracking"
TRACKING_FILE = f"{TRACKING_MOUNT}/processed_ids.json"

SCHEDULE_CRON = "0 6 * * 3"  # Every Wednesday at 6 AM (UTC — adjusted below)

# Modal cron uses UTC. 6 AM Eastern = 10 AM UTC (EDT) or 11 AM UTC (EST).
# Use 10 AM UTC to cover EDT (summer). Adjust to 11 for EST if needed.
SCHEDULE_CRON_UTC = "0 10 * * 3"


@app.function(
    image=image,
    secrets=[secrets],
    timeout=28800,  # 8 hr — obit + heir verification + Ancestry SSDI per heir
    # Function-level retries cover real crashes only (OOM, network blip,
    # unhandled exception). CloudflareBlockError no longer propagates —
    # _safe() catches it per-scraper so one source's CF block can't kill
    # the parallel gather (May 2026 incident: probate CF-blocked, retry-
    # cascade wiped 56 NJLP + 402 sheriff + Somerset sheriff mid-parse).
    # 8 retries are excessive for non-CF crashes; tune down once the new
    # behavior is observed in one or two weekly runs.
    retries=modal.Retries(
        max_retries=8,
        initial_delay=10.0,
        backoff_coefficient=1.0,
    ),
    schedule=modal.Cron(SCHEDULE_CRON_UTC),
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def nj_weekly_all():
    """Scheduled combined NJ scrape — runs every Wednesday 6 AM ET.

    Fans out all 3 NJ scrapers in parallel (NJLP pre-foreclosure, Middlesex
    surrogate probate, Somerset sheriff-sale PDFs), merges their results,
    runs them through a single enrichment pipeline pass, writes one
    combined CSV, and uploads one DataSift file. This replaces 3 separate
    Wednesday cron jobs so there's a single point of observation and the
    enrichment step isn't paid for 3 times.

    Per-source failures don't cascade — each scraper is wrapped so a bad
    run (e.g. Somerset Akamai block) still lets the others ship.
    """
    import asyncio
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("modal_nj_weekly_all")

    from nj_scraper import scrape_nj_lp_notices
    from nj_middlesex_probate import (
        scrape_middlesex_probates, scrape_somerset_probates, CloudflareBlockError,
    )
    from nj_somerset_sheriff import scrape_somerset_notices
    from nj_sheriff_sales import scrape_civilview_notices

    async def _safe(coro, label):
        try:
            notices = await coro
            logger.info("%s: %d notices", label, len(notices))
            return label, notices, None
        except CloudflareBlockError as e:
            # Isolated failure — propagating crashed the container and
            # killed every parallel scraper (May 2026 incident). Now we
            # log, surface the block in the Slack summary, and let the
            # remaining scrapers ship their records. Sentinel value
            # "cloudflare_block" lets the summary distinguish CF blocks
            # from generic errors.
            logger.warning(
                "%s: Cloudflare blocked — skipping, others continue: %s",
                label, e,
            )
            return label, [], "cloudflare_block"
        except Exception as e:
            logger.error("%s failed: %s", label, e)
            return label, [], str(e)

    logger.info("Starting combined weekly scrape via Modal...")
    results = await asyncio.gather(
        _safe(scrape_nj_lp_notices(counties=["Essex", "Middlesex", "Somerset", "Union"]), "NJLP"),
        # Middlesex Bluestone has no File Date filter — only Death Date.
        # 180 days catches the realistic death-to-file gap (filings cluster
        # 30-150 days post-death; recent DoDs return 0 rows because the
        # probate hasn't been filed yet). Dedup via siftstack-tracking
        # volume handles the inevitable repeats from older DoDs.
        _safe(scrape_middlesex_probates(days_back=180), "Middlesex Probate"),
        # Somerset Bluestone supports File Date filtering directly, so 30
        # days of file-date is the natural cron window.
        _safe(scrape_somerset_probates(days_back=30), "Somerset Probate"),
        _safe(scrape_somerset_notices(include_bankruptcy=True, max_records=0), "Somerset Sheriff"),
        _safe(scrape_civilview_notices(counties=["Essex", "Middlesex", "Union"]), "CivilView Sheriff"),
    )
    per_source = {label: notices for label, notices, _ in results}
    errors = [(label, err) for label, _, err in results if err]

    # Read the sheriff scrapers' stale-auction drop counts. Each scraper
    # exports LAST_STALE_DROPPED after its filter pass; surfacing here
    # gives operators visibility into "where did all those 2024 records
    # go?" in the weekly Slack summary.
    import nj_sheriff_sales as _civilview_mod
    import nj_somerset_sheriff as _somerset_sheriff_mod
    stale_dropped_counts = {
        "CivilView Sheriff": _civilview_mod.LAST_STALE_DROPPED,
        "Somerset Sheriff": _somerset_sheriff_mod.LAST_STALE_DROPPED,
    }

    if not any(per_source.values()):
        msg = "All 5 scrapers returned 0 records"
        if errors:
            msg += f" (errors: {errors})"
        logger.error(msg)
        _notify_failure(msg)
        raise RuntimeError(msg)

    logger.info(
        "Raw scrape: NJLP=%d, MidProbate=%d, SomProbate=%d, SomSheriff=%d, CivilView=%d",
        len(per_source.get("NJLP", [])),
        len(per_source.get("Middlesex Probate", [])),
        len(per_source.get("Somerset Probate", [])),
        len(per_source.get("Somerset Sheriff", [])),
        len(per_source.get("CivilView Sheriff", [])),
    )

    # Cross-run dedup: skip records we've already processed in a prior run.
    # Tracking lives in a Modal Volume so it survives container restarts.
    from dedup_tracker import load_tracking, save_tracking, filter_new
    await tracking_volume.reload.aio()  # pull latest committed state
    tracking = load_tracking(TRACKING_FILE)

    new_lp, skipped_lp = filter_new(per_source.get("NJLP", []), "njlp", tracking)
    new_probate, skipped_probate = filter_new(per_source.get("Middlesex Probate", []), "probate", tracking)
    new_som_probate, skipped_som_probate = filter_new(
        per_source.get("Somerset Probate", []), "somerset_probate", tracking,
    )
    new_somerset, skipped_somerset = filter_new(per_source.get("Somerset Sheriff", []), "somerset", tracking)
    new_civilview, skipped_civilview = filter_new(per_source.get("CivilView Sheriff", []), "civilview_sheriff", tracking)

    logger.info(
        "Dedup: NJLP %d new / %d skipped, MidProbate %d new / %d skipped, "
        "SomProbate %d new / %d skipped, SomSheriff %d new / %d skipped, "
        "CivilView %d new / %d skipped",
        len(new_lp), skipped_lp,
        len(new_probate), skipped_probate,
        len(new_som_probate), skipped_som_probate,
        len(new_somerset), skipped_somerset,
        len(new_civilview), skipped_civilview,
    )

    combined = new_lp + new_probate + new_som_probate + new_somerset + new_civilview
    skipped_counts = {
        "NJLP": skipped_lp,
        "Middlesex Probate": skipped_probate,
        "Somerset Probate": skipped_som_probate,
        "Somerset Sheriff": skipped_somerset,
        "CivilView Sheriff": skipped_civilview,
    }
    new_counts = {
        "NJLP": len(new_lp),
        "Middlesex Probate": len(new_probate),
        "Somerset Probate": len(new_som_probate),
        "Somerset Sheriff": len(new_somerset),
        "CivilView Sheriff": len(new_civilview),
    }

    if not combined:
        # Everything was already seen — save tracking (no-op in practice
        # since nothing changed) and notify that it was a clean quiet week.
        save_tracking(tracking, TRACKING_FILE)
        await tracking_volume.commit.aio()
        msg = (f"All {skipped_lp + skipped_probate + skipped_som_probate + skipped_somerset + skipped_civilview} records were "
               f"previously processed — nothing new to enrich or upload")
        logger.info(msg)
        try:
            import config
            if config.SLACK_WEBHOOK_URL:
                from slack_notifier import _send_webhook
                _send_webhook(
                    "*NJ Weekly All — no new records this week*\n" + msg
                )
        except Exception:
            pass
        return {"success": True, "total": 0, "skipped": skipped_counts, "new": new_counts, "errors": errors}

    # Persist the pre-enrichment RAW scrape to the Modal Volume BEFORE
    # enrichment starts. RAW snapshot means any future enrichment
    # failure / OOM / preemption can still recover scraped data. Same
    # ts / date_folder / volume_out_dir get reused below for the per-
    # list persistence step so all outputs share one timestamp.
    from data_formatter import write_csv
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    date_folder = datetime.now().strftime("%Y-%m-%d")
    volume_out_dir = Path(f"{TRACKING_MOUNT}/output/{date_folder}")
    volume_out_dir.mkdir(parents=True, exist_ok=True)
    try:
        raw_csv_path = write_csv(combined, f"nj_weekly_all_RAW_{ts}.csv")
        raw_dst = volume_out_dir / raw_csv_path.name
        raw_dst.write_bytes(raw_csv_path.read_bytes())
        await tracking_volume.commit.aio()
        logger.info(
            "Persisted RAW pre-enrichment scrape to volume: %s (%d records)",
            raw_dst.relative_to(TRACKING_MOUNT).as_posix(), len(combined),
        )
    except Exception as e:
        logger.warning("Failed to persist RAW pre-enrichment CSV: %s", e)

    # Single enrichment pass across all 3 sources' new notices
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        # Obit search runs on all records — catches deceased foreclosure
        # defendants / sheriff-sale owners that the court scrapers don't flag.
        # Probate records route through the probate_preset path inside
        # obituary_enricher which uses the court-named executor directly
        # (never overrides with a wrong obit match).
        skip_obituary=False,
        # Ancestry.com SSDI verification per heir — confirms alive vs
        # deceased status. Requires ANCESTRY_EMAIL/ANCESTRY_PASSWORD in
        # the Modal secret.
        skip_ancestry=False,
        # Multi-tier DM address waterfall: Tier 3 (TruePeopleSearch via
        # Serper+Firecrawl) and Tier 4 (Tracerfy) work for NJ today.
        # Tiers 1 + 2 are TN-specific (Knox Tax API) and no-op for NJ
        # records cleanly — adding NJ MOD-IV is the next optimization.
        skip_dm_address=False,
        # Build the full ranked heir map per deceased record: each heir
        # gets verified-living/verified-deceased status, signing-authority
        # flag, and an address lookup via the waterfall above. Generates
        # heir_map_json + signing_chain_count + signing_chain_names.
        skip_heir_verification=False,
        skip_parcel_lookup=True,
        source_label="NJ Weekly All (NJLP + Middlesex Probate + Somerset Probate + Somerset Sheriff + CivilView Sheriff)",
    )
    enriched, health = run_enrichment_pipeline(combined, opts, return_health=True)

    # One combined CSV (all records, including paused types — for manual review).
    # ts already defined above (hoisted so the RAW pre-enrichment CSV
    # could be persisted before this step ran).
    from data_formatter import write_csv_by_list
    csv_path = write_csv(enriched, f"nj_weekly_all_{ts}.csv")

    # Split off paused notice_types before DataSift upload. Paused records
    # still get enriched and saved to CSV — they just don't auto-upload,
    # so they can be manually cleaned (e.g. probate ownership verification)
    # before being ingested into DataSift.
    import config
    paused = config.SIFTSTACK_UPLOAD_PAUSED_TYPES
    upload_ready = [n for n in enriched if (n.notice_type or "").lower() not in paused]
    held_back = [n for n in enriched if (n.notice_type or "").lower() in paused]
    paused_counts: dict[str, int] = {}
    for n in held_back:
        nt = (n.notice_type or "unknown").lower()
        paused_counts[nt] = paused_counts.get(nt, 0) + 1

    if held_back:
        # Write a separate CSV of just the held-back records for manual cleaning.
        held_csv_path = write_csv(held_back, f"nj_weekly_all_{ts}_HELD_FOR_CLEANING.csv")
        logger.info(
            "Held back %d records from DataSift upload (paused types=%s): %s",
            len(held_back), sorted(paused), held_csv_path.name,
        )
    else:
        held_csv_path = None

    # Per-list CSVs — one file per DataSift list category (Probate,
    # Sheriff Sale, Notice of Default (Lis Pendens), etc.).
    # upload_ready records + held_back records are split independently so
    # each review stream stays separate.
    by_list_ready = write_csv_by_list(upload_ready, prefix="upload_ready") if upload_ready else []
    by_list_held = write_csv_by_list(held_back, prefix="HELD") if held_back else []
    logger.info(
        "Per-list CSVs: %d upload-ready lists, %d held-back lists",
        len(by_list_ready), len(by_list_held),
    )

    # Persist per-list CSVs to the Modal Volume so they survive container
    # shutdown and can be fetched with `modal volume get siftstack-tracking
    # output/{date}/...`. This is the zero-config path — no Dropbox/Drive
    # credentials required. date_folder / volume_out_dir already defined
    # above (hoisted with ts for the RAW pre-enrichment persist step).
    volume_paths: list[tuple[str, str, int]] = []  # (list_name, volume_relpath, count)
    for list_name, src_path, count in by_list_ready + by_list_held:
        dst = volume_out_dir / src_path.name
        try:
            dst.write_bytes(src_path.read_bytes())
            rel = dst.relative_to(TRACKING_MOUNT).as_posix()
            volume_paths.append((list_name, rel, count))
        except Exception as e:
            logger.warning("Failed to copy %s to volume: %s", src_path.name, e)
    if volume_paths:
        logger.info("Per-list CSVs persisted to volume: %d files", len(volume_paths))

    # Optional: also upload to Dropbox if credentials are configured.
    # Without Dropbox creds this is a silent no-op — Slack will fall back
    # to volume paths.
    dbx_links: dict[str, str] = {}  # csv filename -> share URL
    if config.DROPBOX_REFRESH_TOKEN or os.environ.get("DROPBOX_ACCESS_TOKEN"):
        try:
            from dropbox_uploader import upload_batch
            to_upload: list[tuple] = []
            for list_name, path, _count in by_list_ready + by_list_held:
                dest = f"/SiftStack/{date_folder}/{path.name}"
                to_upload.append((path, dest))
            if to_upload:
                results = upload_batch(to_upload)
                for local_path, url in results:
                    if url:
                        dbx_links[local_path.name] = url
                logger.info("Dropbox: %d/%d per-list CSVs uploaded", len(dbx_links), len(to_upload))
        except Exception as e:
            logger.warning("Dropbox per-list upload failed: %s", e)
    else:
        logger.info("Dropbox creds not set — skipping cloud upload, use `modal volume get` instead")

    # One DataSift upload — only the non-paused records.
    upload_info = None
    try:
        if not upload_ready:
            logger.info("No upload-ready records after applying paused types %s", sorted(paused))
        elif config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD:
            from datasift_formatter import write_datasift_split_csvs
            from datasift_uploader import upload_to_datasift
            csv_infos = write_datasift_split_csvs(upload_ready, list_name="")
            if csv_infos:
                upload_info = await upload_to_datasift(
                    csv_infos[0]["path"], enrich=True, skip_trace=True,
                )
                logger.info("DataSift upload: %s", upload_info.get("message", "OK"))
    except Exception as e:
        logger.warning("DataSift upload failed: %s", e)

    # Persist tracking only after enrichment+upload succeed — if they blow
    # up we'd rather redo the dedup work next week than lose records.
    save_tracking(tracking, TRACKING_FILE)
    await tracking_volume.commit.aio()
    logger.info("Tracking saved: %d NJLP / %d probate / %d somerset / %d civilview total IDs",
                len(tracking.get("njlp", {})),
                len(tracking.get("probate", {})),
                len(tracking.get("somerset", {})),
                len(tracking.get("civilview_sheriff", {})))

    # Build summary text, post it, then attach per-list CSVs as threaded
    # replies. When SLACK_BOT_TOKEN + SLACK_CHANNEL_ID are set the summary
    # goes via chat.postMessage (returns thread_ts so files thread under
    # it); otherwise we fall back to the webhook (summary only, no files).
    try:
        import config
        lines = ["*NJ Weekly All — combined Wednesday run*"]
        for label in ("NJLP", "Middlesex Probate", "Somerset Probate", "Somerset Sheriff", "CivilView Sheriff"):
            n, s = new_counts.get(label, 0), skipped_counts.get(label, 0)
            stale = stale_dropped_counts.get(label, 0)
            stale_suffix = f" / {stale} stale auctions filtered" if stale else ""
            lines.append(f"  {label}: {n} new / {s} skipped (already processed){stale_suffix}")
        cf_blocked = [
            label for label, err in errors if err == "cloudflare_block"
        ]
        other_errors = [
            (label, err) for label, err in errors if err != "cloudflare_block"
        ]
        if cf_blocked:
            lines.append(f"  :warning: BLOCKED: {', '.join(cf_blocked)} (Cloudflare)")
        if other_errors:
            lines.append(f"  errors: {other_errors}")
        lines.append(f"Enriched total: {len(enriched)}")
        lines.append(f"Combined CSV: {csv_path.name}")

        # Enrichment health — per-field fill rates with hard/soft floors.
        # Read-only monitoring; pipeline behavior unchanged.
        from enrichment_pipeline import evaluate_enrichment_health
        health_lines, hard_breach, soft_breach = evaluate_enrichment_health(health)
        if health_lines:
            lines.append("")
            lines.append("*Enrichment Health:*")
            lines.extend(health_lines)
            if hard_breach:
                lines[0] = "⚠️ ENRICHMENT HEALTH WARNING\n" + lines[0]
            elif soft_breach:
                lines[0] = "📊 Enrichment health note\n" + lines[0]

        # Per-list breakdown — the actual CSVs attach as thread replies
        # when bot token is configured, so these lines are just a roster.
        if by_list_ready:
            lines.append("")
            lines.append("*Upload-ready CSVs (by DataSift list):*")
            for list_name, _p, count in by_list_ready:
                lines.append(f"  • {list_name}: {count} records")
        if by_list_held:
            lines.append("")
            lines.append(":pause_button: *Held for cleaning (not auto-uploaded):*")
            for list_name, _p, count in by_list_held:
                lines.append(f"  • {list_name}: {count} records")

        if upload_info and upload_info.get("success"):
            lines.append("")
            lines.append(f"DataSift: uploaded {len(upload_ready)} + enrich + skip trace started")
        elif not upload_ready:
            lines.append("DataSift: nothing uploaded (all records paused)")

        # One-time volume-shift heads-up after the MOD-IV pageno bug fix
        # (commit 0412563, 2026-05-10). Auto-clears after the first run
        # that exercises the fix — keeps the note out of long-term noise.
        from datetime import date
        if date.today() <= date(2026, 5, 13):
            lines.append("")
            lines.append(
                ":wrench: MOD-IV single-hit fix applied — expect uptick "
                "in MOD-IV-verified DM addresses vs. prior weeks."
            )

        # Per-run API cost estimate — counts hits in `enriched` directly
        # (no CSV re-read needed), then formats one line for Slack.
        try:
            from cost_estimator import tally_notices, slack_summary_line
            cost_tally = tally_notices(enriched)
            cost_line = slack_summary_line(cost_tally)
            if cost_line:
                lines.append("")
                lines.append(cost_line)
        except Exception as e:
            logger.warning("Cost estimate failed: %s", e)

        summary_text = "\n".join(lines)

        # Post summary and (if bot token set) grab thread_ts for file replies.
        thread_ts = None
        if os.environ.get("SLACK_BOT_TOKEN") or config.SLACK_WEBHOOK_URL:
            from slack_uploader import post_summary_and_get_ts, upload_weekly_csvs
            thread_ts = post_summary_and_get_ts(
                webhook_url=config.SLACK_WEBHOOK_URL,
                summary_text=summary_text,
            )

            # Attach per-list CSVs as thread replies (bot token required —
            # if it's not set, upload_weekly_csvs will log the error per-file
            # and we'll still have the summary).
            if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL_ID"):
                csv_paths = {name: str(p) for name, p, _c in by_list_ready}
                held_paths = {name: str(p) for name, p, _c in by_list_held}
                upload_weekly_csvs(
                    csv_paths=csv_paths,
                    held_paths=held_paths,
                    run_summary=summary_text,
                    thread_ts=thread_ts,
                )
            else:
                logger.info(
                    "SLACK_BOT_TOKEN/SLACK_CHANNEL_ID not set — "
                    "files not attached; use `modal volume get siftstack-tracking "
                    "output/%s/ ./` to pull CSVs",
                    date_folder,
                )
    except Exception as e:
        logger.warning("Slack notification failed: %s", e)

    return {
        "success": True,
        "total": len(enriched),
        "new": new_counts,
        "skipped": skipped_counts,
        "errors": errors,
        "output_csv": str(csv_path),
    }


@app.function(
    image=image,
    secrets=[secrets],
    timeout=7200,  # 2 hr — NJLP volume × obit search on each defendant
    retries=modal.Retries(
        max_retries=2,
        initial_delay=60.0,
        backoff_coefficient=2.0,
    ),
)
async def nj_weekly_scrape():
    """On-demand NJ Lis Pendens scrape (cron moved to nj_weekly_all)."""
    import asyncio
    import logging
    import os
    import sys

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("modal_nj_scrape")

    # Import after path setup so src/ modules resolve
    from nj_scraper import run_nj_scrape

    logger.info("Starting NJ weekly scrape via Modal...")

    try:
        result = await run_nj_scrape(
            counties=["Essex", "Middlesex", "Somerset", "Union"],
            headless=True,
            upload_datasift=True,
            notify_slack=True,
        )

        if result.get("success"):
            logger.info("NJ scrape complete: %s", result.get("message"))
            return result
        else:
            msg = result.get("message", "Unknown error")
            logger.error("NJ scrape failed: %s", msg)
            # Send failure notification to Slack
            _notify_failure(msg)
            raise RuntimeError(f"NJ scrape failed: {msg}")

    except Exception as e:
        logger.error("NJ scrape exception: %s", e)
        _notify_failure(str(e))
        raise


def _notify_failure(error_msg: str):
    """Send failure alert to Slack."""
    import os

    try:
        import requests

        webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if not webhook:
            return
        requests.post(
            webhook,
            json={"text": f"*SiftStack NJ Scrape FAILED*\nError: {error_msg}"},
            timeout=10,
        )
    except Exception:
        pass


@app.function(
    image=image,
    secrets=[secrets],
    timeout=1800,  # 30 min max
    retries=modal.Retries(
        max_retries=2,
        initial_delay=60.0,
        backoff_coefficient=2.0,
    ),
)
async def nj_weekly_probate_scrape():
    """On-demand Middlesex surrogate probate scrape (cron moved to nj_weekly_all).

    30-day lookback on decedent death dates; each day's matches have their
    detail pages scraped for executor/PR + decedent mailing address.
    """
    import logging
    import os
    import sys

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("modal_nj_probate")

    from nj_middlesex_probate import run_middlesex_probate_scrape

    logger.info("Starting Middlesex probate weekly scrape via Modal...")

    try:
        result = await run_middlesex_probate_scrape(
            days_back=30,
            headless=True,
            upload_datasift=True,
            notify_slack=True,
        )
        if result.get("success"):
            logger.info("Probate scrape complete: %s", result.get("message"))
            return result
        else:
            msg = result.get("message", "Unknown error")
            logger.error("Probate scrape failed: %s", msg)
            _notify_failure(f"Middlesex probate: {msg}")
            raise RuntimeError(f"Probate scrape failed: {msg}")
    except Exception as e:
        logger.error("Probate scrape exception: %s", e)
        _notify_failure(f"Middlesex probate exception: {e}")
        raise


@app.function(
    image=image,
    secrets=[secrets],
    timeout=1800,  # 30 min max
    retries=modal.Retries(
        max_retries=2,
        initial_delay=60.0,
        backoff_coefficient=2.0,
    ),
)
async def nj_weekly_somerset_sheriff():
    """On-demand Somerset County sheriff-sale scrape (cron moved to nj_weekly_all).

    Pulls all active + bankruptcy-hold sales from somersetcountynj.gov,
    downloads each sale's PDF, parses docket/plaintiff/address/block-lot/
    judgment, writes a NoticeData CSV to /app/output.
    """
    import logging
    import os
    import sys

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("modal_nj_somerset_sheriff")

    from nj_somerset_sheriff import scrape_somerset_sheriff_sales

    logger.info("Starting Somerset sheriff-sale weekly scrape via Modal...")

    try:
        records = await scrape_somerset_sheriff_sales(
            include_bankruptcy=True,
            max_records=0,
            headless=True,
        )
        logger.info("Somerset sheriff scrape complete: %d records", len(records))
        return {"success": True, "records": len(records)}
    except Exception as e:
        logger.error("Somerset sheriff scrape exception: %s", e)
        _notify_failure(f"Somerset sheriff: {e}")
        raise


@app.function(
    image=image,
    secrets=[secrets],
    timeout=1800,
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def nj_somerset_sheriff_manual(
    include_bankruptcy: bool = True,
    max_records: int = 0,
):
    """On-demand Somerset sheriff-sale scrape with cross-run dedup.

    Converts scraper dicts into NoticeData, then filters against the
    shared tracking Volume — re-running with the same --max-records
    produces 0 new + N skipped on the second invocation.

    Also writes per-list CSVs and posts them to Slack as threaded
    replies (when SLACK_BOT_TOKEN is configured). Smallest live scraper
    — used as the smoke test for the Slack upload wiring.
    """
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("modal_nj_somerset_sheriff_manual")

    from nj_somerset_sheriff import scrape_somerset_notices
    from dedup_tracker import load_tracking, save_tracking, filter_new

    notices = await scrape_somerset_notices(
        include_bankruptcy=include_bankruptcy,
        max_records=max_records,
        headless=True,
    )
    logger.info("Scraped %d Somerset notices", len(notices))

    await tracking_volume.reload.aio()
    tracking = load_tracking(TRACKING_FILE)
    new_notices, skipped = filter_new(notices, "somerset", tracking)
    logger.info("Dedup: %d new / %d skipped", len(new_notices), skipped)

    save_tracking(tracking, TRACKING_FILE)
    await tracking_volume.commit.aio()

    # Per-list CSV + Slack upload — exercises the wiring end-to-end.
    if new_notices:
        from data_formatter import write_csv_by_list
        date_folder = datetime.now().strftime("%Y-%m-%d")
        volume_out_dir = Path(f"{TRACKING_MOUNT}/output/{date_folder}")
        volume_out_dir.mkdir(parents=True, exist_ok=True)
        by_list = write_csv_by_list(new_notices, prefix="upload_ready")
        for _l, src, _c in by_list:
            try:
                (volume_out_dir / src.name).write_bytes(src.read_bytes())
            except Exception as e:
                logger.warning("Failed to copy %s to volume: %s", src.name, e)
        await tracking_volume.commit.aio()

        summary_text = (
            f"*Somerset Sheriff (manual test)*\n"
            f"Scraped {len(notices)} / New {len(new_notices)} / "
            f"Skipped {skipped}"
        )
        try:
            import config
            from slack_uploader import post_summary_and_get_ts, upload_weekly_csvs
            thread_ts = post_summary_and_get_ts(
                webhook_url=config.SLACK_WEBHOOK_URL,
                summary_text=summary_text,
            )
            if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL_ID"):
                upload_weekly_csvs(
                    csv_paths={name: str(p) for name, p, _ in by_list},
                    held_paths={},
                    run_summary=summary_text,
                    thread_ts=thread_ts,
                )
        except Exception as e:
            logger.warning("Slack post/upload failed: %s", e)

    print(f"Somerset sheriff: scraped={len(notices)} new={len(new_notices)} skipped={skipped}")
    return {
        "scraped": len(notices),
        "new": len(new_notices),
        "skipped": skipped,
    }


@app.function(
    image=image,
    secrets=[secrets],
    # 180-day Middlesex DoD scan + Somerset 30-day file-date scan + combined
    # enrichment (obit + Ancestry SSDI + DM address waterfall + heir map)
    # can run 60-90 min on a busy week. Give it 2hr like nj_weekly_all.
    timeout=7200,
    # Cloudflare rotates Modal egress IPs into and out of its bot blocklist.
    # When the Bluestone scrapers detect a CF challenge they raise
    # CloudflareBlockError, which propagates out of nj_probate_manual; Modal
    # then spawns a new container — and (usually) a different egress IP. 8
    # attempts at ~30s cold-start ≈ ~4 min worst-case before giving up.
    # Initial delay is short because the block is IP-bound, not rate-bound;
    # waiting longer doesn't help.
    retries=modal.Retries(
        max_retries=8,
        initial_delay=10.0,
        backoff_coefficient=1.0,
    ),
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def nj_probate_manual(mx_days_back: int = 180, som_days_back: int = 30):
    """On-demand Middlesex + Somerset probate scrape.

    Mirrors the probate slice of nj_weekly_all: parallel scrape both
    counties, cross-run dedup via the siftstack-tracking volume, single
    enrichment pass, split by DataSift list, Slack summary. Probate is
    in SIFTSTACK_UPLOAD_PAUSED_TYPES so records are held for cleaning
    rather than auto-uploaded.

    Run with:
      modal run modal_app.py::nj_probate_manual                       # 180d + 30d defaults
      modal run modal_app.py::nj_probate_manual --mx-days-back 90
    """
    import asyncio
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("modal_nj_probate_manual")

    from nj_middlesex_probate import (
        scrape_middlesex_probates, scrape_somerset_probates, CloudflareBlockError,
    )
    from dedup_tracker import load_tracking, save_tracking, filter_new
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    from data_formatter import write_csv, write_csv_by_list
    import config

    async def _safe(coro, label):
        try:
            notices = await coro
            logger.info("%s: %d notices", label, len(notices))
            return label, notices, None
        except CloudflareBlockError:
            # Propagate so the function-level retry config can rotate the
            # container's egress IP. Catching here would silently degrade
            # to "0 notices" instead of retrying with a fresh IP.
            logger.warning("%s: Cloudflare blocked — raising for Modal retry", label)
            raise
        except Exception as e:
            logger.error("%s failed: %s", label, e)
            return label, [], str(e)

    logger.info(
        "Probate manual: Middlesex %dd DoD + Somerset %dd file-date",
        mx_days_back, som_days_back,
    )
    results = await asyncio.gather(
        _safe(scrape_middlesex_probates(days_back=mx_days_back), "Middlesex Probate"),
        _safe(scrape_somerset_probates(days_back=som_days_back), "Somerset Probate"),
    )
    per_source = {label: n for label, n, _ in results}
    errors = [(label, err) for label, _, err in results if err]

    if not any(per_source.values()):
        msg = "Both probate scrapers returned 0 records"
        if errors:
            msg += f" (errors: {errors})"
        logger.error(msg)
        _notify_failure(msg)
        return {"success": False, "scraped": 0, "message": msg, "errors": errors}

    # Cross-run dedup against the shared tracking volume.
    await tracking_volume.reload.aio()
    tracking = load_tracking(TRACKING_FILE)
    new_mx, skipped_mx = filter_new(per_source.get("Middlesex Probate", []), "probate", tracking)
    new_som, skipped_som = filter_new(
        per_source.get("Somerset Probate", []), "somerset_probate", tracking,
    )
    logger.info(
        "Dedup: Middlesex %d new / %d skipped, Somerset %d new / %d skipped",
        len(new_mx), skipped_mx, len(new_som), skipped_som,
    )

    combined = new_mx + new_som
    if not combined:
        save_tracking(tracking, TRACKING_FILE)
        await tracking_volume.commit.aio()
        msg = f"All {skipped_mx + skipped_som} records previously processed — nothing new to enrich"
        logger.info(msg)
        try:
            if config.SLACK_WEBHOOK_URL:
                from slack_notifier import _send_webhook
                _send_webhook("*NJ Probate Manual — no new records*\n" + msg)
        except Exception:
            pass
        return {
            "success": True, "scraped": skipped_mx + skipped_som, "new": 0,
            "skipped_middlesex": skipped_mx, "skipped_somerset": skipped_som,
        }

    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=False,
        skip_dm_address=False,
        skip_heir_verification=False,
        skip_parcel_lookup=True,
        source_label=f"NJ Probate Manual (Middlesex {mx_days_back}d + Somerset {som_days_back}d)",
    )
    enriched, health = run_enrichment_pipeline(combined, opts, return_health=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = write_csv(enriched, f"nj_probate_manual_{ts}.csv")

    # Probate is paused from auto-upload — records flow to a separate CSV
    # for manual cleaning before DataSift ingest (existing policy).
    paused = config.SIFTSTACK_UPLOAD_PAUSED_TYPES
    upload_ready = [n for n in enriched if (n.notice_type or "").lower() not in paused]
    held_back = [n for n in enriched if (n.notice_type or "").lower() in paused]
    held_csv_path = (
        write_csv(held_back, f"nj_probate_manual_{ts}_HELD_FOR_CLEANING.csv")
        if held_back else None
    )
    by_list_ready = write_csv_by_list(upload_ready, prefix="upload_ready") if upload_ready else []
    by_list_held = write_csv_by_list(held_back, prefix="HELD") if held_back else []

    # Persist per-list CSVs to the tracking volume for retrieval via
    # `modal volume get siftstack-tracking output/{date}/...`.
    date_folder = datetime.now().strftime("%Y-%m-%d")
    volume_out_dir = Path(f"{TRACKING_MOUNT}/output/{date_folder}")
    volume_out_dir.mkdir(parents=True, exist_ok=True)
    for _list_name, src_path, _count in by_list_ready + by_list_held:
        try:
            (volume_out_dir / src_path.name).write_bytes(src_path.read_bytes())
        except Exception as e:
            logger.warning("Failed to copy %s to volume: %s", src_path.name, e)

    # Save tracking only after enrichment + writes succeed.
    save_tracking(tracking, TRACKING_FILE)
    await tracking_volume.commit.aio()
    logger.info(
        "Tracking saved: %d probate / %d somerset_probate total IDs",
        len(tracking.get("probate", {})),
        len(tracking.get("somerset_probate", {})),
    )

    # Slack summary
    try:
        if config.SLACK_WEBHOOK_URL:
            from slack_notifier import _send_webhook
            lines = [
                f"*NJ Probate Manual — Middlesex {mx_days_back}d + Somerset {som_days_back}d*",
                f"  Middlesex Probate: {len(new_mx)} new / {skipped_mx} skipped (already processed)",
                f"  Somerset Probate: {len(new_som)} new / {skipped_som} skipped (already processed)",
                f"Enriched total: {len(enriched)}",
                f"Combined CSV: {csv_path.name}",
            ]
            if held_csv_path:
                lines.append(
                    f":pause_button: Held for cleaning: {len(held_back)} records "
                    f"(probate paused from auto-upload) — {held_csv_path.name}"
                )
            if errors:
                lines.append(f"errors: {errors}")

            # Enrichment health
            from enrichment_pipeline import evaluate_enrichment_health
            health_lines, hard_breach, soft_breach = evaluate_enrichment_health(health)
            if health_lines:
                lines.append("")
                lines.append("*Enrichment Health:*")
                lines.extend(health_lines)
                if hard_breach:
                    lines[0] = "⚠️ ENRICHMENT HEALTH WARNING\n" + lines[0]
                elif soft_breach:
                    lines[0] = "📊 Enrichment health note\n" + lines[0]

            try:
                from cost_estimator import tally_notices, slack_summary_line
                cost_line = slack_summary_line(tally_notices(enriched))
                if cost_line:
                    lines.append("")
                    lines.append(cost_line)
            except Exception as e:
                logger.warning("Cost estimate failed: %s", e)
            _send_webhook("\n".join(lines))
    except Exception as e:
        logger.warning("Slack notify failed: %s", e)

    return {
        "success": True,
        "scraped_middlesex": len(per_source.get("Middlesex Probate", [])),
        "scraped_somerset": len(per_source.get("Somerset Probate", [])),
        "new_middlesex": len(new_mx),
        "new_somerset": len(new_som),
        "skipped_middlesex": skipped_mx,
        "skipped_somerset": skipped_som,
        "enriched": len(enriched),
        "held_back": len(held_back),
        "csv": csv_path.name,
        "errors": errors,
    }


@app.function(
    image=image,
    secrets=[secrets],
    timeout=1800,
)
async def nj_scrape_manual(counties: list[str] | None = None):
    """On-demand NJ scrape — trigger via `modal run modal_app.py::nj_scrape_manual`."""
    import logging
    import os
    import sys

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from nj_scraper import run_nj_scrape

    result = await run_nj_scrape(
        counties=counties or ["Essex", "Middlesex", "Somerset", "Union"],
        headless=True,
        upload_datasift=True,
        notify_slack=True,
    )
    print(f"Result: {result}")
    return result


@app.function(
    image=image,
    secrets=[secrets],
    # Same envelope as nj_probate_manual — obit + Ancestry SSDI + DM
    # address waterfall + heir map can run 60-90 min for a busy county
    # week. Generous 2hr ceiling.
    timeout=7200,
    retries=modal.Retries(
        max_retries=3, initial_delay=10.0, backoff_coefficient=1.0,
    ),
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def _manual_probate_intake_remote(
    county: str,
    filename: str,
    payload: bytes,
):
    """Cloud half of the probate-runner intake flow.

    Reads bytes that the local_entrypoint shipped up, normalizes them to
    NoticeData via probate_intake.parse_runner_workbook, dedups against
    the shared tracking volume, runs the standard probate enrichment
    pass, persists per-list CSVs to the volume, and posts a Slack
    summary. Does NOT auto-upload to DataSift — operator policy says
    probate output gets manual review before ingest.
    """
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("manual_probate_intake")

    from probate_intake import parse_runner_workbook
    from dedup_tracker import load_tracking, save_tracking, filter_new
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    from data_formatter import write_csv, write_csv_by_list
    import config

    county_canonical = county.strip().title()
    logger.info(
        "Probate runner intake: county=%s, file=%s, size=%d bytes",
        county_canonical, filename, len(payload),
    )

    # 1) Stage the raw file on the volume so an operator can pull it
    # back via `modal volume get siftstack-tracking input/...` if the
    # parse later needs to be retried with a fixed parser. Volumes are
    # cheap; PII-containing runner files live elsewhere too so we keep
    # them around for one week's worth of debug.
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    date_folder = datetime.now().strftime("%Y-%m-%d")
    raw_dir = Path(f"{TRACKING_MOUNT}/input/{date_folder}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name or f"runner_{county_canonical}_{ts}.xlsx"
    raw_dst = raw_dir / safe_name
    raw_dst.write_bytes(payload)
    await tracking_volume.commit.aio()
    logger.info("Staged runner XLSX to volume: %s", raw_dst.relative_to(TRACKING_MOUNT))

    # 2) Parse the XLSX. Sentinel "no files available" → graceful skip.
    notices, stats = parse_runner_workbook(payload, county_canonical)
    if stats["sentinel_hit"] and not notices:
        msg = (
            f":information_source: *Probate Runner Intake: {county_canonical}* — "
            f"file flagged 'no files available'; nothing to enrich."
        )
        logger.info("Sentinel skip: %s", msg)
        try:
            if config.SLACK_WEBHOOK_URL:
                from slack_notifier import _send_webhook
                _send_webhook(msg)
        except Exception as e:
            logger.warning("Slack notify failed: %s", e)
        return {
            "success": True, "county": county_canonical, "skipped": "sentinel",
            "stats": stats,
        }
    if not notices:
        msg = (
            f":warning: *Probate Runner Intake: {county_canonical}* — "
            f"0 parseable rows from {safe_name}. Check header layout."
        )
        logger.warning(msg)
        try:
            if config.SLACK_WEBHOOK_URL:
                from slack_notifier import _send_webhook
                _send_webhook(msg)
        except Exception:
            pass
        return {
            "success": False, "county": county_canonical, "reason": "no_rows",
            "stats": stats,
        }

    # 3) Cross-run dedup using the shared volume. Source bucket is
    # `probate_runner` for all 4 counties — the dedup key is
    # `{county}-{docket}` so county-collisions are impossible.
    await tracking_volume.reload.aio()
    tracking = load_tracking(TRACKING_FILE)
    new_notices, skipped = filter_new(notices, "probate_runner", tracking)
    logger.info(
        "Dedup: %d new / %d already processed for %s",
        len(new_notices), skipped, county_canonical,
    )

    if not new_notices:
        save_tracking(tracking, TRACKING_FILE)
        await tracking_volume.commit.aio()
        msg = (
            f":bookmark_tabs: *Probate Runner Intake: {county_canonical}* — "
            f"{len(notices)} records, 0 new, {skipped} already processed, "
            f"0 obituaries found"
        )
        logger.info(msg)
        try:
            if config.SLACK_WEBHOOK_URL:
                from slack_notifier import _send_webhook
                _send_webhook(msg)
        except Exception as e:
            logger.warning("Slack notify failed: %s", e)
        return {
            "success": True, "county": county_canonical, "total": len(notices),
            "new": 0, "skipped": skipped, "stats": stats,
        }

    # 4) Standard probate enrichment pass — obit + Ancestry SSDI + DM
    # address waterfall + heir map. Mirrors nj_probate_manual exactly
    # so the runner pipeline has the same enrichment surface as the
    # Bluestone-scrape pipeline.
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=False,
        skip_dm_address=False,
        skip_heir_verification=False,
        skip_parcel_lookup=True,
        source_label=f"Probate Runner ({county_canonical})",
    )
    enriched, health = run_enrichment_pipeline(new_notices, opts, return_health=True)

    # 5) CSV outputs. Combined + per-list, both to the volume so
    # `modal volume get siftstack-tracking output/{date}/...` picks them
    # up. No auto-DataSift upload — manual review per policy.
    csv_path = write_csv(
        enriched, f"probate_runner_{county_canonical.lower()}_{ts}.csv",
    )
    by_list = write_csv_by_list(
        enriched, prefix=f"probate_runner_{county_canonical.lower()}",
    )

    volume_out_dir = Path(f"{TRACKING_MOUNT}/output/{date_folder}")
    volume_out_dir.mkdir(parents=True, exist_ok=True)
    persisted: list[str] = []
    for src_path in [csv_path] + [p for _l, p, _c in by_list]:
        try:
            dst = volume_out_dir / src_path.name
            dst.write_bytes(src_path.read_bytes())
            persisted.append(dst.relative_to(TRACKING_MOUNT).as_posix())
        except Exception as e:
            logger.warning("Failed to persist %s: %s", src_path.name, e)

    # 6) Save tracking ONLY after enrichment + write succeeded — if
    # anything above crashed we want to retry the same records, not
    # mark them as processed and lose them.
    save_tracking(tracking, TRACKING_FILE)
    await tracking_volume.commit.aio()

    # 7) Slack summary in the operator-requested format.
    obit_found = sum(
        1 for n in enriched if n.owner_deceased == "yes" and n.obituary_url
    )
    msg_lines = [
        f":bookmark_tabs: *Probate Runner Intake: {county_canonical}* — "
        f"{len(notices)} records, {len(new_notices)} new, "
        f"{skipped} already processed, {obit_found} obituaries found",
        f"  Stats: parsed={stats['rows_parsed']}, "
        f"blank={stats['rows_skipped_blank']}, sentinel={stats['sentinel_hit']}",
        f"  Combined CSV: {csv_path.name}",
    ]
    if persisted:
        msg_lines.append(f"  Volume: output/{date_folder}/")

    # Enrichment health
    from enrichment_pipeline import evaluate_enrichment_health
    health_lines, hard_breach, soft_breach = evaluate_enrichment_health(health)
    if health_lines:
        msg_lines.append("")
        msg_lines.append("*Enrichment Health:*")
        msg_lines.extend(health_lines)
        if hard_breach:
            msg_lines[0] = "⚠️ ENRICHMENT HEALTH WARNING\n" + msg_lines[0]
        elif soft_breach:
            msg_lines[0] = "📊 Enrichment health note\n" + msg_lines[0]
    try:
        if config.SLACK_WEBHOOK_URL:
            from slack_notifier import _send_webhook
            _send_webhook("\n".join(msg_lines))
    except Exception as e:
        logger.warning("Slack notify failed: %s", e)

    return {
        "success": True,
        "county": county_canonical,
        "total": len(notices),
        "new": len(new_notices),
        "skipped": skipped,
        "enriched": len(enriched),
        "obit_found": obit_found,
        "csv": csv_path.name,
        "per_list": [p.name for _l, p, _c in by_list],
        "volume_paths": persisted,
        "stats": stats,
    }


@app.local_entrypoint()
def manual_probate_intake(county: str, file: str):
    """Local-side: read the runner XLSX from disk, ship it to the cloud.

    Usage:
        modal run modal_app.py::manual_probate_intake \\
            --county essex --file ~/Desktop/SiftStack/input/Essex_Week20.xlsx

    `county` is one of essex / middlesex / somerset / union (case-
    insensitive). `file` is a local filesystem path; we read its bytes
    here and pass them up so the cloud container doesn't need any host
    mount or upload-step. The cloud function then stages the bytes to
    the tracking volume, runs probate enrichment, writes CSVs, and
    posts a Slack summary.
    """
    from pathlib import Path

    valid_counties = {"essex", "middlesex", "somerset", "union"}
    county_lc = county.strip().lower()
    if county_lc not in valid_counties:
        raise SystemExit(
            f"Invalid county {county!r}. Expected one of: {sorted(valid_counties)}"
        )

    path = Path(file).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"File not found: {path}")
    if not path.is_file():
        raise SystemExit(f"Not a file: {path}")

    payload = path.read_bytes()
    print(f"Uploading {path.name} ({len(payload):,} bytes, county={county_lc}) to Modal…")
    result = _manual_probate_intake_remote.remote(
        county=county_lc, filename=path.name, payload=payload,
    )
    print(f"Done: {result}")


@app.function(
    image=image,
    secrets=[secrets],
    timeout=600,
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def nj_bluestone_diag():
    """Probe Middlesex Bluestone portal from Modal — dump HTML, screenshot,
    and offsetWidth so we can compare against local rendering. Used to
    diagnose 'death-date input never rendered' failures in the cron run.
    """
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger = logging.getLogger("nj_bluestone_diag")

    from playwright.async_api import async_playwright

    diag = Path(f"/tracking/diagnostics/bluestone_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    diag.mkdir(parents=True, exist_ok=True)

    url = "https://surrogatesearch.co.middlesex.nj.us/SurrogateSearch/default.aspx"
    sel = "#ContentPlaceHolder1_ASPxSplitterDefaultMain_ASPxTextBox_to_date_I"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)

        width = await page.evaluate(
            "(s) => { const el = document.querySelector(s); return el ? el.offsetWidth : 'null'; }",
            sel,
        )
        title = await page.title()
        html = await page.content()
        text = await page.evaluate("() => document.body && document.body.innerText || ''")

        (diag / "page.html").write_text(html)
        (diag / "page.txt").write_text(text[:10000])
        (diag / "meta.txt").write_text(f"url={page.url}\ntitle={title!r}\nto_date_I.offsetWidth={width}\nhtml_bytes={len(html)}\n")
        await page.screenshot(path=str(diag / "page.png"), full_page=True)

        await browser.close()

    await tracking_volume.commit.aio()
    logger.info("Diag dumped to %s — fetch with: modal volume get siftstack-tracking %s",
                diag, str(diag).removeprefix("/tracking/"))
    return {
        "ok": True,
        "diag_dir": str(diag),
        "offsetWidth": width,
        "title": title,
        "html_bytes": len(html),
    }


@app.function(
    image=image,
    secrets=[secrets],
    timeout=7200,  # 2 hr — should be plenty for ~5-15 probate records
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def nj_probate_dry_run(days_back: int = 7):
    """End-to-end dry-run of the Wednesday cron's enrichment pipeline,
    scoped to Middlesex probate only.

    Exercises every code path the full nj_weekly_all run will hit:
      - Obit search (incl. probate_preset routing)
      - Ancestry SSDI heir verification (xvfb + persistent profile)
      - DM address waterfall (NJ MOD-IV Tier 1 + Tracerfy + Serper/Firecrawl)
      - Heir map building
      - Per-list CSV split

    Skips:
      - The other scrapers (NJLP, Somerset, CivilView)
      - Cross-run dedup (so already-seen records are re-enriched)
      - DataSift upload
      - Slack notification

    Output: /tracking/output/dry_run/<ts>/  — fetch with `modal volume get`.

    Run with:  modal run modal_app.py::nj_probate_dry_run --days-back 7
    """
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("nj_probate_dry_run")

    from nj_middlesex_probate import scrape_middlesex_probates
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    from data_formatter import write_csv, write_csv_by_list
    from cost_estimator import tally_notices, estimate_costs

    logger.info("DRY RUN: Middlesex probate, last %d days, full enrichment", days_back)

    notices = await scrape_middlesex_probates(days_back=days_back, headless=True)
    if not notices:
        return {"ok": True, "records": 0, "message": "no probate filings in window"}

    logger.info("Scraped %d Middlesex probate filings — running enrichment", len(notices))

    # Same flags as nj_weekly_all (the production cron path)
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=False,
        skip_dm_address=False,
        skip_heir_verification=False,
        skip_parcel_lookup=True,
        source_label="DRY RUN: Middlesex Probate",
    )
    enriched = run_enrichment_pipeline(notices, opts)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_root = Path(f"{TRACKING_MOUNT}/output/dry_run/{ts}")
    out_root.mkdir(parents=True, exist_ok=True)

    main_csv = write_csv(enriched, f"dry_run_middlesex_probate_{ts}.csv")
    dst_main = out_root / main_csv.name
    dst_main.write_bytes(main_csv.read_bytes())

    by_list = write_csv_by_list(enriched, prefix="dry_run") or []
    list_paths = []
    for list_name, src_path, count in by_list:
        dst = out_root / src_path.name
        dst.write_bytes(src_path.read_bytes())
        list_paths.append({"list": list_name, "file": dst.name, "count": count})

    tally = tally_notices(enriched)
    costs = estimate_costs(tally)

    deceased = [n for n in enriched if (n.owner_deceased or "").lower() == "yes"]
    with_dm_addr = [n for n in enriched if (n.decision_maker_street or "")]
    with_heir_map = [n for n in enriched if (n.heir_map_json or "")]

    summary = {
        "ok": True,
        "records": len(enriched),
        "deceased_records": len(deceased),
        "records_with_dm_address": len(with_dm_addr),
        "records_with_heir_map": len(with_heir_map),
        "smarty_hits": tally["smarty_hits"],
        "zillow_hits": tally["zillow_hits"],
        "cost_breakdown": {k: round(v, 4) for k, v in costs.items()},
        "cost_total": round(sum(costs.values()), 4),
        "output_dir": str(out_root),
        "per_list": list_paths,
    }
    logger.info("DRY RUN SUMMARY: %s", summary)

    await tracking_volume.commit.aio()
    return summary


@app.function(
    image=image,
    secrets=[secrets],
    timeout=300,
    volumes={TRACKING_MOUNT: tracking_volume},
)
async def ancestry_login_smoke_test():
    """Pre-flight check for the Wednesday cron — Ancestry login only.

    Boots Xvfb, opens the persistent profile from /tracking/.ancestry_profile,
    and runs `_ensure_logged_in`. No record lookups, no SSDI search — just
    proves the login path works in Modal so we don't discover a 2FA challenge
    8 hours into Wednesday's run.

    On failure, dumps a screenshot + HTML + URL/title to
    /tracking/diagnostics/ancestry_login_<ts>/ so we can see what blocked us
    (most common: 2FA prompt, captcha, or device-verification interstitial).

    Run with:  modal run modal_app.py::ancestry_login_smoke_test
    """
    import logging
    import os
    import sys
    from datetime import datetime
    from pathlib import Path

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("ancestry_login_smoke_test")

    import ancestry_enricher as ae

    logger.info("PROFILE_DIR=%s  PAGE_LOAD_FILE=%s", ae.PROFILE_DIR, ae.PAGE_LOAD_FILE)

    diag_root = Path("/tracking/diagnostics") / f"ancestry_login_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    diag_root.mkdir(parents=True, exist_ok=True)

    async def dump(page, label: str):
        """Save url/title/html/screenshot/text snippet under diag_root."""
        try:
            url = page.url
            title = await page.title()
            html = await page.content()
            text = await page.evaluate("() => document.body && document.body.innerText || ''")
            (diag_root / f"{label}.url.txt").write_text(f"{url}\n{title}\n")
            (diag_root / f"{label}.html").write_text(html)
            (diag_root / f"{label}.text.txt").write_text(text[:8000])
            await page.screenshot(path=str(diag_root / f"{label}.png"), full_page=True)
            logger.info("Diag dump [%s] -> %s  url=%s", label, diag_root, url)
        except Exception as e:
            logger.warning("Diag dump %s failed: %s", label, e)

    pw = context = page = None
    try:
        ae._ensure_xvfb()
        ae.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        context = await pw.chromium.launch_persistent_context(
            str(ae.PROFILE_DIR),
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 1) Check existing session by visiting an auth-only page.
        # /account/orders forwards a logged-in user to /order-history; an
        # anonymous user gets bounced to /account/signin. So the only
        # reliable signal is "did we end up on a signin page or not".
        # (Earlier draft also required "account" in the URL — that produced
        # false-negatives for /order-history and triggered repeated logins,
        # which got the Modal IP flagged with a Cloudflare challenge.)
        await page.goto(f"{ae.ANCESTRY_URL}/account/orders", wait_until="domcontentloaded")
        await dump(page, "01_orders_check")
        landed_url = page.url.lower()
        if "signin" not in landed_url and "challenge" not in landed_url:
            logger.info("Already logged in — protected page loaded: %s", page.url)
            await ae.close_browser(pw, context)
            tracking_volume.commit()
            return {"ok": True, "current_url": page.url, "via": "persistent_profile"}
        logger.info("Not logged in (bounced to %s) — attempting auto-login", page.url)

        # 2) Walk login form manually so we can dump state at every step
        await page.goto(ae.SIGNIN_URL, wait_until="domcontentloaded")
        await dump(page, "02_signin_loaded")

        from config import ANCESTRY_EMAIL, ANCESTRY_PASSWORD
        if not ANCESTRY_EMAIL or not ANCESTRY_PASSWORD:
            return {"ok": False, "error": "ANCESTRY_EMAIL/PASSWORD missing from secret",
                    "diag_dir": str(diag_root)}

        for sel in ["input[name='username']", "input[type='email']", "#username"]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(ANCESTRY_EMAIL)
                break
        for sel in ["input[name='password']", "input[type='password']", "#password"]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(ANCESTRY_PASSWORD)
                break
        await dump(page, "03_form_filled")

        for sel in ["button[type='submit']", "input[type='submit']"]:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                break

        # Wait up to 15s for navigation away from signin
        import asyncio
        for i in range(15):
            await asyncio.sleep(1)
            if "signin" not in page.url.lower():
                break
        await dump(page, "04_after_submit")

        if "signin" in page.url.lower():
            logger.error("Login failed — still on signin URL=%s", page.url)
            await ae.close_browser(pw, context)
            tracking_volume.commit()
            return {"ok": False, "error": "still on signin after submit",
                    "current_url": page.url, "diag_dir": str(diag_root)}

        logger.info("Login OK — landed on %s", page.url)
        await ae.close_browser(pw, context)
        tracking_volume.commit()
        return {"ok": True, "current_url": page.url, "diag_dir": str(diag_root)}

    except Exception as e:
        logger.exception("Smoke test exception")
        if page is not None:
            await dump(page, "99_exception")
        if pw is not None:
            try: await ae.close_browser(pw, context)
            except Exception: pass
        try: tracking_volume.commit()
        except Exception: pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "diag_dir": str(diag_root)}


@app.local_entrypoint()
def main():
    """Local entrypoint for `modal run modal_app.py`."""
    import asyncio

    result = nj_scrape_manual.remote()
    print(f"Completed: {result}")
