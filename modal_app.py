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
    )
    .pip_install_from_requirements("requirements.txt")
    .run_commands("playwright install chromium && playwright install-deps chromium")
    .env({"PYTHONPATH": "/app/src"})
    .add_local_dir("src", remote_path="/app/src")
)

secrets = modal.Secret.from_name("siftstack-secrets")

SCHEDULE_CRON = "0 6 * * 3"  # Every Wednesday at 6 AM (UTC — adjusted below)

# Modal cron uses UTC. 6 AM Eastern = 10 AM UTC (EDT) or 11 AM UTC (EST).
# Use 10 AM UTC to cover EDT (summer). Adjust to 11 for EST if needed.
SCHEDULE_CRON_UTC = "0 10 * * 3"


@app.function(
    image=image,
    secrets=[secrets],
    timeout=3600,  # 60 min max — combined run is slower than any single scraper
    retries=modal.Retries(
        max_retries=2,
        initial_delay=60.0,
        backoff_coefficient=2.0,
    ),
    schedule=modal.Cron(SCHEDULE_CRON_UTC),
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
    )
    per_source = {label: notices for label, notices, _ in results}
    errors = [(label, err) for label, _, err in results if err]
    combined = []
    for _, notices, _ in results:
        combined.extend(notices)

    if not combined:
        msg = "All 3 scrapers returned 0 records"
        if errors:
            msg += f" (errors: {errors})"
        logger.error(msg)
        _notify_failure(msg)
        raise RuntimeError(msg)

    logger.info("Combined scrape: %d total (NJLP=%d, Probate=%d, Somerset=%d)",
                len(combined),
                len(per_source.get("NJLP", [])),
                len(per_source.get("Middlesex Probate", [])),
                len(per_source.get("Somerset Sheriff", [])))

    # Single enrichment pass across all 3 sources' notices
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=True,
        skip_ancestry=True,
        skip_dm_address=True,
        skip_heir_verification=True,
        skip_parcel_lookup=True,
        source_label="NJ Weekly All (NJLP + Middlesex Probate + Somerset Sheriff)",
    )
    enriched = run_enrichment_pipeline(combined, opts)

    # One combined CSV
    from data_formatter import write_csv
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = write_csv(enriched, f"nj_weekly_all_{ts}.csv")

    # One DataSift upload
    upload_info = None
    try:
        import config
        if config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD:
            from datasift_formatter import write_datasift_split_csvs
            from datasift_uploader import upload_to_datasift
            csv_infos = write_datasift_split_csvs(enriched, list_name="")
            if csv_infos:
                upload_info = await upload_to_datasift(
                    csv_infos[0]["path"], enrich=True, skip_trace=True,
                )
                logger.info("DataSift upload: %s", upload_info.get("message", "OK"))
    except Exception as e:
        logger.warning("DataSift upload failed: %s", e)

    # One Slack summary covering all 3 sources
    try:
        import config
        if config.SLACK_WEBHOOK_URL:
            from slack_notifier import _send_webhook
            lines = ["*NJ Weekly All — combined Wednesday run*"]
            for label in ("NJLP", "Middlesex Probate", "Somerset Sheriff"):
                lines.append(f"  {label}: {len(per_source.get(label, []))} records")
            if errors:
                lines.append(f"  errors: {errors}")
            lines.append(f"Enriched total: {len(enriched)}")
            lines.append(f"Output: {csv_path.name}")
            if upload_info and upload_info.get("success"):
                lines.append("DataSift: uploaded + enrich + skip trace started")
            _send_webhook("\n".join(lines))
    except Exception as e:
        logger.warning("Slack notification failed: %s", e)

    return {
        "success": True,
        "total": len(enriched),
        "per_source": {k: len(v) for k, v in per_source.items()},
        "errors": errors,
        "output_csv": str(csv_path),
    }


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
)
async def nj_somerset_sheriff_manual(
    include_bankruptcy: bool = True,
    max_records: int = 0,
):
    """On-demand Somerset sheriff-sale scrape."""
    import logging
    import os
    import sys

    sys.path.insert(0, "/app/src")
    os.chdir("/app")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from nj_somerset_sheriff import scrape_somerset_sheriff_sales

    records = await scrape_somerset_sheriff_sales(
        include_bankruptcy=include_bankruptcy,
        max_records=max_records,
        headless=True,
    )
    print(f"Somerset sheriff: {len(records)} records")
    return {"records": len(records)}


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


@app.local_entrypoint()
def main():
    """Local entrypoint for `modal run modal_app.py`."""
    import asyncio

    result = nj_scrape_manual.remote()
    print(f"Completed: {result}")
