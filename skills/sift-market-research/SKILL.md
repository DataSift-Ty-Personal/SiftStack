---
name: sift-market-research
description: Sift Market Finder research for real estate wholesaling with comprehensive market analysis. Use when the user requests market analysis, finding best zip codes for marketing, identifying investor activity hotspots, analyzing wholesaling markets, or creating detailed county-level investment reports. Combines Sift proprietary data with public sources (BLS, Census, Zillow, Redfin, FBI Crime Data) for quantitative data blending.
---

# Sift Market Research

Automate market research using Sift's Market Finder combined with public data sources to produce comprehensive, actionable market analysis for real estate investors.

## Mandatory Output Requirements

**All data outputs MUST include a properly formatted Excel spreadsheet (.xlsx).** Never use plain text files (.txt) or poorly formatted data dumps.

| Output Type | Required Format | Template Reference |
|-------------|-----------------|-------------------|
| Quick Research | Excel (.xlsx) | `templates/MarketFinderResearchExample.xlsx` |
| Comprehensive Analysis | Excel (.xlsx) + Markdown Report | Use template structure |
| Data Exports | Excel (.xlsx) | Multi-sheet workbook |

**Output Rules:**
1. All tabular data MUST be in Excel spreadsheet format
2. Use the template at `templates/MarketFinderResearchExample.xlsx` as the structural guide
3. Include proper column headers, formatting, and multiple worksheets as needed
4. Pairing a Markdown report with the Excel spreadsheet is encouraged for comprehensive analysis
5. NEVER output raw text files or unformatted data dumps as standalone deliverables

**Acceptable Output Combinations:**
- Excel spreadsheet only (for quick research)
- Excel spreadsheet + Markdown report (for comprehensive analysis)
- Excel spreadsheet + PDF summary (when requested)

**NOT Acceptable:**
- Text files (.txt) containing data tables
- Markdown-only outputs with no accompanying spreadsheet
- Unformatted data dumps

## Credentials

Use your DataSift account credentials (email and password from app.reisift.io). Never hardcode credentials in skill files, prompts, or shared documents.

**Login URL:** `https://app.reisift.io/`

## Execution Mode Detection

Before starting, detect the execution environment to determine whether automation is available:

**Check 1:** Does `scripts/extract_market_finder.py` exist in this skill's directory?
**Check 2:** Is Playwright available? Run: `python -c "from playwright.sync_api import sync_playwright; print('OK')"`
**Check 3:** Are credentials set? Check for `DATASIFT_EMAIL` and `DATASIFT_PASSWORD` in `.env` or environment variables.

### Automated Mode (Claude Code CLI — all 3 checks pass)

Run the extraction script directly:

```bash
# Single county
python scripts/extract_market_finder.py --state "Tennessee" --county "Knox"

# Multiple counties
python scripts/extract_market_finder.py --state "Tennessee" --county "Knox,Blount"

# Headless mode (no visible browser)
python scripts/extract_market_finder.py --state "Tennessee" --county "Knox" --headless
```

The script handles login, Market Finder navigation, data extraction (ZIP codes + neighborhoods + summary panel), and outputs a JSON file. Then generate the Excel report:

```bash
python scripts/generate_market_excel.py --input output/market_finder_Tennessee_Knox_*.json --fred-dom 71
```

After automated extraction, skip directly to the Excel generation and analysis steps (Step 7). The JSON output contains all data that Steps 2-6 would produce manually.

### Manual Mode (Co-Work or no Playwright — any check fails)

Follow Steps 2-6 below manually. Claude will guide you through the DataSift UI step by step. You can still use `scripts/generate_market_excel.py` for Excel generation if you format your extracted data as JSON.

## Workflow Overview

1. Determine analysis type (Quick Research vs. Comprehensive Analysis)
2. Get FRED baseline for Days on Market
3. Login to Sift and extract Market Map data
4. For Comprehensive Analysis: gather public data and blend with Market Map data
5. Generate output (spreadsheet or full market report)

**Quick Research?** -> Follow Steps 1-7 below for Sift data extraction only
**Comprehensive Analysis?** -> Follow all steps, then see `references/ComprehensiveAnalysisFramework.md`

## Step 1: Get FRED National Baseline

Before analyzing markets, get the national Days on Market baseline from FRED.

**CRITICAL: Verify the current value before every analysis.**

1. Navigate to https://fred.stlouisfed.org/series/MEDDAYONMARUS
2. Record the most recent monthly value displayed on the chart (look at the rightmost data point)
3. Note the date of the most recent observation (e.g., "Dec 2025: 71 days")
4. Use this exact value as your national baseline -- do NOT use an estimate

**Why this matters:** The national median DOM fluctuates monthly (it has ranged from 30 days to 90+ days in recent years). Using a stale estimate produces incorrect market comparisons.

- Markets 15-20% below the FRED baseline indicate faster-moving markets
- Markets at or near the baseline are average
- Markets 50%+ above the baseline are slow-moving

## Step 2: Login to Sift

1. Navigate to `https://app.reisift.io/`
2. Enter your DataSift email and password
3. Check the "I agree to the Terms of Use" checkbox
4. Click "Sign In" button
5. Dismiss any notification popups that appear
6. Wait for dashboard to load

## Step 3: Navigate to Market Finder

1. From dashboard, click "Market Finder" in the left sidebar navigation
2. The interface shows:
   - **Top filters**: Select States -> Select Counties -> ZIP Codes/Neighborhoods toggle -> Select ZIP Codes -> Property Type
   - **Heat map**: Interactive US map with color-coded investor transaction density
   - **Legend**: Mo. Investor Transactions dropdown (can switch to Homes Sold, Median Sale Price, Homes on Market, Days on Market)
   - **Data table**: State/County/Zip level data with columns for investor transactions, homes on market, homes sold, days on market, median values
   - **Right panel**: Summary cards (Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month) plus Home Prices distribution, Homeownership Rate, Market Rent, Gross Rental Yield, Property Types, Bedrooms, Year Built

**Navigation Flow:**
- Default view shows all US states ranked by investor transactions
- Click "Select States" dropdown -> select target state -> view updates to show counties
- Click "Select Counties" dropdown -> select target county -> view updates to show zip codes
- ZIP Codes toggle switches between ZIP Codes and Neighborhoods view

### ZIP Code vs. Neighborhood View: When to Use Each

| View | Best For | Scope |
|------|----------|-------|
| ZIP Code | Initial market screening, broad comparison across a county | Each row = one ZIP code (~15-30 rows per county) |
| Neighborhood | Deep-dive into a specific ZIP, micro-pocket analysis | Each row = one neighborhood within the selected area |

**Decision guide:**
- **Start with ZIP Code view** to identify which ZIP codes have the most investor activity, fastest DOM, and best price points
- **Switch to Neighborhood view** after you have identified your top 5-10 target ZIPs -- this reveals block-level variation within a ZIP
- Neighborhood view is especially useful for comp analysis (finding which sub-areas within a ZIP have the tightest spreads)
- Some ZIP codes contain neighborhoods with vastly different investor activity levels -- the neighborhood view exposes this

## Step 4: State-Level Analysis

The default view shows states sorted by "Total Inv. Trans. for 6 Mo." (investor transactions).

**Top States by Investor Activity (as of Jan 2026):**
| State | 6-Mo Investor Trans | Days on Market | Notes |
|-------|---------------------|----------------|-------|
| Texas | 41,709 | 65 | Highest activity |
| Florida | 27,131 | 73 | At FRED baseline |
| California | 15,613 | 41 | Fast market |
| Georgia | 12,299 | 59 | Below baseline |
| North Carolina | 11,869 | 60 | Below baseline |
| Ohio | 11,311 | 45 | Fast market |
| Tennessee | 6,830 | 62 | Below baseline |

**Green Light Criteria:**
- Days on Market at or 15-20% below FRED baseline
- 500+ investor transactions in 6 months
- Reasonable inventory levels

**Red Flags:**
- Days on Market 50%+ above national average
- Less than 10 investor transactions in 6 months

**To analyze a state:**
1. Click the "Select States" dropdown
2. Select your target state from the alphabetical list
3. The map zooms to show county-level data
4. Data table updates to show counties ranked by investor transactions

## Step 5: County-Level Analysis

After selecting a state, the data table shows counties with these columns:
- **County**: County name
- **Total Inv. Trans. for 6 Mo.**: 6-month investor transaction count
- **Homes on Market**: Current inventory
- **Homes Sold Last Month**: Recent sales volume
- **Median Days on Market**: Average time to sell
- **Median Home Value**: Current values
- **Median Sale Price**: Recent sale prices

**Example - Tennessee Counties:**
| County | 6-Mo Inv Trans | Homes on Market | Homes Sold | Days on Market |
|--------|----------------|-----------------|------------|----------------|
| Shelby (Memphis) | 1,306 | 4,200+ | 937 | 58 |
| Davidson (Nashville) | 683 | 4,172 | 853 | 59 |
| Knox (Knoxville) | 462 | 2,700+ | 668 | 57 |
| Hamilton (Chattanooga) | 449 | 2,800+ | 538 | 56 |

**Evaluation Criteria:**
| Metric | Ideal Range | Notes |
|--------|-------------|-------|
| 6-Month Investor Transactions | 50+ | Indicates active market |
| Monthly Investor Transactions | 10+ | Minimum for wholesaling |
| Days on Market | Below state avg | Faster-moving |
| Supply (Months of Inventory) | 3-6 months | Balanced market |

**Calculate Supply (Months of Inventory):**

```
Supply = Homes on Market / Homes Sold Last Month
```

**Formula explained:** This tells you how many months it would take to sell all current inventory at the current sales pace. It is the single most important indicator of whether you are in a buyer's or seller's market.

| Supply Range | Market Type | What It Means for Investors |
|--------------|-------------|----------------------------|
| Under 3 months | Seller's market | Prices rising, homes sell fast, harder to negotiate, strong buyer demand for wholesale deals |
| 3-6 months | Balanced market | Healthy conditions, reasonable negotiation room, good for both wholesale and flip |
| Over 6 months | Buyer's market | Prices declining, homes sit longer, more motivated sellers, but fewer end buyers for assignments |

**Example calculation:**
- Knox County: 2,700 homes on market / 668 homes sold last month = **4.0 months** (balanced market)

**To drill down to zip codes:**
1. Click the "Select Counties" dropdown
2. Select your target county (format: "County Name STATE")
3. The map zooms to show zip code boundaries
4. Data table updates to show zip codes ranked by investor transactions

## Step 6: Zip Code & Neighborhood Data Extraction

After selecting a county, the data table shows either ZIP codes or Neighborhoods (toggle between views).

### CRITICAL: Exact Table Column Headers

The Market Finder displays data with these **exact** column headers. Always capture data using these literal column names:

**ZIP Code View Columns (in order):**
| Column Position | Exact Header | Data Type |
|-----------------|--------------|-----------|
| 1 | ZIP CODE | 5-digit string |
| 2 | TOTAL INV. TRANS. FOR 6 MO. | Integer |
| 3 | HOMES ON MARKET | Integer |
| 4 | HOMES SOLD LAST MONTH | Integer |
| 5 | MEDIAN DAYS ON MARKET | Integer |
| 6 | MEDIAN HOME VALUE | Currency (e.g., $304,569) |
| 7 | MEDIAN SALE PRICE | Currency (e.g., $266,500) |

**Neighborhood View Columns (in order):**
| Column Position | Exact Header | Data Type |
|-----------------|--------------|-----------|
| 1 | NEIGHBORHOOD | String |
| 2 | TOTAL INV. TRANS. FOR 6 MO. | Integer |
| 3 | HOMES ON MARKET | Integer |
| 4 | HOMES SOLD LAST MONTH | Integer |
| 5 | MEDIAN DAYS ON MARKET | Integer |
| 6 | MEDIAN HOME VALUE | Currency (e.g., $270,060) |
| 7 | MEDIAN SALE PRICE | Currency (e.g., $197,248) |

### Pagination and Large Data Sets

**IMPORTANT: Some markets have 100+ ZIP codes.** The data table may not display all rows at once.

1. After the table loads, scroll to the bottom of the visible rows
2. Wait 1-2 seconds for the next batch to load (DataSift uses lazy loading / infinite scroll)
3. Continue scrolling until no new rows appear
4. Verify your total row count matches the county's known ZIP code count
5. If you see a "Load More" button or similar control, click it and wait for data to load before continuing

**How to verify completeness:** The right panel's summary statistics (e.g., "Mo. Investor Transactions: 53") should roughly correspond to the sum of the per-ZIP values in your extracted table. Large discrepancies indicate missing rows.

### Reference Data: Knox County, TN ZIP Codes

This is the **exact** data as displayed in Sift Market Finder for Knox County, TN (ZIP Code view). Use this as a reference for data accuracy validation:

| ZIP CODE | TOTAL INV. TRANS. FOR 6 MO. | HOMES ON MARKET | HOMES SOLD LAST MONTH | MEDIAN DAYS ON MARKET | MEDIAN HOME VALUE | MEDIAN SALE PRICE |
|----------|-----------------------------|-----------------|-----------------------|-----------------------|-------------------|-------------------|
| 37920 | 71 | 292 | 66 | 54 | $304,569 | $266,500 |
| 37917 | 38 | 180 | 38 | 55 | $266,623 | $264,915 |
| 37918 | 38 | 279 | 81 | 59 | $331,421 | $311,718 |
| 37914 | 37 | 195 | 33 | 59 | $254,429 | $193,350 |
| 37919 | 35 | 159 | 28 | 64 | $525,535 | $586,018 |
| 37921 | 35 | 176 | 39 | 57 | $278,046 | $238,554 |
| 37912 | 27 | 129 | 34 | 53 | $291,548 | $256,222 |
| 37922 | 21 | 190 | 41 | 55 | $624,664 | $621,343 |
| 37924 | 20 | 106 | 34 | 64 | $317,819 | $309,371 |
| 37931 | 19 | 186 | 41 | 61 | $426,350 | $423,488 |
| 37849 | 16 | 110 | 30 | 62 | $364,350 | $351,737 |
| 37923 | 15 | 111 | 25 | 48 | $405,088 | $397,413 |
| 37934 | 13 | 171 | 42 | 61 | $645,482 | $625,500 |
| 37938 | 13 | 123 | 32 | 57 | $379,853 | $370,062 |
| 37932 | 10 | 163 | 27 | 68 | $521,476 | $533,745 |
| 37902 | 9 | 11 | 5 | 107 | $597,539 | $5,266,704 |
| 37909 | 9 | 49 | 16 | 59 | $383,403 | $387,466 |

### Reference Data: Knox County, TN Neighborhoods

This is the **exact** data as displayed in Sift Market Finder for Knox County, TN (Neighborhood view). Use this as a reference for data accuracy validation:

| NEIGHBORHOOD | TOTAL INV. TRANS. FOR 6 MO. | HOMES ON MARKET | HOMES SOLD LAST MONTH | MEDIAN DAYS ON MARKET | MEDIAN HOME VALUE | MEDIAN SALE PRICE |
|--------------|-----------------------------|-----------------|-----------------------|-----------------------|-------------------|-------------------|
| Colonial Village - Knoxville | 24 | 38 | 15 | 53 | $270,060 | $197,248 |
| John Sevier | 12 | 45 | 22 | 74 | $307,627 | $274,762 |
| Burlington - Knoxville | 11 | 38 | 10 | 68 | $204,971 | $183,450 |
| Windsor Park - Knoxville | 11 | 27 | 18 | 59 | $258,505 | $212,555 |
| Cherokee Ridge | 10 | 42 | 13 | 51 | $321,044 | $267,514 |
| Vestal | 10 | 34 | 3 | 50 | $235,007 | $185,166 |
| Island Home | 9 | 20 | 9 | 51 | $252,572 | $291,343 |
| Old City - Knoxville | 9 | 21 | 5 | 92 | $599,649 | $4,658,938 |
| Chilhowee Hills | 9 | 39 | 6 | 66 | $219,135 | $158,427 |
| Berkshire Wood | 9 | 36 | 12 | 49 | $431,075 | $399,082 |
| Inskip | 8 | 27 | 8 | 74 | $251,175 | $247,950 |
| Harbison Crossroads | 8 | 50 | 11 | 87 | $314,404 | $294,696 |
| Sequoyah Hills | 7 | 27 | 4 | 74 | $769,508 | $693,336 |
| Marble City - Knoxville | 7 | 13 | 3 | 54 | $211,393 | $166,932 |
| Westlyn | 7 | 27 | 5 | 71 | $488,870 | $818,718 |
| Lonsdale - Knoxville | 7 | 25 | 2 | 40 | $210,157 | $138,435 |
| Lakemoor Hills | 6 | 21 | 5 | 43 | $419,762 | $384,169 |

### Data Extraction Rules

1. **Capture ALL visible rows** - Scroll through the entire table to capture all data (see Pagination section above)
2. **Use EXACT column headers** - Do not rename or abbreviate columns
3. **Preserve data types** - Keep currency formatting with $ and commas
4. **Capture BOTH views** - Extract ZIP Code AND Neighborhood data when available
5. **Note the sort order** - Default sort is by "TOTAL INV. TRANS. FOR 6 MO." descending

### Right Panel Summary Data

Also capture the summary cards from the right panel:
- **Median Home Value**: e.g., $364.4K
- **Homes on Market**: e.g., 2.8K
- **Mo. Investor Transactions**: e.g., 53
- **Homes Sold Last Month**: e.g., 668
- **Market Rent**: e.g., $2,107/mo
- **Gross Rental Yield**: e.g., 7.07%

**Property Characteristics Button:**
Click "Calculate recommended property characteristics" button to get AI-recommended property filters for the selected area.

## Step 7: Create Excel Spreadsheet Output

**ALWAYS create an Excel workbook (.xlsx) with 7 properly formatted worksheets.**

The output spreadsheet should be a complete, self-contained market report that an investor can open and immediately understand the opportunity. The 7-sheet structure mirrors a professional research deliverable: start with the executive summary for quick decision-making, then let the reader drill into ZIP/neighborhood data, economic fundamentals, safety, and actionable recommendations.

Use Python with openpyxl to create the spreadsheet:

```python
import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from datetime import datetime

wb = openpyxl.Workbook()

# Style definitions
header_font = Font(bold=True, color="FFFFFF")
header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
border = Border(
    left=Side(style='thin'),
    right=Side(style='thin'),
    top=Side(style='thin'),
    bottom=Side(style='thin')
)
```

### Conditional Formatting Rules

Apply these color fills to data cells throughout the workbook (Sheets 2, 3, and 6 especially) for instant visual scanning:

| Condition | Fill Color | Applies To |
|-----------|-----------|------------|
| DOM below FRED national average | Green (#C6EFCE) | Median DOM cells |
| DOM within 15% of FRED national | Yellow (#FFEB9C) | Median DOM cells |
| DOM above 15% of FRED national | Red (#FFC7CE) | Median DOM cells |
| 500+ investor transactions (6 mo) | Green (#C6EFCE) | Inv Trans cells |
| 100-499 investor transactions (6 mo) | Yellow (#FFEB9C) | Inv Trans cells |
| Under 100 investor transactions (6 mo) | Red (#FFC7CE) | Inv Trans cells |
| Supply under 3 months | Green (#C6EFCE) | Supply Months cells |
| Supply 3-6 months | Yellow (#FFEB9C) | Supply Months cells |
| Supply over 6 months | Red (#FFC7CE) | Supply Months cells |
| Tier 1 zip code (10+ monthly inv trans) | **Bold text** | Entire row |
| Negative spread % | Green (#C6EFCE) | Spread % cells (buyer negotiation opportunity) |

### Required Worksheets (7 Sheets)

The workbook must contain exactly these 7 sheets, in this order. Each sheet serves a distinct purpose in telling the market story.

---

#### Sheet 1: Executive Summary

This is the first thing the investor sees. It should answer: "Is this market worth my time, and where specifically should I focus?" in under 60 seconds of reading.

**Structure:**

**Row 1**: Report title -- `[COUNTY], [STATE] - MARKET RESEARCH REPORT` (merged across columns A-E)
**Row 2**: Generation date
**Row 3**: Data source attribution -- `Data Source: REI Sift Market Finder + Public Data Sources`

**Section A: County Overview (starts row 5)**
A table with columns: Metric | Value | Notes

Include these metrics (pulled from Sift right panel + public sources):
| Metric | Source | Notes Column Example |
|--------|--------|---------------------|
| Population (latest year) | Census | `+X.X% since 2020` |
| Median Home Value | Sift | `REI Sift data` |
| Median Household Income | Census ACS | `Census ACS` |
| Unemployment Rate | BLS | `BLS [Month Year]` |
| Homes on Market | Sift | `REI Sift data` |
| Monthly Investor Transactions | Sift | `6-month average` |
| Homes Sold Last Month | Sift | `REI Sift data` |
| Market Rent | Sift | `REI Sift data` |
| Gross Rental Yield | Sift | `REI Sift data` |
| Homeownership Rate | Census/Sift | `Renters: XX.X%` |
| Months of Supply | Calculated | `Homes on Market / Homes Sold` |
| National Days on Market | FRED | `FRED [Month Year] value` |

**Section B: Market Assessment (starts ~row 19)**
A qualitative ratings table with columns: Category | Rating | Commentary

Rate each category using the criteria defined below:

| Category | HIGH Rating | MODERATE Rating | LOW Rating |
|----------|------------|-----------------|------------|
| Investor Activity | 500+ investor transactions in 6 months | 100-500 investor transactions in 6 months | Under 100 investor transactions in 6 months |
| Market Velocity | DOM below FRED national average | DOM within 15% of FRED national average | DOM 50%+ above FRED national average |
| Supply Tightness | Under 4 months of inventory | 4-6 months of inventory | Over 6 months of inventory |

Additional assessment categories (use STRONG/STABLE/IMPROVING/DECLINING as appropriate):
| Category | What to assess |
|----------|---------------|
| Price Appreciation | YoY price trend from Redfin |
| Population Growth | Census growth rate and migration patterns |
| Employment | Unemployment rate, job growth |
| Crime Trend | Direction of crime stats (improving/worsening) |

The commentary column should be a brief, specific explanation -- not generic filler. For example: `53 monthly transactions, strong wholesaling market` rather than `Good activity level`.

**Section C: Top 5 ZIP Codes for Wholesaling (starts ~row 28)**
Columns: Rank | ZIP Code | 6-Mo Inv Trans | Median Home Value | Days on Market

Pull the top 5 ZIPs sorted by investor transactions.

**Section D: Top 5 Neighborhoods for Wholesaling (starts ~row 36)**
Columns: Rank | Neighborhood | 6-Mo Inv Trans | Median Home Value | Days on Market

Pull the top 5 neighborhoods sorted by investor transactions.

---

#### Sheet 2: ZIP Code Analysis

This is the deep-dive data sheet. Every ZIP code in the county gets a row, sorted by investor transactions descending. The calculated columns (DOM vs National, Spread %, Supply Months, Wholesaling Score) are what turn raw Sift data into actionable intelligence.

**Columns (in order):**
| Col | Header | Source | Description |
|-----|--------|--------|-------------|
| A | ZIP Code | Sift | 5-digit ZIP |
| B | 6-Mo Inv Trans | Sift | Total investor transactions for 6 months |
| C | Homes on Market | Sift | Current inventory |
| D | Homes Sold/Mo | Sift | Homes sold last month |
| E | Median DOM | Sift | Median days on market |
| F | DOM vs National | **Calculated** | `= Median DOM - FRED baseline` (negative = faster than national) |
| G | Median Home Value | Sift | Currency formatted |
| H | Median Sale Price | Sift | Currency formatted |
| I | Spread % | **Calculated** | `= (Median Sale Price - Median Home Value) / Median Home Value` -- shows the gap between list and sale prices. Negative spread = buyers negotiating below list (opportunity). Large positive spread = competitive/premium market. |
| J | Supply Months | **Calculated** | `= Homes on Market / Homes Sold/Mo` -- months of inventory at current sales pace (see formula and interpretation in Step 5) |
| K | Wholesaling Score | **Calculated** | Star rating (see scoring methodology below) |

**Wholesaling Score Methodology:**

The star rating is a composite score that answers: "How attractive is this ZIP code for a wholesaler?" It weighs three factors:

1. **Investor Activity** (primary signal) -- Higher 6-month investor transactions = more proven buyer demand
2. **Market Velocity** (secondary) -- DOM below national average = properties move quickly, reducing hold risk
3. **Price Accessibility** (tertiary) -- Lower median home values = smaller capital requirements and broader buyer pool

**Scoring rubric:**
| Score | Criteria | All 3 Must Be True Unless Noted |
|-------|----------|---------------------------------|
| ★★★★★ | 30+ inv trans AND DOM 15+ days below FRED national AND median value under $350K | All 3 required |
| ★★★★☆ | 20+ inv trans AND DOM below FRED national AND median value under $400K | All 3 required. Also award for 30+ trans with higher prices (volume overrides price). |
| ★★★☆☆ | 10+ inv trans AND DOM near or below FRED national | Meets 2 of 3 criteria solidly |
| ★★☆☆☆ | Some investor activity but higher prices, slower DOM, or limited volume | Meets 1 of 3 criteria |
| ★☆☆☆☆ | Low activity, DOM above FRED national, or anomalous data (e.g., very high sale price outliers) | Meets 0 criteria or data unreliable |

Use judgment -- these thresholds are guidelines. A ZIP with 35 investor transactions but $600K+ median values might be ★★★☆☆ because the price point limits the wholesale buyer pool, even though volume is strong.

**Formatting notes:**
- Spread % should show sign: `-12.5%` or `+11.5%`
- DOM vs National should show sign: `-19` or `+34`
- Supply Months to 1 decimal: `4.4`
- Star ratings as text: `★★★★★`, `★★★★☆`, etc.
- Apply conditional formatting per the rules table above

---

#### Sheet 3: Neighborhood Analysis

Identical structure to Sheet 2, but for neighborhoods instead of ZIP codes. Same columns, same calculated fields, same scoring methodology.

| Col | Header |
|-----|--------|
| A | Neighborhood |
| B | 6-Mo Inv Trans |
| C | Homes on Market |
| D | Homes Sold/Mo |
| E | Median DOM |
| F | DOM vs National |
| G | Median Home Value |
| H | Median Sale Price |
| I | Spread % |
| J | Supply Months |
| K | Wholesaling Score |

Include ALL neighborhoods from Sift (typically 20-30 rows), sorted by investor transactions descending.

---

#### Sheet 4: Economic Indicators

This sheet provides the macroeconomic context that explains WHY the market behaves as it does. An investor needs to know: Is the local economy healthy? Are jobs growing? Is the population stable or growing?

Organize into 3 sections:

**Section A: Employment Data (BLS)**
Pull from BLS (bls.gov/eag) for the county's MSA:
Columns: Metric | Value | Trend

| Metric | Example |
|--------|---------|
| Civilian Labor Force | 468,800 |
| Employment | 453,100 |
| Unemployment | 15,700 |
| Unemployment Rate | 3.4% |
| Total Nonfarm Jobs | 463,600 |

**Employment by Sector sub-table:**
Columns: Sector | Jobs (000s) | 12-Mo Change

List all major BLS sectors (Education & Health, Trade/Transport/Utilities, Professional Services, Government, Leisure & Hospitality, Manufacturing, Financial Activities, Mining/Logging/Construction, Other Services, Information). Include the 12-month percentage change for each.

**Section B: Demographic Data (Census)**
Columns: Metric | Value | Notes

Pull from Census QuickFacts (census.gov/quickfacts):
| Metric | Notes |
|--------|-------|
| Population (latest) | Include growth since 2020 |
| Population Growth Rate | Annual rate |
| Median Age | |
| Median Household Income | Compare to national |
| Per Capita Income | |
| Poverty Rate | |
| Bachelor's Degree or Higher | Education level indicator |
| Owner-Occupied Housing | |
| Median Home Value (Census) | Note: ACS multi-year estimate |

**Section C: Housing Market (Redfin)**
Columns: Metric | Value | YoY Change

Pull from Redfin county page:
| Metric | Include |
|--------|---------|
| Median Sale Price | YoY change |
| Median Price/Sq Ft | YoY change |
| Homes Sold | YoY change |
| Median Days on Market | YoY change |
| Sale-to-List Price | YoY change (as percentage points) |
| Homes Above List Price | YoY change (as percentage points) |
| Homes with Price Drops | YoY change (as percentage points) |

---

#### Sheet 5: Crime & Safety

Crime data matters for investors because it directly impacts property values, insurance costs, and buyer pool. This sheet should make clear: Is the area getting safer or more dangerous? Which specific areas are higher risk?

**Section A: Crime Statistics**
Columns: Crime Type | Prior Year | Current Year | Change

Pull from local police department or FBI UCR data. Include:
- Murders
- Non-Fatal Shootings
- Robberies
- Motor Vehicle Thefts
- Car Burglaries
- Aggravated Assaults

Format the Change column as percentage: `-26%`, `+2%`

**Section B: Historical Murder Trend**
Columns: Year | Murders | Notes

Show 3-4 years of trend data. Murders are the most reliable crime stat (hardest to reclassify) and the strongest signal of neighborhood safety trajectory.

**Section C: Safety Assessment by Area**
Columns: Area | Safety Rating | Notes

Rate major sub-areas within the county (e.g., West Knoxville, North Knoxville, East Knoxville, Downtown, suburban areas) as High, Moderate, or Lower. Include brief context like `Lower crime, higher home values` or `Higher crime, but improving`.

**Section D: Key Insights for Investors**
Bullet-point rows (single column, merged) with 4-5 actionable observations connecting crime data to investment decisions. Examples:
- `Overall crime trending down significantly - positive for property values`
- `East Knoxville has higher crime but also higher investor activity`
- `Consider crime trends when evaluating neighborhood investments`

---

#### Sheet 6: Investment Recommendations

This is the action sheet -- it translates all the data into specific, prioritized recommendations. An investor should be able to read this sheet alone and know exactly where to focus their marketing.

**Section A: Tier 1 - Highest Priority ZIP Codes**
Columns: ZIP Code | Inv Trans | Median Value | DOM | Supply Months | Rationale

Include 4-6 top ZIPs. The Rationale column should be a concise sentence explaining WHY this ZIP is Tier 1: `Highest activity, below-avg DOM, moderate prices`

**Section B: Tier 1 - Highest Priority Neighborhoods**
Columns: Neighborhood | Inv Trans | Median Value | DOM | Supply Months | Rationale

Include 4-6 top neighborhoods with specific rationale for each.

**Section C: Tier 2 - Secondary Opportunities**
Columns: ZIP Code | Inv Trans | Median Value | DOM | Supply Months | Rationale

Include 3-5 ZIPs that have merit but with caveats (higher prices, slower DOM, etc.).

**Section D: Market Timing Considerations**
Columns: Factor | Current Status | Implication

This helps investors understand whether NOW is a good time to enter the market:
| Factor | Example Status | Example Implication |
|--------|---------------|-------------------|
| Price Trend | -2.9% YoY | Buyer's market, negotiate harder |
| Inventory | +9.7% sales volume | More deals available |
| Days on Market | 63 days (below national) | Market still moving |
| Competition | 18.6% above list | Less bidding wars than peak |
| Price Drops | 19.3% with reductions | Motivated sellers exist |

**Section E: Recommended Strategy**
Numbered rows (single column) with 5-7 specific, actionable steps. These should reference specific ZIP codes, neighborhoods, price ranges, and DOM thresholds from the data. Example:
1. `Focus marketing on 37920, 37914, 37917, 37921 ZIP codes`
2. `Target Colonial Village, Lonsdale, Vestal neighborhoods`
3. `Look for properties with 60+ DOM for motivated sellers`
4. `Target homes in $200K-$350K range for best wholesale margins`

---

#### Sheet 7: Data Sources

Transparency matters. This sheet lets the investor (or their team) verify any number in the report and understand the methodology behind calculated fields.

**Section A: Primary Data Sources**
Columns: Source | Data Type | Date Retrieved | URL/Notes

List every source used:
| Source | Data Type |
|--------|-----------|
| REI Sift Market Finder | Investor transactions, home values, DOM |
| FRED (St. Louis Fed) | National Days on Market baseline |
| U.S. Census Bureau | Demographics, population, income |
| Bureau of Labor Statistics | Employment, unemployment |
| Redfin | Housing market trends, prices |
| Local Police Department | Crime statistics |
| Local Chamber of Commerce | Major employers (if referenced) |

**Section B: Methodology**
Columns: Analysis Component | Description

Explain each calculated field:
| Component | Description |
|-----------|-------------|
| Wholesaling Score | Composite of investor transactions (primary), DOM vs national (secondary), price accessibility (tertiary) |
| DOM vs National | Comparison to FRED national median -- state the exact FRED value and observation date used |
| Price Spread | (Median Sale Price - Median Home Value) / Median Home Value |
| Supply Months | Homes on Market / Homes Sold Last Month -- under 3 = seller's market, 3-6 = balanced, over 6 = buyer's market |
| Tier Classification | Based on investor activity volume and market fundamentals |

**Section C: Disclaimers**
Bullet-point rows with standard disclaimers:
- Data is current as of retrieval date and subject to change
- REI Sift data represents proprietary investor transaction tracking
- Crime statistics may be preliminary and pending audit
- Investment recommendations are for informational purposes only
- Always conduct independent due diligence before investing

### Formatting Requirements

1. **Headers**: Bold, white text on blue background (#4472C4)
2. **Section headers**: Bold, larger font, merged across columns where appropriate
3. **Borders**: Thin borders on all data cells
4. **Number Formatting**: Currency for prices ($XXX,XXX), 1 decimal for calculations, percentages with sign
5. **Column Widths**: Auto-fit to content (minimum 12 for data columns, wider for text)
6. **Freeze Panes**: Freeze header row on data-heavy sheets (Sheets 2, 3, 4)
7. **Star ratings**: Use Unicode star characters: ★ (filled) and ☆ (empty)
8. **Conditional formatting**: Apply the color rules from the Conditional Formatting Rules table above

## Zip Code Scoring Weights (Source Code Reference)

These weights are used in the SiftStack `market_analyzer.py` scoring engine. They are verified against source code and should not be modified without updating the codebase.

| Factor | Weight | Direction | Rationale |
|--------|--------|-----------|-----------|
| Distress Density | 30% | Higher = better | Foreclosure/tax sale/probate count per ZIP -- more distress = more motivated sellers |
| Median Value | 20% | Lower = better | Lower-value properties have broader buyer pools for wholesale assignments |
| Equity | 15% | Higher = better | Higher average equity = more room for deals and negotiation |
| Tax Delinquency | 15% | Higher = better | More delinquency = more financial distress = more motivated sellers |
| Competition | 10% | Lower = better | Lower investor activity = less competition for deals |
| Days on Market | 10% | Higher = better | Higher DOM = more negotiating leverage and motivated sellers |

**Grade thresholds** (from normalized 0-100 composite score):
- **A**: Score >= 75
- **B**: Score >= 55
- **C**: Score >= 35
- **D**: Score < 35

## Decision Framework

### Zip Code Selection Criteria

**Tier 1 (Best):**
- 10+ monthly investor transactions
- Supply 3-6 months
- Days on Market below national average

**Tier 2 (Good):**
- 5-10 monthly investor transactions
- Supply 2-7 months
- Days on Market near national average

**Tier 3 (Requires Closing Ability):**
- Under 5 monthly investor transactions
- Uncontested but requires flip/whole-tail capability

### How Many Zip Codes to Target

| Monthly Deal Goal | Recommended Zip Codes |
|-------------------|----------------------|
| 1-2 deals | 5-10 zip codes |
| 3-5 deals | 10-15 zip codes |
| 5+ deals | 15-20 zip codes |

## Output Filename Convention

**Required format:** `[County]_[State]_Market_Research.xlsx`

Examples:
- `Knox_County_TN_Market_Research.xlsx`
- `Shelby_County_TN_Market_Research.xlsx`
- `Fulton_County_GA_Market_Research.xlsx`

## Comprehensive Market Analysis

For detailed county-level analysis that blends Sift data with public sources, see:
- `references/ComprehensiveAnalysisFramework.md` - Full analysis structure and methodology

This framework produces:
- **Excel Workbook**: All quantitative data in properly formatted spreadsheets
- **Markdown Report**: Narrative analysis covering 12 core indicator categories

**Comprehensive Analysis Output Structure:**

| Deliverable | Format | Contents |
|-------------|--------|----------|
| Data Workbook | Excel (.xlsx) | County data, Zip code data, Top 25 analysis, Public data blend |
| Analysis Report | Markdown (.md) | Narrative analysis, recommendations, outlook |

**Public Data Sources to Incorporate:**

| Source | Data Points |
|--------|-------------|
| BLS | Employment, unemployment, wages |
| Census Bureau | Demographics, population trends, migration |
| Realtor.com, Zillow, Redfin | Home prices, days on market, inventory |
| FBI Crime Data | Crime rates by ZIP code |
| Local Government | Tax rates, zoning, regulations |

## Reference Files

- `references/ComprehensiveAnalysisFramework.md` - Detailed analysis structure for blending quantitative data
- `references/MarketAnalysisPrompt.pdf` - Original methodology document
- `references/MarketFinderNavigationAndAnalysisSOP.pdf` - Detailed SOP with screenshots
- `templates/MarketFinderResearchExample.xlsx` - Example output format (use as structural template for spreadsheet outputs)
