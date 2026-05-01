"""Pre-upload dedup against persisted set of already-uploaded addresses.

Background
----------
The daily Apify run was re-uploading records that already exist in DataSift —
including ones already marked Sold/Closed. Root cause: the local
`output/master_ledger_*.csv` files used for cross-run dedup don't survive
between Apify runs (the filesystem is wiped). Result: every day, every
historical record looks new, gets re-uploaded, and accumulates list cruft.

This module provides a persistent address set backed by:
  1. Apify Key-Value Store (when APIFY_IS_AT_HOME is set) — survives runs
  2. Local JSON file at output/uploaded_addresses.json — fallback for CLI mode

Workflow
--------
- Before upload: filter out NoticeData records whose normalized address is in
  the set. Returns (fresh_records, duplicates).
- After successful upload: append the uploaded addresses to the set + persist.

Address normalization
---------------------
Lowercased, stripped, single-spaced, with the state appended. e.g.
"123  Main St., Columbus, OH 43215" → "123 main st columbus oh 43215".
This matches addresses across casing/punctuation but stays distinct between
truly different properties (different ZIPs / streets).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable

from models import NoticeData

logger = logging.getLogger(__name__)

KV_STORE_KEY = "uploaded_addresses"
LOCAL_FALLBACK_PATH = Path("output") / "uploaded_addresses.json"

_PUNCT_RE = re.compile(r"[.,;:#]")
_WS_RE = re.compile(r"\s+")


def normalize_address(notice: NoticeData) -> str | None:
    """Return a normalized address key, or None if address is unusable.

    Format: "{street} {city} {state} {zip}" all lowercased, single-spaced,
    punctuation stripped. Returns None if any of street/city/zip missing.
    """
    street = (notice.address or "").strip()
    city = (notice.city or "").strip()
    state = (notice.state or "").strip()
    zip_code = (notice.zip or "").strip()
    if not street or not city or not zip_code:
        return None
    raw = f"{street} {city} {state} {zip_code}"
    raw = _PUNCT_RE.sub(" ", raw)
    raw = _WS_RE.sub(" ", raw).strip().lower()
    return raw


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


async def _load_from_apify() -> set[str]:
    try:
        from apify import Actor
        kvs = await Actor.open_key_value_store()
        stored = await kvs.get_value(KV_STORE_KEY)
        if isinstance(stored, list):
            return set(stored)
        if isinstance(stored, dict):
            return set(stored.keys())
        return set()
    except Exception as e:
        logger.warning("Apify KV load failed for %s: %s — starting with empty set", KV_STORE_KEY, e)
        return set()


async def _save_to_apify(addresses: set[str]) -> None:
    try:
        from apify import Actor
        kvs = await Actor.open_key_value_store()
        await kvs.set_value(KV_STORE_KEY, sorted(addresses))
        logger.info("Apify KV: persisted %d addresses to %s", len(addresses), KV_STORE_KEY)
    except Exception as e:
        logger.warning("Apify KV save failed for %s: %s", KV_STORE_KEY, e)


def _load_from_local() -> set[str]:
    if not LOCAL_FALLBACK_PATH.exists():
        return set()
    try:
        with LOCAL_FALLBACK_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        if isinstance(data, dict):
            return set(data.keys())
    except Exception as e:
        logger.warning("Local dedup load failed: %s — starting with empty set", e)
    return set()


def _save_to_local(addresses: set[str]) -> None:
    try:
        LOCAL_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCAL_FALLBACK_PATH.open("w", encoding="utf-8") as f:
            json.dump(sorted(addresses), f)
        logger.info("Local dedup: persisted %d addresses to %s", len(addresses), LOCAL_FALLBACK_PATH)
    except Exception as e:
        logger.warning("Local dedup save failed: %s", e)


async def load_uploaded_addresses() -> set[str]:
    """Load the persisted set of already-uploaded addresses."""
    if _is_apify():
        return await _load_from_apify()
    return _load_from_local()


async def save_uploaded_addresses(addresses: set[str]) -> None:
    """Persist the set of uploaded addresses."""
    if _is_apify():
        await _save_to_apify(addresses)
    else:
        _save_to_local(addresses)


async def filter_already_uploaded(
    notices: Iterable[NoticeData],
) -> tuple[list[NoticeData], list[NoticeData]]:
    """Split notices into (fresh, duplicates) based on persisted address set.

    Records with no usable address are treated as fresh (caller logs/handles).
    """
    notices_list = list(notices)
    seen = await load_uploaded_addresses()
    if not seen:
        logger.info("Pre-upload dedup: no prior uploads on record — all %d are fresh", len(notices_list))
        return notices_list, []

    fresh: list[NoticeData] = []
    dupes: list[NoticeData] = []
    for n in notices_list:
        key = normalize_address(n)
        if key is None:
            fresh.append(n)  # un-keyable — let downstream decide
            continue
        if key in seen:
            dupes.append(n)
        else:
            fresh.append(n)

    logger.info(
        "Pre-upload dedup: %d fresh, %d already-uploaded (skipped), %d total in set",
        len(fresh), len(dupes), len(seen),
    )
    return fresh, dupes


async def append_uploaded(notices: Iterable[NoticeData]) -> int:
    """Add successfully-uploaded notices to the persisted set. Returns count added."""
    notices_list = list(notices)
    if not notices_list:
        return 0
    seen = await load_uploaded_addresses()
    added = 0
    for n in notices_list:
        key = normalize_address(n)
        if key and key not in seen:
            seen.add(key)
            added += 1
    if added:
        await save_uploaded_addresses(seen)
    return added
