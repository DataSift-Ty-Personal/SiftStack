"""Serper obit search fallback — fires when DDGS returns 0 candidates.

Slice 3: DDGS non-determinism was the BACKLOG'd cause of `phase_2_no_obit
_found` on Marie + Maryann (same query returns 6 candidates one minute,
0 the next). Serper is a deterministic Google wrapper — paid, but flat
$0.001/search at our volume and the free tier covers 2,500/mo at no
cost.

Return shape matches `obituary_enricher._search_obituary` so phase_2
can swap candidate lists without further plumbing:
    [{"url": str, "title": str, "snippet": str}, ...]

The waterfall continues unchanged from there — Firecrawl page fetch
then Haiku parse via the bridge.

When DDGS hits cleanly, this module is NEVER called — phase_2 only
falls back when both the city-narrowed and state-only DDGS queries
return empty. So the Serper budget only burns on the recall-gap cases.
"""

from __future__ import annotations

import asyncio
import logging
import os

import requests

from deep_prospecting._utils import _safe_call

logger = logging.getLogger(__name__)


_BASE_URL = "https://google.serper.dev/search"
_TIMEOUT_SECONDS = 10.0
_CALL_COST_USD = 0.001  # Serper Hobby tier rate; free tier covers 2,500/mo

# Same domain filter the SiftStack obit-enricher uses — single source of
# truth for "is this URL an obituary." Imported lazily via the bridge to
# keep this module's import graph clean.
_OBIT_DOMAINS = {
    "legacy.com", "dignitymemorial.com", "echovita.com",
    "tributearchive.com", "findagrave.com", "obituaries.com",
    "funeralhomes.com", "meaningfulfunerals.net", "mem.com",
    "forevermissed.com", "mycentraljersey.com", "nj.com",
    "northjersey.com",
}


def _is_obituary_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    if any(d in u for d in _OBIT_DOMAINS):
        return True
    return "/obituar" in u or "/memorial" in u


def _build_query(name: str, city: str | None, state: str | None) -> str:
    """Google query string: name + 'obituary' + geo hints when present.

    Serper handles natural-language queries well; we don't need quoted
    exact-match because the obit-domain URL filter does the heavy work.
    """
    parts = [f'"{name}"', "obituary"]
    if city:
        parts.append(city)
    if state:
        parts.append(state)
    return " ".join(parts)


def _sync_serper(api_key: str, query: str) -> dict:
    """Blocking HTTP. Returns the parsed JSON dict, or {} on non-200."""
    resp = requests.post(
        _BASE_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "num": 10},
        timeout=_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 500:
        logger.warning(
            "serper 5xx: status=%d query=%r body_len=%d",
            resp.status_code, query, len(resp.text or ""),
        )
        resp.raise_for_status()
    if resp.status_code >= 400:
        logger.info(
            "serper non-200: status=%d query=%r body=%s",
            resp.status_code, query, (resp.text or "")[:200],
        )
        return {}
    try:
        return resp.json() or {}
    except ValueError as e:
        logger.warning("serper non-JSON: %s body=%s", e, (resp.text or "")[:200])
        return {}


def _parse_serper_results(payload: dict) -> list[dict]:
    """Pull obit-domain URLs from a Serper response. Same shape as
    `obituary_enricher._search_obituary` so phase_2 can consume directly."""
    out: list[dict] = []
    for item in payload.get("organic", []) or []:
        url = (item.get("link") or "").strip()
        if not url or not _is_obituary_url(url):
            continue
        out.append({
            "url": url,
            "title": (item.get("title") or "").strip(),
            "snippet": (item.get("snippet") or "").strip(),
        })
    # Also include knowledge-graph / answer-box items that happen to be
    # obit URLs (rare, but Serper sometimes promotes findagrave / legacy
    # there for high-profile names).
    for kw in ("answerBox", "knowledgeGraph"):
        item = payload.get(kw)
        if isinstance(item, dict):
            url = (item.get("link") or "").strip()
            if url and _is_obituary_url(url) and not any(o["url"] == url for o in out):
                out.append({
                    "url": url,
                    "title": (item.get("title") or "").strip(),
                    "snippet": (item.get("snippet") or item.get("description") or "").strip(),
                })
    return out


async def fallback_obit_search(
    decedent_name: str,
    city: str | None,
    state: str | None,
) -> list[dict]:
    """Serper-keyed obit search. Returns a list of {url, title, snippet}
    matching the shape of `obituary_enricher._search_obituary`.

    Empty list on miss / blocked / error — caller treats the same as
    "DDGS also returned nothing" and emits `phase_2_no_obit_found`.
    """
    name = (decedent_name or "").strip()
    if not name:
        return []

    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        logger.warning("serper_obit_fallback: SERPER_API_KEY missing")
        return []

    # Try city+state first (most specific), then state-only retry. Mirrors
    # the existing DDGS-search structure phase_2 uses — when the
    # narrowing miss is the geo, broadening to state often finds the obit
    # (decedent died at an out-of-town hospital, etc.).
    queries = []
    if city and state:
        queries.append(_build_query(name, city, state))
    if state:
        queries.append(_build_query(name, None, state))
    if not queries:
        queries.append(_build_query(name, None, None))

    async def _try(query: str) -> list[dict]:
        payload = await _safe_call(
            lambda q=query: asyncio.to_thread(_sync_serper, api_key, q),
            name=f"serper_obit[{query[:60]}]",
            retries=3, backoff=1.0,
        )
        if payload is None:
            return []
        return _parse_serper_results(payload)

    for q in queries:
        results = await _try(q)
        if results:
            return results

    return []


# Cost is fixed per call. Phase 2 increments cost.serper by this much
# every time it invokes the fallback (regardless of whether the fallback
# returned results — Serper bills on the request, not the response).
CALL_COST_USD: float = _CALL_COST_USD
