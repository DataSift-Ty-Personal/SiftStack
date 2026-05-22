"""Canonical Kentucky name-variant resolver.

Owns the name primitives shared by PVA, deeds, and obituary enrichment.
Imports nothing from those modules — no cycles.

This module is the foundation of Phase 2e (name-variant resolution). It owns:

  * ``SUFFIX_RE`` — the single canonical JR/SR/II/III/IV/ESQ name-suffix regex
    (de-duplicated from ``kentucky_pva_lookup`` and ``jefferson_deeds_scraper``).
  * ``name_tokens`` / ``score_match`` / ``_search_variations`` — promoted verbatim
    from ``kentucky_pva_lookup`` (behavior-preserving; spec task 2e-1).
  * ``NameVariant`` / ``CandidatePerson`` / ``DisambigResult`` dataclass contracts
    and ``generate_variants`` / ``disambiguate`` stubs that later plans (2e-2/2e-3)
    fill in.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Name matching / scoring ───────────────────────────────────────────

SUFFIX_RE = re.compile(r"\b(JR|SR|II|III|IV|ESQ)\b\.?", re.IGNORECASE)


def name_tokens(name: str) -> list[str]:
    """Normalize a name to a list of uppercase alphabetical tokens."""
    cleaned = SUFFIX_RE.sub("", name).upper()
    cleaned = re.sub(r"[^A-Z\s]", " ", cleaned)
    return [t for t in cleaned.split() if len(t) > 1]


def _search_variations(name: str) -> list[str]:
    """Generate PVA search variations for a decedent name.

    KCOJ decedent names come in multiple formats:
      * "ROLAND, WELDON GENE"     — LAST, FIRST MIDDLE (court format)
      * "WELDON GENE ROLAND"      — FIRST MIDDLE LAST  (natural format)
      * "EWING, WELDON GENE JR"   — with suffix

    PVA owner-search is substring match. Return variations in priority order:
      1. Plain LAST FIRST — matches when decedent is current owner directly
      2. LAST FIRST MIDDLE — same, with middle name/initial
      3. ESTATE OF LAST FIRST — matches when PVA has retitled the property
         to the estate (common after probate is opened; the property is
         still controlled by the estate until distributed to heirs)
    """
    tokens = name_tokens(name)
    if not tokens:
        return []

    variations: list[str] = []
    last = ""
    first_parts: list[str] = []

    comma_match = re.match(r"\s*([^,]+),\s*(.+)", name)
    if comma_match:
        last = " ".join(name_tokens(comma_match.group(1)))
        first_parts = name_tokens(comma_match.group(2))
    elif len(tokens) >= 2:
        # Natural order "FIRST MIDDLE LAST" — assume last token is surname
        last = tokens[-1]
        first_parts = tokens[:-1]

    if last and first_parts:
        # Direct-ownership variations
        variations.append(f"{last} {first_parts[0]}")            # LAST first
        if len(first_parts) > 1:
            variations.append(f"{last} {' '.join(first_parts)}")  # LAST first middle

        # Estate-titled variations. PVA stores these verbatim, e.g.
        # "ESTATE OF SMITH DOLLY" — common format for properties where
        # probate has been opened and title re-issued to the estate.
        variations.append(f"ESTATE OF {last} {first_parts[0]}")
        if len(first_parts) > 1:
            variations.append(f"ESTATE OF {last} {' '.join(first_parts)}")

    # Dedup preserving order, filter empties
    return list(dict.fromkeys(v.strip() for v in variations if v.strip()))


def score_match(decedent_name: str, owner_string: str) -> float:
    """Score how well an owner string matches a decedent name.

    Returns 0..1. Joint owners ("SMITH JOHN & SMITH JANE") score high if the
    decedent's first+last both appear as adjacent tokens.
    """
    dec_tokens = name_tokens(decedent_name)
    owner_tokens = name_tokens(owner_string)
    if not dec_tokens or not owner_tokens:
        return 0.0

    # Must have last name present
    # Assume last token of decedent name is surname for natural order;
    # comma-formatted ("SMITH, JOHN") starts with surname.
    dec_surname = dec_tokens[-1]
    if "," in decedent_name.split(" ", 1)[0]:
        dec_surname = dec_tokens[0]
    if dec_surname not in owner_tokens:
        return 0.0

    # Base: surname match
    score = 0.5

    # Bonus: first-name token appears
    dec_first_candidates = [t for t in dec_tokens if t != dec_surname]
    if dec_first_candidates:
        dec_first = dec_first_candidates[0]
        if dec_first in owner_tokens:
            score += 0.35
            # Extra bonus if surname + first are adjacent (dominant owner,
            # not just a buried joint-owner mention)
            try:
                si = owner_tokens.index(dec_surname)
                fi = owner_tokens.index(dec_first)
                if abs(si - fi) <= 2:
                    score += 0.1
            except ValueError:
                pass

    # Penalty: owner string is an obvious business entity
    if re.search(r"\b(LLC|INC|CORP|TRUST|LP|CO|COMPANY|BANK)\b", owner_string.upper()):
        score -= 0.2

    return max(0.0, min(score, 1.0))


# ── Variant generation + disambiguation contracts ─────────────────────
# Dataclass field shapes are stable now so Plans 02/03/04 implement against
# a fixed contract; the generator/disambiguator bodies land in those plans.


@dataclass
class NameVariant:
    value: str        # normalized search string, e.g. "GREATHOUSE DOROTHY"
    fmt: str          # LAST_FIRST | LAST_FIRST_MIDDLE | ESTATE_OF | SURNAME_ONLY
    source: str       # primary | maiden_obit | maiden_positional | prior_married
                      #  | non_anglo_surname | name_change | typo_fuzzy
    confidence: float


@dataclass
class CandidatePerson:
    name: str
    age: int | None = None
    addresses: list[str] | None = None
    dod: str | None = None


@dataclass
class DisambigResult:
    person: CandidatePerson
    score: float
    reason: str


def generate_variants(decedent_name: str, *, maiden_name: str | None = None,
                      prior_surnames: list[str] | None = None,
                      enable_fuzzy: bool = False) -> list[NameVariant]:
    raise NotImplementedError  # filled in Plan 02 (task 2e-2)


def disambiguate(query_name: str, candidates: list[CandidatePerson], *,
                 expected_dod: str | None = None, known_addresses: list[str] | None = None,
                 min_score: float = 0.6) -> "DisambigResult | None":
    raise NotImplementedError  # filled in Plan 02 (task 2e-3)
