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
    retries=modal.Retries(
        max_retries=2,
        initial_delay=60.0,
        backoff_coefficient=2.0,
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
    from nj_middlesex_probate import scrape_middlesex_probates
    from nj_somerset_sheriff import scrape_somerset_notices
    from nj_tax_sale_monitor import scrape_nj_tax_sale_notices
    from nj_sheriff_sales import scrape_civilview_notices

    async def _safe(coro, label):
        try:
            notices = await coro
            logger.info("%s: %d notices", label, len(notices))
            return label, notices, None
        except Exception as e:
            logger.error("%s failed: %s", label, e)
            return label, [], str(e)

    logger.info("Starting combined weekly scrape via Modal...")
    results = await asyncio.gather(
        _safe(scrape_nj_lp_notices(counties=["Essex", "Middlesex", "Somerset", "Union"]), "NJLP"),
        _safe(scrape_middlesex_probates(days_back=30), "Middlesex Probate"),
        _safe(scrape_somerset_notices(include_bankruptcy=True, max_records=0), "Somerset Sheriff"),
        _safe(scrape_nj_tax_sale_notices(
            counties=["Middlesex", "Essex", "Somerset", "Union"],
            fetch_details=True,
        ), "Tax Sale"),
        _safe(scrape_civilview_notices(counties=["Essex", "Middlesex", "Union"]), "CivilView Sheriff"),
    )
    per_source = {label: notices for label, notices, _ in results}
    errors = [(label, err) for label, _, err in results if err]

    if not any(per_source.values()):
        msg = "All 5 scrapers returned 0 records"
        if errors:
            msg += f" (errors: {errors})"
        logger.error(msg)
        _notify_failure(msg)
        raise RuntimeError(msg)

    logger.info("Raw scrape: NJLP=%d, Probate=%d, Somerset=%d, TaxSale=%d, CivilView=%d",
                len(per_source.get("NJLP", [])),
                len(per_source.get("Middlesex Probate", [])),
                len(per_source.get("Somerset Sheriff", [])),
                len(per_source.get("Tax Sale", [])),
                len(per_source.get("CivilView Sheriff", [])))

    # Cross-run dedup: skip records we've already processed in a prior run.
    # Tracking lives in a Modal Volume so it survives container restarts.
    from dedup_tracker import load_tracking, save_tracking, filter_new
    await tracking_volume.reload.aio()  # pull latest committed state
    tracking = load_tracking(TRACKING_FILE)

    new_lp, skipped_lp = filter_new(per_source.get("NJLP", []), "njlp", tracking)
    new_probate, skipped_probate = filter_new(per_source.get("Middlesex Probate", []), "probate", tracking)
    new_somerset, skipped_somerset = filter_new(per_source.get("Somerset Sheriff", []), "somerset", tracking)
    new_taxsale, skipped_taxsale = filter_new(per_source.get("Tax Sale", []), "tax_sale", tracking)
    new_civilview, skipped_civilview = filter_new(per_source.get("CivilView Sheriff", []), "civilview_sheriff", tracking)

    logger.info(
        "Dedup: NJLP %d new / %d skipped, Probate %d new / %d skipped, "
        "Somerset %d new / %d skipped, TaxSale %d new / %d skipped, "
        "CivilView %d new / %d skipped",
        len(new_lp), skipped_lp,
        len(new_probate), skipped_probate,
        len(new_somerset), skipped_somerset,
        len(new_taxsale), skipped_taxsale,
        len(new_civilview), skipped_civilview,
    )

    combined = new_lp + new_probate + new_somerset + new_taxsale + new_civilview
    skipped_counts = {
        "NJLP": skipped_lp,
        "Middlesex Probate": skipped_probate,
        "Somerset Sheriff": skipped_somerset,
        "Tax Sale": skipped_taxsale,
        "CivilView Sheriff": skipped_civilview,
    }
    new_counts = {
        "NJLP": len(new_lp),
        "Middlesex Probate": len(new_probate),
        "Somerset Sheriff": len(new_somerset),
        "Tax Sale": len(new_taxsale),
        "CivilView Sheriff": len(new_civilview),
    }

    if not combined:
        # Everything was already seen — save tracking (no-op in practice
        # since nothing changed) and notify that it was a clean quiet week.
        save_tracking(tracking, TRACKING_FILE)
        await tracking_volume.commit.aio()
        msg = (f"All {skipped_lp + skipped_probate + skipped_somerset + skipped_taxsale + skipped_civilview} records were "
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
        source_label="NJ Weekly All (NJLP + Middlesex Probate + Somerset Sheriff + Tax Sale + CivilView Sheriff)",
    )
    enriched = run_enrichment_pipeline(combined, opts)

    # One combined CSV (all records, including paused types — for manual review)
    from data_formatter import write_csv, write_csv_by_list
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
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
    # Sheriff Sale, Notice of Default (Lis Pendens), Tax Sale, etc.).
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
    # credentials required.
    date_folder = datetime.now().strftime("%Y-%m-%d")
    volume_out_dir = Path(f"{TRACKING_MOUNT}/output/{date_folder}")
    volume_out_dir.mkdir(parents=True, exist_ok=True)
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
    logger.info("Tracking saved: %d NJLP / %d probate / %d somerset / %d tax_sale / %d civilview total IDs",
                len(tracking.get("njlp", {})),
                len(tracking.get("probate", {})),
                len(tracking.get("somerset", {})),
                len(tracking.get("tax_sale", {})),
                len(tracking.get("civilview_sheriff", {})))

    # Build summary text, post it, then attach per-list CSVs as threaded
    # replies. When SLACK_BOT_TOKEN + SLACK_CHANNEL_ID are set the summary
    # goes via chat.postMessage (returns thread_ts so files thread under
    # it); otherwise we fall back to the webhook (summary only, no files).
    try:
        import config
        lines = ["*NJ Weekly All — combined Wednesday run*"]
        for label in ("NJLP", "Middlesex Probate", "Somerset Sheriff", "Tax Sale", "CivilView Sheriff"):
            n, s = new_counts.get(label, 0), skipped_counts.get(label, 0)
            lines.append(f"  {label}: {n} new / {s} skipped (already processed)")
        if errors:
            lines.append(f"  errors: {errors}")
        lines.append(f"Enriched total: {len(enriched)}")
        lines.append(f"Combined CSV: {csv_path.name}")

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
    timeout=1800,
)
async def nj_probate_manual(days_back: int = 30):
    """On-demand Middlesex probate scrape."""
    import logging
    import os
    import sys

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from nj_middlesex_probate import run_middlesex_probate_scrape

    result = await run_middlesex_probate_scrape(
        days_back=days_back,
        headless=True,
        upload_datasift=True,
        notify_slack=True,
    )
    print(f"Result: {result}")
    return result


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
