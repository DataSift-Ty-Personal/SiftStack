"""Phase 1 — Title lookup + death-signal detection.

Input:  ProspectInput (address + optional owner + county)
Output: Lead (always returned, even sparse — never raises)

Sources consulted (in order):
  1. NJ MOD-IV (taxrecords-nj.com) via _siftstack_bridge.
     Covers Middlesex / Somerset / Union. Essex falls through (different
     vendor, deferred). Knox/Blount (TN) also fall through here — Phase 1
     for TN counties is a stub that returns an empty Lead with a warning.
  2. Owner-name death-indicator classifier (et_al / life_est / personal_rep
     / care_of / trustee) — pure string→string from SiftStack's
     tax_enricher.

Death-signal heuristics (any one trips `death_signal=True`):
  - MOD-IV owner-name contains: "personal_rep", "life_estate", "care_of",
    "et_al", or "trustee" (trustee filtered to exclude business entities).
  - Owner mismatch: caller-supplied owner ≠ MOD-IV owner BUT they share
    the same last name. Strong signal of an estate transfer ("OLIVE
    GECZIK" on title vs "Catherine Geczik" on probate docket = same
    family, owner died).
  - "ESTATE OF" appears in the MOD-IV owner field literally.

Phase 1 NEVER consults LLMs and NEVER hits the obit endpoints — those
are Phase 2 concerns. It costs ~$0.000 (one HTTP POST). Keep it that way.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime

from deep_prospecting import _utils
from deep_prospecting._siftstack_bridge import (
    ModIVParcel,
    classify_owner_death_indicator,
    modiv_lookup_by_address,
    modiv_lookup_by_owner,
)
from deep_prospecting.models import (
    Lead,
    ProspectInput,
    SourceCheck,
)

logger = logging.getLogger(__name__)


# Counties this phase can resolve via taxrecords-nj.com. Anything else
# returns an empty Lead with a "skipped" SourceCheck so the run continues.
_MODIV_COUNTIES = {"Middlesex", "Somerset", "Union"}


# ── Address normalization ───────────────────────────────────────────────


_STREET_SUFFIX_NORMALIZE = {
    "STREET": "ST", "ST.": "ST",
    "AVENUE": "AVE", "AVE.": "AVE",
    "ROAD": "RD", "RD.": "RD",
    "PLACE": "PL", "PL.": "PL",
    "DRIVE": "DR", "DR.": "DR",
    "LANE": "LN", "LN.": "LN",
    "COURT": "CT", "CT.": "CT",
    "BOULEVARD": "BLVD", "BLVD.": "BLVD",
    "TERRACE": "TER", "TER.": "TER",
    "CIRCLE": "CIR", "CIR.": "CIR",
    "PARKWAY": "PKWY", "PKWY.": "PKWY",
}


def _normalize_property_address(addr: str) -> str:
    """Uppercase + collapse whitespace + abbreviate street suffix.

    Mirrors what taxrecords-nj stores so we can exact-compare candidate
    rows against the caller's address. Strips trailing ZIP / city.
    """
    if not addr:
        return ""
    s = addr.upper().strip()
    # Drop a trailing zip if present
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", s)
    # Drop everything after a comma (city/state tail)
    s = s.split(",")[0].strip()
    parts = s.split()
    parts = [_STREET_SUFFIX_NORMALIZE.get(p, p) for p in parts]
    return " ".join(parts)


def _house_number(addr: str) -> str:
    """Extract the leading house number from a normalized address.

    Used to disambiguate substring hits — "8 PHYLLIS PL" must not match
    "18 PHYLLIS PL" or "108 PHYLLIS PL".
    """
    m = re.match(r"^\s*(\d+[A-Z]?)\b", addr.upper())
    return m.group(1) if m else ""


def _last_name(name: str) -> str:
    """Pull the surname out of a free-text owner field.

    Handles both "LAST, FIRST" (MOD-IV format) and "First Last" (caller
    input). Lower-cases for case-insensitive comparison.
    """
    if not name:
        return ""
    n = name.strip()
    if "," in n:
        return n.split(",", 1)[0].strip().lower()
    tokens = [t for t in re.split(r"\s+", n) if t]
    return tokens[-1].lower() if tokens else ""


def _name_token_key(name: str) -> str:
    """Order-independent identity key: sorted lowercased alphanumeric tokens.

    "Sally Baksh" and "BAKSH, SALLY" both → "baksh sally" — that's the
    SAME person in two formats, NOT a mismatch. Used to short-circuit
    the same-surname death-signal check.
    """
    if not name:
        return ""
    return " ".join(sorted(re.findall(r"[a-z0-9]+", name.lower())))


def _collapse_ws(s: str | None) -> str | None:
    """Collapse internal whitespace runs to single spaces; preserve None."""
    if s is None:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ── Death-signal classifier ─────────────────────────────────────────────


_BUSINESS_RE = re.compile(
    r"\b(LLC|L\.L\.C\.|INC|CORP|TRUST|LP|LTD|CO\.|COMPANY|BANK|ASSOC)\b",
    re.IGNORECASE,
)


# ── Slice 4: (ESTATE)-marker validator ─────────────────────────────────
#
# Phase 1's title_owner_estate_marker heuristic fires on any MOD-IV
# "LASTNAME, FIRST (ESTATE)" string. Real-world testing (Slice 3) found
# this fires false-positive on alive owners whose title carries a joint-
# estate marker from a deceased spouse. Validator: run 2 Serper queries
# + 2 Haiku boolean calls to decide whether the marker reflects the
# actual title-owner's death (L3 path), or carry-over from someone
# else's estate (L1 + advisory warning).
#
# Cost: ~$0.002 Serper + ~$0.010 Haiku = ~$0.012 per (ESTATE) record.
# Only runs when _classify_death_signal returned title_owner_estate_marker.

_DECEDENT_PROMPT = """\
Below are search-result snippets from a Google search for a person's
obituary. Determine whether THIS PERSON is the deceased subject of an
obituary.

Person: {name}
Hint location: {city}, {state}

Treat common nicknames as equivalent — Daniel↔Dan, Catherine↔Cathy,
Robert↔Bob, Margaret↔Maggie/Peggy, William↔Will/Bill, etc. First-name
strict match would produce false negatives.

Return ONLY a JSON object with these keys:
- "is_decedent": true if any snippet clearly identifies this person as
  the deceased (e.g., "Daniel Bernshock passed away", "Daniel Bernshock
  Obituary"). false otherwise.
- "matched_obit_title": short title of the matched obit, or "" if none
- "dod": date of death in YYYY-MM-DD if extractable from the snippets,
  else "". Only fill when you set is_decedent=true.
- "confidence": "high" / "medium" / "low"

Be conservative — only return is_decedent=true when the snippet text
clearly puts THIS specific person (right surname + matching first name
or nickname) as the deceased subject, not as a survivor.

Snippets:
{snippets}"""

_SURVIVOR_PROMPT = """\
Below are search-result snippets from a Google search for "<name>
survived" / "preceded in death" obituary mentions. Determine whether
THIS PERSON is mentioned as a SURVIVING family member of someone
else's obituary (i.e., this person is alive, the obit is for a
relative).

Person: {name}
Hint location: {city}, {state}

Treat common nicknames as equivalent.

Return ONLY a JSON object with these keys:
- "is_survivor": true if any snippet clearly identifies this person as
  a surviving family member of a different decedent (e.g., "Survived
  by ... Daniel Bernshock of Linden, NJ"). false otherwise.
- "decedent_name": name of the deceased person whose obit mentions
  the survivor, or "" if not extractable
- "decedent_obit_date": YYYY-MM-DD obit / death date of the OTHER
  decedent if extractable, else ""
- "relationship": "son" / "daughter" / "spouse" / etc. if extractable,
  else ""
- "confidence": "high" / "medium" / "low"

Be conservative — only return is_survivor=true when the snippet text
clearly puts THIS person (right surname + matching first name or
nickname) on the survivors list of someone else's obit.

Snippets:
{snippets}"""


# Cost rates — match the rates in cost_estimator.py.
_SLICE4_RATE_SERPER = 0.001
_SLICE4_RATE_HAIKU = 0.005


def _modiv_owner_to_search_name(modiv_owner: str | None) -> str:
    """'GECZIK, OLIVE' / 'BERNSHOCK, DANIEL S (ESTATE)' → 'Olive Geczik' /
    'Daniel S Bernshock'.

    Drops parenthetical annotations ("(ESTATE)", "(LIFE EST)", etc.) and
    swaps "LAST, FIRST" → "First Last". Same logic phase_2_genealogy uses
    to build its obit-search target; duplicated here to avoid a phase→
    phase import. If the formats ever diverge, factor into _utils.
    """
    if not modiv_owner:
        return ""
    s = re.sub(r"\([^)]*\)", "", modiv_owner).strip()
    s = re.sub(r"\s+", " ", s)
    if "," in s:
        last, _, rest = s.partition(",")
        return f"{rest.strip()} {last.strip()}".strip().title()
    return s.title()


def _city_state_from_address(addr: str | None) -> tuple[str, str]:
    """'8 Phyllis Pl, Milltown, NJ 08850' → ('Milltown', 'NJ').

    Same shape as phase_skiptrace's parser; duplicated here to avoid a
    phase→phase import.
    """
    if not addr:
        return "", ""
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", addr.strip())
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) < 3:
        return "", ""
    return parts[-2].strip(), parts[-1].strip()[:2].upper()


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


async def _serper_search_snippets(
    query: str, *, max_results: int = 5,
) -> list[dict]:
    """Run one Serper query and return list of {title, snippet, url}.

    Re-uses the SERPER_API_KEY env var; same client as
    serper_obit_fallback.py. Empty list on miss / error.
    """
    import os
    import requests
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key or not query:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.info("estate-validator serper non-200: %d", resp.status_code)
            return []
        data = resp.json() or {}
    except Exception as e:
        logger.debug("estate-validator serper error: %s", e)
        return []
    out: list[dict] = []
    for item in (data.get("organic") or [])[:max_results]:
        out.append({
            "title": (item.get("title") or "").strip(),
            "snippet": (item.get("snippet") or "").strip(),
            "url": (item.get("link") or "").strip(),
        })
    return out


def _format_snippets_for_prompt(results: list[dict]) -> str:
    """Build the snippets block the Haiku prompt expects."""
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")[:120]
        snippet = r.get("snippet", "")[:300]
        url = r.get("url", "")
        # Filter to obit-domain URLs only — Phase 2's serper_obit_fallback
        # uses the same domain filter. Non-obit URLs leak noise into the
        # boolean decision.
        u = url.lower()
        if not any(d in u for d in (
            "legacy.com", "echovita.com", "tributearchive.com",
            "findagrave.com", "dignitymemorial.com", "obituaries.com",
            "mycentraljersey.com", "nj.com", "obits.", "northjersey.com",
            "republicanherald.com", "thedailytimes.com",
        )) and "/obituar" not in u and "/memorial" not in u:
            continue
        lines.append(f"[{i}] {title}\n    URL: {url}\n    SNIPPET: {snippet}")
    return "\n\n".join(lines) if lines else "(no obit-domain results)"


async def _haiku_classify(prompt: str, *, api_key: str) -> dict | None:
    """One Haiku call returning a parsed JSON dict. None on failure."""
    if not api_key:
        return None
    try:
        # Reuse the bridge's anthropic client — chat_json gives us the
        # JSON-mode parsing the prompt expects.
        from deep_prospecting._siftstack_bridge import _llm_client
        return await asyncio.to_thread(
            _llm_client.chat_json,
            prompt,
            system="You return ONLY a JSON object. No prose, no markdown.",
            max_tokens=300,
            api_key=api_key,
        )
    except Exception as e:
        logger.debug("estate-validator haiku error: %s", e)
        return None


async def validate_estate_marker(
    name: str,
    city: str,
    state: str,
) -> tuple[str, str | None, dict, float]:
    """Slice 4 estate-marker validator.

    Args:
        name: Title owner's name in "FIRST [MIDDLE] LAST" form (already
              run through _title_owner_to_search_name by the caller).
        city / state: Property city + 2-letter state code (NJ).

    Returns:
        (decision, warning_key, evidence, cost_usd)

        decision ∈ {
          "CONFIRMED_DECEDENT",   # decedent-hit, survivor-miss OR DOD wins
          "ALIVE_SURVIVOR",       # decedent-miss, survivor-hit OR survivor-DOD wins
          "UNVERIFIED",           # neither hit clearly, or LLM failed
        }
        warning_key: "phase_1_estate_marker_advisory_spouse_estate" for
                    ALIVE_SURVIVOR, "phase_1_estate_marker_unverified"
                    for UNVERIFIED, None for CONFIRMED_DECEDENT
        evidence: dict with raw query results + Haiku decisions for the
                  research pack's audit trail
        cost_usd: total Serper + Haiku spend (~$0.012 typical)
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    cost = 0.0
    evidence: dict = {"queries": [], "decedent": None, "survivor": None}

    if not name:
        return "UNVERIFIED", "phase_1_estate_marker_unverified", evidence, cost

    geo = f"{city} {state}".strip() or state or ""
    decedent_query = f'"{name}" obituary {geo}'.strip()
    survivor_query = f'"{name}" "survived by" OR "preceded in death" {geo}'.strip()

    # Two Serper searches, run in parallel.
    dec_results, sur_results = await asyncio.gather(
        _serper_search_snippets(decedent_query),
        _serper_search_snippets(survivor_query),
    )
    cost += 2 * _SLICE4_RATE_SERPER
    evidence["queries"] = {
        "decedent": decedent_query,
        "survivor": survivor_query,
    }

    # Two Haiku calls — one per query — in parallel.
    dec_prompt = _DECEDENT_PROMPT.format(
        name=name, city=city or "(unknown)", state=state or "(unknown)",
        snippets=_format_snippets_for_prompt(dec_results),
    )
    sur_prompt = _SURVIVOR_PROMPT.format(
        name=name, city=city or "(unknown)", state=state or "(unknown)",
        snippets=_format_snippets_for_prompt(sur_results),
    )
    dec_parsed, sur_parsed = await asyncio.gather(
        _haiku_classify(dec_prompt, api_key=api_key),
        _haiku_classify(sur_prompt, api_key=api_key),
    )
    cost += 2 * _SLICE4_RATE_HAIKU
    evidence["decedent"] = dec_parsed
    evidence["survivor"] = sur_parsed

    is_dec = bool((dec_parsed or {}).get("is_decedent"))
    is_sur = bool((sur_parsed or {}).get("is_survivor"))

    # 4-outcome decision tree per spec.
    if is_dec and not is_sur:
        return "CONFIRMED_DECEDENT", None, evidence, cost
    if is_sur and not is_dec:
        return (
            "ALIVE_SURVIVOR",
            "phase_1_estate_marker_advisory_spouse_estate",
            evidence, cost,
        )
    if is_dec and is_sur:
        # Both hits — date precedence (Q2 spec):
        #   most-recent DOD wins; missing dates → fall through to
        #   UNVERIFIED with warning. False positive on death is cheaper
        #   than false negative — Phase 2.5 catches misrouted-alive.
        dec_dod = _parse_iso_date((dec_parsed or {}).get("dod"))
        sur_dod = _parse_iso_date((sur_parsed or {}).get("decedent_obit_date"))
        if dec_dod and sur_dod:
            if dec_dod >= sur_dod:
                return "CONFIRMED_DECEDENT", None, evidence, cost
            else:
                return (
                    "ALIVE_SURVIVOR",
                    "phase_1_estate_marker_advisory_spouse_estate",
                    evidence, cost,
                )
        # Neither parseable — default to L3 (conservative) with warning.
        return (
            "UNVERIFIED",
            "phase_1_estate_marker_unverified",
            evidence, cost,
        )
    # Neither hit.
    return (
        "UNVERIFIED",
        "phase_1_estate_marker_unverified",
        evidence, cost,
    )


# Lists-column tags that confirm an operator-resolved probate/inheritance
# situation. When the DataSift contact name doesn't match the title owner
# AND one of these tags is present, the operator has already done the
# executor research upstream and swapped First/Last to the executor —
# the title owner is the decedent.
_EXECUTOR_SWAP_LIST_TAGS = {
    "probate",
    "inheritance",
    "notice of default (lis pendens)",
}


def _classify_death_signal(
    *,
    modiv_owner: str,
    caller_owner: str | None,
    list_tags: list[str] | None = None,
) -> tuple[bool, str | None, str | None, str | None, list[str]]:
    """Decide whether the title evidence justifies a death signal.

    Returns (death_signal, reason, decedent_name, named_contact_role,
    warnings). The decedent_name / named_contact_role pair is set only
    on the executor-swap-confirmed path; other paths return (None, None)
    for those slots so the orchestrator falls back to title_owner.
    """
    warnings: list[str] = []

    if not modiv_owner:
        return False, None, None, None, warnings

    upper = modiv_owner.upper()

    # 1. NJ MOD-IV "(ESTATE)" suffix OR "ESTATE OF X" form — both fire.
    #    Examples seen in the wild:
    #      SCHWICHTENBERG, MARIE (ESTATE)
    #      BERNSHOCK, DANIEL S (ESTATE)
    #      ESTATE OF JOHN SMITH
    #    Skip when ESTATE is part of a business name ("ESTATE GARDENS
    #    LLC", "REAL ESTATE TRUST OF NJ") so we don't false-positive on
    #    commercial owners. ESTATE marker takes precedence over the
    #    executor-swap signal so Slice 4's validator still runs (the
    #    marker can be a false positive on joint-estate carry-over).
    if re.search(r"\bESTATE\b", upper) and not _BUSINESS_RE.search(upper):
        return True, "title_owner_estate_marker", None, None, warnings

    # 2. Owner-name pattern classifier (life_est, personal_rep, et_al, etc.)
    indicator = classify_owner_death_indicator(modiv_owner) or ""
    if indicator:
        return True, f"title_owner_indicator_{indicator}", None, None, warnings

    # 3. Executor-swap confirmed by Lists tag. The operator's upstream
    #    workflow swaps the executor name into the DataSift First/Last
    #    fields once they've resolved the probate. So when the contact
    #    name is different from the MOD-IV title owner AND the row
    #    carries Probate / Inheritance / Notice of Default in Lists,
    #    title_owner IS the decedent — no need to guess from surname.
    #    Routes Phase 2 obit search at the explicit decedent name and
    #    flags the named contact as the executor for Phase 3.
    if (
        caller_owner
        and list_tags
        and _name_token_key(caller_owner) != _name_token_key(modiv_owner)
    ):
        normalized_tags = {t.strip().lower() for t in list_tags}
        if normalized_tags & _EXECUTOR_SWAP_LIST_TAGS:
            return (
                True,
                "executor_swap_confirmed",
                modiv_owner,
                "executor",
                warnings,
            )

    # 4. Surname-share with caller-supplied owner. Catches "Catherine
    #    Geczik" on probate docket vs "GECZIK, OLIVE" on MOD-IV — same
    #    family, deceased owner transferred title to surviving relative.
    if caller_owner:
        caller_last = _last_name(caller_owner)
        modiv_last = _last_name(modiv_owner)
        if caller_last and modiv_last and caller_last == modiv_last:
            # Same-person check: token-key compare so "Sally Baksh" and
            # "BAKSH, SALLY" don't fire a false-positive death signal.
            if _name_token_key(caller_owner) != _name_token_key(modiv_owner):
                return (
                    True,
                    "title_owner_mismatch_same_surname",
                    None, None, warnings,
                )
        elif caller_last and modiv_last and caller_last != modiv_last:
            # Different surname entirely — might mean stale caller data
            # (recent sale), might mean ID confusion. Warn but don't
            # fire the death signal.
            warnings.append(
                f"caller-supplied owner '{caller_owner}' has different "
                f"surname than MOD-IV owner '{modiv_owner}'"
            )

    return False, None, None, None, warnings


# ── Address-match filtering ─────────────────────────────────────────────


def _select_parcel_for_address(
    parcels: list[ModIVParcel],
    target_address: str,
) -> ModIVParcel | None:
    """Pick the parcel whose property_location exactly matches the caller's
    address, ignoring suffix variations (ST vs STREET).

    Returns None when no parcel matches or multiple ambiguous matches
    remain — caller treats this as "title unknown" rather than guessing.
    """
    if not parcels:
        return None

    target_norm = _normalize_property_address(target_address)
    target_house = _house_number(target_norm)

    if not target_house:
        # No house number to disambiguate on — fall back to first row,
        # but only if there's exactly one.
        return parcels[0] if len(parcels) == 1 else None

    matches: list[ModIVParcel] = []
    for p in parcels:
        cand_norm = _normalize_property_address(p.property_location)
        cand_house = _house_number(cand_norm)
        if cand_house == target_house and cand_norm == target_norm:
            matches.append(p)

    if len(matches) == 1:
        return matches[0]
    # Loose retry: same house number, suffix differs slightly.
    if not matches:
        loose = [
            p for p in parcels
            if _house_number(_normalize_property_address(p.property_location))
            == target_house
        ]
        if len(loose) == 1:
            return loose[0]
    return None


# ── Public entry point ──────────────────────────────────────────────────


async def run(prospect: ProspectInput) -> tuple[Lead, list[SourceCheck]]:
    """Phase 1: resolve title + death signal for `prospect`.

    Always returns a (Lead, [SourceCheck]) pair. The Lead may be sparse
    when the county isn't covered or the lookup returns nothing.
    """
    checks: list[SourceCheck] = []
    warnings: list[str] = []

    county = prospect.county
    if county is None or county not in _MODIV_COUNTIES:
        reason = (
            "essex_needs_separate_vendor" if county == "Essex"
            else "county_outside_modiv_coverage"
        )
        checks.append(SourceCheck(
            source="mod_iv",
            status="SKIPPED",
            notes=reason,
        ))
        warnings.append(
            f"MOD-IV title lookup skipped: {reason} (county={county})"
        )
        return (
            Lead(input=prospect, warnings=warnings),
            checks,
        )

    # MOD-IV lookup — prefer address (most specific), fall back to owner.
    # Both must be normalized to the taxrecords-nj format BEFORE the HTTP
    # call: addr lookup needs "102 GRACEY ST" (no city tail), owner lookup
    # needs the surname alone (e.g. "BAKSH") because the form's substring
    # matcher requires "LAST,FIRST" exact-comma form, not loose "Last First".
    parcel: ModIVParcel | None = None
    if prospect.address:
        normalized_addr = _normalize_property_address(prospect.address)
        parcels_by_addr = await _utils._safe_call(
            lambda: _async_modiv_lookup_by_address(normalized_addr, county),
            name=f"modiv.lookup_by_address[{county}]",
        ) or []
        parcel = _select_parcel_for_address(parcels_by_addr, prospect.address)
        if parcel is None and parcels_by_addr:
            warnings.append(
                f"MOD-IV returned {len(parcels_by_addr)} candidates for "
                f"'{prospect.address}' but none exact-matched"
            )

    if parcel is None and prospect.owner:
        owner_last = _last_name(prospect.owner).upper()
        if owner_last:
            parcels_by_owner = await _utils._safe_call(
                lambda: _async_modiv_lookup_by_owner(owner_last, county),
                name=f"modiv.lookup_by_owner[{county}]",
            ) or []
            # When falling back to owner-only, narrow by matching the
            # caller's address (if known) against property_location.
            if prospect.address:
                parcel = _select_parcel_for_address(parcels_by_owner, prospect.address)
            elif len(parcels_by_owner) == 1:
                parcel = parcels_by_owner[0]
            elif len(parcels_by_owner) > 1:
                warnings.append(
                    f"MOD-IV returned {len(parcels_by_owner)} parcels for "
                    f"owner '{prospect.owner}' — ambiguous, title unresolved"
                )

    if parcel is None:
        checks.append(SourceCheck(
            source="mod_iv",
            status="EMPTY",
            notes="no exact-match parcel found",
        ))
        return (
            Lead(input=prospect, warnings=warnings),
            checks,
        )

    checks.append(SourceCheck(source="mod_iv", status="HIT", notes=""))

    # Classify death signal from owner-name + caller cross-check.
    (
        death_signal,
        death_reason,
        explicit_decedent,
        named_contact_role,
        ds_warnings,
    ) = _classify_death_signal(
        modiv_owner=parcel.owner_name,
        caller_owner=prospect.owner,
        list_tags=prospect.list_tags,
    )
    warnings.extend(ds_warnings)

    # Slice 4: validate (ESTATE) marker via Serper before locking in
    # death_signal=True. The heuristic over-fires on joint-estate
    # carry-over from a deceased spouse (Daniel Bernshock pattern).
    # Only runs when _classify_death_signal returned the estate-marker
    # reason — the other death-signal paths (mismatch_same_surname,
    # life_estate, et_al, etc.) stay intact.
    if death_signal and death_reason == "title_owner_estate_marker":
        search_name = _modiv_owner_to_search_name(parcel.owner_name)
        city_hint, state_hint = _city_state_from_address(prospect.address)
        decision, warning_key, evidence, validator_cost = await validate_estate_marker(
            search_name, city_hint, state_hint,
        )
        checks.append(SourceCheck(
            source="obit_search",
            status="HIT" if decision != "UNVERIFIED" else "EMPTY",
            notes=(
                f"phase_1_estate_validator → {decision}"
                f" (decedent_hit={(evidence.get('decedent') or {}).get('is_decedent')}"
                f", survivor_hit={(evidence.get('survivor') or {}).get('is_survivor')}"
                f", cost=${validator_cost:.4f})"
            ),
        ))
        if decision == "ALIVE_SURVIVOR":
            # Flip death_signal off; mark advisory so the operator sees
            # this property has joint-estate carry-over from a deceased
            # spouse / relative — the title owner is alive.
            death_signal = False
            death_reason = None
            warnings.append(warning_key or "phase_1_estate_marker_advisory_spouse_estate")
            sur_evidence = evidence.get("survivor") or {}
            sur_decedent = sur_evidence.get("decedent_name") or "(unknown)"
            sur_dod = sur_evidence.get("decedent_obit_date") or "(no date)"
            warnings.append(
                f"phase_1_estate_marker_validator: title_owner appears as "
                f"surviving {sur_evidence.get('relationship') or 'family member'} "
                f"of {sur_decedent} (obit date {sur_dod}); routing L1"
            )
        elif decision == "UNVERIFIED":
            # Keep death_signal=True (conservative — false-positive on
            # death is cheaper than false-negative) but mark unverified
            # so Phase 2 / Phase 2.5 know to lower confidence.
            warnings.append(warning_key or "phase_1_estate_marker_unverified")

    # Name variants — useful for downstream obit/heir search. Caller
    # owner (if given), MOD-IV owner, and a swapped "FIRST LAST" form
    # for "LAST, FIRST" rows.
    name_variants: list[str] = []
    if prospect.owner:
        name_variants.append(prospect.owner.strip())
    if parcel.owner_name:
        name_variants.append(parcel.owner_name.strip())
        if "," in parcel.owner_name:
            last, _, rest = parcel.owner_name.partition(",")
            name_variants.append(f"{rest.strip()} {last.strip()}".strip())
    # Dedup, preserve order
    seen: set[str] = set()
    name_variants = [
        n for n in name_variants
        if not (n.lower() in seen or seen.add(n.lower()))
    ]

    return (
        Lead(
            input=prospect,
            title_owner=_collapse_ws(parcel.owner_name),
            death_signal=death_signal,
            death_signal_reason=death_reason,
            decedent_name=(
                _collapse_ws(explicit_decedent) if explicit_decedent else None
            ),
            named_contact_role=named_contact_role,
            name_variants=name_variants,
            mailing_address=_collapse_ws(parcel.mailing_full),
            parcel_id=parcel.parcel_id or None,
            warnings=warnings,
        ),
        checks,
    )


# ── Async wrappers around the sync bridge calls ─────────────────────────
# nj_taxrecords is sync (`requests`). Wrap each call in a thread executor
# via asyncio.to_thread so the orchestrator can await it without
# blocking other phases.

async def _async_modiv_lookup_by_address(
    address: str,
    county: str,
) -> list[ModIVParcel]:
    return await asyncio.to_thread(modiv_lookup_by_address, address, county)


async def _async_modiv_lookup_by_owner(
    owner: str,
    county: str,
) -> list[ModIVParcel]:
    return await asyncio.to_thread(modiv_lookup_by_owner, owner, county)
