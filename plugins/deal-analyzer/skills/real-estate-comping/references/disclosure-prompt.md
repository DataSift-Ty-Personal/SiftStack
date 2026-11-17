# Disclosure State Comping Framework (9-Step)

Use this framework for properties in disclosure states where sold prices are publicly recorded.

## Table of Contents
1. [Subject Property & Public Records](#1-subject-property--public-records)
2. [Comp Set Tightening](#2-comp-set-tightening)
3. [Market Direction & Sentiment](#3-market-direction--sentiment)
4. [Lot, Zoning & Land Characteristics](#4-lot-zoning--land-characteristics)
5. [Feature & Location Adjustments](#5-feature--location-adjustments)
6. [Basements & ADUs](#6-basements--adus)
7. [Comparable Sales - Two-Bucket Method](#7-comparable-sales---two-bucket-method)
8. [Time & Size Normalization](#8-time--size-normalization)
9. [Final ARV Assembly](#9-final-arv-assembly)

---

## 1) Subject Property & Public Records

**Auto-gather & confirm:**
- Lot size, living area (above-grade GLA), beds/baths, year built
- Parking (garage/carport/none), basement/ADU presence, pool, view, zoning, HOA

**Ownership & history:**
- Tax history, owner type (LLC vs individual)
- Recent sales, liens, permits (especially additions/garage conversions/ADUs/basements)

**Cross-check sources:**
- Local MLS, county assessor/recorder, permits
- Zillow/Redfin/Realtor.com, parcel GIS

**Flag discrepancies:**
- Example: Public records 1,300 sf vs MLS 1,500 sf
- Unpermitted additions, basement counted as GLA

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
- Ranch ↔ ranch, 2-story ↔ 2-story
- Historic district ↔ same district
- Match front-elevation style (colonial, Tudor, cottage, MCM)

**If constraints yield no comps:**
- Expand incrementally (radius/time)
- Document each relaxation + corresponding adjustments
- Flag if no credible comps remain (may not be a "comp-supported" deal)

---

## 3) Market Direction & Sentiment

**Check current market conditions:**
- Actives & Pendings (renovated and unrenovated)
- DOM, price cuts, sale-to-list trajectories

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

## 4) Lot, Zoning & Land Characteristics

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

## 5) Feature & Location Adjustments

Use paired sales when possible; otherwise use these anchor ranges:

### By Price Tier

| Feature | <$500k | >$500k |
|---------|--------|--------|
| Bedroom | +$10k | +$25k |
| Bathroom | ±$10k | ±$10k |
| Garage | $10k-$25k | $10k-$25k |
| Carport | $5k-$10k | $5k-$10k |

**Climate adjustment:** Use high end ($25k garage) in very hot/cold markets (AZ, IL)

### Traffic/Commercial/Multifamily Adjacency

| Location Issue | <$500k | >$500k |
|----------------|--------|--------|
| Backing/siding | -$10k | -10-15% |
| Fronting | -$20k | -20% |

### Views/Hillside
- Value can range $100k → $1M in high-end markets
- Compare on same street and similar grade only
- Cross-street often breaks line-of-sight

**Avoid double-counting:** If a comp already reflects a discount, don't deduct again unless normalizing against a non-discounted benchmark.

---

## 6) Basements & ADUs

**Basement rules:**
- Appraisers count above-grade GLA only; basements are NOT GLA
- Basement value: up to ~50% of equivalent above-grade value if finished to same quality
- Less if inferior finish (e.g., drop ceilings)

**ADU/Guest house rules:**
| Scenario | Value Credit |
|----------|--------------|
| Not separately deeded | ~50% of equivalent value |
| Separately deeded/titled | Dollar-for-dollar at local PPSF |

**Always state which rule applied and why.**

---

## 7) Comparable Sales - Two-Bucket Method

Create two buckets under the tight filters from Step 2:

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

### Market Premium Calculation (Bucket Spread)

This is a market-derived metric — the percentage premium that renovated homes command over unrenovated homes in this micro-market. It is NOT an estimate of rehab costs.

```
Market Premium (%) = (PPSF_B - PPSF_A) / PPSF_A × 100%
```

**Sanity check:**
- Typical spread: 10-30%
- If <5% or >30%: Re-examine comp validity or acknowledge atypical area dynamics

---

## 8) Time & Size Normalization

### Time Adjustment
- Adjust any comp older than 3-6 months
- Use local trend (monthly appreciation/depreciation from MLS medians or repeat sales)
- Example: +/−3-6% annualized

**Market cycle awareness:**
- Account for 2022 peak / 2023-2024 trough + rebound patterns
- Don't justify today's value with an unadjusted 2022 peak comp

### Size Curve
- Smaller homes → higher PPSF
- Larger homes → lower PPSF
- Normalize when comp is near min/max of neighborhood size

---

## 9) Final ARV Assembly

### Calculation Steps

1. **Base PPSF** = PPSF_A (unrenovated baseline)
2. **Apply Market Premium** (bucket spread) → Renovated PPSF
3. **Apply market sentiment** (0-7% tweak):
   - 0-2%: Cooler market
   - 3-5%: Balanced
   - 5-7%: Very hot (≥50% over list, fast pendings)
4. **Fold in adjustments** from Steps 5-6 not already captured
5. **Calculate ARV** = Adjusted PPSF × Subject GLA (above-grade)
6. **Present confidence band** (±2-5%) tied to comp spread and market volatility

---

## Output Template

### A) Step-by-Step ARV Breakdown
```
Unrenovated PPSF: $___
Market Premium (Bucket Spread): ___%
Renovated PPSF: $___
Sentiment Adjustment: ___%
Feature Adjustments: $___
Time/Size Adjustments: $___
Final ARV: $___
Confidence Range: $___-$___
```

### B) Comps Summary Table

| Address | Sale Date | Sale Price | GLA | Bed/Bath | Year | Condition | Raw PPSF | Adjustments | Final Adj Value |
|---------|-----------|------------|-----|----------|------|-----------|----------|-------------|-----------------|
| | | | | | | | | | |

### C) Market Overview
- Median price, median PPSF, avg DOM
- Sale-to-list ratio, % > list
- Active vs pending counts
- Market phase read (hot/balanced/cool)

### D) Sources & Assumptions
- Data sources: MLS, county, Zillow, Redfin, parcel GIS, permit portals
- Time window: ___
- Radius: ___
- Subdivision constraints: ___
- Outlier exclusions: ___

### E) Recommendations & Caveats
- Data verification needs
- No double-counting confirmation
- Re-check risk for volatile periods
- **Disclaimer:** This mimics an appraisal process but is not a formal appraisal.
