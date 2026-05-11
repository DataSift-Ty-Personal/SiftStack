"""Tracerfy skip-trace source — `/v1/api/trace/lookup/` (sync per-address).

Slice 2 primary skip-trace finder. Given a residential address, returns
the persons living there with phones (number/type/dnc/carrier/rank),
emails, mailing address, age, and the `deceased` / `property_owner` /
`litigator` flags Tracerfy populates for free.

Billing: 5 credits per HIT = $0.10. Misses are free. The caller
increments `CostBreakdown.tracerfy` based on the returned `cost_usd`,
which is derived from `credits_deducted` × $0.02/credit.

Failure mode: 60-second timeout, three retries with exponential backoff
via `_safe_call`. If the API hangs or all retries fail, returns a
miss-shaped TracerfyResult so the orchestrator's CBC fallback gets a
chance. Full request + response are logged on failure for diagnostics.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

from deep_prospecting._utils import _safe_call

logger = logging.getLogger(__name__)


_BASE_URL = "https://tracerfy.com/v1/api"
_TIMEOUT_SECONDS = 60.0
_CREDIT_USD = 0.02  # 1 credit = $0.02


# ── Response dataclasses ────────────────────────────────────────────────


@dataclass
class TracerfyPhone:
    number: str
    type: str  # "Mobile" | "Landline" | (anything else Tracerfy returns)
    dnc: bool
    carrier: str
    rank: int


@dataclass
class TracerfyEmail:
    email: str
    rank: int


@dataclass
class TracerfyMailingAddress:
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.street or self.city or self.state or self.zip)


@dataclass
class TracerfyPerson:
    first_name: str
    last_name: str
    full_name: str
    dob: str | None
    age: int | None
    deceased: bool
    property_owner: bool
    litigator: bool
    mailing_address: TracerfyMailingAddress
    phones: list[TracerfyPhone] = field(default_factory=list)
    emails: list[TracerfyEmail] = field(default_factory=list)


@dataclass
class TracerfyResult:
    """Result of one /trace/lookup/ call.

    `hit` mirrors the upstream `hit` boolean; misses still return a
    valid TracerfyResult with `persons=[]` and `cost_usd=0.0` so the
    caller doesn't need to check for None.

    `status` ∈ {"HIT", "EMPTY", "ERROR", "BLOCKED"} for SourceState bookkeeping.
    """
    address: str
    city: str
    state: str
    zip: str | None
    find_owner: bool
    hit: bool
    persons_count: int
    credits_deducted: int
    cost_usd: float
    persons: list[TracerfyPerson] = field(default_factory=list)
    status: str = "EMPTY"
    error: str | None = None  # populated on ERROR for diagnostics


# ── Parsers ─────────────────────────────────────────────────────────────


def _parse_phone(d: dict) -> TracerfyPhone:
    return TracerfyPhone(
        number=str(d.get("number") or "").strip(),
        type=str(d.get("type") or "").strip() or "Unknown",
        dnc=bool(d.get("dnc")),
        carrier=str(d.get("carrier") or "").strip(),
        rank=int(d.get("rank") or 0),
    )


def _parse_email(d: dict) -> TracerfyEmail:
    return TracerfyEmail(
        email=str(d.get("email") or "").strip(),
        rank=int(d.get("rank") or 0),
    )


def _parse_mailing_address(d: dict | None) -> TracerfyMailingAddress:
    if not d:
        return TracerfyMailingAddress()
    return TracerfyMailingAddress(
        street=str(d.get("street") or "").strip(),
        city=str(d.get("city") or "").strip(),
        state=str(d.get("state") or "").strip(),
        zip=str(d.get("zip") or "").strip(),
    )


def _parse_person(d: dict) -> TracerfyPerson:
    # Age is documented as string in some examples; coerce gracefully.
    age_raw = d.get("age")
    age: int | None = None
    if age_raw is not None and age_raw != "":
        try:
            age = int(age_raw)
        except (TypeError, ValueError):
            age = None
    return TracerfyPerson(
        first_name=str(d.get("first_name") or "").strip(),
        last_name=str(d.get("last_name") or "").strip(),
        full_name=str(d.get("full_name") or "").strip(),
        dob=str(d.get("dob") or "").strip() or None,
        age=age,
        deceased=bool(d.get("deceased")),
        property_owner=bool(d.get("property_owner")),
        litigator=bool(d.get("litigator")),
        mailing_address=_parse_mailing_address(d.get("mailing_address")),
        phones=[_parse_phone(p) for p in (d.get("phones") or [])],
        emails=[_parse_email(e) for e in (d.get("emails") or [])],
    )


def _parse_response(
    payload: dict, *, address: str, city: str, state: str, zip: str | None,
    find_owner: bool,
) -> TracerfyResult:
    hit = bool(payload.get("hit"))
    credits = int(payload.get("credits_deducted") or 0)
    return TracerfyResult(
        address=str(payload.get("address") or address),
        city=str(payload.get("city") or city),
        state=str(payload.get("state") or state),
        zip=str(payload.get("zip") or (zip or "")) or None,
        find_owner=bool(payload.get("find_owner", find_owner)),
        hit=hit,
        persons_count=int(payload.get("persons_count") or 0),
        credits_deducted=credits,
        cost_usd=round(credits * _CREDIT_USD, 4),
        persons=[_parse_person(p) for p in (payload.get("persons") or [])],
        status="HIT" if hit else "EMPTY",
    )


# ── HTTP plumbing ───────────────────────────────────────────────────────


def _sync_post_lookup(
    api_key: str,
    *,
    address: str,
    city: str,
    state: str,
    zip: str | None,
    find_owner: bool,
    first_name: str | None,
    last_name: str | None,
) -> dict[str, Any]:
    """Blocking HTTP. Caller wraps in asyncio.to_thread."""
    body: dict[str, Any] = {
        "address": address,
        "city": city,
        "state": state,
        "find_owner": find_owner,
    }
    if zip:
        body["zip"] = zip
    if not find_owner:
        # Required when we ask Tracerfy to skip-trace a named person at
        # the address rather than identify the owner.
        if first_name:
            body["first_name"] = first_name
        if last_name:
            body["last_name"] = last_name

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    resp = requests.post(
        f"{_BASE_URL}/trace/lookup/",
        json=body,
        headers=headers,
        timeout=_TIMEOUT_SECONDS,
    )
    # Treat 4xx as "well-formed miss / bad input" so the orchestrator's
    # fallback path runs; only 5xx and timeouts hit the retry loop.
    if resp.status_code >= 500:
        logger.warning(
            "tracerfy 5xx: status=%d body_len=%d req=%s",
            resp.status_code, len(resp.text or ""), body,
        )
        resp.raise_for_status()
    if resp.status_code >= 400:
        logger.info(
            "tracerfy non-200: status=%d body=%s req=%s",
            resp.status_code, (resp.text or "")[:300], body,
        )
        return {"hit": False, "persons": [], "credits_deducted": 0}
    try:
        return resp.json()
    except ValueError as e:
        logger.warning("tracerfy non-JSON: %s body=%s", e, (resp.text or "")[:300])
        return {"hit": False, "persons": [], "credits_deducted": 0}


# ── Public entry point ─────────────────────────────────────────────────


async def search(
    address: str,
    city: str,
    state: str,
    zip: str | None = None,
    find_owner: bool = True,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
) -> TracerfyResult:
    """Skip-trace one residential address via Tracerfy.

    Returns a TracerfyResult even on miss / error — caller branches on
    `result.hit` and `result.status`. Cost accounting uses `result.cost_usd`
    (derived from `credits_deducted` × $0.02/credit; $0 on miss).

    Args:
        address: street line (e.g. "145 E 22nd St Apt 6C")
        city: city name
        state: 2-letter state code
        zip: ZIP (strongly recommended for ambiguity resolution)
        find_owner: True = let Tracerfy identify the owner; False = trace
            the named first_name/last_name at this address
        first_name / last_name: required when find_owner=False
    """
    api_key = os.environ.get("TRACERFY_API_KEY", "")
    if not api_key:
        logger.warning("tracerfy: TRACERFY_API_KEY missing — returning miss")
        return TracerfyResult(
            address=address, city=city, state=state, zip=zip,
            find_owner=find_owner, hit=False, persons_count=0,
            credits_deducted=0, cost_usd=0.0,
            status="BLOCKED", error="TRACERFY_API_KEY not set",
        )

    if not find_owner and not (first_name or last_name):
        return TracerfyResult(
            address=address, city=city, state=state, zip=zip,
            find_owner=find_owner, hit=False, persons_count=0,
            credits_deducted=0, cost_usd=0.0,
            status="ERROR",
            error="find_owner=False requires first_name or last_name",
        )

    async def _one_attempt() -> dict[str, Any] | None:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _sync_post_lookup,
                api_key,
                address=address, city=city, state=state, zip=zip,
                find_owner=find_owner,
                first_name=first_name, last_name=last_name,
            ),
            timeout=_TIMEOUT_SECONDS + 5.0,
        )

    payload = await _safe_call(
        _one_attempt, name=f"tracerfy.lookup[{address}]",
        retries=3, backoff=2.0,
    )
    if payload is None:
        return TracerfyResult(
            address=address, city=city, state=state, zip=zip,
            find_owner=find_owner, hit=False, persons_count=0,
            credits_deducted=0, cost_usd=0.0,
            status="ERROR",
            error="all retries failed (timeout or 5xx)",
        )

    return _parse_response(
        payload, address=address, city=city, state=state, zip=zip,
        find_owner=find_owner,
    )
