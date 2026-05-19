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


# ── Pre-flight credit check ───────────────────────────────────────────────
#
# /v1/api/analytics/ is free — call it before kicking off a batch to confirm
# enough credits to finish the run. Prevents the "silent 402-on-every-call"
# scenario surfaced during the Week 21 cleanup, where the batch ran to
# completion looking healthy but every Tracerfy lookup returned 0 results
# because the account was drained mid-run.

# How many credits Tracerfy charges per Instant Trace lookup. Quoted in the
# 402 error body — see Week 21 logs: "Instant trace requires 5 credits per
# lookup". If Tracerfy changes pricing, update here.
CREDITS_PER_LOOKUP = 5


@dataclass
class CreditBalanceResult:
    """Outcome of a /v1/api/analytics/ probe.

    `balance` is None when the probe itself failed (missing key, network
    error, unexpected response shape) — in that case the caller should
    decide policy (fail-open vs fail-closed). `error` carries a one-line
    diagnostic for surfacing to the operator.
    """
    balance: int | None
    error: str | None = None


def get_credit_balance() -> CreditBalanceResult:
    """Fetch current Tracerfy credit balance (free, no credits consumed)."""
    api_key = os.environ.get("TRACERFY_API_KEY", "")
    if not api_key:
        return CreditBalanceResult(balance=None, error="TRACERFY_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            f"{_BASE_URL}/analytics/", headers=headers, timeout=_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        return CreditBalanceResult(balance=None, error=f"network error: {e}")

    if resp.status_code >= 400:
        return CreditBalanceResult(
            balance=None,
            error=f"HTTP {resp.status_code}: {(resp.text or '')[:200]}",
        )
    try:
        data = resp.json()
    except ValueError as e:
        return CreditBalanceResult(balance=None, error=f"non-JSON response: {e}")

    # Tolerant lookup — Tracerfy hasn't published a stable analytics schema,
    # so probe common field names and fall back to scanning the response
    # for an integer-valued key that contains 'credit' or 'balance'.
    for key in ("credits", "balance", "current_balance", "remaining_credits", "credits_remaining"):
        v = data.get(key)
        if isinstance(v, (int, float)):
            return CreditBalanceResult(balance=int(v))
    # Some endpoints nest the value: {"account": {"credits": 100}} or similar.
    for parent_key in ("account", "user", "data"):
        nested = data.get(parent_key)
        if isinstance(nested, dict):
            for k in ("credits", "balance", "current_balance", "remaining_credits"):
                v = nested.get(k)
                if isinstance(v, (int, float)):
                    return CreditBalanceResult(balance=int(v))
    return CreditBalanceResult(
        balance=None,
        error=f"could not locate credit balance in response (keys: {sorted(data.keys())})",
    )


def preflight_check(batch_size: int, buffer: float = 1.2) -> tuple[bool, str]:
    """Block batches that don't have enough Tracerfy credits to complete.

    Computes required_credits = batch_size × CREDITS_PER_LOOKUP × buffer
    (default 20% buffer to absorb retries / unanticipated extra calls).

    Returns (ok, message). On `ok=False`, message is the operator-facing
    warning the caller should print before aborting. On `ok=True`, message
    is a one-line summary suitable for logging at INFO level.

    Fail-open policy when the balance probe itself fails: emit a warning
    and return ok=True so the batch can still run. The alternative (hard
    fail on missing analytics) would block legitimate runs whenever
    Tracerfy's analytics endpoint is briefly down.
    """
    required = int(batch_size * CREDITS_PER_LOOKUP * buffer)
    result = get_credit_balance()
    if result.balance is None:
        return True, (
            f"WARNING: could not probe Tracerfy credit balance "
            f"({result.error}); proceeding with batch_size={batch_size}, "
            f"required ≈ {required} credits"
        )
    if result.balance < required:
        msg = (
            "ERROR: Tracerfy credits insufficient for this batch.\n"
            f"  Current balance: {result.balance} credits\n"
            f"  Estimated need: {required} credits "
            f"({batch_size} records × {CREDITS_PER_LOOKUP} credits, ×{buffer} buffer)\n"
            f"  Top up at https://tracerfy.com/dashboard before running."
        )
        return False, msg
    return True, (
        f"Tracerfy credit pre-flight OK: balance={result.balance}, "
        f"required={required} (batch_size={batch_size})"
    )


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
