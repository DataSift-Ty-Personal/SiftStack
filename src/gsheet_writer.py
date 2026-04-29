"""Append SiftStack daily summary rows to a Google Sheet via Apps Script webhook.

The webhook is deployed from `scripts/gsheet_apps_script.js` (paste-into-Sheet,
deploy as web app). The deploy URL goes into `.env` as `GSHEET_WEBHOOK_URL`.

This module is called at the end of each daily run alongside Slack notify.
Failures are non-fatal — we log a warning and continue. The CSV at
`output/daily_summary.csv` is the canonical local record; the Sheet is
just a convenience surface for at-a-glance browsing.

Why Apps Script vs Google service account:
  - 5-min setup (paste + deploy) vs 30-min service-account ritual
  - No JSON keys to manage, rotate, or leak
  - Daily-once volume is way under Apps Script rate limits
  - Sheet ownership stays clean — Aaron's account owns it, no service-
    account email cluttering the share list
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds


def append_summary_to_gsheet(summary: dict[str, Any]) -> bool:
    """POST one summary row to the Google Sheet webhook.

    Returns True on success, False on any failure (logged as warning).
    Never raises — the daily run must not abort because the Sheet append failed.
    """
    url = (config.GSHEET_WEBHOOK_URL or "").strip()
    if not url:
        logger.info("GSHEET_WEBHOOK_URL not set — skipping Google Sheet append")
        return False

    try:
        resp = requests.post(
            url,
            data=json.dumps(summary),
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
            if body.get("status") == "ok":
                logger.info("Google Sheet append OK — row %s, %s",
                            body.get("row", "?"), body.get("appended", ""))
                return True
            logger.warning("Google Sheet returned non-ok status: %s", body)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Google Sheet response not JSON: %s", resp.text[:200])
    except requests.exceptions.Timeout:
        logger.warning("Google Sheet append timed out after %ds", REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.warning("Google Sheet append failed: %s", e)
    except Exception as e:
        logger.warning("Google Sheet append unexpected error: %s", e)

    return False
