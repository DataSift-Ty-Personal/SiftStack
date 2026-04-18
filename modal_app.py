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
    timeout=1800,  # 30 min max
    retries=modal.Retries(
        max_retries=2,
        initial_delay=60.0,
        backoff_coefficient=2.0,
    ),
    schedule=modal.Cron(SCHEDULE_CRON_UTC),
)
async def nj_weekly_scrape():
    """Scheduled NJ Lis Pendens scrape — runs every Wednesday 6 AM ET."""
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
    schedule=modal.Cron(SCHEDULE_CRON_UTC),
)
async def nj_weekly_probate_scrape():
    """Scheduled Middlesex surrogate probate scrape — runs every Wednesday 6 AM ET.

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
