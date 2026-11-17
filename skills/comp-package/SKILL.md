---
name: comp-package
description: >
  Build a boundary-filtered comparable sales package for one subject property and deliver it as an Excel workbook. Pulls sold and active comps live from the Zillow data API with price-band partitioning, clips them to a real boundary (bounding box plus street regex), buckets each comp by condition from its sold-price to Zestimate ratio, and produces a dual-track ARV that prices the same-bedroom base case separately from a labeled reconfiguration upside. Adds 4-tier rehab scenarios, wholesale MAO math, and matched cash buyers. Trigger for: run comps, comp package, what is the ARV, pull comparable sales, comps on this property, value this house, or a drawn map boundary plus an address.
---

# Comp Package Builder

Build a complete boundary-filtered comp package for a single subject property: sold comps + actives pulled live from the Zillow data API, condition bucketing, a dual-track ARV, 4-tier rehab scenarios, wholesale MAO math, and buyer targeting, delivered as one Excel workbook.

Use this skill when someone says "run comps on this property", "what's the ARV", "build a comp package", or shares a map with a drawn boundary and an address.

## Requirements

- Python 3.10+ with `requests` and `openpyxl`
- An OpenWeb Ninja "Real-Time Zillow Data" API key in the `OPENWEBNINJA_API_KEY` environment variable (free tier available at openwebninja.com)

## Workflow

### Step 1: Establish subject truth from the county, not aggregators

Pull the county assessor card first (beds, baths, living sqft, year built, condition grade, sale history). Aggregator sites routinely report the wrong bedroom count (a Knox County card said 2/1 where aggregators said 5 bed). The county card also reveals the seller story: forced-sale deed types (Master's deed = tax sale, Trustee's deed = foreclosure), purchase price, and current tax delinquency are your negotiation leverage.

### Step 2: Map the drawn boundary

Convert the user's drawn boundary into two machine filters:
1. A lat/lon bounding box (read corner coordinates off the map's landmarks)
2. A street-name whitelist regex (every street visibly inside the loop)

Apply BOTH: the bbox catches street-name misses, the street list catches bbox bleed across a highway or interstate edge. When a comp sits near the line, verify it against the map before keeping it.

### Step 3: Pull sold + active comps via the API

Run `scripts/zillow_market_pull.py` (see `references/api_contract.md` for the endpoint contract and its traps). Key trap: a single sold search returns at most 41 rows (about 5 weeks in an active zip), so the script partitions by `min_price`/`max_price` bands and recursively splits any saturated band. Pull 12-24 months.

The API only sees MLS activity. Auction, wholesale, and off-market transfers will NOT appear; pull those from county records when they matter.

### Step 4: Bucket comps by condition

Without photos, bucket each sold comp by sold price vs Zestimate:
- Price >= 90% of Zestimate: RENOVATED/RETAIL
- Price <= 70% of Zestimate: DISTRESSED (this is your as-is / acquisition band)
- Between: AVERAGE

Then sanity-check the outliers by hand: a `LOT` home type at a house-like price is usually a teardown sale or a new-construction record with missing sqft. Verify anything surprising against the county card before using it (a "$265K lot" next door turned out to be a 2025 new build).

### Step 5: Dual-track ARV (the bedroom-band rule)

Full method in `references/dual_track_arv.md`. Short version: a subject whose bedroom count is below the comp set lives in a LOWER value band, and the gap is bigger than a per-bedroom line adjustment implies.

- **Base ARV (underwrite here):** median $/sf of same-bed renovated comps x subject sqft, CLAMPED to no more than 105% of the highest same-bed comp price. Big sqft does not rescue a 2-bed; the buyer pool caps the band.
- **Upside ARV (labeled, never assumed):** the higher-bed renovated band, credited only after a walkthrough verifies the layout actually converts (plumbing runs, egress, framing).
- Future value follows the same rule: a 2-bed appreciates on the 2-bed curve unless the reconfig happens.

### Step 6: Rehab scenarios (three numbers, always)

Report all three so the buyer conversation is scoped, using $/sf of living area (tier-2 builder-grade finishes, adjust for your market's labor multiplier):

| Scenario | Typical $/sf | Scope |
|---|---|---|
| Cosmetic | $22-28 | Paint, flooring, kitchen/bath refresh, exterior tidy. Keeps bed count |
| Mid reno | $32-40 | Kitchen, all baths (incl. added bath), HVAC, partial electric/plumbing |
| Full gut | $55-70 | Everything + roof, windows, structural allowance + $15/sf demo-drywall allowance |

For a stalled or partially-renovated property, underwrite FULL GUT until walked: prior work often has to be redone and unpermitted-work risk is real.

### Step 7: Wholesale math off the BASE track

- Flipper MAO = 70% x base ARV - rehab (75% in hot pockets)
- Contract target = full-gut MAO minus your assignment fee
- Show the upside-track MAO as buyer-side room, not as your underwrite

### Step 8: Buyer targeting

Priority order:
1. **Investors active inside the boundary right now** (bought distressed in the last 90 days, or have an active flip resale). Pull their names from county deed records.
2. **Infill builders who bought lots/teardowns on the street** (they know the street and have a proven exit).
3. Zip-level repeat cash buyers from your buyer list, matched to the deal's price band.
4. Landlord fallback if renovated retail rent supports roughly the 1% rule at the buyer's all-in.
Owner-occupants are out for any unfinanceable partial shell.

### Step 9: Deliverable

One Excel workbook, sheets: Summary (subject, four numbers, deal math, seller story), Sold Comps (bucketed, with URLs), Active/Pending, Rehab Scenarios, ARV + Deal Math (dual-track logic shown), Buyer Targets. Format per `references/workbook_format.md`. No em or en dashes anywhere in the deliverable.
