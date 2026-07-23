"""Enformion Business Search V2 client.

Verified live 2026-07-22 (158 Old State dispo build):
- Endpoint: POST https://devapi.enformion.com/BusinessV2Search with header
  ``galaxy-search-type: BusinessV2``. The v1 ``BusinessSearch`` type returns
  "Access denied" on this account; ``AddressSearch`` is not licensed at all.
- Response: ``businessV2Records[]``, each carrying ``usCorpFilings[]`` (SOS
  corp records: officers with title REGISTERED AGENT etc.) and/or
  ``newBusinessFilings[]`` (contacts + mail/business addresses).
- Registered agents are often commercial fronts (Northwest Registered Agent,
  Registered Agents Inc, US Corp Agents): when no human surfaces, fall back to
  the SiftMap reverse-address unmask (see buyer_sweep.resolve_principal).
"""

from __future__ import annotations

import logging

import requests

import config as cfg

logger = logging.getLogger(__name__)

BUSINESS_V2_URL = "https://devapi.enformion.com/BusinessV2Search"
TIMEOUT = 60

# Commercial registered-agent fronts that are NOT principals.
AGENT_FRONTS = (
    "REGISTERED AGENT", "NORTHWEST", "CORPORATION AGENTS", "CORPORATE DIRECT",
    "REGISTERED AGENTS INC", "CT CORPORATION", "COGENCY", "INCORP",
)


def is_configured() -> bool:
    return bool(cfg.ENFORMION_AP_NAME and cfg.ENFORMION_AP_PASSWORD)


def business_search(name: str, city_state: str = "", *, results: int = 3) -> list[dict]:
    """One BusinessV2 search. Returns businessV2Records (possibly empty)."""
    if not is_configured():
        return []
    body: dict = {"BusinessName": name, "Page": 1, "ResultsPerPage": results}
    if city_state:
        body["Addresses"] = [{"AddressLine2": city_state}]
    try:
        resp = requests.post(BUSINESS_V2_URL, json=body, timeout=TIMEOUT, headers={
            "galaxy-ap-name": cfg.ENFORMION_AP_NAME,
            "galaxy-ap-password": cfg.ENFORMION_AP_PASSWORD,
            "galaxy-search-type": "BusinessV2",
            "Content-Type": "application/json",
        })
    except requests.RequestException as e:
        logger.warning("BusinessV2 request failed for %s: %s", name, e)
        return []
    if resp.status_code != 200:
        logger.warning("BusinessV2 HTTP %s for %s: %s", resp.status_code, name, resp.text[:200])
        return []
    try:
        return resp.json().get("businessV2Records") or []
    except ValueError:
        return []


def extract_officers(records: list[dict], entity_name: str) -> list[dict]:
    """Pull human officers/contacts from corp + new-business filings.

    Filters out entity self-references and commercial registered-agent fronts.
    Returns [{name, title, address}] deduped by name.
    """
    token = entity_name.split()[0].upper()
    seen: set[str] = set()
    officers: list[dict] = []
    for rec in records:
        filings = (rec.get("usCorpFilings") or []) + (rec.get("newBusinessFilings") or [])
        for filing in filings:
            fname = (filing.get("name") or filing.get("company") or "").upper()
            if token not in fname:
                continue
            for off in (filing.get("officers") or []) + (filing.get("contacts") or []):
                n = off.get("name") or {}
                full = (n.get("nameRaw") or n.get("fullName") or "").strip()
                upper = full.upper()
                if (not full or upper in seen or "LLC" in upper or "INC" in upper
                        or any(f in upper for f in AGENT_FRONTS)):
                    continue
                seen.add(upper)
                officers.append({
                    "name": full,
                    "title": off.get("title") or off.get("officerTitleDesc") or "",
                    "address": (off.get("address") or {}).get("fullAddress") or "",
                })
    return officers


def find_principals(entity_name: str, city_state: str = "") -> list[dict]:
    """Convenience: search + extract in one call."""
    return extract_officers(business_search(entity_name, city_state), entity_name)
