"""Enformion (Endato) Person Search — reusable heir-resolution client.

This is the production module behind the "Primary Path" of the deep-prospecting
skill: when an owner is confirmed deceased, one Person Search on the decedent
returns the relatives graph (the heir set) directly from the provider, instead of
parsing a survivors paragraph out of an obituary with an LLM (which can
hallucinate — see project_obituary_heir_hallucination). Heirs that come back here
are GROUNDED by definition: every name is a real record in Enformion's graph.

Two consumers:
  - obituary_enricher.enrich_obituary_data(..., deep_heirs=True) calls
    resolve_heirs_enformion() to replace the Haiku-survivor heir search with the
    Enformion relatives graph (1 call/record — Step A of the waterfall).
  - run_deep_prospect.py runs the full A-E waterfall on a single record
    (decedent -> signers -> per-signer search -> dedupe phones -> Trestle).

Auth + endpoint verified live (June 2026). Creds come from config
(ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD); never hardcode them.

GOTCHA: Enformion ALWAYS returns an `error` object ({inputErrors, warnings})
even on a successful search. Detect failure by HTTP status, NOT by `error`.
"""

import logging
import re

import requests

import config as cfg

logger = logging.getLogger(__name__)

PERSON_SEARCH_URL = "https://devapi.enformion.com/PersonSearch"
SEARCH_TYPE = "Person"
TIMEOUT = 45

# relativeLevel codes (verified live). "ab" = closest kin (children/spouse/
# siblings/parents); ac/ad/ae = progressively more distant (grandkids, cousins,
# in-laws). The actual relationship label lives in relativeType.
CLOSEST_KIN_LEVEL = "ab"


def is_configured() -> bool:
    """True if Enformion API credentials are present."""
    return bool(cfg.ENFORMION_AP_NAME and cfg.ENFORMION_AP_PASSWORD)


def _headers() -> dict:
    return {
        "galaxy-ap-name": cfg.ENFORMION_AP_NAME,
        "galaxy-ap-password": cfg.ENFORMION_AP_PASSWORD,
        "galaxy-search-type": SEARCH_TYPE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def person_search(
    first: str,
    last: str,
    *,
    city: str = "",
    state: str = "",
    zip_code: str = "",
    dob_year: str = "",
    results_per_page: int = 5,
) -> dict:
    """One Person Search POST. Returns parsed JSON, or {} on hard failure.

    Provide EITHER an address anchor (city/state/zip — best for the decedent) OR
    a dob_year (best for resolving a named signer; Enformion rejects name+city
    alone as insufficient criteria). Billing is per match — a miss is free.
    """
    if not is_configured():
        logger.debug("Enformion not configured — skipping person_search")
        return {}

    body: dict = {"FirstName": first, "LastName": last,
                  "Page": 1, "ResultsPerPage": results_per_page}
    addr2 = " ".join(p for p in [
        f"{city}," if city else "", state or "", zip_code or "",
    ] if p).strip()
    if addr2:
        body["Addresses"] = [{"AddressLine2": addr2}]
    if dob_year:
        body["Dob"] = str(dob_year)

    try:
        resp = requests.post(PERSON_SEARCH_URL, headers=_headers(), json=body, timeout=TIMEOUT)
    except Exception as e:
        logger.warning("Enformion request failed for %s %s: %s", first, last, e)
        return {}

    # Detect failure by HTTP status — NOT by the always-present `error` object.
    if resp.status_code != 200:
        logger.warning(
            "Enformion HTTP %s for %s %s: %s",
            resp.status_code, first, last, resp.text[:300],
        )
        return {}
    try:
        return resp.json()
    except ValueError:
        logger.warning("Enformion returned non-JSON for %s %s", first, last)
        return {}


def first_match(data: dict) -> dict | None:
    """Return the first matched person object, or None."""
    persons = data.get("persons") or data.get("people") or data.get("results") or []
    return persons[0] if persons else None


# ── Schema helpers ────────────────────────────────────────────────────


def full_name(rel: dict) -> str:
    """Build a display name from first/middle/last (or rawNames fallback)."""
    parts = [rel.get("firstName", ""), rel.get("middleName", ""), rel.get("lastName", "")]
    name = " ".join(p.strip() for p in parts if p and p.strip())
    if name:
        return name
    raw = rel.get("rawNames") or rel.get("name")
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if isinstance(raw, dict):
        return (raw.get("fullName") or "").strip()
    return (raw or "").strip()


def _dob_year(rel: dict) -> str:
    """Extract a 4-digit birth year from a (possibly masked) dob like '9/XX/1955'."""
    dob = rel.get("dob") or rel.get("dateOfBirth") or ""
    if isinstance(dob, dict):
        dob = dob.get("year") or dob.get("dob") or ""
    m = re.search(r"(19|20)\d{2}", str(dob))
    return m.group(0) if m else ""


def extract_dod(person: dict) -> str:
    """Return date of death as YYYY-MM-DD (best effort), or ''."""
    candidates = []
    if person.get("dod"):
        candidates.append(person["dod"])
    for d in person.get("datesOfDeath") or []:
        if isinstance(d, dict) and d.get("dod"):
            candidates.append(d["dod"])
        elif isinstance(d, str):
            candidates.append(d)
    for raw in candidates:
        if isinstance(raw, dict):
            raw = raw.get("dod") or raw.get("year") or ""
        raw = str(raw).strip()
        # MM/DD/YYYY
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        # YYYY-MM-DD already
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            return m.group(0)
        # bare year OR masked date (e.g. '3/XX/2026') — recover the year so the
        # year-only dod_conflict check still fires (mirrors _dob_year).
        m = re.search(r"(19|20)\d{2}", raw)
        if m:
            return f"{m.group(0)}-01-01"
    return ""


def _is_deceased(rel: dict) -> bool:
    val = rel.get("isDeceased")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return False


def relatives_to_survivors(person: dict) -> list[dict]:
    """Convert Enformion relativesSummary[] into survivor dicts.

    Returns [{name, relationship, city, _dob, _level, _score, _deceased}] sorted
    closest-kin first. relationship comes from relativeType (e.g. 'Son'); when
    absent we leave it blank and let the caller's surname heuristic decide.
    """
    relatives = person.get("relativesSummary") or person.get("relatives") or []
    out: list[dict] = []
    for rel in relatives:
        if not isinstance(rel, dict):
            continue
        name = full_name(rel)
        if not name:
            continue
        rel_type = (rel.get("relativeType") or rel.get("relationship") or "").strip()
        level = (rel.get("relativeLevel") or "").strip().lower()
        try:
            score = int(rel.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        out.append({
            "name": name,
            "relationship": rel_type.lower(),
            "city": "",
            "_dob": _dob_year(rel),
            "_level": level,
            "_score": score,
            "_deceased": _is_deceased(rel),
            "_lastName": (rel.get("lastName") or "").strip(),
        })
    # Closest kin first (ab < ac < ad < ae), then highest score.
    out.sort(key=lambda r: (r["_level"] or "zz", -r["_score"]))
    return out


def required_signers(survivors: list[dict], decedent_surname: str) -> list[dict]:
    """Cost gate: the heirs who must be Person-Searched individually (Step C).

    Living closest-kin (level 'ab') sharing the decedent's surname, with a known
    birth year. These are the intestate heirs-at-law (children) — searching only
    these keeps a messy 12-relative estate to ~4 paid lookups.
    """
    sur = (decedent_surname or "").strip().lower()
    signers = []
    for s in survivors:
        if s.get("_deceased"):
            continue
        if s.get("_level") != CLOSEST_KIN_LEVEL:
            continue
        # Surname match: structured lastName OR the final name token equals the
        # decedent surname. Plain endswith() would false-match "Maxwell" vs "well".
        name_tokens = s["name"].lower().split()
        if sur and s.get("_lastName", "").lower() != sur and not (
            name_tokens and name_tokens[-1] == sur
        ):
            continue
        if not s.get("_dob"):
            continue
        signers.append(s)
    return signers


def dedupe_phones(phone_entries: list[dict]) -> list[dict]:
    """Collapse phones to a unique set by number (cost gate before Trestle).

    Each entry is {number, ...}. Siblings share household landlines, so the same
    number recurs across signers — score each number once.
    """
    seen: dict[str, dict] = {}
    for p in phone_entries:
        num = re.sub(r"\D", "", str(p.get("number", "")))
        if not num:
            continue
        if num not in seen:
            seen[num] = {**p, "number": num}
    return list(seen.values())


# ── Pipeline integration ──────────────────────────────────────────────


def resolve_heirs_enformion(
    notice,
    parsed: dict,
    deceased_name: str = "",
) -> tuple[list[dict], dict] | None:
    """Resolve a deceased owner's heirs via one Enformion Person Search (Step A).

    Returns (ranked_dms, error_info) shaped exactly like build_heir_map() so
    _apply_obituary_match() consumes it unchanged, or None when Enformion is
    unconfigured / no match / no usable relatives (caller falls back to the
    obituary-survivor waterfall).

    Heir set is grounded in the provider graph, so the obituary-text grounding
    guard is intentionally bypassed here.
    """
    if not is_configured():
        return None

    name = (deceased_name or parsed.get("full_name") or notice.owner_name or "").strip()
    name_parts = name.split()
    if len(name_parts) < 2:
        return None
    first, last = name_parts[0], name_parts[-1]
    surname = last

    data = person_search(
        first, last,
        city=notice.city or "Knoxville", state="TN", zip_code=notice.zip,
    )
    person = first_match(data)
    if not person:
        logger.info("  Enformion: no match for %s — falling back to obituary heirs", name)
        return None

    survivors = relatives_to_survivors(person)
    if not survivors:
        logger.info("  Enformion: matched %s but no relatives — falling back", name)
        return None

    # Reuse the existing TN-intestacy ranking (signing authority, ordering).
    from obituary_enricher import rank_decision_makers  # lazy: avoids import cycle

    # Fill blank relationships with a surname heuristic so ranking can classify
    # closest-kin children even when relativeType is missing.
    rank_input = []
    statuses: dict[str, str] = {}
    unlabeled = 0
    for s in survivors:
        rel = s["relationship"]
        # Do NOT guess "child" from a surname match alone: relativeLevel "ab" also
        # covers spouse/siblings/parents, and mislabeling a same-surname sibling as
        # a child would wrongly grant signing authority. Leave it unlabeled (ranks
        # as "other", no signing authority) and flag for human/L4 verification.
        if not rel:
            unlabeled += 1
        rank_input.append({"name": s["name"], "relationship": rel})
        statuses[s["name"]] = "deceased" if s["_deceased"] else "verified_living"

    ranked = rank_decision_makers(rank_input, executor_name="", heir_statuses=statuses)
    if not ranked:
        return None

    # Re-attach Enformion provenance (dob/score/source) to each ranked entry.
    by_name = {s["name"].lower(): s for s in survivors}
    for entry in ranked:
        s = by_name.get(entry["name"].lower())
        entry["source"] = "enformion_relatives"
        if s:
            entry["dob"] = s.get("_dob", "")
            entry["enformion_score"] = s.get("_score", 0)
            entry["relative_level"] = s.get("_level", "")

    living = sum(1 for e in ranked if e["status"] == "verified_living")
    deceased = sum(1 for e in ranked if e["status"] == "deceased")

    flags = ["heirs_from_enformion"]
    if unlabeled:
        flags.append("enformion_unlabeled_relatives")

    # DOD conflict: Enformion death index vs the obituary DOD already on file.
    enf_dod = extract_dod(person)
    obit_dod = (parsed.get("date_of_death") or getattr(notice, "date_of_death", "") or "").strip()
    if enf_dod and obit_dod and enf_dod[:4] != obit_dod[:4]:
        flags.append("dod_conflict")
        logger.warning(
            "  Enformion DOD %s conflicts with obituary DOD %s for %s — "
            "possible second household death; surfaced, not resolved.",
            enf_dod, obit_dod, name,
        )

    signers = required_signers(survivors, surname)
    if living:
        top = next(e for e in ranked if e["status"] == "verified_living")
        # Dampen confidence when some relatives came back without a relativeType.
        confidence = "medium" if unlabeled else "high"
        _label = top["relationship"] or "relationship unlabeled"
        reason = (
            f"Enformion relatives graph: {living} living heir(s); "
            f"{len(signers)} required signer(s); DM={top['name']} ({_label})"
            + ("; some relativeType missing — verify" if unlabeled else "")
        )
    else:
        confidence = "low"
        reason = "Enformion: all relatives deceased — escalate (per stirpes / L4)"

    error_info = {
        "heir_search_depth": 2,
        "heirs_verified_living": living,
        "heirs_verified_deceased": deceased,
        "heirs_unverified": 0,
        "missing_flags": flags,
        "dm_confidence": confidence,
        "dm_confidence_reason": reason,
    }

    logger.info(
        "  Enformion heirs for %s: %d living, %d deceased, %d signer(s)",
        name, living, deceased, len(signers),
    )
    return ranked, error_info
