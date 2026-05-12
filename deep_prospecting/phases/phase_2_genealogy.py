"""Phase 2 — Obituary search + heir discovery.

Input:  Lead (from phase_1_title)
Output: HeirMap (heirs marked UNVERIFIED — phase_2_5 does verification)
        + list[SourceCheck]
        + CostBreakdown (additive delta for the obit + Haiku spend)

The search target is **Lead.title_owner**, not the input owner. For a
probate case the title owner IS the decedent — input.owner typically
names the petitioner/PR, which is the heir we want to *find in the
survivors list*, not the search subject.

Waterfall (reuse of obituary_enricher's weekly-cron pipeline):
  1. DDGS multi-backend search → list of candidate obit URLs/snippets
  2. _fetch_page_text → HTTP/BS4 (with Firecrawl fallback for 403s/JS)
  3. _parse_obituary_with_llm → Haiku validates identity + extracts
     survivors, DOD, executor, age

We stop at the first high-confidence match. Medium-confidence is accepted
when survivors[] is non-empty (the parse is information-rich enough to
be useful even without a slam-dunk geo match). Low-confidence is
discarded to avoid identity confusion.

Cost discipline: capped at MAX_PAGES_TO_PARSE LLM parses per run. Each
parse adds RATE_ANTHROPIC_PER_PARSE to the running cost. Page fetches
add RATE_FIRECRAWL_PER_PAGE only when the HTTP fast path falls back to
Firecrawl — we don't have visibility into which fetch path was taken
from inside the bridge, so we apply the conservative full charge per
page fetched.

When the waterfall returns nothing useful, phase 2 returns an empty
HeirMap with `escalation_needed=False` and emits a `"phase_2_no_obit_found"`
warning that the orchestrator propagates onto the ResearchPack. Phase 3
still gets to try.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime

from deep_prospecting import _utils
from deep_prospecting._siftstack_bridge import (
    obit_fetch_page_text,
    obit_parse_raw,
    obit_search,
)
from deep_prospecting.models import (
    CostBreakdown,
    Heir,
    HeirMap,
    Lead,
    SourceCheck,
)

logger = logging.getLogger(__name__)


# ── Tuning constants ─────────────────────────────────────────────────────
# Hard cap on Haiku parses per Phase 2 run. The obit_search waterfall
# can return up to 8 candidate URLs; parsing all 8 would blow the
# COST_TARGET_USD. Stop early once we have a high-confidence match.
MAX_PAGES_TO_PARSE = 5

# Cost rates — sourced from cost_estimator.py so the deep_prospecting
# CostBreakdown stays consistent with the weekly Slack tally.
_RATE_ANTHROPIC_PER_PARSE = 0.003
_RATE_FIRECRAWL_PER_PAGE = 0.005
_RATE_SERPER_PER_SEARCH = 0.001  # nominal; obit_search uses DDGS for now


# ── State expansion ──────────────────────────────────────────────────────
# obit_search wants the full state name (e.g. "New Jersey"). Our model
# has either a 2-letter code or full name in the address tail. Map both.
_STATE_EXPAND = {
    "NJ": "New Jersey", "NY": "New York", "PA": "Pennsylvania",
    "CT": "Connecticut", "TN": "Tennessee", "KY": "Kentucky",
    "GA": "Georgia", "NC": "North Carolina", "SC": "South Carolina",
    "FL": "Florida", "AL": "Alabama", "MS": "Mississippi",
    "OH": "Ohio", "IN": "Indiana", "IL": "Illinois", "MI": "Michigan",
}


def _expand_state(code_or_name: str) -> str:
    code = (code_or_name or "").strip().upper()
    return _STATE_EXPAND.get(code, code_or_name or "New Jersey").title()


# ── Name + address normalization ─────────────────────────────────────────


def _title_owner_to_search_name(title_owner: str) -> str:
    """Convert MOD-IV title format → search-friendly "First Last".

    Examples:
      "GECZIK, OLIVE"                 → "Olive Geczik"
      "SCHWICHTENBERG, MARIE (ESTATE)" → "Marie Schwichtenberg"
      "BERNSHOCK, DANIEL S (ESTATE)"  → "Daniel S Bernshock"
    """
    if not title_owner:
        return ""
    # Strip parenthetical annotations ("(ESTATE)", "(LIFE EST)", etc.)
    s = re.sub(r"\([^)]*\)", "", title_owner).strip()
    s = re.sub(r"\s+", " ", s)
    if "," in s:
        last, _, rest = s.partition(",")
        first_middle = rest.strip()
        last = last.strip()
        return f"{first_middle} {last}".strip().title()
    return s.title()


def _parse_city_state_from_address(addr: str | None) -> tuple[str, str]:
    """Pull city + state code out of '8 Phyllis Pl, Milltown, NJ 08850'.

    Returns ("", "") when the address doesn't follow the expected
    'street, city, state ZIP' layout — caller falls back to state-only
    search.
    """
    if not addr:
        return "", ""
    # Drop trailing ZIP for cleaner parsing
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", addr.strip())
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) < 3:
        return "", ""
    # Last token is "STATE [ZIP]" — already stripped above
    state_tail = parts[-1].strip()
    state_code = state_tail[:2].upper() if state_tail else ""
    city = parts[-2].strip()
    return city, state_code


# ── DOD parsing ──────────────────────────────────────────────────────────


def _parse_dod(raw: str | None) -> tuple[date | None, str | None]:
    """Best-effort YYYY-MM-DD parse with lossless text fallback.

    Returns (parsed_date_or_None, original_text_if_parse_failed_or_None).
    The model keeps `dod` AND `dod_text` precisely for cases where the
    obit says "April 2025" — the typed field stays None, the raw goes
    into dod_text.
    """
    if not raw:
        return None, None
    s = raw.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date(), None
    except ValueError:
        return None, s


# ── Heir construction ────────────────────────────────────────────────────


def _survivors_to_heirs(
    survivors: list[dict],
    *,
    decedent_last: str,
) -> list[Heir]:
    """Convert LLM survivors[] objects into typed Heir records.

    Per spec, every heir from Phase 2 is UNVERIFIED — Phase 2.5 escalates
    to living/deceased via Find-a-Grave + secondary checks.
    """
    heirs: list[Heir] = []
    for s in survivors or []:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        rel = (s.get("relationship") or "").strip().lower()
        city = (s.get("city") or "").strip() or None
        # If the LLM gave us a first name only, prepend the decedent's
        # surname (the prompt instructs Haiku to fall back this way).
        if " " not in name and decedent_last:
            name = f"{name} {decedent_last}".strip()
        heirs.append(Heir(
            name=name.title(),
            relationship=rel or "family_member",
            city=city,
            state=None,
            status="UNVERIFIED",
            sources=["obit_search"],
        ))
    return heirs


# ── Async wrappers ───────────────────────────────────────────────────────
# The bridge calls are sync; wrap each in a thread so the orchestrator
# can await them. obit_parse_with_llm is the hottest call (Haiku) — keep
# it isolated.


async def _async_obit_search(name: str, city: str, state: str) -> list[dict]:
    return await asyncio.to_thread(obit_search, name, city, "", state)


async def _async_obit_fetch(url: str) -> str:
    return await asyncio.to_thread(obit_fetch_page_text, url)


async def _async_obit_parse(
    text: str, owner: str, city: str, address: str, api_key: str, state: str,
) -> dict | None:
    return await asyncio.to_thread(
        obit_parse_raw, text, owner, city, address, api_key, state,
    )


# ── Phase 2's own match validation ───────────────────────────────────────


def _names_match(search_name: str, obit_full_name: str) -> bool:
    """First-name + surname token check.

    The LLM's `match` field is conservative on geography — for our use
    case "Olive Geczik in Milltown" vs "Olive Marie Geczik died at hospital
    in East Brunswick" is the SAME person (decedent often dies at an
    out-of-town hospital or nursing home). Phase 2 does identity matching
    by name tokens, not geography. State-narrowing already happened at
    search time, so a false positive that survives this check would
    require two same-name same-state people in the same generation — rare.
    """
    if not search_name or not obit_full_name:
        return False
    search_tokens = re.findall(r"[a-z]+", search_name.lower())
    obit_tokens = re.findall(r"[a-z]+", obit_full_name.lower())
    if len(search_tokens) < 2 or len(obit_tokens) < 2:
        return False
    # First token + last token of search must both appear in the obit.
    return (
        search_tokens[0] in obit_tokens
        and search_tokens[-1] in obit_tokens
    )


# ── Public entry point ───────────────────────────────────────────────────


async def run(lead: Lead) -> tuple[HeirMap | None, list[SourceCheck], CostBreakdown]:
    """Phase 2: search for the decedent's obituary and extract heirs.

    Always returns a (HeirMap | None, checks, cost) triple. HeirMap is
    None only when there is no title_owner to search on; otherwise we
    return a HeirMap (possibly empty) so the orchestrator can still
    annotate "Phase 2 ran but found nothing."
    """
    checks: list[SourceCheck] = []
    cost = CostBreakdown()

    # Prefer the explicit decedent_name when Phase 1 set it
    # (executor-swap-confirmed path) — the operator's upstream signal is
    # more authoritative than title-owner guessing. Falls back to
    # title_owner for the heuristic-driven paths.
    search_source = lead.decedent_name or lead.title_owner or ""
    decedent_search_name = _title_owner_to_search_name(search_source)
    if not decedent_search_name:
        checks.append(SourceCheck(
            source="obit_search",
            status="SKIPPED",
            notes="no title_owner from phase 1 — cannot search",
        ))
        return None, checks, cost

    decedent_last = decedent_search_name.split()[-1] if decedent_search_name else ""

    city, state_code = _parse_city_state_from_address(lead.input.address)
    state_full = _expand_state(state_code)
    address_for_prompt = lead.input.address or ""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    # ── Search ──
    try:
        candidates = await _utils._safe_call(
            lambda: _async_obit_search(decedent_search_name, city, state_full),
            name=f"obit_search[{decedent_search_name}]",
        ) or []
        # The search call uses DDGS in this implementation — no per-search
        # billing today. When/if we swap to Serper, the nominal $0.001 is
        # already wired through.
        cost.serper += _RATE_SERPER_PER_SEARCH
    except Exception as e:
        logger.warning("obit_search failed for %s: %s", decedent_search_name, e)
        candidates = []

    if not candidates and city:
        # Geo-narrowing returned nothing — retry with state only. Common
        # when the decedent died in a different town than the property
        # (nursing home / assisted living / hospital out of county).
        try:
            candidates = await _utils._safe_call(
                lambda: _async_obit_search(decedent_search_name, "", state_full),
                name=f"obit_search[state-only,{decedent_search_name}]",
            ) or []
            cost.serper += _RATE_SERPER_PER_SEARCH
        except Exception as e:
            logger.warning("obit_search retry failed: %s", e)

    # Slice 3: Serper fallback when DDGS returns nothing. Resolves the
    # DDGS non-determinism (BACKLOG'd) that produced phase_2_no_obit_found
    # warnings on Marie + Maryann in the Slice 2 cohort. Serper is paid
    # but flat $0.001/search, free tier covers 2,500/mo, and only fires
    # on the recall-gap path — DDGS-first behavior is preserved.
    if not candidates:
        from deep_prospecting.sources import serper_obit_fallback
        state_code = state_code if state_code else ""
        try:
            serper_results = await _utils._safe_call(
                lambda: serper_obit_fallback.fallback_obit_search(
                    decedent_search_name, city or None, state_code or None,
                ),
                name=f"obit_search[serper,{decedent_search_name}]",
            ) or []
            cost.serper += serper_obit_fallback.CALL_COST_USD
            candidates = serper_results
            if serper_results:
                checks.append(SourceCheck(
                    source="obit_search",
                    status="HIT",
                    notes=(
                        f"serper fallback fired (DDGS empty); "
                        f"{len(serper_results)} obit URLs returned"
                    ),
                ))
        except Exception as e:
            logger.warning("serper obit fallback failed: %s", e)

    if not candidates:
        checks.append(SourceCheck(
            source="obit_search",
            status="EMPTY",
            notes=f"no obituary hits for '{decedent_search_name}' in {state_full}",
        ))
        return (
            HeirMap(
                decedent_name=decedent_search_name,
                heirs=[],
                generations_searched=1,
            ),
            checks,
            cost,
        )

    # ── Fetch + parse loop ──
    parsed_match: dict | None = None
    parses_attempted = 0
    pages_fetched = 0

    for cand in candidates[:MAX_PAGES_TO_PARSE]:
        url = cand.get("url") or ""
        if not url:
            continue

        page_text = await _utils._safe_call(
            lambda u=url: _async_obit_fetch(u),
            name=f"obit_fetch[{url[:60]}]",
        )
        pages_fetched += 1
        cost.firecrawl += _RATE_FIRECRAWL_PER_PAGE
        if not page_text or len(page_text) < 100:
            continue

        parsed = await _utils._safe_call(
            lambda t=page_text: _async_obit_parse(
                t, decedent_search_name, city, address_for_prompt,
                api_key, state_full,
            ),
            name=f"obit_parse[{decedent_search_name}]",
        )
        parses_attempted += 1
        cost.anthropic += _RATE_ANTHROPIC_PER_PARSE
        if not parsed:
            continue

        full_name = (parsed.get("full_name") or "").strip()
        if not _names_match(decedent_search_name, full_name):
            continue

        # Phase 2 accepts the parse on its own name-match decision. The
        # LLM's `confidence` field still feeds the source-check notes for
        # observability, but doesn't gate the accept here.
        survivors = parsed.get("survivors") or []
        parsed_match = parsed
        parsed_match["_source_url"] = url
        parsed_match["_llm_match"] = bool(parsed.get("match"))
        parsed_match["_llm_confidence"] = parsed.get("confidence")
        if survivors:
            # First name+surname match with survivors present — accept
            # and stop. Without survivors we keep scanning for a richer
            # obit on a different domain.
            break

    if parsed_match is None:
        checks.append(SourceCheck(
            source="obit_search",
            status="EMPTY",
            notes=(
                f"{parses_attempted} Haiku parse(s) of {pages_fetched} page(s); "
                f"no first+surname token match for '{decedent_search_name}'"
            ),
        ))
        return (
            HeirMap(
                decedent_name=decedent_search_name,
                heirs=[],
                generations_searched=1,
            ),
            checks,
            cost,
        )

    # ── Build HeirMap from match ──
    checks.append(SourceCheck(
        source="obit_search",
        status="HIT",
        notes=(
            f"parsed obit at {parsed_match.get('_source_url','?')} "
            f"(llm_match={parsed_match.get('_llm_match')} "
            f"conf={parsed_match.get('_llm_confidence')})"
        ),
    ))

    decedent_name = (parsed_match.get("full_name") or decedent_search_name).strip()
    decedent_dod, decedent_dod_text = _parse_dod(parsed_match.get("date_of_death"))
    decedent_city = (parsed_match.get("city") or city or "").strip() or None

    heirs = _survivors_to_heirs(
        parsed_match.get("survivors") or [],
        decedent_last=decedent_last,
    )

    # If the obit named an executor and they aren't already in the
    # survivors list, prepend them as a separate "executor" heir. The
    # named executor is the highest-priority DM candidate.
    executor = (parsed_match.get("executor_named") or "").strip()
    if executor:
        executor_lower = executor.lower()
        if not any(h.name.lower() == executor_lower for h in heirs):
            heirs.insert(0, Heir(
                name=executor.title(),
                relationship="executor",
                city=None,
                state=None,
                status="UNVERIFIED",
                sources=["obit_search"],
                verification_notes="named as executor/PR in obituary",
            ))

    heir_map = HeirMap(
        decedent_name=decedent_name,
        decedent_dod=decedent_dod,
        decedent_dod_text=decedent_dod_text,
        decedent_city=decedent_city,
        heirs=heirs,
        generations_searched=1,
    )
    return heir_map, checks, cost
