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

### Comp Selection Filters
- **Age**: ≤90 days preferred, 6 months max
- **Subdivision**: Same micro-pocket, do not cross major roads
- **GLA**: ±100 sqft ideal, ±250 sqft outer bound
- **Property type**: Match elevation style (ranch↔ranch, 2-story↔2-story)
- **Build year**: ±10 years

### Feature Adjustments (by price tier)

See `references/adjustment-cheatsheet.md` for complete adjustment values.

**Quick reference:**
| Feature | <$500k Tier | >$500k Tier |
|---------|-------------|-------------|
| Bedroom | +$10k | +$25k |
| Bathroom | ±$10k | ±$10k |
| Garage | +$10-25k | +$10-25k |
| Traffic (backing) | -$10k | -10-15% |
| Traffic (fronting) | -$20k | -20% |

### Basement/ADU Rules
- Basements: Not counted as GLA; value at ~50% of above-grade PPSF if finished
- ADUs: 50% value if not separately deeded; 100% if separately titled

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
- Market Overview (Median price, PPSF, DOM, sale-to-list ratio, market phase)
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
            "adjusted_value": 480000
        }
    ],
    "bucket_analysis": {
        "unrenovated": { "count": 2, "median_ppsf": 235.20, "avg_ppsf": 235.20 },
        "renovated": { "count": 2, "median_ppsf": 257.33, "avg_ppsf": 257.33 },
        "market_premium_pct": 9.4
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
7. **Verify results**: Cross-reference with market knowledge
8. **Prepare data structure**: Compile all analysis into JSON format
9. **Generate deliverables**:
   - Run `generate_excel_report.py` for Excel breakdown
10. **Deliver all outputs** to user: Excel and in-context analysis

## Special Considerations

### Non-Disclosure State Caveats
- Wider confidence bands (±5-7% vs ±2-5%)
- Must derive sold prices using triangulation methods
- Recommend "Option Period Verification" once under contract

### Market Sentiment Adjustments
- Hot market (fast pendings, >50% over list): +5-7%
- Balanced market: +3-5%
- Cool market (60+ DOM, price cuts): 0-2% or negative

### Two-Bucket Spread Sanity Check
The spread between unrenovated and renovated bucket PPSF is a market-derived metric (not a rehab cost estimate). It reflects what buyers in that market are willing to pay for updated finishes.
- Typical spread: 10-30%
- <5% or >30%: Re-examine comp validity — comps may be miscategorized or the market may have unusual dynamics
