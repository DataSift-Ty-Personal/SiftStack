# Non-Disclosure State Comping Framework (9-Step)

Use this framework for properties in non-disclosure states where sold prices are NOT publicly recorded (TX, UT, WY, NM, ID, MT, ND, AK, KS, MS, LA, MO).

**Key difference:** Must derive sold prices using the Triangulation Method since actual sale prices are hidden.

## Table of Contents
1. [Subject Property & CAD Check](#1-subject-property--cad-check)
2. [Comp Set Tightening - Visibility Filter](#2-comp-set-tightening---visibility-filter)
3. [Price Triangulation - The Non-Disclosure Solver](#3-price-triangulation---the-non-disclosure-solver)
4. [Outlier Screening](#4-outlier-screening)
5. [Market Direction & Inventory Analysis](#5-market-direction--inventory-analysis)
6. [Feature & Location Adjustments](#6-feature--location-adjustments)
7. [Basements, ADUs & Garage Conversions](#7-basements-adus--garage-conversions)
8. [Comparable Sales - Two-Bucket Estimate](#8-comparable-sales---two-bucket-estimate)
9. [Time & Size Normalization](#9-time--size-normalization)
10. [Final ARV Assembly - Range-Based](#10-final-arv-assembly---range-based)

---

## 1) Subject Property & CAD Check

In non-disclosure states, the **County Appraisal District (CAD)** is your source of truth for physical data (not market value).

**Auto-gather & confirm:**
- Lot size, GLA (Living Area), beds/baths, year built
- Garage, pool, zoning

**GLA verification (critical):**
- GLA = Gross Living Area = above-grade finished living space only
- Excludes: unfinished basement, garage, covered porches, attic, carports
- Includes: all finished rooms above grade, walkout basement with separate exterior entrance
- If finished basement without separate entrance: value separately at 50% of above-grade PPSF
- Note: CAD square footage and MLS square footage often differ — MLS is usually more accurate for livable space, CAD is the legal taxing baseline

**Verify ownership:**
- Current owner (LLC vs Individual)
- Recent deed transfers
- Check for significant price jump between last two sales (renovation indicator)

**Constraint check:**
- Zoning/HOA
- Floodplains (common in TX)
- Easements

**Discrepancy check:**
- Compare CAD Square Footage vs previous MLS listings
- **Note:** If CAD says 2,000 sqft but prior MLS listing says 2,400 sqft, MLS is often more accurate for "livable" space, but CAD is the legal taxing baseline
- If GLA differs by >5% between sources, flag and document which source used

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
Estimated Sold Price = Loan Amount / 0.80 (standard 20% down assumption)
```

**Corrections for loan type:**
| Loan Type | Calculation |
|-----------|-------------|
| Conventional | Loan / 0.80 |
| FHA | Loan / 0.965 (3.5% down) |
| VA | Loan / 1.00 (0% down) |

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

## 4) Outlier Screening

**After deriving ESPs, screen for outliers before bucket classification:**

1. Calculate the **median estimated PPSF** of all comps (ESP / GLA)
2. Calculate the **standard deviation** of comp PPSF values
3. Flag any comp with PPSF more than **2 standard deviations from the median**

**Exclusion rules:**
- **5+ comps total:** Exclude outliers from the analysis. Document each exclusion with the reason and derivation method used.
- **3-4 comps total:** Keep outliers but flag them with a notation. In non-disclosure states you often have fewer comps, so preserve data where possible.
- **2+ outliers:** Re-examine derivation methods. In non-disclosure states, outliers may indicate a derivation error (wrong loan type assumption, stale LLP, etc.) rather than a true market outlier.

**Non-disclosure-specific outlier causes:**
- Derivation error (wrong down payment assumption in Method B)
- Stale LLP from multiple price reductions not captured
- Cash sale with no deed of trust (Method B unavailable, LLP may be inaccurate)
- Estate/distress sale
- REO/bank-owned
- Major unreported renovation

**Document in output:** List any excluded or flagged comps under "Sources & Assumptions."

---

## 5) Market Direction & Inventory Analysis

**Since sold data is hidden, Active data is your only clear signal.**

### Actives & Pendings Analysis
- Analyze List Price of current competition
- Track DOM trends

### Quantitative Sentiment Assessment

| Indicator | Hot Market | Balanced Market | Cool Market |
|-----------|-----------|-----------------|-------------|
| DOM trend | Declining month-over-month | Stable | Increasing month-over-month |
| Inventory supply | <3 months | 3-6 months | >6 months |
| Active listing price cuts | <10% of actives | 10-25% of actives | >25% of actives |
| Pending-to-active ratio | >0.5 | 0.25-0.5 | <0.25 |
| **Sentiment adjustment** | **+5-7%** | **+3-5%** | **0-2%** |

**Data sources:** Redfin market tracker, Zillow ZHVI, local MLS, county assessor sale volumes.

### The "Ceiling" Test
> If fully renovated homes are sitting Active at $500k for 60+ days, your ARV cannot be $500k, regardless of what a hidden comp might suggest.

### Macro Factors
- TX/Non-Disclosure states often have high property taxes
- Check if recent tax hike is cooling the buyer pool
- Insurance rates (coastal/hail zones)

---

## 6) Feature & Location Adjustments

Adjust ESP based on visible differences (listing photos usually available even if price hidden).

### Critical Adjustments for Non-Disclosure States

| Feature | <$500k | >$500k | Notes |
|---------|--------|--------|-------|
| Bedroom | +$5,000 | +$10,000 | Per bedroom difference |
| Bathroom (full) | +/-$7,500 | +/-$10,000 | Per bathroom difference |
| Bathroom (half) | +/-$3,750 | +/-$5,000 | Per half bath difference |
| Foundation (TX/OK) | -$10k to -$30k | -$15k to -$40k | Check photos for cracks |
| Pool (hot climate) | +$15,000-$25,000 | +$20,000-$40,000 | TX/AZ/NV |
| Traffic/Backing | -$10,000 | -10-15% | Standard busy road discount |
| Garage vs Carport | Significant | Significant | Major in hail-prone states |

### Unusual Features

| Feature | Adjustment | Notes |
|---------|-----------|-------|
| Water/mountain views | +5-10% of home value | Same grade/elevation only |
| Easements | -5-10% | Worse if crosses buildable area |
| Corner lot | -3-5% | Less privacy, more maintenance |
| Cul-de-sac | +3-5% | More privacy, less traffic |
| Backing to commercial | -10-15% | Noise, traffic, visual impact |
| Power lines (<300 ft) | -5-10% | Impact zone ~300 feet |

### Foundation Check (TX/OK Priority)
- Look for cracks in listing photos
- Check for "foundation repair" mentions in prior descriptions
- Deduction: $10k-$30k depending on severity

---

## 7) Basements, ADUs & Garage Conversions

### Basements
- Rare in many non-disclosure states (like TX)
- If present, check if counted in CAD GLA
- NOT counted as GLA unless walkout with separate exterior entrance
- Finished basement: value at ~50% of above-grade PPSF
- Unfinished basement: value at ~10-15% of above-grade PPSF

### Garage Conversions
Common in older neighborhoods. Apply this rule:

| Conversion Quality | GLA Treatment |
|-------------------|---------------|
| Matches house (level floors, HVAC) | Count as full GLA |
| "Painted garage" (step down, window unit) | Value at 50% of PPSF |

---

## 8) Comparable Sales - Two-Bucket Estimate

Sort your **derived comps** (after outlier screening) into two buckets:

### Bucket A (Unrenovated)
- Similar age/size, dated finishes
- Calculate: **Median Estimated PPSF_A**

### Bucket B (Renovated)
- Fully updated
- Calculate: **Median Estimated PPSF_B**

### Recent Renovation Classification

| Renovation Timing | Bucket Classification |
|-------------------|-----------------------|
| Within 2 years of sale | Bucket B regardless of original condition |
| Within 5 years, substantial (kitchen + bath minimum) | Bucket B |
| Within 5 years, minor (paint, carpet only) | Bucket A |
| More than 5 years before sale | Classify based on current condition |

**Key indicator:** Significant price jump between last two sales strongly suggests renovation. In non-disclosure states, compare the LLPs of consecutive listings or check permit history.

### Market Premium Calculation (Bucket Spread)

This is a market-derived metric — the percentage premium that renovated homes command over unrenovated homes in this micro-market. It is NOT an estimate of rehab costs.

```
Market Premium (%) = (Est. PPSF_B - Est. PPSF_A) / Est. PPSF_A x 100%
```

**Non-Disclosure Constraint:**
> Assume a wider margin of error. If the spread is <10%, you likely overestimated the Unrenovated prices (sellers often list high and take lower offers that you can't see).

**Sanity check:**
- Typical spread: 5-30%
- If <5%: Re-examine Bucket A prices — likely overestimated
- If >30%: Re-examine both buckets — may include distressed or ultra-luxury comps

### Worked Example (Non-Disclosure)

**Subject:** 1,500 SF ranch, 3/2, built 1978, dated condition, Dallas TX

**Bucket A (4 comps, derived via LLP + DOM):** Median Est. PPSF_A = $120.20
**Bucket B (4 comps, derived via LLP + Deed of Trust):** Median Est. PPSF_B = $165.56

**Spread:** ($165.56 - $120.20) / $120.20 x 100% = 37.7%

**Sanity:** 37.7% exceeds 30%. Investigate:
- Are any Bucket A ESPs derived from stale LLPs?
- Were down payment assumptions correct for Bucket B (Method B)?
- Are Bucket B comps truly standard flips?

**Values (if comps check out, widen confidence band):**
- As-is: $120.20 x 1,500 = $180,300
- ARV: $165.56 x 1,500 = $248,340 (+/-7% = $230,956 - $265,724)
- Market premium: $68,040
- Rehab budget sanity: 40-70% of premium = $27,216-$47,628

---

## 9) Time & Size Normalization

### Time Adjustment
- If using "Last List Price," ensure it's not from 6 months ago in a dropping market
- If market dropped 5% since then, deduct 5% from ESP
- Default time adjustment: 0.3%/month (adjust based on local data)

### Size Curve
- Smaller homes = Higher PPSF
- Larger homes = Lower PPSF
- Apply standard normalization

---

## 10) Final ARV Assembly - Range-Based

**Due to data opacity, point estimates are risky. Provide a tight range.**

### Calculation Steps

1. **Base** = Est. PPSF_B (Renovated Bucket)
2. **Sentiment Adjustment** (from Step 5 assessment):
   - 0-2%: Cool market
   - 3-5%: Balanced market
   - 5-7%: Hot market
   - If Actives sitting >60 days, reduce Base by 3-5% regardless
3. **Calculate**: (Adjusted PPSF_B) x Subject GLA (above-grade only)
4. **Confidence Band**: Apply +/-5-7% range (wider than disclosure states due to derived prices)

---

## Output Requirements

### A) Step-by-Step ARV Logic

Show the math trail:
1. Derivation of Comp Prices (Method A/B/C used for each)
2. Outlier screening results (median, SD, excluded/flagged comps)
3. Unrenovated vs Renovated Spread (with sanity check)
4. Sentiment assessment (indicators and classification)
5. Adjustments for features
6. Final ARV Range

### B) Comps Summary Table (Non-Disclosure Format)

| Address | Status | LLP | DOM at LLP | Derived Price | Method | GLA | Bed/Bath | Year | Condition | Bucket | Est. PPSF | Adjustments | Final Adj Value | Outlier? |
|---------|--------|-----|------------|---------------|--------|-----|----------|------|-----------|--------|-----------|-------------|-----------------|----------|
| | | | | | | | | | | | | | | |

### C) Outlier Screening Results
- Median estimated PPSF: $___
- Standard deviation: $___
- Threshold (2 SD): $___-$___
- Excluded comps: [list with reasons and derivation method]
- Flagged comps: [list with reasons]

### D) Neighborhood & Market Overview

**Active Inventory Analysis** (primary market health indicator since sold data hidden):
- Current active listings and prices
- Pending activity

**Sentiment Assessment:**
- DOM trend: ___
- Inventory months: ___
- Active price cuts: ___%
- Classification: hot/balanced/cool
- Sentiment adjustment applied: ___%

**DOM Trends:**
- Are pendings happening in <10 days or >60 days?

**Tax/Insurance Note:**
- Mention if high property taxes or insurance rates (coastal/hail) are impacting affordability

### E) Sources & Assumptions

**Cite sources:**
- "Local MLS (via syndication)"
- "County Appraisal District"
- "Deed Records"
- "Redfin Market Tracker" / "Zillow ZHVI" (for sentiment)

**Required disclaimer:**
> "Sold prices are estimated based on Last List Price and DOM heuristics due to Non-Disclosure State regulations."

### F) Recommendations & Caveats

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
