"""Cross-cutting utilities for the deep_prospecting module.

Lives outside `phases/` and `sources/` so any layer can import without
creating cycles. Keep this module DEPENDENCY-LIGHT — stdlib + this
module's own models only. Anything heavier (Anthropic, Playwright) goes
in `llm.py` or specific source modules.

Contents:
  - Cost ceiling constants (the ceiling/target for the orchestrator).
  - `_safe()` — coroutine wrapper with retry/backoff/logging.
  - `slug()` — filesystem-safe identifier from an address/owner string.
  - Path helpers for outputs/{date}/{slug}/...
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

# ── Cost discipline ───────────────────────────────────────────────────
# Hard ceiling: orchestrator stamps abort + halts before a phase whose
# projected spend would push total over this.
#
# Slice 1 placeholder was $0.18 (CBC-only, no paid APIs). Slice 2 raised
# to $1.00 because Tracerfy at $0.10/heir × 3 heirs + Trestle at $0.015
# × ~10 phones lands an L3 case near $0.50 with headroom for outliers.
COST_CEILING_USD: float = 1.00
# Target: aspirational — runs above this aren't aborted, but the orchestrator
# can use it to choose cheaper variants (e.g., Haiku vs Sonnet for the
# DM-reasoning paragraph) when below target, switch to faster/cheaper
# strategies when above. Slice 2: $0.50 target per spec.
COST_TARGET_USD: float = 0.50


# ── _safe wrapper ─────────────────────────────────────────────────────

T = TypeVar("T")


async def _safe(
    coro: Awaitable[T],
    *,
    name: str,
    retries: int = 3,
    backoff: float = 2.0,
) -> T | None:
    """Catch + log + retry an awaitable. Returns None on final failure.

    Mirrors SiftStack's `_safe` ethos: a single hostile source must NEVER
    crash the run. Phases wrap external calls in `_safe()`; if a source
    is permanently down, the phase records `[MISSING]` in its output and
    moves on.

    Retry semantics (per spec):
      attempt 1 → fail → sleep `backoff` seconds
      attempt 2 → fail → sleep `backoff * 2`
      attempt 3 → fail → return None

    Args:
        coro: awaitable to invoke. NOTE: an awaitable can only be awaited
            once — for retry semantics with side-effecting calls, prefer
            `_safe_call(factory)` below where the factory can produce a
            fresh coroutine per attempt.
        name: short label for logs ("findagrave.search", "obit_search.firecrawl").
        retries: total attempts. 1 = no retry. Default 3.
        backoff: base seconds; doubles each failure (2, 4, 8, ...).
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro
        except Exception as e:
            last_err = e
            logger.warning("%s: attempt %d/%d failed: %s", name, attempt, retries, e)
            if attempt < retries:
                sleep_for = backoff * (2 ** (attempt - 1))
                await asyncio.sleep(sleep_for)
    logger.error("%s: all %d attempts failed (last: %s)", name, retries, last_err)
    return None


async def _safe_call(
    factory: Callable[[], Awaitable[T]],
    *,
    name: str,
    retries: int = 3,
    backoff: float = 2.0,
) -> T | None:
    """Variant of _safe for retry-with-side-effects.

    Many awaitables are single-shot (an HTTP call, a Playwright navigation).
    Retrying the SAME coroutine raises RuntimeError after the first await.
    `_safe_call` takes a factory so each attempt builds a fresh coroutine.

    Use this anywhere the operation has side effects you want to retry —
    network calls, Playwright actions, LLM completions.
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await factory()
        except Exception as e:
            last_err = e
            logger.warning("%s: attempt %d/%d failed: %s", name, attempt, retries, e)
            if attempt < retries:
                sleep_for = backoff * (2 ** (attempt - 1))
                await asyncio.sleep(sleep_for)
    logger.error("%s: all %d attempts failed (last: %s)", name, retries, last_err)
    return None


# ── Filesystem helpers ────────────────────────────────────────────────

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slug(text: str, max_len: int = 60) -> str:
    """Make a filesystem-safe identifier from a free-text input.

    Used for `outputs/{date}/{slug}/...` paths. Lowercases, replaces
    non-alphanum with hyphens, collapses runs, trims. Caps length at
    max_len so a long owner+address combo doesn't blow past path limits.
    """
    if not text:
        return "unknown"
    s = _SLUG_NON_ALNUM.sub("-", text.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "unknown"


def output_dir_for_run(slug_text: str, *, base: Path | None = None) -> Path:
    """Return (and mkdir) the output dir for a single run.

    Layout: outputs/YYYY-MM-DD/{slug}/

    The base can be overridden via `PROSPECT_OUTPUT_DIR` env var; default
    is `outputs/` next to the deep_prospecting module.
    """
    if base is None:
        env_base = os.environ.get("PROSPECT_OUTPUT_DIR")
        base = Path(env_base) if env_base else Path(__file__).resolve().parent / "outputs"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = base / today / slug(slug_text)
    out.mkdir(parents=True, exist_ok=True)
    return out


def now_utc() -> datetime:
    """Tz-aware UTC timestamp. Stamped on every ResearchPack."""
    return datetime.now(timezone.utc)
