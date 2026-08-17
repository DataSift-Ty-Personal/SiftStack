# Feature & Location Adjustment Cheatsheet

Quick reference for property adjustments by price tier and market conditions.

## Source Code Reference (comp_analyzer.py)

These are the exact values used in the automated Knoxville-calibrated comp analyzer. When performing manual analysis, use these as the baseline for <$500K properties:

| Parameter | Code Value | Code Constant |
|-----------|-----------|---------------|
| $/sqft (GLA) | $85.00 | ADJ_PER_SQFT |
| Bedroom | $5,000 | ADJ_PER_BEDROOM |
| Bathroom | $7,500 | ADJ_PER_BATHROOM |
| Year built | $500/year | ADJ_PER_YEAR_BUILT |
| Lot size | $2.00/sqft (max $15K) | ADJ_PER_LOT_SQFT |
| Garage | $8,000/stall | ADJ_PER_GARAGE |
| Market condition | 0.3%/month | MARKET_CONDITION_PCT_PER_MONTH |

## GLA (Gross Living Area) Definition

**GLA = above-grade finished living space.** This is the denominator in all PPSF calculations.

**Included:** All finished rooms above grade, second/third stories, walkout basement with separate exterior entrance at grade.

**Excluded:** Unfinished basement, garage, covered porches, attic space (unless finished with permanent stairs and standard ceiling height), carports, breezeways, storage.

**Basement is NOT GLA** but has value — see Basement section below.

## Bedroom Adjustments

| Price Tier | Adjustment per Bedroom |
|------------|----------------------|
| <$500k | +$5,000 |
| >$500k | +$10,000 |

Note: comp_analyzer.py uses $5,000 flat (Knoxville calibration). For markets >$500K, scale to $10,000.

## Bathroom Adjustments

| Type | <$500k | >$500k |
|------|--------|--------|
| Full bath | +/-$7,500 | +/-$10,000 |
| Half bath | +/-$3,750 | +/-$5,000 |

Note: comp_analyzer.py uses $7,500 flat (Knoxville calibration). For markets >$500K, scale to $10,000.

## Parking Adjustments

| Feature | Standard | Hot/Cold Climate (AZ, IL, etc.) |
|---------|----------|--------------------------------|
| Garage | $8,000-$15,000/stall | $20,000-$25,000/stall |
| Carport | $5,000-$7,500 | $10,000 |
| No parking | Baseline | Baseline |

**Climate note:** Use high end in very hot markets (AZ, NV, TX) or very cold markets (IL, MN, WI) where covered parking is essential.

**Hail-prone areas:** Garage vs carport is a major value differentiator in TX, OK, CO.

Note: comp_analyzer.py uses $8,000/stall (Knoxville calibration). Scale up for climate-sensitive markets.

## Traffic & Location Adjustments

### <$500k Price Tier

| Location Issue | Adjustment |
|----------------|------------|
| Backing busy road | -$10,000 |
| Siding busy road | -$10,000 |
| Fronting busy road | -$20,000 |
| Commercial/industrial adjacency | -$10,000 to -$20,000 |
| Multifamily adjacency | -$5,000 to -$15,000 |

### >$500k Price Tier

| Location Issue | Adjustment |
|----------------|------------|
| Backing busy road | -10% to -15% |
| Siding busy road | -10% to -15% |
| Fronting busy road | -20% |
| Commercial/industrial adjacency | -10% to -15% |
| Multifamily adjacency | -5% to -10% |

## Unusual Feature Adjustments

| Feature | Adjustment | Notes |
|---------|-----------|-------|
| Pool (warm climate: TX, AZ, FL, NV) | +$15,000-$25,000 | Year-round use, highest value add |
| Pool (seasonal climate: TN, NC, VA) | +$5,000-$15,000 | 4-6 month use season |
| Pool (cold climate: MN, WI, MI) | $0 to -$5,000 | Maintenance liability, can be negative |
| Water/mountain views | +5-10% of home value | Compare only on same grade/elevation |
| Easements (utility, access) | -5-10% | Worse if easement crosses buildable area |
| Corner lot | -3-5% | Less privacy, more road frontage/maintenance |
| Cul-de-sac | +3-5% | More privacy, less traffic, child-friendly |
| Backing to commercial/industrial | -10-15% | Noise, traffic, visual impact |
| Power lines/cell tower (<300 ft) | -5-10% | Impact zone is roughly 300 feet |

**Pool condition matters:**
- Updated pool with modern finishes: full value
- Dated pool needing resurface: 50% of value
- Pool needing major repairs: may be net negative (removal cost $10,000-$15,000)

## View Premiums

| View Type | <$500k | >$500k | Luxury (>$1M) |
|-----------|--------|--------|---------------|
| Water view | +$20,000-$50,000 | +$50,000-$150,000 | +$100,000-$500,000+ |
| Mountain view | +$15,000-$40,000 | +$40,000-$100,000 | +$100,000-$300,000 |
| City skyline | +$10,000-$30,000 | +$30,000-$75,000 | +$75,000-$200,000 |
| Golf course | +$15,000-$35,000 | +$35,000-$80,000 | +$80,000-$150,000 |

**Hillside rule:** Compare only on same street and similar grade. Cross-street often breaks line-of-sight.

## Lot Size Adjustments

| Price Tier | Extra 5,000 sqft Value |
|------------|------------------------|
| <$500k | $5,000-$10,000 |
| >$500k | $30,000-$50,000 |
| Luxury (>$1M) | $50,000-$100,000+ |

**Additional lot value factors:**
- ADU eligibility: +$25,000-$75,000 (market dependent)
- Subdivision potential: Significant (calculate separately)
- Corner lot: -$5,000 to -$15,000 (market dependent, see Unusual Features above)
- Cul-de-sac: +$5,000-$15,000

Note: comp_analyzer.py uses $2.00/sqft capped at $15,000 (Knoxville calibration).

## Basement & ADU Adjustments

### Basements (Not counted as GLA)

| Finish Level | Value as % of Above-Grade PPSF |
|--------------|-------------------------------|
| Finished to same quality as main | ~50% |
| Finished with drop ceilings | ~35-40% |
| Partially finished | ~25-35% |
| Unfinished | ~10-15% |

### ADUs / Guest Houses

| Scenario | Value Credit |
|----------|--------------|
| Not separately deeded | ~50% of equivalent value |
| Separately deeded/titled | 100% at local PPSF |

## Foundation Adjustments (Critical for TX/OK)

| Issue | Adjustment |
|-------|------------|
| Minor cracks (cosmetic) | -$5,000 to -$10,000 |
| Moderate foundation issues | -$15,000 to -$25,000 |
| Major foundation repair needed | -$25,000 to -$40,000+ |
| Previous repair (documented) | -$5,000 to -$10,000 |

## Age/Condition Adjustments

| Condition | Adjustment |
|-----------|------------|
| Newer construction (<5 years) | +5-10% |
| Well-maintained original | Baseline |
| Dated but functional | -5-10% |
| Needs significant updates | -10-20% |
| Major deferred maintenance | -20-30% |

Note: comp_analyzer.py uses $500/year for year-built differences. The percentage-based condition adjustments above are applied separately when condition diverges significantly from age-based expectations.

## Recent Renovation Handling

| Renovation Timing | Bucket Classification |
|-------------------|-----------------------|
| Within 2 years of sale | Bucket B (renovated) regardless of original condition |
| Within 5 years of sale | Bucket B if substantial (kitchen + bath minimum) |
| More than 5 years before sale | Classify based on current condition vs comps |

**Key indicator:** Significant price jump between last two sales of the same property strongly suggests a flip/renovation. Cross-reference with permit history and listing photos.

## Market Sentiment Adjustments

| Indicator | Hot Market | Balanced Market | Cool Market |
|-----------|-----------|-----------------|-------------|
| DOM trend | Declining MoM | Stable | Increasing MoM |
| Inventory | <3 months supply | 3-6 months supply | >6 months supply |
| Price trend (YoY) | Rising 5%+ | Flat to +3% | Flat to declining |
| Sale-to-list ratio | >100% | 97-100% | <97% |
| **Sentiment adjustment** | **+5-7%** | **+3-5%** | **0-2%** |

**Data sources:**
- Redfin market tracker (monthly, metro + zip level)
- Zillow Home Value Index (ZHVI) for YoY trends
- Local MLS statistics (DOM, inventory, sale-to-list)
- County assessor recent sale volumes

## Time Adjustments

| Market Trend | Monthly Adjustment |
|--------------|-------------------|
| Appreciating (+6%/year) | +0.5% per month |
| Stable | 0% |
| Depreciating (-6%/year) | -0.5% per month |

**Rule:** Adjust any comp older than 3 months based on local trend data.

Note: comp_analyzer.py uses 0.3%/month (3.6% annualized) as the default appreciation rate for Knoxville. Adjust based on actual local market data.

## Outlier Detection Protocol

1. Calculate median PPSF of all comps
2. Calculate standard deviation of comp PPSF values
3. Flag comps >2 standard deviations from median
4. If 5+ comps: exclude outliers, document reason
5. If 3-4 comps: flag but keep outliers with notation
6. Common causes: estate/distress sale, REO, unreported renovation, seller concessions, related-party transaction

## Double-Counting Warning

**Do NOT apply the same adjustment twice:**
- If a comp already reflects a busy road discount in its sale price, don't deduct again
- Only adjust when normalizing against a non-discounted benchmark
- Document which adjustments are already "baked in" to comp prices
