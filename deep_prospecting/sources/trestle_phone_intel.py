"""Trestle Phone Intel — /3.0/phone_intel (phone scoring + line-type).

Slice 2's post-skip-trace scorer. Given an E.164 (or 10-digit US) phone,
returns the carrier, line type, activity score, prepaid flag, and
validity. Phase Trestle (step 5) loops every phone in the pack (existing
row phones + newly added) through this and folds the result into the
Phone model + Phone Tags column.

Cost: $0.015/call flat. No litigator_checks add-on per Slice 2 cost
decision — Tracerfy already returns a person-level `litigator` boolean
for free, and Slice 2's dialer workflow doesn't require per-phone TCPA
grading.

Failure mode: 30s timeout (Trestle's lookup-table API is fast — a slow
call is a signal, not a normal case). Three retries with backoff via
`_safe_call`. On exhaustion, returns a typed miss (`status="ERROR"`)
so Phase Trestle can leave the phone's existing fields untouched
(operator-friendly: never overwrite known data with a question mark).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

from deep_prospecting._utils import _safe_call

logger = logging.getLogger(__name__)


_BASE_URL = "https://api.trestleiq.com/3.0/phone_intel"
_TIMEOUT_SECONDS = 30.0
_CALL_COST_USD = 0.015


# Trestle's line_type enum, mirrored as strings here so the consumer
# doesn't need to import this module to compare. Phase Trestle maps
# these → the PhoneType Literal on `Phone` (MOBILE / LANDLINE / VOIP /
# UNKNOWN) in step 5.
LINE_TYPE_VALUES = {
    "Landline", "Mobile",
    "FixedVOIP", "NonFixedVOIP",
    "Premium", "TollFree", "Voicemail", "Other",
}


@dataclass
class TrestleIntelResult:
    """One Phone Intel response.

    `status` ∈ {"HIT", "EMPTY", "ERROR", "BLOCKED"}:
      HIT     — Trestle returned a normal response (even if is_valid=False)
      EMPTY   — Trestle returned 200 but body was unparseable / empty
      ERROR   — all retries failed (timeout / 5xx / network)
      BLOCKED — missing API key or 403 (insufficient access)

    `cost_usd` is set to $0.015 ONLY when the call hit the wire (HIT or
    EMPTY). BLOCKED / ERROR are free.
    """
    phone: str
    is_valid: bool | None
    activity_score: int | None  # 0..100
    line_type: str | None       # one of LINE_TYPE_VALUES, or None
    carrier: str | None
    is_prepaid: bool | None
    country_calling_code: str | None
    status: str
    cost_usd: float
    error: str | None = None
    raw: dict[str, Any] | None = None  # full response for diagnostics


# ── HTTP plumbing ───────────────────────────────────────────────────────


def _sync_get_intel(api_key: str, *, phone: str) -> tuple[int, dict[str, Any] | None, str]:
    """Blocking HTTP. Returns (status_code, parsed_json_or_none, raw_text)."""
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
    }
    params = {"phone": phone}
    resp = requests.get(
        _BASE_URL, params=params, headers=headers,
        timeout=_TIMEOUT_SECONDS,
    )
    # 5xx triggers retry; 4xx is a well-formed answer (bad input, no auth).
    if resp.status_code >= 500:
        logger.warning(
            "trestle 5xx: status=%d phone=%s body_len=%d",
            resp.status_code, phone, len(resp.text or ""),
        )
        resp.raise_for_status()
    body = resp.text or ""
    if resp.status_code >= 400:
        logger.info(
            "trestle non-200: status=%d phone=%s body=%s",
            resp.status_code, phone, body[:300],
        )
        return resp.status_code, None, body
    try:
        return resp.status_code, resp.json(), body
    except ValueError as e:
        logger.warning("trestle non-JSON: %s body=%s", e, body[:300])
        return resp.status_code, None, body


# ── Parser ──────────────────────────────────────────────────────────────


def _coerce_activity(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        score = int(raw)
    except (TypeError, ValueError):
        return None
    if 0 <= score <= 100:
        return score
    return None


def _coerce_line_type(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    return s if s in LINE_TYPE_VALUES else s or None


def _parse_payload(phone: str, payload: dict[str, Any]) -> TrestleIntelResult:
    return TrestleIntelResult(
        phone=phone,
        is_valid=(bool(payload["is_valid"]) if "is_valid" in payload else None),
        activity_score=_coerce_activity(payload.get("activity_score")),
        line_type=_coerce_line_type(payload.get("line_type")),
        carrier=(str(payload.get("carrier") or "").strip() or None),
        is_prepaid=(
            bool(payload["is_prepaid"]) if "is_prepaid" in payload else None
        ),
        country_calling_code=(
            str(payload.get("country_calling_code") or "").strip() or None
        ),
        status="HIT",
        cost_usd=_CALL_COST_USD,
        raw=payload,
    )


# ── Public entry point ─────────────────────────────────────────────────


async def score(phone: str) -> TrestleIntelResult:
    """Run /3.0/phone_intel on one phone.

    Accepts E.164 ("+12125551234"), 10-digit US ("2125551234"), or
    1+10-digit ("12125551234"). Trestle handles all three formats with
    `phone.country_hint` defaulting to +1 (US).
    """
    api_key = os.environ.get("TRESTLE_API_KEY", "")
    if not api_key:
        logger.warning("trestle: TRESTLE_API_KEY missing — returning BLOCKED")
        return TrestleIntelResult(
            phone=phone, is_valid=None, activity_score=None,
            line_type=None, carrier=None, is_prepaid=None,
            country_calling_code=None,
            status="BLOCKED", cost_usd=0.0,
            error="TRESTLE_API_KEY not set",
        )

    if not phone or not phone.strip():
        return TrestleIntelResult(
            phone=phone, is_valid=None, activity_score=None,
            line_type=None, carrier=None, is_prepaid=None,
            country_calling_code=None,
            status="ERROR", cost_usd=0.0,
            error="empty phone",
        )

    async def _one_attempt() -> tuple[int, dict[str, Any] | None, str] | None:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_get_intel, api_key, phone=phone),
            timeout=_TIMEOUT_SECONDS + 5.0,
        )

    outcome = await _safe_call(
        _one_attempt, name=f"trestle.phone_intel[{phone}]",
        retries=3, backoff=2.0,
    )
    if outcome is None:
        return TrestleIntelResult(
            phone=phone, is_valid=None, activity_score=None,
            line_type=None, carrier=None, is_prepaid=None,
            country_calling_code=None,
            status="ERROR", cost_usd=0.0,
            error="all retries failed (timeout or 5xx)",
        )

    status_code, payload, raw_body = outcome
    if status_code == 403:
        return TrestleIntelResult(
            phone=phone, is_valid=None, activity_score=None,
            line_type=None, carrier=None, is_prepaid=None,
            country_calling_code=None,
            status="BLOCKED", cost_usd=0.0,
            error=f"403 forbidden: {raw_body[:120]}",
        )
    if status_code >= 400:
        # 4xx is a well-formed "we charged you nothing" outcome —
        # treat as ERROR for the caller without claiming spend.
        return TrestleIntelResult(
            phone=phone, is_valid=None, activity_score=None,
            line_type=None, carrier=None, is_prepaid=None,
            country_calling_code=None,
            status="ERROR", cost_usd=0.0,
            error=f"HTTP {status_code}: {raw_body[:120]}",
        )
    if payload is None:
        # 200 but unparseable — we got charged but got nothing back.
        # Bill it so reconciliation lines up with the Trestle dashboard.
        return TrestleIntelResult(
            phone=phone, is_valid=None, activity_score=None,
            line_type=None, carrier=None, is_prepaid=None,
            country_calling_code=None,
            status="EMPTY", cost_usd=_CALL_COST_USD,
            error="200 OK but non-JSON / empty body",
        )

    return _parse_payload(phone, payload)
