"""SiftStack ↔ deep_prospecting import boundary.

Every `from src.X import Y` in this package goes here, and nowhere else.
Other modules in `deep_prospecting/` import from THIS file, never directly
from `src/`. Two reasons:

  1. Splittability. This module is designed to graduate into its own
     repo eventually. When it does, only this file needs to be
     replaced — every other deep_prospecting/ module stays as-is.
  2. Paradigm boundary. SiftStack's NoticeData is a `@dataclass`.
     deep_prospecting uses Pydantic v2. Conversion + adaptation lives
     here, not scattered across phase code.

How to add a new SiftStack import:

  1. Import the SiftStack symbol at the top of this file.
  2. Re-export it (or wrap it in an adapter function) with a name that
     reflects deep_prospecting's vocabulary, not SiftStack's.
  3. Document the conversion contract in a docstring.

What NOT to put here:

  - Generic Python stdlib imports.
  - Pure third-party imports (anthropic, playwright, etc.).
  - Anything that could equally live in `_utils.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make src/ importable when running from the project root. SiftStack's
# convention is "PYTHONPATH=src + cwd=project root"; replicate that here
# so deep_prospecting works in both layouts (CLI, REPL, pytest).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── NJ MOD-IV (taxrecords-nj.com) — three of our four counties ──────────
# Used by Phase 1 (title lookup) to resolve owner of record + parcel ID +
# mailing address for a target property. The vendor (Vital Communications)
# covers Middlesex / Somerset / Union. Essex is on a different vendor with
# reCAPTCHA, so phase 1 records SourceStatus=SKIPPED for Essex inputs
# rather than blowing up.
from nj_taxrecords import (  # noqa: E402
    Parcel as ModIVParcel,
    lookup_by_address as modiv_lookup_by_address,
    lookup_by_owner_name as modiv_lookup_by_owner,
)

# ── Owner-name death-indicator classifier ───────────────────────────────
# Pure string→string function. Returns one of {"personal_rep","life_estate",
# "care_of","et_al","trustee",""}. Same logic used by SiftStack's Knox
# enrichment — no adapter needed, just re-export under a name that doesn't
# leak the SiftStack module path.
from tax_enricher import detect_deceased_indicator as classify_owner_death_indicator  # noqa: E402

# ── Obituary search waterfall ───────────────────────────────────────────
# The same DDGS-search → page-fetch → Haiku-parse pipeline the weekly cron
# uses. Phase 2 (genealogy) reuses it verbatim so:
#   1. We don't fork validation effort — the waterfall is already
#      battle-tested against the runner's PDFs.
#   2. Pricing stays consistent — Haiku cost goes to the same model the
#      cost_estimator already accounts for.
#
# Conversion contract:
#   - obit_search(name, city, state)    : list[{url, title, snippet}]
#   - obit_fetch_page_text(url)         : str (HTTP→BS4→Firecrawl fallback)
#   - obit_parse_with_llm(...)          : dict | None
#       The SiftStack-side function filters to match=True only — used by
#       the weekly cron where false-positives are costly.
#   - obit_parse_raw(...)               : dict | None
#       Phase 2 needs the LLM's structured output even when it self-
#       reports match=False — common when the obit's city differs from
#       the property city (decedent died at out-of-town hospital). Phase
#       2 applies its own first-name + surname match check on top.
from obituary_enricher import (  # noqa: E402
    OBITUARY_PROMPT,
    SYSTEM_PROMPT,
    MAX_TOKENS,
    MAX_OBITUARY_TEXT,
    _search_obituary as obit_search,
    _fetch_page_text as obit_fetch_page_text,
    _parse_obituary_with_llm as obit_parse_with_llm,
)
import llm_client as _llm_client  # noqa: E402


def sonnet_text(
    prompt: str,
    *,
    system: str,
    max_tokens: int = 400,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-5",
) -> str | None:
    """Free-form Sonnet completion. Returns text or None on failure.

    llm_client only exposes chat_json which forces JSON-mode output;
    Phase 3 needs free-form prose for the DM reasoning paragraph, so we
    call Anthropic directly. Used once per ResearchPack — cost ~$0.005.
    """
    try:
        import anthropic
    except Exception:
        return None
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return None


def firecrawl_fetch_full(url: str) -> str:
    """Raw Firecrawl fetch that bypasses obituary_enricher's filters.

    Two reasons we can't go through `_fetch_firecrawl`:
      1. It applies `_filter_cbc_markdown` to any cyberbackgroundchecks.com
         URL — which strips Phones, Related to, and Associated with
         sections. Those are exactly what Phase skip-trace needs.
      2. It truncates to MAX_OBITUARY_TEXT (6000 chars) — CBC detail
         pages exceed that easily due to Google Maps tile blocks before
         the data we care about.

    This thin wrapper calls Firecrawl directly with the same auth + scrape
    parameters, returning the raw markdown.
    """
    if not url:
        return ""
    import os
    import requests
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return ""
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"], "waitFor": 5000},
            timeout=45,
        )
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("markdown", "") or ""
    except Exception:
        return ""


def obit_parse_raw(
    obituary_text: str,
    owner_name: str,
    city: str,
    address: str,
    api_key: str,
    state: str = "New Jersey",
) -> dict | None:
    """Same Haiku call as `obit_parse_with_llm`, but returns the LLM's
    structured dict regardless of its self-reported `match` field.

    Phase 2 uses the structured extraction (survivors, DOD, full_name)
    even when the LLM's binary match decision is conservative on geo.
    Phase 2 validates the identity match itself (first + surname).
    """
    if not obituary_text or not obituary_text.strip():
        return None
    if not api_key:
        return None
    prompt = OBITUARY_PROMPT.format(
        owner_name=owner_name,
        city=city or "unknown",
        address=address or "unknown",
        state=state,
        obituary_text=obituary_text[:MAX_OBITUARY_TEXT],
    )
    try:
        return _llm_client.chat_json(
            prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, api_key=api_key,
        )
    except Exception:
        return None


__all__ = [
    "ModIVParcel",
    "modiv_lookup_by_address",
    "modiv_lookup_by_owner",
    "classify_owner_death_indicator",
    "obit_search",
    "obit_fetch_page_text",
    "obit_parse_with_llm",
    "obit_parse_raw",
]
