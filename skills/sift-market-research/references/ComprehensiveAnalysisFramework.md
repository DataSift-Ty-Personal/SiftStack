# Comprehensive Market Analysis Framework

This framework blends Sift Market Map data with public data sources to produce actionable county-level market analysis for real estate investors.

## Prerequisites

Before starting any comprehensive analysis:

1. **Get the current FRED national DOM baseline** from https://fred.stlouisfed.org/series/MEDDAYONMARUS -- record the exact value and observation date
2. **Login to DataSift** using your account credentials at https://app.reisift.io/ (never hardcode credentials)
3. **Identify the target county** and confirm it has data in Sift Market Finder

## Data Sources

| Source Type | Sources | Data Points |
|-------------|---------|-------------|
| Proprietary | Sift Market Map | Investor transactions by ZIP, 6-9 month history |
| Public | BLS, Census Bureau | Employment, demographics, population trends |
| Real Estate | Realtor.com, Zillow, Redfin | Home prices, days on market, inventory |
| Safety | FBI Crime Data | Crime rates by ZIP code |
| Government | Local sources | Tax rates, zoning, regulations |
| Baseline | FRED | National median days on market (verify monthly) |

## Core Indicators (12 Categories)

Assess each indicator qualitatively using current and historical data:

### 1. Affordability & Interest Rate Environment
- **Median Income vs. Median Home Price**: Overall affordability and potential buyer pool
- **Impact of Mortgage Rates**: How rate changes affect local demand and investor activity

### 2. Market Saturation & Competition
- **Investor Activity**: Combine Market Map Data + public sources. Is the area crowded with institutional investors, or are smaller players still competitive?
- **Inventory Levels & Deal Flow**: Enough supply for new investors to enter without fierce bidding wars?
- **Competition Rating**: HIGH (500+ inv trans/6mo), MODERATE (100-500), LOW (under 100)

### 3. Supply & Demand Dynamics
- **Days on Market, Buyer Demand**: Inventory shortages or oversupply in specific locations
- **New Construction**: Projects or developments adding to housing stock
- **Supply (Months of Inventory)**: Calculate as `Homes on Market / Homes Sold Last Month`
  - Under 3 months = Seller's market (prices rising, fast sales, strong buyer demand for wholesale deals)
  - 3-6 months = Balanced market (healthy conditions, reasonable negotiation room)
  - Over 6 months = Buyer's market (prices declining, more motivated sellers, fewer end buyers)

### 4. Home Prices & Appreciation
- **Current Median Prices & Trends**: Are prices peaking or still climbing?
- **Value Plays**: Which neighborhoods or ZIP codes show steady but not overheated growth?

### 5. Distressed Properties & Sales Activity
- **Foreclosures, Pre-Foreclosures, REOs**: Opportunities for investors seeking discounted deals
- **Transaction Volume**: Areas with high activity vs. pockets with low turnover

### 6. Demographics & Population Trends
- **In-Migration, Household Formation**: Who's moving in or out of the county, and why?
- **Age & Lifestyle Factors**: Retirees, remote workers, families--how they shape housing demand

### 7. Local Amenities & Infrastructure
- **Schools, Healthcare, Transportation Access**: Neighborhood desirability drivers
- **Future Infrastructure Projects**: Upcoming developments likely to boost property values

### 8. Crime Data
- **Overall Rates & Local Nuances**: How crime affects perceived safety and insurance costs
- **Neighborhood-Level Disparities**: ZIP codes with significantly higher or lower crime rates

### 9. Mortgage Health Indicators
- **Delinquency & Default Trends**: Sign of homeowner distress or market stability
- **Local Lending Climate**: How easy/difficult is it to secure financing?

### 10. Government Policies & Taxes
- **Property Tax Rates, Zoning, Incentives**: Any location-specific regulations favoring or deterring investment
- **Rent Controls & Landlord-Tenant Laws**: Impact on buy-and-hold viability

### 11. Builder & Investor Sentiment
- **New Construction Pipeline**: Are builders optimistic or pulling back?
- **Investor-Friendly Climate**: Indications that local regulations or community attitudes support real estate investment

### 12. Economic Conditions (place at bottom of final output)
- **Job Growth & Stability**: Major employers, emerging job sectors, recent relocations
- **Unemployment Trends & Wages**: Indicators of local economic health and spending power

---

## County Analysis Report Structure

Provide a narrative-style breakdown weaving Market Map Data alongside publicly available stats:

### A. Snapshot Overview
One or two sentences capturing the county's standout attributes or biggest risks.

Include the **Market Assessment Summary** using these defined rating criteria:

| Category | HIGH | MODERATE | LOW |
|----------|------|----------|-----|
| Investor Activity | 500+ investor transactions in 6 months | 100-500 investor transactions in 6 months | Under 100 investor transactions in 6 months |
| Market Velocity | DOM below FRED national average | DOM within 15% of FRED national average | DOM 50%+ above FRED national average |
| Supply Tightness | Under 4 months of inventory | 4-6 months of inventory | Over 6 months of inventory |

### B. Key Highlights (Qualitative)
- **Crime & Demographics**: Overall safety, migration patterns, household profiles
- **Affordability & Market Saturation**: Where housing remains undervalued or under the radar
- **Infrastructure & Amenities**: Schools, transport, healthcare, planned upgrades

### C. Flipping Potential
- **Acquisition Costs & ARV Ranges**: Qualitative estimates
- **Renovation Environment**: Availability and cost of contractors in key neighborhoods
- **Resale Timeframes**: Typical Days on Market for renovated properties

### D. Wholesaling Prospects
- **Distressed or Off-Market Inventory**: Foreclosures, probate listings, motivated sellers
- **Local Investor Demand**: Concentration of buy-and-hold investors or smaller fix-and-flip operators looking for deals
- **Potential Assignment Spreads**: Qualitative sense of how profitable wholesaling might be
- **Wholesaling Viability Score**: Rate using star criteria from SKILL.md (based on inv trans, DOM vs FRED national, median value)

### E. Buy-and-Hold Viability
- **Rent Levels & Growth Trajectory**: Key neighborhoods with strong rental demand
- **Vacancy Rates & Turnover**: Gauging tenant stability by ZIP code or submarket
- **Landlord-Tenant Environment**: Local regulations and how they might affect ROI

### F. Top 25 ZIP Codes to Watch
Leverage both public data and Market Map Data to identify the 25 most significant opportunities. For each ZIP code provide:

| Data Point | Description |
|------------|-------------|
| Median Home Value | Current median from public sources |
| Investor Activity | Transaction volume, recent trends from Market Map |
| Supply Months | Homes on Market / Homes Sold Last Month (include interpretation) |
| Crime Data | How crime rates compare to county or state averages |
| School Data | General quality, relevant ratings |
| Recommended Exit Strategy | Flip, rental, or both |
| Rationale | Proximity to job centers, ARV growth potential, stable rental demand, etc. |

### G. Pitfalls & Risks
- **Crime Hotspots & Insurance Costs**: Areas with higher risk
- **Zoning Restrictions, Flood Zones, Environmental Hazards**
- **Regulatory & Tax Considerations**: Any county-level issues that could hamper returns

### H. Short-Term Outlook (6-12 Months)
- **Projection of Home Prices & Investor Demand**: Potential triggers like interest rate changes, local job announcements
- **Leading Indicators**: Business openings/closings, major employers expanding or contracting

### I. Long-Term Considerations (2-5 Years)
- **Major Infrastructure or Redevelopment Projects**: Potential catalysts for property appreciation
- **Corporate Relocations & Economic Trends**: Factors shaping housing demand over time

### J. Location-Based Tactics
- **Identifying High-Opportunity Neighborhoods**: Based on home price trends, crime stats, population growth
- **Focusing on Under-the-Radar ZIP Codes**: Using Market Map Data to find areas with fewer large institutional investors

### K. Conclusion & Key Takeaways
- **Top Submarkets** (by city or neighborhood) for flipping, wholesaling, and buy-and-hold
- **Major Risks** and how to mitigate them (e.g., focusing on stable neighborhoods, verifying zoning)
- **Emerging Trends**: Any new developments or policies likely to change the market in the next few years

### L. Economic Health & Employment (placed at bottom)
- **Job Growth & Stability**: Major employers, emerging job sectors, recent relocations
- **Unemployment Trends & Wages**: Indicators of local economic health and spending power

---

## Additional Notes & Disclaimers

- **Data Volatility**: Statistics can shift quickly; official figures often lag real-time trends
- **FRED Baseline**: Always verify the current national median DOM before analysis -- do not use stale estimates
- **Local Due Diligence**: Encourage site visits, networking with local professionals, and thorough property inspections
- **No Guarantees**: All real estate investments involve inherent risks. This analysis is a starting point for deeper investigation
- **Credentials**: Never include login credentials in reports, shared documents, or skill files

---

## Usage

Use this structure to provide a comprehensive market analysis of the target county. The Top 25 ZIP Codes section should clearly identify which exit strategy is best for each ZIP code, supported by key data points like median home values, crime, and school quality. Place "Economic Health & Employment" at the bottom, after all other sections.

This ensures investors get a quick view of **where** to invest and **how** to invest--while still offering deeper context for those who want it.
