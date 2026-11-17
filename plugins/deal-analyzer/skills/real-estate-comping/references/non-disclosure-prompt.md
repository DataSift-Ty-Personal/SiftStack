# Non-Disclosure State Comping Framework (9-Step)

Use this framework for properties in non-disclosure states where sold prices are NOT publicly recorded (TX, UT, WY, NM, ID, MT, ND, AK, KS, MS, LA, MO).

**Key difference:** Must derive sold prices using the Triangulation Method since actual sale prices are hidden.

## Table of Contents
1. [Subject Property & CAD Check](#1-subject-property--cad-check)
2. [Comp Set Tightening - Visibility Filter](#2-comp-set-tightening---visibility-filter)
3. [Price Triangulation - The Non-Disclosure Solver](#3-price-triangulation---the-non-disclosure-solver)
4. [Market Direction & Inventory Analysis](#4-market-direction--inventory-analysis)
5. [Feature & Location Adjustments](#5-feature--location-adjustments)
6. [Basements, ADUs & Garage Conversions](#6-basements-adus--garage-conversions)
7. [Comparable Sales - Two-Bucket Estimate](#7-comparable-sales---two-bucket-estimate)
8. [Time & Size Normalization](#8-time--size-normalization)
9. [Final ARV Assembly - Range-Based](#9-final-arv-assembly---range-based)

---

## 1) Subject Property & CAD Check

In non-disclosure states, the **County Appraisal District (CAD)** is your source of truth for physical data (not market value).

**Auto-gather & confirm:**
- Lot size, GLA (Living Area), beds/baths, year built
- Garage, pool, zoning

**Verify ownership:**
- Current owner (LLC vs Individual)
- Recent deed transfers

**Constraint check:**
- Zoning/HOA
- Floodplains (common in TX)
- Easements

**Discrepancy check:**
- Compare CAD Square Footage vs previous MLS listings
- **Note:** If CAD says 2,000 sqft but prior MLS listing says 2,400 sqft, MLS is often more accurate for "livable" space, but CAD is the legal taxing baseline

---

## 2) Comp Set Tightening - Visibility Filter

Since prices aren't visible, first find the **Right Houses**, then solve for price.

| Filter | Requirement | Notes |
|--------|-------------|-------|
| Age of Sale | ≤90 days preferred | >6 months is dangerous - can't easily time-adjust unknown prices |
| Subdivision | Strict adherence | Do NOT cross major arterials |
| GLA Proximity | ±100-250 sqft | |
| Visual Match | Same elevation style | "Texas Hill Country," "Dallas Brick Traditional," "Austin Bungalow" |

**Selection:** Choose 3-5 solid matches based on physical traits first, regardless of whether price is visible yet.

---

## 3) Price Triangulation - The Non-Disclosure Solver

**This is the unique step for non-disclosure states.** Derive the "Estimated Sold Price" (ESP) for each comp using at least two methods:

### Method A: Last List Price (LLP) + DOM Logic

1. Find the Last List Price before listing went "Pending"
2. Check DOM (Days on Market) at that price
3. Apply logic:

| DOM | Estimated Sold Price |
|-----|---------------------|
| <7 days | LLP or 101% of LLP |
| 7-30 days | 97-100% of LLP |
| 30-90+ days | 90-95% of LLP |

### Method B: Deed of Trust Calculation (Advanced)

1. Access public records (County Clerk/Recorder) for "Deed of Trust" or Mortgage
2. Find the Loan Amount recorded on sale date
3. Reverse math:

```
Estimated Sold Price = Loan Amount ÷ 0.80 (standard 20% down assumption)
```

**Corrections for loan type:**
| Loan Type | Calculation |
|-----------|-------------|
| Conventional | Loan ÷ 0.80 |
| FHA | Loan ÷ 0.965 (3.5% down) |
| VA | Loan ÷ 1.00 (0% down) |

Check buyer name/loan type to determine down payment assumption.

### Method C: Tax Value Ratio (Sanity Check Only)

1. Look at Assessed Value for year of sale
2. Calculate ratio of Assessed Value to List Price for active homes in area
   - Example: Homes listing at 1.2x their Tax Value
3. Apply multiplier to sold comp's Tax Value

**Use only as sanity check, not primary method.**

### Output for Each Comp

Clearly state:
- ESP (Estimated Sold Price)
- Method used (e.g., "$450k derived via Method A - LLP")

---

## 4) Market Direction & Inventory Analysis

**Since sold data is hidden, Active data is your only clear signal.**

### Actives & Pendings Analysis
- Analyze List Price of current competition
- Track DOM trends

### The "Ceiling" Test
> If fully renovated homes are sitting Active at $500k for 60+ days, your ARV cannot be $500k, regardless of what a hidden comp might suggest.

### Macro Factors
- TX/Non-Disclosure states often have high property taxes
- Check if recent tax hike is cooling the buyer pool
- Insurance rates (coastal/hail zones)

---

## 5) Feature & Location Adjustments

Adjust ESP based on visible differences (listing photos usually available even if price hidden).

### Critical Adjustments for Non-Disclosure States

| Feature | Adjustment | Notes |
|---------|------------|-------|
| Foundation | -$10k to -$30k | Crucial in TX/OK - look for cracks in photos |
| Pools | +$20k to +$40k | Valuable in hot climates (TX/AZ/NV) |
| Traffic/Backing | -10% | Standard discount for busy roads |
| Garage vs Carport | Significant | Major differentiator in hail-prone states |

### Foundation Check (TX/OK Priority)
- Look for cracks in listing photos
- Check for "foundation repair" mentions in prior descriptions
- Deduction: $10k-$30k depending on severity

---

## 6) Basements, ADUs & Garage Conversions

### Basements
- Rare in many non-disclosure states (like TX)
- If present, check if counted in CAD GLA

### Garage Conversions
Common in older neighborhoods. Apply this rule:

| Conversion Quality | GLA Treatment |
|-------------------|---------------|
| Matches house (level floors, HVAC) | Count as full GLA |
| "Painted garage" (step down, window unit) | Value at 50% of PPSF |

---

## 7) Comparable Sales - Two-Bucket Estimate

Sort your **derived comps** into two buckets:

### Bucket A (Unrenovated)
- Similar age/size, dated finishes
- Calculate: **Median Estimated PPSF_A**

### Bucket B (Renovated)
- Fully updated
- Calculate: **Median Estimated PPSF_B**

### Market Premium Calculation (Bucket Spread)

This is a market-derived metric — the percentage premium that renovated homes command over unrenovated homes in this micro-market. It is NOT an estimate of rehab costs.

```
Market Premium (%) = (Est. PPSF_B - Est. PPSF_A) / Est. PPSF_A × 100%
```

**Non-Disclosure Constraint:**
> Assume a wider margin of error. If the spread is <10%, you likely overestimated the Unrenovated prices (sellers often list high and take lower offers that you can't see).

---

## 8) Time & Size Normalization

### Time Adjustment
- If using "Last List Price," ensure it's not from 6 months ago in a dropping market
- If market dropped 5% since then, deduct 5% from ESP

### Size Curve
- Smaller homes → Higher PPSF
- Larger homes → Lower PPSF
- Apply standard normalization

---

## 9) Final ARV Assembly - Range-Based

**Due to data opacity, point estimates are risky. Provide a tight range.**

### Calculation Steps

1. **Base** = Est. PPSF_B (Renovated Bucket)
2. **Sentiment Adjustment**: If Actives sitting >60 days, reduce Base by 3-5%
3. **Calculate**: (Adjusted PPSF_B) × Subject GLA
4. **Confidence Band**: Apply ±5-7% range (wider than disclosure states)

---

## Output Requirements

### A) Step-by-Step ARV Logic

Show the math trail:
1. Derivation of Comp Prices (Method A/B)
2. Unrenovated vs Renovated Spread
3. Adjustments for features
4. Final ARV Range

### B) Comps Summary Table (Non-Disclosure Format)

| Column | Description |
|--------|-------------|
| Address | Property address |
| Status | Sold / Pending |
| Last List Price (LLP) | Final list price before pending |
| DOM at LLP | Days on market at that price |
| Derived Sold Price | Your calculated ESP |
| Method Used | e.g., "LLP - 3% discount" or "Loan Calc" |
| GLA / Beds / Baths / Year | Physical characteristics |
| Condition | Reno / Dated |
| Adjustments | Feature adjustments applied |
| Final Adjusted Value | ESP after all adjustments |

### C) Neighborhood & Market Overview

**Active Inventory Analysis** (primary market health indicator since sold data hidden):
- Current active listings and prices
- Pending activity

**DOM Trends:**
- Are pendings happening in <10 days or >60 days?

**Tax/Insurance Note:**
- Mention if high property taxes or insurance rates (coastal/hail) are impacting affordability

### D) Sources & Assumptions

**Cite sources:**
- "Local MLS (via syndication)"
- "County Appraisal District"
- "Deed Records"

**Required disclaimer:**
> "Sold prices are estimated based on Last List Price and DOM heuristics due to Non-Disclosure State regulations."

### E) Recommendations & Caveats

**Option Period Verification:**
> Advise that once property is under contract, the buyer (or their agent) must pull hard sold data from MLS to confirm ARV before option period expires.

**Loan Assumption Risk:**
> If using Method B (Loan math), note that large down payments can skew results lower.

---

## Quick Reference: Non-Disclosure States

| State | Notes |
|-------|-------|
| TX | High property taxes, foundation issues common, hail zones |
| UT | Mountain markets, seasonal variations |
| WY | Rural, limited comps |
| NM | Mixed markets, verify CAD data |
| ID | Growing markets, verify recent trends |
| MT | Rural, limited comps |
| ND | Oil market influence |
| AK | Unique market conditions |
| KS | Midwest market dynamics |
| MS | Flood zones common |
| LA | Flood/hurricane zones, insurance costs |
| MO | Mixed urban/rural |
