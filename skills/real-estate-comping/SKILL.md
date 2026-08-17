---
name: real-estate-comping
description: Perform AI-powered property valuation and comparable sales analysis for real estate wholesaling. Use when the user needs to comp a property, determine ARV, analyze comparable sales, or perform property valuation. Automatically detects disclosure vs non-disclosure states and applies the appropriate methodology (standard comping for disclosure states, triangulation method for non-disclosure states like Texas). This skill focuses purely on comping — determining market value through comparable sales analysis. It does NOT estimate rehab costs or renovation budgets; those are handled separately.
---

# Real Estate Comping Skill

Perform appraiser-grade property valuations using the Two-Bucket method for disclosure states or the Triangulation method for non-disclosure states. This skill is strictly about comparable sales analysis and ARV determination — it does not estimate rehab costs, renovation budgets, or scope of work. If the user needs rehab cost estimation, direct them to the appropriate skill for that.

## Workflow Overview

1. **Identify property location** → Determine state from address
2. **Route to correct methodology** → Disclosure or Non-Disclosure framework
3. **Execute 9-step analysis** → Follow the appropriate prompt framework
4. **Generate deliverables** → PDF summary report + Excel breakdown + comps table

## Comp Data Acquisition (before any analysis)

**Primary path: the Zillow /search API pull.** If an OpenWeb Ninja Real-Time Zillow Data key is available (`OPENWEBNINJA_API_KEY` env var), pull the comp universe programmatically instead of browsing listing sites. The companion **comp-package** skill ships the full contract and a self-contained puller script. The load-bearing facts:
- `/search` with `home_status=RECENTLY_SOLD` (exact enum; other casings return 400) for solds, `FOR_SALE` for actives
- Every search caps at 41 rows (about 5 weeks of sales in an active zip): partition by `min_price`/`max_price` bands and recursively split any band returning 41 (recovers 12-24 months per zip)
- `price_min`/`price_max` are silently ignored; confirm every filter against the echoed `parameters` object
- `dateSold` is epoch milliseconds; use `unformattedPrice`, not the `soldPrice` display string
- Clip to the target pocket with a lat/lon bounding box AND a street-name whitelist; verify LOT-type or missing-sqft surprises against the county card (often teardown sales or new builds)
- The API is MLS-only: auction, wholesale, and off-market transfers come from county records

**Fallback path:** no API key, or the user prefers manual comping: browse Zillow/Redfin/MLS exactly as the framework references describe. The methodology below is identical either way; only data acquisition changes.

**Bedroom-band rule (both paths):** when the subject's bedroom count is below the comp set (especially 2-bed vs 3-bed), run a dual-track ARV: the base case is valued ONLY against same-bed comps, clamped to that band's median price (extra sqft does not escape the band); reconfig to more bedrooms is a labeled upside credited only after a walkthrough verifies the layout converts. Underwrite off the base track, and project future value on the same-bed curve.

## State Detection & Routing

**Determine state type from property address:**

- **Non-Disclosure States** (sold prices not publicly recorded): TX, UT, WY, NM, ID, MT, ND, AK, KS, MS, LA, MO
- **Disclosure States**: All other US states

**Routing:**
- Non-disclosure state → Read `references/non-disclosure-prompt.md`
- Disclosure state → Read `references/disclosure-prompt.md`

## Quick Reference

| Framework | States | Key Method | Price Source |
|-----------|--------|------------|--------------|
| Disclosure | Most US states | Two-Bucket (Unrenovated vs Renovated PPSF) | MLS sold prices |
| Non-Disclosure | TX, UT, WY, NM, ID, MT, ND, AK, KS, MS, LA, MO | Triangulation (LLP + DOM, Deed of Trust, Tax Ratio) | Derived estimates |

## Core Comping Rules (Both Frameworks)

These rules apply regardless of disclosure status:

### GLA Definition

**GLA = Gross Living Area = above-grade finished living space.**

GLA is the single most important measurement in comparable sales analysis. Misidentifying GLA leads to incorrect PPSF calculations and unreliable ARVs.

**Included in GLA:**
- All finished rooms above grade (living rooms, bedrooms, kitchens, bathrooms, hallways)
- Second/third stories above grade
- Finished basement with separate exterior entrance at grade level (walkout basement)

**Excluded from GLA:**
- Unfinished basement (any portion below grade)
- Finished basement without separate exterior entrance (value separately, see Basement rules)
- Garage (attached or detached)
- Covered porches, screened porches, patios
- Attic space (unless finished with permanent stairs and standard ceiling height)
- Carports, breezeways, storage sheds

**Basement valuation (not GLA but has value):**
- Finished to same quality as main floor: value at ~50% of above-grade PPSF
- Finished with drop ceilings/inferior finishes: value at ~35-40% of above-grade PPSF
- Partially finished: value at ~25-35% of above-grade PPSF
- Unfinished: value at ~10-15% of above-grade PPSF (storage/utility value only)

**Discrepancy protocol:** When public records GLA differs from MLS GLA by >5%, flag it. Public records often include non-GLA space. Use MLS GLA as primary, cross-reference with county assessor measurements.

### Comp Selection Filters
- **Age**: ≤90 days preferred, 6 months max
- **Subdivision**: Same micro-pocket, do not cross major roads
- **GLA**: ±100 sqft ideal, ±250 sqft outer bound
- **Property type**: Match elevation style (ranch to ranch, 2-story to 2-story)
- **Build year**: ±10 years

### Outlier Detection

After gathering comps, screen for PPSF outliers before bucket analysis:

1. **Calculate median PPSF** of all comps in the set
2. **Calculate standard deviation** of comp PPSF values
3. **Flag any comp >2 standard deviations from the median PPSF**
4. **Apply exclusion rules:**
   - If 5+ comps total and one is an outlier: **exclude it** and note the exclusion with reason
   - If only 3-4 comps total and one is an outlier: **keep it but flag it** with notation in the analysis (too few comps to discard)
   - If 2+ outliers in a small set: re-examine comp selection criteria — the set may be too heterogeneous

**Common outlier causes to document:**
- Estate/distress sale (below market)
- REO/bank-owned (typically 10-15% below market)
- Major unreported renovation (above market)
- Seller concessions not reflected in price
- Related-party transaction (non-arm's-length)

### Market Sentiment Assessment

Assess market conditions using quantitative indicators before applying sentiment adjustments:

| Indicator | Hot Market | Balanced Market | Cool Market |
|-----------|-----------|-----------------|-------------|
| DOM trend | Declining month-over-month | Stable | Increasing month-over-month |
| Inventory | <3 months supply | 3-6 months supply | >6 months supply |
| Price trend (YoY) | Rising 5%+ | Flat to +3% | Flat to declining |
| Sale-to-list ratio | >100% | 97-100% | <97% |
| Sentiment adjustment | +5-7% | +3-5% | 0-2% |

**Data sources for market sentiment:**
- Redfin market tracker (monthly updates, metro and zip level)
- Zillow Home Value Index (ZHVI) for YoY price trends
- Local MLS statistics (DOM, inventory, sale-to-list)
- County assessor recent sale volumes

**Apply the sentiment adjustment in Step 9 (Final ARV Assembly), not during comp selection.**

### Recent Renovation Handling (Bucket Classification)

When classifying comps into Bucket A (unrenovated) or Bucket B (renovated), apply these rules for properties with known renovation history:

| Renovation Timing | Classification Rule |
|-------------------|-------------------|
| Within 2 years of sale | Bucket B regardless of original condition |
| Within 5 years of sale | Bucket B if renovation was **substantial** (see definition below) |
| More than 5 years before sale | Classify based on current condition vs comps |

**Key indicator:** A significant price jump between the last two sales of the same property (e.g., bought for $150K, sold 18 months later for $260K) strongly suggests a flip/renovation. Cross-reference with permit history and listing photos.

**Definition of "substantial renovation":**
- Kitchen + at least 1 full bathroom remodel (minimum ~$15K combined spend)
- OR full kitchen gut (cabinets + countertops + appliances replaced)
- Cosmetic-only (paint + flooring only) does NOT qualify — classify as Bucket A
- Paint + flooring + fixture updates = borderline — classify as Bucket A unless listing explicitly says "renovated" or "remodeled"

**If renovation scope is unknown:** Default to listing photos and remarks. "Updated," "remodeled," or "new kitchen/bath" language = Bucket B. "Original," "vintage," "needs TLC" = Bucket A.

### Feature Adjustments (by price tier)

See `references/adjustment-cheatsheet.md` for complete adjustment values.

**Quick reference (aligned with comp_analyzer.py source):**
| Feature | <$500k Tier | >$500k Tier |
|---------|-------------|-------------|
| $/sqft (GLA) | $85/sqft | $85/sqft |
| Bedroom | +$5,000 | +$10,000 |
| Bathroom (full) | +/-$7,500 | +/-$10,000 |
| Bathroom (half) | +/-$3,750 | +/-$5,000 |
| Garage | +$8,000/stall | +$10,000-$25,000/stall |
| Age (year built) | $500/year | $500/year |
| Market condition (time) | 0.3%/month | 0.3%/month |

### Unusual Feature Adjustments

| Feature | Adjustment | Notes |
|---------|-----------|-------|
| Pool (warm climate: TX, AZ, FL, NV) | +$15,000-$25,000 | Year-round use markets |
| Pool (seasonal climate: TN, NC, VA) | +$5,000-$15,000 | 4-6 month use season |
| Pool (cold climate: MN, WI, MI) | $0 to -$5,000 | Maintenance liability can be negative |
| Water/mountain views | +5-10% of home value | Compare only on same grade/elevation |
| Easements (utility, access) | -5-10% depending on impact | Worse if easement crosses buildable area |
| Corner lot | -3-5% | Less privacy, more road frontage/maintenance |
| Cul-de-sac | +3-5% | More privacy, less traffic, child-friendly |
| Backing to commercial/industrial | -10-15% | Noise, traffic, visual impact |
| Backing to multifamily | -5-10% | Less impact than commercial |
| Power lines/cell tower proximity | -5-10% | Within 300 ft is the impact zone |

**Pool condition matters:**
- Updated pool with modern finishes: full value
- Dated pool needing resurface: 50% of value
- Pool needing major repairs: may be net negative (removal cost $10,000-$15,000)

### Basement/ADU Rules
- Basements: Not counted as GLA; value at ~50% of above-grade PPSF if finished (see GLA Definition section for full breakdown)
- ADUs: 50% value if not separately deeded; 100% if separately titled

## Two-Bucket Methodology (Core of This Skill)

The Two-Bucket method separates comps into unrenovated (Bucket A) and renovated (Bucket B) to derive the market premium that renovation commands. This is a market-derived metric, not a rehab cost estimate.

### Bucket Spread Sanity Check

The spread between Bucket A and Bucket B PPSF values tells you what the market pays for renovation in that micro-market.

- **5-30% spread**: Normal range. Proceed with analysis.
- **<5% spread**: Suspect — unrenovated comps may be mispriced, or the market doesn't reward renovation strongly. Re-examine Bucket A comp conditions.
- **>30% spread**: Suspect — Bucket A comps may include distressed sales, or Bucket B comps may be ultra-luxury renovations beyond typical flip quality. Re-examine both buckets.

### Worked Example: Two-Bucket Spread Analysis

**Subject property:** 1,500 SF ranch, 3 bed/2 bath, built 1978, dated condition, Knoxville TN

**Bucket A (as-is/dated) — 5 comps:**

| Comp | GLA | Sale Price | PPSF |
|------|-----|-----------|------|
| A1 | 1,480 | $175,000 | $118.24 |
| A2 | 1,520 | $184,000 | $121.05 |
| A3 | 1,450 | $172,500 | $118.97 |
| A4 | 1,540 | $186,000 | $120.78 |
| A5 | 1,510 | $181,500 | $120.20 |

Median PPSF_A: **$120.20**

**Bucket B (renovated) — 5 comps:**

| Comp | GLA | Sale Price | PPSF |
|------|-----|-----------|------|
| B1 | 1,470 | $240,000 | $163.27 |
| B2 | 1,530 | $256,000 | $167.32 |
| B3 | 1,490 | $243,500 | $163.42 |
| B4 | 1,510 | $250,000 | $165.56 |
| B5 | 1,460 | $244,000 | $167.12 |

Median PPSF_B: **$165.56**

**Spread calculation:**
```
Market Premium = ($165.56 - $120.20) / $120.20 x 100% = 37.7%
```

**Sanity check:** 37.7% exceeds the normal 5-30% range. Investigation needed:
- Check if Bucket A comps include any distress/estate sales pulling the average down
- Verify Bucket B comps are standard flips, not ultra-luxury renovations
- Check if subject micro-market has high renovation demand (near university, hospital, etc.)

If comps check out, proceed but note the elevated spread and widen confidence band to +/-5%.

**Value calculations:**
- As-is value: $120.20 x 1,500 SF = **$180,300**
- ARV (after renovation): $165.56 x 1,500 SF = **$248,340**
- Market premium from renovation: $248,340 - $180,300 = **$68,040**

**Rehab budget sanity check:** Rehab budget should be 40-70% of the market premium to maintain deal economics:
- Rehab range: $27,216 to $47,628
- If rehab estimate exceeds $47,628, the deal margin is thin — re-evaluate

**Maximum Allowable Offer (MAO) check:**
- MAO = ARV x 70% - Rehab = $248,340 x 0.70 - $35,000 (est. rehab) = **$138,838**
- Compare MAO to as-is value ($180,300) — if as-is value > MAO, the deal likely does not work as a wholesale flip

## Required Deliverables

**Every comp analysis MUST produce these two outputs:**

### 1. Excel Breakdown Workbook
Comprehensive multi-sheet workbook with:
- **Executive Summary** sheet: Quick-view of key findings
- **Subject Property** sheet: All property details
- **Comparable Sales** sheet: Full comps table with bucket analysis
- **Adjustments Detail** sheet: Line-by-line adjustment breakdown
- **Market Analysis** sheet: Market metrics and trends
- **ARV Calculation** sheet: Step-by-step ARV math
- **Sources & Notes** sheet: Data sources, parameters, recommendations

**Generate using:** `scripts/generate_excel_report.py`

### 2. In-Context Analysis
The detailed analysis text with tables shown directly in the conversation, including:
- Step-by-Step ARV Breakdown (Base PPSF → Adjustments → Final ARV)
- Comps Summary Table (Address, Sale Date, Price, GLA, Beds/Baths, Year, Condition, Adjustments, Final Adjusted Value)
- Outlier Screening Results (any comps flagged or excluded, with reasons)
- Market Overview (Median price, PPSF, DOM, sale-to-list ratio, market phase, sentiment assessment)
- Sources & Assumptions (Data sources, time window, radius constraints)
- Recommendations & Caveats (Verification steps, risk factors, disclaimer)

## Output Generation Instructions

### Data Structure for Report Generation

Prepare analysis data as JSON with this structure:

```json
{
    "subject_property": {
        "address": "123 Main St",
        "city": "Austin",
        "state": "TX",
        "zip": "78701",
        "county": "Travis",
        "subdivision": "Downtown",
        "property_type": "Single Family",
        "gla": 1850,
        "lot_size": 6500,
        "beds": 3,
        "baths": 2,
        "year_built": 1985,
        "condition": "Dated"
    },
    "comps": [
        {
            "address": "456 Oak Ave",
            "sale_date": "2025-12-15",
            "sale_price": 485000,
            "gla": 1780,
            "ppsf": 272.47,
            "beds": 3,
            "baths": 2,
            "year_built": 1982,
            "condition": "Renovated",
            "distance": 0.3,
            "total_adjustments": -5000,
            "adjusted_value": 480000,
            "outlier_flag": false,
            "outlier_reason": null
        }
    ],
    "outlier_screening": {
        "median_ppsf": 255.00,
        "std_dev_ppsf": 18.50,
        "threshold_low": 218.00,
        "threshold_high": 292.00,
        "excluded_comps": [],
        "flagged_comps": []
    },
    "bucket_analysis": {
        "unrenovated": { "count": 2, "median_ppsf": 235.20, "avg_ppsf": 235.20 },
        "renovated": { "count": 2, "median_ppsf": 257.33, "avg_ppsf": 257.33 },
        "market_premium_pct": 9.4,
        "spread_sanity": "normal"
    },
    "market_sentiment": {
        "dom_trend": "stable",
        "inventory_months": 4.2,
        "yoy_price_change_pct": 2.1,
        "sale_to_list_ratio": 0.98,
        "classification": "balanced",
        "sentiment_adjustment_pct": 3.5
    },
    "market_overview": {
        "market_phase": "Balanced",
        "median_price": 455000,
        "median_ppsf": 248.50,
        "avg_dom": 28,
        "sale_to_list_ratio": 0.98,
        "active_count": 45,
        "pending_count": 22,
        "notes": ["Market observations..."]
    },
    "arv_calculation": {
        "base_ppsf": 235.20,
        "market_premium_pct": 9.4,
        "renovated_ppsf": 257.33,
        "subject_gla": 1850,
        "base_arv": 476061,
        "feature_adjustments": -5000,
        "sentiment_adjustment_pct": 3.5,
        "final_arv": 471000,
        "confidence_level": "Moderate",
        "confidence_band_pct": 5.0,
        "arv_low": 447450,
        "arv_high": 494550
    },
    "adjustments_applied": [
        {
            "comp_number": 1,
            "comp_address": "456 Oak Ave",
            "adjustment_type": "GLA",
            "reason": "Subject 70 sqft larger",
            "amount": -5000
        }
    ],
    "sources": ["MLS", "County Records", "Zillow"],
    "search_parameters": {
        "time_window": "90 days",
        "radius": "0.5 miles",
        "gla_range": "1600-2100 sqft"
    },
    "recommendations": ["Verification steps..."],
    "caveats": ["Disclaimers..."]
}
```

### Generate Reports

1. Save the analysis data to a JSON file
2. Run the Excel generator:
   ```bash
   python scripts/generate_excel_report.py output_report.xlsx data.json
   ```

## Execution Instructions

1. **Gather property address** from user
2. **Identify state** and determine disclosure status
3. **Load appropriate framework**:
   - Disclosure: `references/disclosure-prompt.md`
   - Non-Disclosure: `references/non-disclosure-prompt.md`
4. **Add property context** (if provided): current condition, seller notes, known issues
5. **Optional boundary drawing**: For block-by-block markets, use Zillow boundary tool
6. **Execute analysis** following the 9-step framework
7. **Screen for outliers** after gathering comps, before bucket classification
8. **Assess market sentiment** using quantitative indicators
9. **Classify recent renovations** properly into buckets
10. **Verify results**: Cross-reference with market knowledge
11. **Prepare data structure**: Compile all analysis into JSON format
12. **Generate deliverables**:
    - Run `generate_excel_report.py` for Excel breakdown
13. **Deliver all outputs** to user: Excel and in-context analysis

## Special Considerations

### Non-Disclosure State Caveats
- Wider confidence bands (+/-5-7% vs +/-2-5%)
- Must derive sold prices using triangulation methods
- Recommend "Option Period Verification" once under contract

### Source Code Alignment (comp_analyzer.py)

The following adjustment values are calibrated for the Knoxville, TN regional market and are used in the automated comp analyzer:

| Parameter | Value | Notes |
|-----------|-------|-------|
| $/sqft (GLA) | $85.00 | Per sqft of GLA difference |
| Bedroom | $5,000 | Per bedroom difference |
| Bathroom | $7,500 | Per bathroom difference |
| Year built | $500 | Per year of age difference |
| Lot size | $2.00/sqft | Capped at $15,000 max |
| Garage | $8,000 | Per stall difference |
| Market condition | 0.3%/month | Applied to comps >1 month old |

These are conservative Knoxville-calibrated values. For higher-value markets (>$500K), use the scaled values in the adjustment cheatsheet.
