"""Shared phone scoring utilities for Philadelphia pipeline.

Used by both:
  - philly_pipeline.py (embedded in the daily run after skip trace wait)
  - run_philly_phone_score.py (standalone manual re-run)

Core flow:
  1. wait_for_phones()   — poll DataSift export until skip trace populates phones
  2. run_phone_validation() — score all phones via Trestle phone_intel API
  3. upload_tags()          — upload tier tags back to DataSift
  4. score_and_tag()        — orchestrate all three steps, return full result dict
"""
from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def count_phones_in_csv(csv_path: str | Path) -> int:
    """Count records in a Phone Enrichment CSV that have ≥1 phone populated."""
    try:
        with open(csv_path, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        return sum(
            1 for r in rows
            if any(r.get(f"Phone {i}", "").strip() for i in range(1, 11))
        )
    except (OSError, csv.Error):
        return 0


def _covers(csv_path: str | Path | None, expect_addresses: set[str] | None) -> bool:
    """True when every expected property address has >=1 phone in the export."""
    if not expect_addresses:
        return True
    if not csv_path:
        return False
    try:
        with open(csv_path, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return False
    with_phones = {
        (r.get("Property address") or "").strip().upper()
        for r in rows
        if any((r.get(f"Phone {i}") or "").strip() for i in range(1, 31))
    }
    missing = {a.strip().upper() for a in expect_addresses if a.strip()} - with_phones
    if missing:
        logger.info("Export missing phones for %d/%d just-merged addresses (e.g. %s)",
                    len(missing), len(expect_addresses), sorted(missing)[:3])
    return not missing


async def export_phones(
    list_name: str | None, email: str, password: str, tag_name: str | None = None
) -> tuple[int, str | None]:
    """Export Phone Enrichment CSV for one cohort via Playwright.

    Pass tag_name for the run-cohort tag (`siftstack_YYYY-MM-DD`) or list_name for
    a list. Exactly one is needed; tag_name wins if both are given.

    Returns (phone_record_count, csv_path). Returns (0, None) on failure.
    """
    from playwright.async_api import async_playwright
    from datasift_core import login
    from datasift_uploader import export_phone_enrichment

    label = tag_name or list_name
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720}, user_agent=_UA)
        page = await ctx.new_page()
        try:
            ok = await login(page, email, password)
            if not ok:
                logger.error("DataSift login failed during phone export")
                return 0, None
            if tag_name:
                result = await export_phone_enrichment(page, tag_name=tag_name)
            else:
                result = await export_phone_enrichment(page, list_name=list_name)
        finally:
            await browser.close()

    if not result.get("success"):
        logger.warning("Export failed for %s: %s", label, result.get("message"))
        return 0, None

    csv_path = result["download_path"]
    count = count_phones_in_csv(csv_path)
    logger.info("Exported %s: %d records with phones", label, count)
    return count, csv_path


async def wait_for_phones(
    list_name: str | None,
    email: str,
    password: str,
    max_retries: int = 3,
    wait_seconds: int = 300,
    expect_addresses: set[str] | None = None,
    tag_name: str | None = None,
) -> tuple[int, str | None]:
    """Poll DataSift until skip trace populates phones or retries are exhausted.

    Returns (phone_count, csv_path). Returns (0, None) if skip trace still running.

    `expect_addresses` are the properties a merge just wrote. Without it a list that
    ALREADY has phones satisfies `count > 0` on the first export, so a merge still
    processing in DataSift's background yields a stale export: the new numbers are
    absent, never get Trestle-scored, and land as untagged "Unknown" phones.
    """
    label = tag_name or list_name
    for attempt in range(1, max_retries + 1):
        count, csv_path = await export_phones(list_name, email, password,
                                              tag_name=tag_name)
        if count > 0 and _covers(csv_path, expect_addresses):
            return count, csv_path
        if attempt < max_retries:
            logger.info(
                "No phones on %s yet (attempt %d/%d) — waiting %ds for DataSift skip trace...",
                label, attempt, max_retries, wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
        else:
            logger.warning(
                "No phones on %s after %d attempts — skip trace incomplete.",
                label, max_retries,
            )
    return 0, None


async def upload_tags(tag_csv_path: str | Path, email: str, password: str) -> dict:
    """Upload phone tier tag CSV to DataSift via Playwright."""
    from playwright.async_api import async_playwright
    from datasift_core import login
    from datasift_uploader import upload_phone_tags

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720}, user_agent=_UA)
        page = await ctx.new_page()
        try:
            ok = await login(page, email, password)
            if not ok:
                return {"success": False, "message": "DataSift login failed for tag upload"}
            result = await upload_phone_tags(page, tag_csv_path)
        finally:
            await browser.close()
    return result


async def score_and_tag(
    list_name: str | None,
    email: str,
    password: str,
    api_key: str,
    do_upload: bool = True,
    max_retries: int = 3,
    wait_seconds: int = 300,
    expect_addresses: set[str] | None = None,
    tag_name: str | None = None,
) -> dict:
    """Full phone scoring pipeline: wait → export → Trestle → upload tags.

    Scope the run with tag_name (the `siftstack_YYYY-MM-DD` run cohort) or
    list_name. If the cohort filter cannot be applied the export aborts rather
    than falling back to the whole account.

    Returns a dict with:
      phones_found   int — records with phones in the export
      phones_scored  int — unique phones scored by Trestle
      tier_counts    dict — {tier_name: count}
      tag_csv_path   str | None
      upload_ok      bool
      cost           float — est. Trestle cost
      skipped        bool — True if skip trace hadn't finished (0 phones)
      message        str
    """
    label = tag_name or list_name
    result: dict = {
        "list_name":    label,
        "phones_found": 0,
        "phones_scored": 0,
        "tier_counts":  {},
        "tag_csv_path": None,
        "upload_ok":    False,
        "cost":         0.0,
        "skipped":      False,
        "message":      "",
    }

    if not api_key:
        result["message"] = "TRESTLE_API_KEY not set — phone scoring skipped"
        logger.warning(result["message"])
        result["skipped"] = True
        return result

    # Step 1: Wait for skip trace to populate phones
    phone_count, csv_path = await wait_for_phones(
        list_name, email, password, max_retries=max_retries, wait_seconds=wait_seconds,
        expect_addresses=expect_addresses, tag_name=tag_name,
    )
    result["phones_found"] = phone_count

    if phone_count == 0 or csv_path is None:
        result["skipped"] = True
        result["message"] = f"No phones found on {label} after {max_retries} attempts"
        return result

    logger.info("%s: %d records with phones — scoring with Trestle", label, phone_count)

    # Step 2: Trestle scoring
    from phone_validator import run_phone_validation
    validation = run_phone_validation(csv_path=csv_path, api_key=api_key)

    if not validation.get("success"):
        result["message"] = f"Trestle scoring failed: {validation.get('message')}"
        logger.error(result["message"])
        return result

    result["phones_scored"] = validation["results_count"]
    result["phones_new"]    = validation.get("new_scored", validation["results_count"])
    result["tier_counts"]   = validation.get("tier_counts", {})
    result["tag_csv_path"]  = validation["tag_csv_path"]
    # Bill only NEW Trestle calls — already-scored numbers are reused for free from
    # the persistent cache (data/cache/scored_phones.json).
    result["cost"]          = result["phones_new"] * 0.015

    logger.info("%s: %d phones on list, %d newly scored, est. $%.2f",
                label, result["phones_scored"], result["phones_new"], result["cost"])

    # Step 3: Upload tier tags
    if do_upload and result["tag_csv_path"]:
        upload_result = await upload_tags(result["tag_csv_path"], email, password)
        result["upload_ok"] = upload_result.get("success", False)
        if result["upload_ok"]:
            logger.info("%s: tier tags uploaded", label)
        else:
            logger.error("%s: tag upload failed — %s", label, upload_result.get("message"))
    elif not do_upload:
        result["upload_ok"] = True   # vacuously OK when skipped by choice

    result["message"] = "OK"
    return result
