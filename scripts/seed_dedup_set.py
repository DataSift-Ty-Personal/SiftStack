"""One-time bootstrap: seed the persistent dedup set from existing data.

Why this exists
---------------
The new datasift_dedup module persists addresses across runs (Apify KV store
on cloud, local JSON in CLI). On day 1 the set is empty, so dedup catches
nothing and the daily run re-uploads everything that's already in DataSift.

This script seeds the set from any sources we have:
  1. Any datasift_upload_*.csv in output/ — addresses we know we've uploaded
  2. Any *.csv with Property Street Address column

The result is written to output/uploaded_addresses.json, which the dedup
module reads as the local fallback. For Apify, also push to the KV store
(see --push-apify flag).

Usage
-----
    PYTHONPATH=src python scripts/seed_dedup_set.py
    PYTHONPATH=src python scripts/seed_dedup_set.py --include "output/*.csv"
    PYTHONPATH=src python scripts/seed_dedup_set.py --push-apify
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_dedup import LOCAL_FALLBACK_PATH, KV_STORE_KEY  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_PUNCT = re.compile(r"[.,;:#]")
_WS = re.compile(r"\s+")


def _normalize(street: str, city: str, state: str, zip_code: str) -> str | None:
    street = (street or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    zip_code = (zip_code or "").strip()
    if not street or not city or not zip_code:
        return None
    raw = f"{street} {city} {state} {zip_code}"
    raw = _PUNCT.sub(" ", raw)
    raw = _WS.sub(" ", raw).strip().lower()
    return raw


def _collect_from_csv(path: Path) -> set[str]:
    addresses: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            # Try DataSift-formatted column names first
            street_keys = ["Property Street Address", "address", "Address", "property_street"]
            city_keys = ["Property City", "city", "City"]
            state_keys = ["Property State", "state", "State"]
            zip_keys = ["Property ZIP Code", "zip", "ZIP", "Zip"]
            for row in reader:
                street = next((row[k] for k in street_keys if k in row and row[k]), "")
                city = next((row[k] for k in city_keys if k in row and row[k]), "")
                state = next((row[k] for k in state_keys if k in row and row[k]), "")
                zip_code = next((row[k] for k in zip_keys if k in row and row[k]), "")
                key = _normalize(street, city, state, zip_code)
                if key:
                    addresses.add(key)
    except Exception as e:
        logger.warning("Skip %s: %s", path, e)
    return addresses


async def _push_to_apify(addresses: set[str]) -> None:
    try:
        from apify_client import ApifyClientAsync
        import os
        token = os.environ.get("APIFY_TOKEN", "")
        if not token:
            logger.error("--push-apify requires APIFY_TOKEN env var")
            return
        client = ApifyClientAsync(token)
        # User must specify the Actor's KV store ID (default named: same as Actor name)
        store_name = os.environ.get("APIFY_KV_STORE_NAME", "siftstack-actor-default")
        store = await client.key_value_stores().get_or_create(name=store_name)
        store_client = client.key_value_store(store["id"])
        await store_client.set_record(KV_STORE_KEY, sorted(addresses))
        logger.info("Pushed %d addresses to Apify KV store %s key %s",
                    len(addresses), store_name, KV_STORE_KEY)
    except Exception as e:
        logger.error("Apify push failed: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed dedup set from existing CSVs")
    parser.add_argument(
        "--include", action="append", default=[],
        help="Glob pattern to scan (repeatable). Defaults to output/datasift_upload_*.csv + output/*.csv",
    )
    parser.add_argument(
        "--push-apify", action="store_true",
        help="Also push the resulting set to Apify KV store (requires APIFY_TOKEN env var)",
    )
    args = parser.parse_args()

    patterns = args.include or [
        "output/datasift_upload_*.csv",
        "output/test_oh_*.csv",
    ]

    all_addresses: set[str] = set()
    files_seen = 0
    for pat in patterns:
        for path_str in glob.glob(pat):
            path = Path(path_str)
            if not path.exists():
                continue
            addrs = _collect_from_csv(path)
            logger.info("  + %d addresses from %s", len(addrs), path.name)
            all_addresses |= addrs
            files_seen += 1

    if not all_addresses:
        logger.warning("No addresses collected. Patterns: %s", patterns)
        return 1

    LOCAL_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_FALLBACK_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(all_addresses), f)

    logger.info(
        "Seeded %d unique addresses from %d files into %s",
        len(all_addresses), files_seen, LOCAL_FALLBACK_PATH,
    )

    if args.push_apify:
        asyncio.run(_push_to_apify(all_addresses))

    return 0


if __name__ == "__main__":
    sys.exit(main())
