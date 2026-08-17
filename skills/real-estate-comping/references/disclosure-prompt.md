# Disclosure State Comping Framework (9-Step)

Use this framework for properties in disclosure states where sold prices are publicly recorded.

## Table of Contents
1. [Subject Property & Public Records](#1-subject-property--public-records)
2. [Comp Set Tightening](#2-comp-set-tightening)
3. [Outlier Screening](#3-outlier-screening)
4. [Market Direction & Sentiment](#4-market-direction--sentiment)
5. [Lot, Zoning & Land Characteristics](#5-lot-zoning--land-characteristics)
6. [Feature & Location Adjustments](#6-feature--location-adjustments)
7. [Basements & ADUs](#7-basements--adus)
8. [Comparable Sales - Two-Bucket Method](#8-comparable-sales---two-bucket-method)
9. [Time & Size Normalization](#9-time--size-normalization)
10. [Final ARV Assembly](#10-final-arv-assembly)

---

## 1) Subject Property & Public Records

**Auto-gather & confirm:**
- Lot size, living area (above-grade GLA), beds/baths, year built
- Parking (garage/carport/none), basement/ADU presence, pool, view, zoning, HOA

**GLA verification (critical):**
- GLA = Gross Living Area = above-grade finished living space only
- Excludes: unfinished basement, garage, covered porches, attic, carports
- Includes: all finished rooms above grade, walkout basement with separate exterior entrance
- If finished basement without separate entrance: value separately at 50% of above-grade PPSF

**Ownership & history:**
- Tax history, owner type (LLC vs individual)
- Recent sales, liens, permits (especially additions/garage conversions/ADUs/basements)
- Check for significant price jump between last two sales (renovation indicator)

**Cross-check sources:**
- Local MLS, county assessor/recorder, permits
- Zillow/Redfin/Realtor.com, parcel GIS

**Flag discrepancies:**
- Example: Public records 1,300 sf vs MLS 1,500 sf
- Unpermitted additions, basement counted as GLA
- If GLA differs by >5% between sources, flag and use MLS as primary

**Legal/Physical constraints:**
- Zoning/HOA, historic district rules, floodplain
- Hillside overlays, lot coverage/FAR limits, easements

---

## 2) Comp Set Tightening

Apply these hard filters first:

| Filter | Ideal | Outer Bound |
|--------|-------|-------------|
| Age of comps | ≤90 days | 6 months max |
| GLA proximity | ±100 sf | ±250 sf |
| Build generation | ±10 years | Wider for 1890-1920 stock |

**Subdivision/Micro-pocket rules:**
- Do NOT cross major roads (thick yellow lines on Zillow = "do not cross")
- Confirm matching neighborhood/subdivision name when possible

**Property type matching:**
- Ranch to ranch, 2-story to 2-story
- Historic district to same district
- Match front-elevation style (colonial, Tudor, cottage, MCM)

**If constraints yield no comps:**
- Expand incrementally (radius/time)
- Document each relaxation + corresponding adjustments
- Flag if no credible comps remain (may not be a "comp-supported" deal)

---

## 3) Outlier Screening

**Before bucket classification, screen all comps for PPSF outliers:**

1. Calculate the **median PPSF** of all gathered comps
2. Calculate the **standard deviation** of comp PPSF values
3. Flag any comp with PPSF more than **2 standard deviations from the median**

**Exclusion rules:**
- **5+ comps total:** Exclude outliers from the analysis. Document each exclusion with the reason.
- **3-4 comps total:** Keep outliers but flag them with a notation. Too few comps to discard data.
- **2+ outliers in a small set:** Re-examine comp selection criteria. The set may be too heterogeneous — tighten filters or acknowledge wide variance.

**Common outlier causes to investigate:**
- Estate/distress sale (typically 10-20% below market)
- REO/bank-owned sale (typically 10-15% below market)
- Major unreported renovation (above-market PPSF)
- Seller concessions not reflected in recorded price
- Related-party/non-arm's-length transaction
- Lot premium or deficiency not captured in PPSF alone

**Document in output:** List any excluded or flagged comps under "Sources & Assumptions > Outlier exclusions."

---

## 4) Market Direction & Sentiment

**Check current market conditions:**
- Actives & Pendings (renovated and unrenovated)
- DOM, price cuts, sale-to-list trajectories

**Quantitative sentiment assessment:**

| Indicator | Hot Market | Balanced Market | Cool Market |
|-----------|-----------|-----------------|-------------|
| DOM trend | Declining month-over-month | Stable | Increasing month-over-month |
| Inventory supply | <3 months | 3-6 months | >6 months |
| YoY price change | Rising 5%+ | Flat to +3% | Flat to declining |
| Sale-to-list ratio | >100% | 97-100% | <97% |
| **Sentiment adjustment** | **+5-7%** | **+3-5%** | **0-2%** |

**Data sources for assessment:**
- Redfin market tracker (monthly updates, metro and zip level)
- Zillow Home Value Index (ZHVI) for YoY price trends
- Local MLS statistics (DOM, inventory, sale-to-list)
- County assessor recent sale volumes

**Macro factors to consider:**
- Elections, rate spikes, hurricanes, wildfires
- If remodeled actives languish (90-100+ DOM with price reductions), buyers will price risk in

**Market phase classification:**
| Phase | Indicators |
|-------|------------|
| Hot | Fast pendings, >50% over list, <10 DOM |
| Balanced | Normal DOM, list-price sales |
| Cool | 60+ DOM, price cuts, sitting inventory |

---

## 5) Lot, Zoning & Land Characteristics

**Additional value factors:**
- Expansion potential, ADU eligibility
- Small-lot splits, land assemblies, Opportunity Zones

**Lot size contribution by price tier:**
| Price Tier | Extra 5,000 sf Value |
|------------|---------------------|
| <$500k | $5k-$10k |
| >$500k | $30k-$50k |

**Neighborhood patterns to scan:**
- Are additions common?
- Are homes being scraped/rebuilt?
- Common play: "add a primary suite" (+400-500 sf)

---

## 6) Feature & Location Adjustments

Use paired sales when possible; otherwise use these anchor ranges:

### By Price Tier (aligned with comp_analyzer.py for <$500K)

| Feature | <$500k | >$500k |
|---------|--------|--------|
| Bedroom | +$5,000 | +$10,000 |
| Bathroom (full) | +/-$7,500 | +/-$10,000 |
| Bathroom (half) | +/-$3,750 | +/-$5,000 |
| Garage | $8,000-$15,000/stall | $10,000-$25,000/stall |
| Carport | $5,000-$10,000 | $5,000-$10,000 |

**Climate adjustment:** Use high end ($25k garage) in very hot/cold markets (AZ, IL)

### Traffic/Commercial/Multifamily Adjacency

| Location Issue | <$500k | >$500k |
|----------------|--------|--------|
| Backing/siding | -$10k | -10-15% |
| Fronting | -$20k | -20% |

### Unusual Features

| Feature | Adjustment | Notes |
|---------|-----------|-------|
| Pool (warm climate) | +$15,000-$25,000 | TX, AZ, FL, NV |
| Pool (seasonal climate) | +$5,000-$15,000 | TN, NC, VA |
| Pool (cold climate) | $0 to -$5,000 | MN, WI, MI |
| Water/mountain views | +5-10% of home value | Same grade only |
| Easements | -5-10% | Worse if crosses buildable area |
| Corner lot | -3-5% | Less privacy, more maintenance |
| Cul-de-sac | +3-5% | More privacy, less traffic |
| Backing to commercial | -10-15% | Noise, traffic, visual impact |
| Power lines (<300 ft) | -5-10% | Impact zone ~300 feet |

### Views/Hillside
- Value can range $100k to $1M in high-end markets
- Compare on same street and similar grade only
- Cross-street often breaks line-of-sight

**Avoid double-counting:** If a comp already reflects a discount, don't deduct again unless normalizing against a non-discounted benchmark.

---

## 7) Basements & ADUs

**Basement rules:**
- Appraisers count above-grade GLA only; basements are NOT GLA
- Finished to same quality as main floor: value at ~50% of above-grade PPSF
- Finished with drop ceilings/inferior: value at ~35-40%
- Partially finished: value at ~25-35%
- Unfinished: value at ~10-15%
- Exception: walkout basement with separate exterior entrance at grade = counted as GLA

**ADU/Guest house rules:**
| Scenario | Value Credit |
|----------|--------------|
| Not separately deeded | ~50% of equivalent value |
| Separately deeded/titled | Dollar-for-dollar at local PPSF |

**Always state which rule applied and why.**

---

## 8) Comparable Sales - Two-Bucket Method

Create two buckets under the tight filters from Step 2 (after outlier screening from Step 3):

### Bucket A (Unrenovated/Dated)
- Similar size/plan
- Average or below-average condition
- ≤6 months (prefer ≤90 days)
- Arm's-length sales only
- **Compute: Median PPSF_A**

### Bucket B (Fully Renovated/Premium)
- Flips or clearly modernized to market standard
- Verify via photos/remarks/permits
- Quality-adjust if one comp is ultra-lux beyond typical
- **Compute: Median PPSF_B**

### Recent Renovation Classification

Apply these rules when classifying comps with known renovation history:

| Renovation Timing | Bucket Classification |
|-------------------|-----------------------|
| Within 2 years of sale | Bucket B regardless of original condition |
| Within 5 years, substantial (kitchen + bath minimum) | Bucket B |
| Within 5 years, minor (paint, carpet only) | Bucket A |
| More than 5 years before sale | Classify based on current condition |

**Key indicator:** Significant price jump between last two sales of the same property (e.g., bought $150K, sold $260K 18 months later) = strong renovation signal. Cross-reference with permits and listing photos.

### Market Premium Calculation (Bucket Spread)

This is a market-derived metric — the percentage premium that renovated homes command over unrenovated homes in this micro-market. It is NOT an estimate of rehab costs.

```
Market Premium (%) = (PPSF_B - PPSF_A) / PPSF_A x 100%
```

**Sanity check:**
- Typical spread: 5-30%
- If <5%: Re-examine — unrenovated comps may be overpriced or market doesn't reward renovation
- If >30%: Re-examine — Bucket A may include distressed sales or Bucket B may include ultra-luxury beyond typical flip quality

### Worked Example

**Subject:** 1,500 SF ranch, 3/2, built 1978, dated condition

**Bucket A (5 comps):** Median PPSF_A = $120.20
**Bucket B (5 comps):** Median PPSF_B = $165.56

**Spread:** ($165.56 - $120.20) / $120.20 x 100% = 37.7%

**Sanity:** 37.7% exceeds 30% threshold. Investigate:
- Are any Bucket A comps distressed/estate sales pulling median down?
- Are Bucket B comps standard flips or ultra-luxury?
- Does this micro-market have unusual renovation demand?

**Values (if comps check out):**
- As-is: $120.20 x 1,500 = $180,300
- ARV: $165.56 x 1,500 = $248,340
- Market premium: $68,040
- Rehab budget sanity: 40-70% of premium = $27,216-$47,628

---

## 9) Time & Size Normalization

### Time Adjustment
- Adjust any comp older than 3-6 months
- Use local trend (monthly appreciation/depreciation from MLS medians or repeat sales)
- Default: 0.3%/month (comp_analyzer.py calibration); adjust based on actual local data
- Example: +/-3-6% annualized

**Market cycle awareness:**
- Account for 2022 peak / 2023-2024 trough + rebound patterns
- Don't justify today's value with an unadjusted 2022 peak comp

### Size Curve
- Smaller homes = higher PPSF
- Larger homes = lower PPSF
- Normalize when comp is near min/max of neighborhood size

---

## 10) Final ARV Assembly

### Calculation Steps

1. **Base PPSF** = PPSF_A (unrenovated baseline)
2. **Apply Market Premium** (bucket spread) = Renovated PPSF
3. **Apply market sentiment** (from Step 4 assessment):
   - 0-2%: Cool market
   - 3-5%: Balanced market
   - 5-7%: Hot market
4. **Fold in adjustments** from Steps 6-7 not already captured
5. **Calculate ARV** = Adjusted PPSF x Subject GLA (above-grade only)
6. **Present confidence band** (+/-2-5%) tied to comp spread and market volatility

---

## Output Template

### A) Step-by-Step ARV Breakdown
```
Unrenovated PPSF: $___
Market Premium (Bucket Spread): ___%
Renovated PPSF: $___
Sentiment Adjustment: ___% (classification: hot/balanced/cool)
Feature Adjustments: $___
Time/Size Adjustments: $___
Final ARV: $___
Confidence Range: $___-$___
```

### B) Comps Summary Table

| Address | Sale Date | Sale Price | GLA | Bed/Bath | Year | Condition | Bucket | Raw PPSF | Adjustments | Final Adj Value | Outlier? |
|---------|-----------|------------|-----|----------|------|-----------|--------|----------|-------------|-----------------|----------|
| | | | | | | | | | | | |

### C) Outlier Screening Results
- Median PPSF: $___
- Standard deviation: $___
- Threshold (2 SD): $___-$___
- Excluded comps: [list with reasons]
- Flagged comps: [list with reasons]

### D) Market Overview
- Median price, median PPSF, avg DOM
- Sale-to-list ratio, % > list
- Active vs pending counts
- Market phase read (hot/balanced/cool)
- Sentiment assessment: DOM trend, inventory months, YoY price change
- Sentiment adjustment applied: ___%

### E) Sources & Assumptions
- Data sources: MLS, county, Zillow, Redfin, parcel GIS, permit portals
- Time window: ___
- Radius: ___
- Subdivision constraints: ___
- Outlier exclusions: ___
- Renovation classifications: ___

### F) Recommendations & Caveats
- Data verification needs
- No double-counting confirmation
- Re-check risk for volatile periods
- **Disclaimer:** This mimics an appraisal process but is not a formal appraisal.
