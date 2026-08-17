# Deal Analyzer Plugin

End-to-end investment property deal analysis — from property photos to offer strategy.

## What It Does

Combines property valuation (comping) and renovation cost estimation (rehab) into one unified workflow. Give it a property address and photos, and it returns:

- **ARV** — what the property is worth after renovation, backed by comparable sales, with confidence level (HIGH/MODERATE/LOW) based on comp count, recency, and spread
- **Rehab costs** — itemized scope of work for both full rehab (flip) and wholetail exits
- **Full deal math** — all-in costs including financing (6 loan types supported), holding, buying, and selling with verified default assumptions
- **Offer strategy** — MAO calculation (both 75% and 70% rules), exit strategy comparison using Profit Per Month tiebreaker, and risk disclaimer

## Components

| Component | Name | Description |
|-----------|------|-------------|
| Skill | `deal-analyzer` | Auto-triggers on deal analysis keywords. Orchestrates the full pipeline. |
| Command | `/analyze-deal` | Explicit trigger to run the workflow for a specific property. |

## How to Use

### Option 1: Natural Language
Just describe what you need:
- "Analyze this deal at 123 Main St, Dayton OH"
- "I've got photos of a property — what's it worth and what would the rehab cost?"
- "Run the numbers on this house"

### Option 2: Slash Command
```
/analyze-deal 123 Main St, Dayton OH 45402
```

### Best Results
For the most accurate analysis, provide:
1. **Property address** (required)
2. **Your walkthrough photos** (strongly recommended — the more rooms, the better)
3. **Purchase/contract price** (needed for deal math; without it you'll still get ARV + rehab + MAO)
4. **Any known issues** (roof age, HVAC status, foundation concerns)

## Key Features (v0.3.0)

- **ARV Confidence Levels** — every ARV includes HIGH/MODERATE/LOW rating based on comp count (5+/3-4/<3), recency (90 days/6 months/>6 months), and spread (<15%/15-25%/>25%)
- **Profit Per Month Tiebreaker** — when both exits are profitable, recommends based on capital velocity (profit/month), not just absolute profit
- **Dual MAO Calculation** — presents both 75% Rule (standard) and 70% Rule (conservative) so investors can choose their margin
- **6 Financing Types** — Hard Money, Conventional, DSCR, Portfolio, Seller Financing, Private Money with typical terms for each
- **Inline Photo Checklist** — detailed room-by-room assessment covering exterior, interior, kitchen, bathrooms, systems, and red flags
- **Verified Cost Defaults** — all assumptions (12% HML, 2 pts, 2.5% closing, 6% commission, $150-225/mo insurance, $200/mo utilities) cross-referenced with `src/deal_analyzer.py`
- **Mandatory Disclaimer** — every analysis output includes risk/estimation disclaimer

## Dependencies

This plugin bundles its own copies of the comping and rehab skills:
- **real-estate-comping** — provides the comp analysis methodology and Excel report generator
- **rehab-estimator** — provides the rehab estimation methodology, calibration data, and Excel report generator

## Deliverables Generated

Each analysis produces 2 Excel files:
1. **Comp Excel Workbook** — detailed comp breakdown with adjustments, ARV calculation
2. **Rehab Excel Workbook** — itemized cost breakdown for both exit strategies + deal math
