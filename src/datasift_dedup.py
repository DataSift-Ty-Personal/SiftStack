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

Dedup key
---------
Lowercased, stripped, single-spaced address with notice_type appended.
e.g. "123 Main St., Columbus, OH 43215" + probate notice
     → "123 main st columbus oh 43215|probate".

Why notice_type is part of the key
----------------------------------
The same property progresses through multiple distress events over its
lifecycle — probate filed, then 60 days later a foreclosure case, then a
sheriff sale, then a redemption window. Each is a legitimately new lead
with different decision-maker, different cadence, different conversation.
A pure address key collapsed all of those into a single "uploaded once,
silenced forever" state — every progression after the first was dropped.

Pre-2026-06-01 the seed file (`src/data/seed_uploaded_addresses.json`,
176 entries) and the persistent KV store (`siftstack-dedup`, ~461 net
adds) hold *plain address strings* without the |notice_type suffix. Those
old entries become inert under the new keying — they never match a
new-format key — and are harmless to keep around. Resetting the KV store
is recommended for cleanliness but not required for correctness.
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
# CRITICAL: must use a NAMED KV store so the set persists across Apify runs.
# Actor.open_key_value_store() (default, no name) opens the DEFAULT store
# which is unique per-run — the dedup set was being wiped every morning.
# A named store is shared across all runs of the Actor account.
APIFY_KV_STORE_NAME = "siftstack-dedup"
LOCAL_FALLBACK_PATH = Path("output") / "uploaded_addresses.json"
# Bootstrap seed: ships with the repo so day-1 Apify runs (where the KV store
# is empty) still have a baseline dedup set. After the first successful run,
# the KV store is populated and the seed is no longer consulted.
SEED_PATH = Path(__file__).resolve().parent / "data" / "seed_uploaded_addresses.json"

_PUNCT_RE = re.compile(r"[.,;:#]")
_WS_RE = re.compile(r"\s+")


def normalize_address(notice: NoticeData) -> str | None:
    """Return a normalized dedup key, or None if address is unusable.

    Format: "{street} {city} {state} {zip}|{notice_type}" all lowercased,
    single-spaced, punctuation stripped. Returns None if any of
    street/city/zip missing.

    The |notice_type suffix is what lets the same property re-appear as a
    new lead type — see module docstring "Why notice_type is part of the
    key" for the lifecycle rationale.

    Records with no notice_type fall back to "{...address...}|" (empty
    suffix), distinguishable from pre-fix plain-address keys.
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
    notice_type = (notice.notice_type or "").strip().lower()
    return f"{raw}|{notice_type}"


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


def _load_seed() -> set[str]:
    """Load the bundled seed file (one-time bootstrap baseline)."""
    if not SEED_PATH.exists():
        return set()
    try:
        with SEED_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        if isinstance(data, dict):
            return set(data.keys())
    except Exception as e:
        logger.warning("Seed file load failed: %s", e)
    return set()


async def _load_from_apify() -> set[str]:
    try:
        from apify import Actor
        # NAMED store — persists across runs (the per-run default store wipes daily)
        kvs = await Actor.open_key_value_store(name=APIFY_KV_STORE_NAME)
        stored = await kvs.get_value(KV_STORE_KEY)
        if isinstance(stored, list) and stored:
            return set(stored)
        if isinstance(stored, dict) and stored:
            return set(stored.keys())
        # KV empty — fall back to seed (first-run bootstrap)
        seed = _load_seed()
        if seed:
            logger.info("Apify KV %s empty — bootstrapped from seed (%d addresses)",
                        KV_STORE_KEY, len(seed))
        return seed
    except Exception as e:
        logger.warning("Apify KV load failed for %s: %s — starting with empty set", KV_STORE_KEY, e)
        return _load_seed()


async def _save_to_apify(addresses: set[str]) -> None:
    try:
        from apify import Actor
        # Same NAMED store as load — must match for cross-run persistence
        kvs = await Actor.open_key_value_store(name=APIFY_KV_STORE_NAME)
        await kvs.set_value(KV_STORE_KEY, sorted(addresses))
        logger.info("Apify KV (named=%s): persisted %d addresses to %s",
                    APIFY_KV_STORE_NAME, len(addresses), KV_STORE_KEY)
    except Exception as e:
        logger.warning("Apify KV save failed for %s: %s", KV_STORE_KEY, e)


def _load_from_local() -> set[str]:
    if not LOCAL_FALLBACK_PATH.exists():
        # No local file yet — bootstrap from seed
        return _load_seed()
    try:
        with LOCAL_FALLBACK_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return set(data)
        if isinstance(data, dict) and data:
            return set(data.keys())
    except Exception as e:
        logger.warning("Local dedup load failed: %s — falling back to seed", e)
    return _load_seed()


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
