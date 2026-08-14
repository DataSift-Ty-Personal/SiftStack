# SerpApi Home Depot materials pull for rehab budgets (replaces the cancelled BigBox script).
# ~30 searches per run against the 5,000/month Developer plan. Local pricing via delivery_zip.
# Usage: python serp_materials_pull.py [--zip 37701]
# Output: serp_materials_items.json (workflow-shaped, feeds compute_rehab_final.py) + printed table.
import os, re, sys, json, time, statistics
import requests

def load_key():
    k = os.getenv("SERPAPI_KEY")
    if k:
        return k
    try:
        for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8"):
            if line.strip().startswith("SERPAPI_KEY="):
                return line.strip().split("=", 1)[1]
    except OSError:
        pass
    sys.exit("SERPAPI_KEY not found in env or .env")

KEY = load_key()
ZIP = sys.argv[sys.argv.index("--zip") + 1] if "--zip" in sys.argv else "37701"

# (key, search_term, relevance_regex, price_lo, price_hi)
QUERIES = [
    ("shingles_bundle", "architectural shingles", r"shingle", 25, 60),
    ("underlayment", "synthetic roofing underlayment", r"underlayment", 50, 150),
    ("drip_edge", "aluminum drip edge 10 ft", r"drip", 5, 30),
    ("ridge_cap", "ridge cap shingles", r"ridge|hip", 30, 90),
    ("osb_decking", "7/16 osb sheathing", r"osb|sheathing", 10, 40),
    ("heat_pump", "4 ton heat pump split system", r"heat pump|split system", 3000, 9000),
    ("water_heater", "50 gallon electric water heater", r"water heater", 400, 1200),
    ("window", "vinyl single hung window 36 x 60", r"single hung|window", 120, 400),
    ("base_cabinet", "shaker base cabinet 24 in", r"base cabinet", 150, 450),
    ("wall_cabinet", "shaker wall cabinet 30 in", r"wall cabinet", 100, 350),
    ("countertop_sqft", "prefab granite countertop", r"granite|quartz|countertop", 200, 2000),
    ("appliance_suite", "stainless steel kitchen appliance package", r"appliance|suite|package", 1500, 4500),
    ("kitchen_sink", "stainless steel drop in kitchen sink 33", r"sink", 100, 500),
    ("lvp_sqft", "lifeproof luxury vinyl plank flooring", r"vinyl plank|lvp", 30, 120),  # per-case pricing; sqft derived below
    ("paint_int_5gal", "5 gallon interior paint", r"interior|paint", 100, 300),
    ("primer_5gal", "5 gallon primer", r"primer", 60, 250),
    ("paint_ext_5gal", "5 gallon exterior paint", r"exterior", 120, 350),
    ("bathtub", "60 in alcove bathtub", r"tub", 250, 900),
    ("tub_surround", "bathtub wall surround", r"surround|wall set", 300, 1200),
    ("shower_kit", "48 in shower kit", r"shower", 400, 1600),
    ("vanity", "36 in bathroom vanity with top", r"vanity", 300, 1200),
    ("toilet", "2 piece elongated toilet", r"toilet", 100, 400),
    ("tile_sqft", "porcelain floor tile 12x24", r"porcelain|tile", 15, 80),  # per-case; sqft derived below
    ("int_door", "prehung interior door 6 panel", r"prehung|door", 60, 250),
    ("ceiling_fan", "52 in ceiling fan with light", r"ceiling fan", 60, 250),
    ("insulation_bag", "blown in insulation", r"blown|insulation", 10, 40),
    ("fascia_board", "primed fascia board 1x6", r"fascia|primed|trim|board", 8, 60),
    ("garage_door", "16x7 garage door", r"garage door", 700, 2500),
    ("patio_slider", "72 in sliding patio door", r"sliding|patio", 400, 1600),
    ("deck_board", "5/4 pressure treated deck board 12 ft", r"deck|pressure", 8, 35),
]
# Per-case items: divide case price by typical case coverage to get per-sqft.
CASE_SQFT = {"lvp_sqft": 20.1, "tile_sqft": 15.6}

items = []
used = 0
for key, term, rx, lo, hi in QUERIES:
    try:
        r = requests.get("https://serpapi.com/search.json", params={
            "engine": "home_depot", "q": term, "delivery_zip": ZIP,
            "hd_sort": "top_sellers", "api_key": KEY}, timeout=60)
        d = r.json()
    except Exception as e:
        print(f"  [{key}] EXC {e}")
        continue
    if r.status_code != 200 or d.get("error"):
        print(f"  [{key}] HTTP {r.status_code} {str(d.get('error'))[:100]}")
        continue
    used += 1
    rows = []
    for p in d.get("products", []):
        price = p.get("price")
        title = p.get("title") or ""
        if price is None or not re.search(rx, title, re.I):
            continue
        if not (lo <= float(price) <= hi):
            continue
        rows.append({"title": title[:120], "brand": p.get("brand"), "price": float(price),
                     "product_id": p.get("product_id"), "link": p.get("link"),
                     "rating": p.get("rating"), "reviews": p.get("reviews")})
    if not rows:
        print(f"  [{key}] no in-band matches ({len(d.get('products', []))} raw)")
        continue
    prices = sorted(x["price"] for x in rows)
    med = statistics.median(prices)
    rep = min(rows, key=lambda x: abs(x["price"] - med))
    unit_price = round(med / CASE_SQFT[key], 2) if key in CASE_SQFT else round(med, 2)
    unit = "per sqft (case-derived)" if key in CASE_SQFT else "each"
    items.append({"key": key, "product": rep["title"], "brand": rep.get("brand"),
                  "unit_price": unit_price, "unit": unit,
                  "source_url": rep.get("link") or "", "source": "serpapi-home-depot-local-" + ZIP,
                  "confidence": "high",
                  "notes": f"{len(rows)} in-band of {len(d.get('products', []))} results; median case/unit ${med:,.2f}; rep item {rep.get('product_id')}"})
    print(f"  [{key}] {len(rows)} in-band, median ${med:,.2f} -> unit ${unit_price} | {rep['title'][:60]}")
    time.sleep(0.5)

# heat pump scope flag: boxed system price excludes heat strip kit/lineset/stat
flags = []
hp = next((x for x in items if x["key"] == "heat_pump"), None)
if hp:
    flags.append({"key": "heat_pump", "issue": "boxed split-system price excludes heat strip kit, line set, thermostat; add $600 scope",
                  "suggested_price": round(hp["unit_price"] + 600, 2)})

out = {"result": {"items": items, "sanity": {"flags": flags,
       "verdict": f"SerpApi home_depot engine, delivery_zip {ZIP} local pricing, {used} searches used, {len(items)}/30 items priced"}}}
json.dump(out, open("serp_materials_items.json", "w"), indent=1)
print(f"\nsearches used this run: {used} | items priced: {len(items)}/30")
print("wrote serp_materials_items.json (feed to compute_rehab_final.py)")
