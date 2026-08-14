"""Enformion (Endato) Person Search — live heir-finding test harness.

GOAL: Replace token-heavy browser automation ("computer use" clicking through
TruePeopleSearch/FastPeopleSearch) with a single HTTP POST that returns a
deceased owner's relatives + associates + date-of-death directly.

Now that Sift's obituary data confirms WHICH records are deceased, we feed the
deceased owner's name + last-known address to Enformion Person Search and get
back the candidate heirs. The output is shaped to match the `heir_map_json`
that `tracerfy_skip_tracer.py` already consumes (name, relationship, signing
authority, address), so phones/emails still come from the existing Tracerfy step.

USAGE
-----
  # 1. Put creds in .env:
  #      ENFORMION_AP_NAME=...
  #      ENFORMION_AP_PASSWORD=...
  #
  # 2. First run = SCHEMA DISCOVERY (costs 1 of 100 free searches).
  #    Dumps the raw JSON so we map Enformion's real field names.
  python src/enformion_heir_test.py --first Sheadrick --last Tillman \
      --city Knoxville --state TN --zip 37909 --raw

  # 3. Normal run = ranked heir candidates.
  python src/enformion_heir_test.py --first Geraldine --last Littlejohn \
      --city Knoxville --state TN

COST: 1 Person Search per record. We have ~100 free searches — this script
makes exactly ONE call per invocation and never loops.
"""

import argparse
import json
import logging
import os
import sys

import requests

# Allow running as `python src/enformion_heir_test.py` (src/ on path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Enformion / Endato "Galaxy" API. Person Search returns the full person object
# including relatives + associates + (when present) date of death.
PERSON_SEARCH_URL = "https://devapi.enformion.com/PersonSearch"
SEARCH_TYPE = "Person"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


def _headers() -> dict:
    name = cfg.ENFORMION_AP_NAME
    password = cfg.ENFORMION_AP_PASSWORD
    if not name or not password:
        logger.error(
            "Missing creds. Set ENFORMION_AP_NAME and ENFORMION_AP_PASSWORD in .env\n"
            "(Enformion Console -> API -> Access Profile name + password)."
        )
        sys.exit(1)
    return {
        "galaxy-ap-name": name,
        "galaxy-ap-password": password,
        "galaxy-search-type": SEARCH_TYPE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def person_search(first, last, city, state, zip_code) -> dict:
    """One Person Search POST. Returns parsed JSON (or {} on hard failure)."""
    body = {
        "FirstName": first,
        "LastName": last,
        # AddressLine2 = "City, ST ZIP" per Enformion's address convention.
        "Addresses": [{
            "AddressLine2": " ".join(p for p in [
                f"{city}," if city else "", state or "", zip_code or "",
            ] if p).strip(),
        }],
        "Page": 1,
        "ResultsPerPage": 5,
    }
    logger.info("POST %s  search-type=%s", PERSON_SEARCH_URL, SEARCH_TYPE)
    logger.info("Body: %s", json.dumps(body))
    resp = requests.post(PERSON_SEARCH_URL, headers=_headers(), json=body, timeout=45)
    logger.info("HTTP %s", resp.status_code)
    if resp.status_code != 200:
        logger.error("Response: %s", resp.text[:2000])
        resp.raise_for_status()
    return resp.json()


# --- Schema-agnostic helpers (first call has unknown exact field names) ------

def _find_keys(obj, needles, path="", hits=None):
    """Recursively locate keys whose name contains any needle (case-insensitive).

    Lets us discover Enformion's death/relative field names on the FIRST live
    call without hardcoding a schema we have not seen yet.
    """
    if hits is None:
        hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if any(n in kl for n in needles):
                preview = v if not isinstance(v, (dict, list)) else f"<{type(v).__name__} len={len(v)}>"
                hits.append((f"{path}.{k}".lstrip("."), preview))
            _find_keys(v, needles, f"{path}.{k}", hits)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):  # sample first few
            _find_keys(v, needles, f"{path}[{i}]", hits)
    return hits


def explore_schema(data):
    """Print a map of the response so we can wire up the real fields."""
    persons = data.get("persons") or data.get("people") or data.get("results") or []
    logger.info("\n=== SCHEMA DISCOVERY ===")
    logger.info("Top-level keys: %s", list(data.keys()))
    logger.info("Match count: %s", len(persons))
    if not persons:
        logger.info("No persons returned. Full payload saved to file for inspection.")
        return
    p = persons[0]
    logger.info("\nFirst person top-level keys:\n  %s", "\n  ".join(sorted(p.keys())))
    logger.info("\n-- death-related fields --")
    for path, val in _find_keys(p, ["death", "deceas", "dod", "dod_", "died"]):
        logger.info("  %s = %s", path, val)
    logger.info("\n-- relatives / associates fields --")
    for path, val in _find_keys(p, ["relat", "associat", "kin", "household"]):
        logger.info("  %s = %s", path, val)


def main():
    ap = argparse.ArgumentParser(description="Enformion Person Search heir test")
    ap.add_argument("--first", required=True)
    ap.add_argument("--last", required=True)
    ap.add_argument("--city", default="")
    ap.add_argument("--state", default="")
    ap.add_argument("--zip", dest="zip_code", default="")
    ap.add_argument("--raw", action="store_true",
                    help="Schema-discovery mode: dump full JSON + field map")
    args = ap.parse_args()

    data = person_search(args.first, args.last, args.city, args.state, args.zip_code)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe = f"{args.first}_{args.last}".replace(" ", "_")
    out_path = os.path.join(OUTPUT_DIR, f"enformion_{safe}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    logger.info("Raw response saved -> %s", out_path)

    explore_schema(data)
    if not args.raw:
        logger.info(
            "\nRe-run with --raw on the first record to see the full field map, "
            "then we wire heir-ranking to the real schema."
        )


if __name__ == "__main__":
    main()
