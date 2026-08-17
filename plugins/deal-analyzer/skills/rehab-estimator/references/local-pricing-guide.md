# Local Pricing Guide — Market-Specific Cost Calibration

National averages are useful baselines, but the actual cost to rehab a property varies dramatically by market. A $40K rehab in Dayton, OH might cost $80K+ in Denver, CO for the same scope of work. This guide provides the methodology for localizing every line item.

---

## Regional Cost Multipliers

The baseline prices in `rehab-categories.md` reflect national averages. Apply the multiplier below to adjust for the specific market.

### Tier 1: Low-Cost Markets (Multiplier: 0.75 – 0.90)

These markets have lower labor costs, cheaper materials, and less regulatory burden.

| Market Examples | Typical Multiplier | Notes |
|-----------------|-------------------|-------|
| Dayton, OH | 0.82 | Strong contractor availability, low permit costs |
| Cleveland, OH | 0.85 | Slightly higher than rural OH |
| Memphis, TN | 0.80 | Very affordable labor, watch quality |
| Birmingham, AL | 0.78 | Lowest tier, excellent contractor rates |
| Indianapolis, IN | 0.85 | Growing market but still affordable |
| St. Louis, MO | 0.83 | Metro affordable, county varies |
| Detroit, MI | 0.80 | Labor cheap, material delivery can add cost |
| Jackson, MS | 0.75 | Lowest cost markets in US |
| Little Rock, AR | 0.78 | Low cost, limited contractor pool in rural |
| Tulsa, OK | 0.80 | Affordable, watch foundation costs |
| Knoxville, TN | 0.88 | Primary SiftStack market. Confirmed in rehab_estimator.py source |
| Blount County, TN | 0.86 | Secondary SiftStack market. Slightly lower than Knox |
| Chattanooga, TN | 0.90 | Mid-range TN market |
| San Antonio, TX | 0.88 | Growing but still below national |

### Tier 2: Mid-Cost Markets (Multiplier: 0.90 – 1.10)

These markets track close to national averages.

| Market Examples | Typical Multiplier | Notes |
|-----------------|-------------------|-------|
| Columbus, OH | 0.92 | Growing market pushing costs up |
| Nashville, TN | 1.05 | Rapid growth = higher labor demand |
| Charlotte, NC | 0.95 | Mid-range, good contractor pool |
| Atlanta, GA | 0.98 | Metro varies widely by county |
| Phoenix, AZ | 1.00 | Dead average nationally |
| Tampa, FL | 0.95 | Competitive market |
| Dallas, TX | 1.00 | Near national average |
| Houston, TX | 0.95 | Large contractor pool keeps prices competitive |
| Kansas City, MO | 0.88 | Below average but trending up |
| Raleigh, NC | 0.98 | Tech growth pushing costs |
| Orlando, FL | 0.95 | Tourism economy, good labor pool |
| Las Vegas, NV | 1.00 | Average |
| Minneapolis, MN | 1.05 | Cold climate adds seasonal premium |

### Tier 3: High-Cost Markets (Multiplier: 1.10 – 1.40)

Higher labor rates, more permitting, and expensive materials.

| Market Examples | Typical Multiplier | Notes |
|-----------------|-------------------|-------|
| Denver, CO | 1.15 | Mountain premium, strong demand |
| Austin, TX | 1.10 | Rapid growth, above TX average |
| Portland, OR | 1.20 | Strict permitting, union influence |
| Seattle, WA | 1.25 | High labor rates, progressive regulations |
| Miami, FL | 1.15 | Hurricane code, specialty labor |
| Washington, DC | 1.25 | Government area premium |
| Chicago, IL | 1.15 | Union labor, winter premium |
| Boston, MA | 1.30 | Extreme permitting, old housing stock |

### Tier 4: Premium Markets (Multiplier: 1.40+)

Top-of-market labor and regulatory costs.

| Market Examples | Typical Multiplier | Notes |
|-----------------|-------------------|-------|
| San Francisco, CA | 1.55 | Highest in nation for labor |
| Los Angeles, CA | 1.40 | High labor, earthquake retrofit costs |
| San Diego, CA | 1.35 | California regulatory burden |
| New York City, NY | 1.60 | Union, licensing, material delivery costs |
| Honolulu, HI | 1.65 | Island premium on everything |
| Northern NJ (NYC metro) | 1.35 | NJ licensing adds cost |

---

## How to Apply the Multiplier

The multiplier applies differently to labor vs materials:

**Labor component** (typically 40-60% of line item cost): Apply full multiplier.
**Material component** (typically 40-60% of line item cost): Apply 50% of the multiplier deviation from 1.0.

### Example
National average for LVP flooring install: $5.00/SF (mid)
- Assume 50/50 labor/material split: $2.50 labor + $2.50 material
- Dayton, OH multiplier: 0.82
- Labor adjustment: $2.50 × 0.82 = $2.05
- Material adjustment: $2.50 × (1 + (0.82 - 1.0) × 0.5) = $2.50 × 0.91 = $2.28
- Local price: $2.05 + $2.28 = **$4.33/SF** (vs $5.00 national)

### Simplified Method
For speed, you can apply the multiplier to the full line item when the split isn't worth calculating individually. The blended approach introduces ~5% error which is acceptable for estimation.

---

## Researching Local Rates

When the multiplier tables above don't cover a market or you need validation, use web search:

### Search Queries (Replace [City] [State] [Year])
1. `"[City] [State]" general contractor rates [Year]`
2. `"[City] [State]" kitchen remodel cost [Year]`
3. `"[City] [State]" bathroom remodel cost per square foot [Year]`
4. `"[City] [State]" home renovation cost [Year]`
5. `"[Metro area]" construction cost index [Year]`
6. `cost of living index "[City]" OR "[County]" [Year]`
7. `RSMeans construction cost data "[City]" OR "[State]" [Year]`

### Key Sources
- **RSMeans / Gordian** — Industry standard construction cost data (may require subscription, but summaries are often available)
- **HomeAdvisor / Angi** — Crowd-sourced project costs by zip code
- **Remodeling Magazine Cost vs Value Report** — Annual report by metro area
- **Thumbtack** — Real contractor pricing by market
- **BLS Occupational Employment Statistics** — Actual labor rates by metro area for construction trades
- **Zillow / Redfin renovation cost tools** — Market-specific estimates

### BLS Labor Rate Lookup
The Bureau of Labor Statistics publishes actual wages for construction trades by metro area. These are the most authoritative source for labor rate calibration.

Key occupation codes:
- 47-2031: Carpenters
- 47-2111: Electricians
- 47-2152: Plumbers
- 47-2141: Painters
- 47-1011: Construction supervisors (GC rate proxy)
- 47-2181: Roofers
- 49-9021: HVAC mechanics

---

## Special Local Considerations

### Climate-Specific Costs
| Climate Factor | Markets Affected | Cost Impact |
|---------------|-----------------|-------------|
| Hurricane/wind code | FL, TX coast, Gulf states | +10-20% on roofing, windows, structural |
| Earthquake retrofit | CA, PNW, parts of TN/MO | +5-15% on foundation, framing |
| Freeze/thaw foundation | Northern states | +10% on concrete, exterior |
| Snow load roofing | CO, MN, WI, northern states | +5-10% on roofing specs |
| High humidity (mold) | Gulf states, FL, HI | +10% on waterproofing, ventilation |
| Radon prevalence | OH, PA, IA, IL | Budget radon mitigation (~$1,200) |

### Regulatory Cost Factors
| Factor | Markets | Impact |
|--------|---------|--------|
| Union labor requirements | NYC, Chicago, parts of CA, NJ | +20-40% on labor |
| Historic district restrictions | Varies (check locally) | +15-30%, material matching required |
| Point-of-sale inspection | Some OH/PA cities | Pre-sale repairs mandated |
| Lead paint disclosure (pre-1978) | National | Testing + potential abatement |
| Asbestos (pre-1985) | National | Testing $200-600, abatement varies |
| Short-term rental restrictions | Many cities | May limit exit strategy, not cost |

### Material Availability
| Material | Availability Notes |
|----------|-------------------|
| Lumber | Prices swing 30-50% based on supply chain; check current pricing |
| Concrete | Regional pricing varies 20-30% |
| Specialty tile | Coastal/urban markets have more selection at better prices |
| Appliances | National pricing mostly flat; delivery costs vary |
| HVAC equipment | Regional distributor pricing varies 10-15% |

---

## Permit Cost Research

Permit costs are one of the most variable local factors. They range from $100 for a simple cosmetic permit in a rural county to $5,000+ for a major renovation permit in a strict urban jurisdiction.

### How to Research
1. Search: `"[City]" OR "[County]" building permit fees schedule`
2. Check the city/county building department website directly
3. Look for the published fee schedule (usually a PDF)
4. Note whether permits require licensed contractors (some cities do)

### Common Permit Triggers
| Work Type | Typically Requires Permit |
|-----------|--------------------------|
| Structural changes | Always |
| Electrical panel work | Always |
| Plumbing rough-in changes | Always |
| HVAC system replacement | Usually |
| Roof replacement | Often (varies by jurisdiction) |
| Window replacement (same size) | Sometimes |
| Cosmetic only (paint, flooring, cabinets) | Rarely |
| Water heater replacement | Often |

---

## Calibration Methodology

The regional multiplier is a living number that should be refined with real data. Here is how to calibrate for a new market:

### Step 1: Use Initial Multiplier
Start with the closest market from the tables above. If the exact city is not listed, use the state/regional average or interpolate between two nearby markets.

### Step 2: Collect Real Data
After receiving 3-5 actual contractor SOWs (scopes of work) for properties in the target market, compare each SOW total against the skill's estimate for the same scope.

### Step 3: Calculate Actual Multiplier
```
Actual Multiplier = Average(Real SOW Total / Skill Estimate at 1.0 multiplier)
```

### Step 4: Update and Track
Record the calibrated multiplier with the date and sample size. Re-calibrate annually or when material/labor costs shift significantly (e.g., post-COVID lumber spikes).

### Example: Knoxville, TN Calibration
- Source: `rehab_estimator.py` in SiftStack codebase
- Multiplier: **0.88** (12% below national average)
- Basis: Regional labor rates and material costs in East Tennessee
- Blount County variation: **0.86** (slightly lower than Knox metro)
- Nashville, TN comparison: **0.95** (higher demand market pushing costs up)
