"""One-shot deep-prospect + verify on a single address.

Usage: PYTHONPATH=src python scripts/single_address_lookup.py "2420 Crystal Springs Dr" "Hilliard" "OH" "43026"
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config
from models import NoticeData
from address_standardizer import standardize_addresses
from property_enricher import enrich_properties


def header(s):
    print(f"\n{'='*70}\n{s}\n{'='*70}")


def section(title, body):
    print(f"\n--- {title} ---")
    if isinstance(body, dict):
        for k, v in body.items():
            if v not in (None, "", 0):
                print(f"  {k}: {v}")
    else:
        print(f"  {body}")


def main():
    if len(sys.argv) < 5:
        print("Usage: python scripts/single_address_lookup.py STREET CITY STATE ZIP")
        sys.exit(1)

    street, city, state, zipc = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

    header(f"DEEP PROSPECT + VERIFY: {street}, {city}, {state} {zipc}")

    notice = NoticeData(
        notice_type="manual_lookup",
        county="Franklin",  # Hilliard is Franklin County
        address=street,
        city=city,
        state=state,
        zip=zipc,
    )

    # 1. Smarty USPS standardization + vacancy/RDI
    header("STEP 1 — Smarty USPS Verification")
    standardize_addresses([notice], config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN)

    section("Standardized address", {
        "USPS street": notice.address,
        "City": notice.city,
        "State": notice.state,
        "ZIP+4": notice.zip,
        "DPV match code": getattr(notice, "dpv_match_code", "?"),
        "Vacant flag (USPS)": getattr(notice, "vacant", "?"),
        "RDI (Residential/Commercial)": getattr(notice, "rdi", "?"),
        "Latitude": getattr(notice, "latitude", "?"),
        "Longitude": getattr(notice, "longitude", "?"),
    })

    # 2. Zillow / OpenWebNinja
    header("STEP 2 — Zillow (OpenWebNinja) Property Enrichment")
    try:
        enrich_properties([notice], config.OPENWEBNINJA_API_KEY)
        section("Zillow data", {
            "Property type": getattr(notice, "property_type", "?"),
            "Zestimate (estimated value)": getattr(notice, "estimated_value", "?"),
            "Estimated equity": getattr(notice, "estimated_equity", "?"),
            "Equity %": getattr(notice, "equity_percent", "?"),
            "MLS status": getattr(notice, "mls_status", "?"),
            "MLS listing price": getattr(notice, "mls_listing_price", "?"),
            "Last sold date": getattr(notice, "mls_last_sold_date", "?"),
            "Last sold price": getattr(notice, "mls_last_sold_price", "?"),
            "Beds": getattr(notice, "bedrooms", "?"),
            "Baths": getattr(notice, "bathrooms", "?"),
            "SqFt": getattr(notice, "sqft", "?"),
            "Year built": getattr(notice, "year_built", "?"),
        })
    except Exception as e:
        print(f"  Zillow lookup failed: {e}")

    # 3. Serper web search for who's connected to this address
    header("STEP 3 — Serper Web Search (people connected to address)")
    try:
        import requests
        queries = [
            f'"{street}" "{city}" Ohio',
            f'"{street}" Hilliard',
            f'"{street}" obituary',
            f'"{street}" probate',
            f'"{street}" foreclosure',
        ]
        for q in queries:
            print(f"\n  Query: {q}")
            r = requests.post(
                "https://google.serper.dev/search",
                json={"q": q, "num": 5},
                headers={"X-API-KEY": config.SERPER_API_KEY},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"    [error {r.status_code}]")
                continue
            data = r.json()
            for item in data.get("organic", [])[:5]:
                print(f"    • {item.get('title', '')[:90]}")
                print(f"      {item.get('link', '')[:90]}")
                snip = item.get("snippet", "")
                if snip:
                    print(f"      {snip[:200]}")
    except Exception as e:
        print(f"  Serper search failed: {e}")

    # 4. Final verdict
    header("ASSESSMENT")
    is_real = getattr(notice, "dpv_match_code", "") in ("Y", "S", "D")
    is_vacant = getattr(notice, "vacant", "") == "Y"
    rdi = getattr(notice, "rdi", "")
    print(f"  USPS verified real: {'YES' if is_real else 'NO'}")
    print(f"  USPS vacancy flag: {'VACANT' if is_vacant else 'OCCUPIED'}")
    print(f"  Type: {rdi or '?'}")
    last_sold = getattr(notice, "mls_last_sold_date", "")
    if last_sold:
        print(f"  Last sale: {last_sold} for {getattr(notice, 'mls_last_sold_price', '?')}")
    zest = getattr(notice, "estimated_value", "")
    if zest:
        print(f"  Zestimate: {zest}")

    # Dump full notice as JSON for archival
    out_path = ROOT / "output" / f"lookup_{street.replace(' ', '_').replace('/', '-')}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(notice.__dict__, indent=2, default=str))
    print(f"\n  Full record JSON: {out_path}")


if __name__ == "__main__":
    main()
