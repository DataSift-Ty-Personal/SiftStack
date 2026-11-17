"""Standardize addresses via Smarty (formerly SmartyStreets) US Street API.

Processes a batch of NoticeData records, overwrites address/city/zip with
USPS-standardized versions, and populates geocode + validation fields.

Graceful degradation: if no API keys or API errors, all notices pass through
unchanged.
"""

import logging
import time

from smartystreets_python_sdk import (
    BasicAuthCredentials,
    Batch,
    ClientBuilder,
    exceptions,
)
from smartystreets_python_sdk.us_street import Lookup as StreetLookup
from smartystreets_python_sdk.us_street.match_type import MatchType

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 100


def _build_client(auth_id: str, auth_token: str):
    """Build an authenticated Smarty US Street API client."""
    credentials = BasicAuthCredentials(auth_id, auth_token)
    return ClientBuilder(credentials).build_us_street_api_client()


def _build_lastline(notice: NoticeData) -> str:
    """Build a 'city, state zip' lastline string from notice fields."""
    parts = []
    if notice.city:
        parts.append(notice.city)
    if notice.state:
        parts.append(notice.state)
    lastline = ", ".join(parts)
    if notice.zip:
        lastline += " " + notice.zip if lastline else notice.zip
    return lastline or "TN"


def standardize_addresses(
    notices: list[NoticeData],
    auth_id: str,
    auth_token: str,
) -> list[NoticeData]:
    """Standardize addresses in-place via Smarty US Street API.

    Args:
        notices: List of NoticeData (modified in-place).
        auth_id: Smarty auth-id credential.
        auth_token: Smarty auth-token credential.

    Returns:
        The same list (modified in-place) for chaining convenience.
        On any credential/API failure, returns notices unchanged.
    """
    if not auth_id or not auth_token:
        logger.info("Smarty credentials not configured -- skipping address standardization")
        return notices

    # Filter to notices that have an address worth standardizing
    eligible = [(i, n) for i, n in enumerate(notices) if n.address.strip()]
    if not eligible:
        logger.info("No notices with addresses to standardize")
        return notices

    logger.info(
        "Standardizing %d addresses via Smarty (%d skipped -- no address)",
        len(eligible),
        len(notices) - len(eligible),
    )

    try:
        client = _build_client(auth_id, auth_token)
    except Exception as e:
        logger.error("Failed to build Smarty client: %s", e)
        return notices

    matched = 0
    failed = 0

    for batch_start in range(0, len(eligible), MAX_BATCH_SIZE):
        batch_slice = eligible[batch_start : batch_start + MAX_BATCH_SIZE]
        batch = Batch()

        for orig_idx, notice in batch_slice:
            lookup = StreetLookup()
            lookup.street = notice.address
            lookup.lastline = _build_lastline(notice)
            lookup.candidates = 1
            lookup.match = MatchType.INVALID
            lookup.input_id = str(orig_idx)
            batch.add(lookup)

        try:
            client.send_batch(batch)
        except exceptions.SmartyException as e:
            logger.error("Smarty batch API error: %s", e)
            failed += len(batch_slice)
            continue
        except Exception as e:
            logger.error("Unexpected Smarty error: %s", e)
            failed += len(batch_slice)
            continue

        # Process results
        for lookup in batch:
            candidates = lookup.result
            if not candidates:
                failed += 1
                continue

            candidate = candidates[0]
            orig_idx = int(lookup.input_id)
            notice = notices[orig_idx]

            components = candidate.components
            metadata = candidate.metadata
            analysis = candidate.analysis

            # Safety: reject non-TN results (bad match on out-of-state address)
            if components and components.state_abbreviation and components.state_abbreviation != "TN":
                logger.warning(
                    "Smarty returned %s for '%s' -- keeping original",
                    components.state_abbreviation,
                    notice.address,
                )
                failed += 1
                continue

            # Overwrite address with standardized version
            if candidate.delivery_line_1:
                notice.address = candidate.delivery_line_1

            # Overwrite city/state/zip from components
            if components:
                if components.city_name:
                    notice.city = components.city_name
                if components.state_abbreviation:
                    notice.state = components.state_abbreviation
                if components.zipcode:
                    notice.zip = components.zipcode
                if components.zipcode and components.plus4_code:
                    notice.zip_plus4 = f"{components.zipcode}-{components.plus4_code}"

            # Populate metadata fields
            if metadata:
                if metadata.latitude is not None:
                    notice.latitude = str(metadata.latitude)
                if metadata.longitude is not None:
                    notice.longitude = str(metadata.longitude)
                if metadata.rdi:
                    notice.rdi = metadata.rdi

            # Populate analysis fields
            if analysis:
                if analysis.dpv_match_code:
                    notice.dpv_match_code = analysis.dpv_match_code
                if analysis.vacant:
                    notice.vacant = analysis.vacant

            matched += 1

    logger.info(
        "Smarty standardization complete: %d matched, %d failed/no-match, %d skipped",
        matched,
        failed,
        len(notices) - len(eligible),
    )

    return notices


def _reverse_geocode(lat: str, lon: str) -> dict | None:
    """Reverse geocode lat/lon via Nominatim to get city and ZIP.

    Returns dict with 'city' and 'postcode', or None on failure.
    Nominatim rate limit: 1 request per second.
    """
    import requests

    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=json&addressdetails=1"
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "TN-Notice-Scraper/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    addr = data.get("address", {})
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or ""
    )
    postcode = addr.get("postcode", "")
    return {"city": city, "postcode": postcode}


def retry_with_geocoded_city(
    notices: list[NoticeData],
    auth_id: str,
    auth_token: str,
) -> None:
    """Retry Smarty for failed lookups using reverse-geocoded city/ZIP.

    Finds notices that have an address and lat/lon but no ZIP (Smarty failed),
    reverse geocodes the lat/lon via Nominatim to get the correct city/ZIP,
    then retries Smarty with the corrected lastline.

    Updates notices in-place.
    """
    # Find candidates: have address + lat/lon but Smarty didn't match (no zip)
    candidates = [
        (i, n) for i, n in enumerate(notices)
        if n.address.strip() and n.latitude and n.longitude and not n.zip
    ]

    if not candidates:
        logger.info("No Smarty failures with lat/lon to retry")
        return

    logger.info(
        "Reverse geocoding %d Smarty failures to get correct city/ZIP...",
        len(candidates),
    )

    # Step 1: Reverse geocode each candidate to get city/ZIP
    geocoded = 0
    for i, (orig_idx, notice) in enumerate(candidates):
        result = _reverse_geocode(notice.latitude, notice.longitude)
        if result:
            if result["postcode"]:
                notice.zip = result["postcode"]
                geocoded += 1
            if result["city"]:
                notice.city = result["city"]
        if i < len(candidates) - 1:
            time.sleep(1.1)  # Nominatim rate limit: 1 req/sec
        if (i + 1) % 20 == 0:
            logger.info("Reverse geocode progress: %d/%d", i + 1, len(candidates))

    logger.info("Reverse geocoded: %d/%d got ZIP codes", geocoded, len(candidates))

    # Step 2: Retry Smarty with the new city/ZIP for records that got geocoded
    retry = [
        (orig_idx, notices[orig_idx]) for orig_idx, n in candidates
        if n.zip  # Only retry if we got a ZIP from geocoding
    ]

    if not retry:
        logger.info("No records to retry with Smarty after geocoding")
        return

    logger.info("Retrying Smarty for %d records with geocoded city/ZIP...", len(retry))

    try:
        client = _build_client(auth_id, auth_token)
    except Exception as e:
        logger.error("Failed to build Smarty client for retry: %s", e)
        return

    matched = 0
    failed = 0

    for batch_start in range(0, len(retry), MAX_BATCH_SIZE):
        batch_slice = retry[batch_start : batch_start + MAX_BATCH_SIZE]
        batch = Batch()

        for orig_idx, notice in batch_slice:
            lookup = StreetLookup()
            lookup.street = notice.address
            lookup.lastline = _build_lastline(notice)
            lookup.candidates = 1
            lookup.match = MatchType.INVALID
            lookup.input_id = str(orig_idx)
            batch.add(lookup)

        try:
            client.send_batch(batch)
        except exceptions.SmartyException as e:
            logger.error("Smarty retry batch error: %s", e)
            failed += len(batch_slice)
            continue
        except Exception as e:
            logger.error("Unexpected Smarty retry error: %s", e)
            failed += len(batch_slice)
            continue

        for lookup in batch:
            result_candidates = lookup.result
            if not result_candidates:
                failed += 1
                continue

            candidate = result_candidates[0]
            orig_idx = int(lookup.input_id)
            notice = notices[orig_idx]

            components = candidate.components
            metadata = candidate.metadata
            analysis = candidate.analysis

            if components and components.state_abbreviation and components.state_abbreviation != "TN":
                failed += 1
                continue

            if candidate.delivery_line_1:
                notice.address = candidate.delivery_line_1
            if components:
                if components.city_name:
                    notice.city = components.city_name
                if components.state_abbreviation:
                    notice.state = components.state_abbreviation
                if components.zipcode:
                    notice.zip = components.zipcode
                if components.zipcode and components.plus4_code:
                    notice.zip_plus4 = f"{components.zipcode}-{components.plus4_code}"
            if metadata:
                if metadata.latitude is not None:
                    notice.latitude = str(metadata.latitude)
                if metadata.longitude is not None:
                    notice.longitude = str(metadata.longitude)
                if metadata.rdi:
                    notice.rdi = metadata.rdi
            if analysis:
                if analysis.dpv_match_code:
                    notice.dpv_match_code = analysis.dpv_match_code
                if analysis.vacant:
                    notice.vacant = analysis.vacant

            matched += 1

    logger.info(
        "Smarty retry complete: %d matched, %d failed",
        matched, failed,
    )
