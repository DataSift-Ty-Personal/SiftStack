---
name: buyer-prospector
description: Build a real estate buyers list for any US county by pulling from a nationwide database of active buyers, categorizing entities (LLCs, Trusts, Corporations), and researching decision-makers for skip tracing. Use this skill whenever someone wants to find buyers in a specific county, build a buyers list, identify who's buying in a market, research active investors in an area, prospect cash buyers, or needs to know who the top buyers are in a county. Trigger for "buyers list", "find buyers", "who's buying in", "cash buyers", "active buyers", "buyer prospecting", "build a buyers list for [county]", "investors buying in [market]", "buyer research", or any request to identify and research real estate buyers in a specific geographic area. Even if the user just says "pull buyers for Knox County" or "who's buying in Harris County TX" — use this skill.
---

# Buyer Prospector

Pull active real estate buyer data for any US county from a nationwide database of 84,000+ buyer records across 1,471 counties. The skill filters buyers by the user's target county, categorizes entities, and walks through a structured research workflow to identify the actual decision-makers behind LLCs, trusts, and corporations — turning raw transaction data into a skip-traceable buyers list.

## When to Use

- User wants to find active buyers in a specific county/state
- Building a buyers list for a new market
- Need to identify who's buying properties in an area
- Prospecting cash buyers for wholesaling deals
- Researching investor activity in a target county

## What the User Needs to Provide

At minimum, the user needs to tell you:
1. **County name** (e.g., "Knox", "Harris", "Maricopa")
2. **State** (e.g., "TN", "TX", "AZ")

Optional preferences:
- Minimum purchase threshold (default: 2 purchases in 6 months)
- How many records to research (batch size)
- Whether they want the full research workflow or just the analysis

## Workflow Overview

```
1. Filter nationwide data for the target county
2. Run entity analysis (categorize + prioritize)
3. Review results with user
4. Research decision-makers for High priority entities
5. Update the Excel workbook with findings
6. Deliver completed buyer analysis
```

## Step 1: Filter and Analyze

Run the analysis script on the bundled nationwide dataset. The script path is relative to this skill's directory:

```bash
python <skill-path>/scripts/analyze_buyers.py <skill-path>/data/nationwide_buyers.csv \
  --county "<county_name>" \
  --state "<state_abbrev>" \
  --output "<output_path>"
```

**Example:**
```bash
python <skill-path>/scripts/analyze_buyers.py <skill-path>/data/nationwide_buyers.csv \
  --county "Knox" \
  --state "TN" \
  --output "Knox_TN_Buyer_Analysis.xlsx"
```

The `--min-purchases` flag defaults to 2. If the user wants a broader or narrower list, adjust accordingly.

Save the output Excel file to the user's workspace so they can access it.

### What the Script Produces

A multi-tab Excel workbook (`<County>_<State>_Buyer_Analysis.xlsx`) with these columns:

| Column | Description |
|--------|-------------|
| ResearchPriority | High (entity needs research) or Low (individual, skip trace directly) |
| BuyerPurchases6MSum | Number of purchases in last 6 months — the activity indicator |
| County Name / County State | Where this buyer has been purchasing |
| BuyerFullName | Entity or individual name |
| EntityType | LLC, TRUST, CORPORATION, ESTATE, INDIVIDUAL, etc. |
| DecisionMaker_FullName | Full name of decision-maker (fill during research) |
| DecisionMaker_FirstName | First name (for skip tracing upload) |
| DecisionMaker_LastName | Last name (for skip tracing upload) |
| DecisionMaker_Role | Member, Trustee, Officer, etc. |
| Verification_Source | Where the information was found |
| BuyerAddress/City/State/ZIP | Buyer's registered address |
| BuyerMailingAddress/City/State/ZIP | Duplicated for Sift upload compatibility |

### Output Tabs

| Tab | Contents |
|-----|----------|
| All Records | Complete filtered dataset |
| Found | Records where decision-makers were identified |
| Not Found | Records needing research |

After the script runs, report the results to the user:
- Total buyer count for the county
- Entity type breakdown (how many LLCs, Trusts, Individuals, etc.)
- Research priority split (High vs Low)
- Top 10 most active buyers
- Ask how many records they want to research

## Step 2: Research Decision-Makers

This is where the real value is. For each High priority entity, research the actual person behind the entity so the user can skip trace and contact them.

### Research Priority by Entity Type

| Entity Type | Research Required | What to Find |
|-------------|-------------------|--------------|
| LLC | Yes | Registered Agent, Members, or Managers |
| TRUST | Yes | Trustee (often in trust name or deed) |
| CORPORATION | Yes | Officers, Directors, or Registered Agent |
| ESTATE | Yes | Executor or Personal Representative |
| LIMITED PARTNERSHIP | Yes | General Partner |
| OTHER ENTITY | Yes | Principal or Registered Agent |
| INDIVIDUAL | No | Can skip trace directly using name |
| GOVERNMENT/AGENCY | No | Not a target for buyers list |

### Research Methods

For detailed research instructions by entity type (including Secretary of State URLs for all 50 states), read `references/research_guide.md`. Here's the quick version:

**LLCs / Corporations:**
1. Identify the formation state from `BuyerState`
2. Go to that state's Secretary of State business search (URLs in research guide)
3. Search the exact entity name
4. Extract: Registered Agent, Members/Managers/Officers, Principal address
5. If SOS doesn't reveal members, try secondary sources

**Secondary Research Sources** (use when SOS alone isn't enough):
- **Bizapedia** — aggregates SOS data, sometimes has more detail
- **LinkedIn** — search entity name to find principals
- **Company websites** — check About/Team pages
- **Dun & Bradstreet / Buzzfile** — business directory listings
- **Public property records** — deed signers reveal decision-makers
- **CorporationWiki** — maps relationships between entities and people
- **News articles** — local press often names principals of active investors
- **LEI lookups** — for larger entities with Legal Entity Identifiers
- **BBB Business Profiles** — often list the principal/owner

**Trusts:**
1. Check if the trust name reveals the trustee (e.g., "SMITH FAMILY TRUST" → Smith)
2. For revocable living trusts, the grantor IS the trustee — extract from the name
3. If unclear, check county deed records where the trust purchased property
4. The mailing address may also help identify the trustee

**Estates:**
1. Search probate court records in the relevant county
2. Look for the Executor or Personal Representative

### Multi-Owner LLC Tiebreaker

When an LLC has multiple members listed, use this priority order to identify the contact person:

1. **Registered Agent** — This is the official contact for the entity. Contact them first.
2. **Largest ownership percentage** — If no registered agent (or the RA is a commercial service like CT Corporation), contact the member with the largest ownership stake.
3. **First-listed member** — If ownership percentages are equal, contact the member listed first in the SOS filing. This is typically the managing member.

**For Trusts:** Always contact the **trustee**, not the beneficiaries. The trustee is the decision-maker with authority to buy/sell property.

**For LLCs with a commercial Registered Agent** (CT Corporation, CSC, Northwest Registered Agent, etc.): Skip to step 2 — the RA is a service, not a person.

### Confidence Scoring

Tag every researched entity with a confidence level in the `Verification_Source` column:

| Level | Tag | Criteria | Example |
|-------|-----|----------|---------|
| HIGH | `[H]` | SOS match with Active status + registered agent verified + phone or email found | `[H] TN SOS - Active, RA: John Smith, phone verified` |
| MEDIUM | `[M]` | SOS match but entity is Inactive/Dissolved, OR only deed records found (no SOS confirmation) | `[M] TN SOS - Dissolved 2024, deed signer: Jane Doe` |
| LOW | `[L]` | No SOS match, only Google/LinkedIn results, OR common name with ambiguous match | `[L] Google only - possible match, needs verification` |

Add a `Confidence` column to the Excel output with H, M, or L. This lets the user prioritize outreach — start with HIGH confidence contacts, follow up on MEDIUM, and revisit LOW later.

**Important:** The script auto-populates INDIVIDUAL records (sets DecisionMaker fields = BuyerFullName and marks them "Individual (Skip Trace Directly)"). These don't need research — they go straight to the Found tab.

### Sampling Strategy (Large Datasets)

Researching every entity hits diminishing returns fast. Use this sampling approach based on dataset size:

| High Priority Count | Research Scope | Rationale |
|---------------------|---------------|-----------|
| 500+ entities | Top 50 by transaction count | Diminishing returns after ~50 |
| 200-500 entities | Top 30 by transaction count | Covers the most active buyers |
| Under 200 entities | All High priority + top 10 Medium | Small enough to be thorough |

**Always start with entities that have 3+ purchases in the last 6 months.** These are the most active buyers and the most likely to buy your next deal. Sort by `BuyerPurchases6MSum` descending and work from the top.

After the initial sample, report results to the user. If identification rate is high (>70%), offer to continue with the next batch. If low, discuss whether to switch research methods before investing more time.

### Batch Processing

Within each sampling tier, process in manageable batches:
- **10-20 records**: Good starting batch, lets the user see the process
- **50 records**: Balanced approach for experienced users
- **All High priority**: If the user wants everything done (only practical for <200 entities)

Pause after each batch to report progress and confirm continuation.

## Step 3: Document Findings

As you research each entity, fill in all decision-maker columns. This is critical for skip tracing compatibility — the name needs to be properly parsed.

**Successful Research Example:**
```
BuyerFullName: GDP PROPERTIES LLC
EntityType: LLC
→ DecisionMaker_FullName: John Smith
→ DecisionMaker_FirstName: John
→ DecisionMaker_LastName: Smith
→ DecisionMaker_Role: Registered Agent / Member
→ Verification_Source: TN SOS - Control #000123456
```

**Trust Name Analysis Example:**
```
BuyerFullName: BENNETT REVOCABLE LIVING TRUST
EntityType: TRUST
→ DecisionMaker_FullName: [First Name] Bennett
→ DecisionMaker_FirstName: [First Name]
→ DecisionMaker_LastName: Bennett
→ DecisionMaker_Role: Trustee
→ Verification_Source: Trust naming convention (revocable living trust)
```

**Unsuccessful Research Example:**
```
BuyerFullName: ANONYMOUS HOLDINGS LLC
EntityType: LLC
→ DecisionMaker_FullName: Not Found
→ DecisionMaker_FirstName: Not Found
→ DecisionMaker_LastName: Not Found
→ DecisionMaker_Role: Not Found
→ Verification_Source: WY SOS - Anonymous LLC, registered agent is commercial service
```

### Standardized Convention

Always use **"Not Found"** (exactly) when research is unsuccessful. This enables clean filtering in the Excel output.

### Name Parsing

| Full Name | First Name | Last Name |
|-----------|------------|-----------|
| John Smith | John | Smith |
| Mary Jane Watson | Mary Jane | Watson |
| Robert J. Williams III | Robert J. | Williams III |

Keep compound last names and suffixes with the last name field.

## Step 4: Update the Excel File

After researching a batch, update the Excel workbook with findings. Use pandas or openpyxl to:
1. Load the existing workbook
2. Update the decision-maker columns for researched records
3. Re-sort the tabs (Found = identified, Not Found = still needs work)
4. Save back to the same file

```python
import pandas as pd

# Load
df = pd.read_excel('output_file.xlsx', sheet_name='All Records')

# Update records (example)
mask = df['BuyerFullName'] == 'GDP PROPERTIES LLC'
df.loc[mask, 'DecisionMaker_FullName'] = 'John Smith'
df.loc[mask, 'DecisionMaker_FirstName'] = 'John'
df.loc[mask, 'DecisionMaker_LastName'] = 'Smith'
df.loc[mask, 'DecisionMaker_Role'] = 'Registered Agent'
df.loc[mask, 'Verification_Source'] = 'TN SOS'

# Re-save with updated tabs
found_mask = (df['DecisionMaker_FullName'] != '') & (df['DecisionMaker_FullName'].str.upper() != 'NOT FOUND')
with pd.ExcelWriter('output_file.xlsx', engine='openpyxl') as writer:
    df.to_excel(writer, sheet_name='All Records', index=False)
    df[found_mask].to_excel(writer, sheet_name='Found', index=False)
    df[~found_mask].to_excel(writer, sheet_name='Not Found', index=False)
```

## Step 5: Deliver Results

Provide the completed Excel workbook and summarize:
- Total buyers found for the county
- How many decision-makers were identified vs still needed
- Top buyers by volume with their identified contacts
- Next steps: upload the "Found" tab to Sift for skip tracing

The user can then:
1. **Found tab** → Upload to Sift for skip tracing (includes mailing address columns)
2. **Not Found tab** → Follow up with deeper research or skip trace the entity address
3. Start outreach to build buyer relationships

## Multi-County Support

If the user wants buyers across multiple counties, run the script once per county and combine:

```bash
# Run for each county
python <script> <data> --county "Knox" --state "TN" --output "Knox_TN.xlsx"
python <script> <data> --county "Anderson" --state "TN" --output "Anderson_TN.xlsx"
```

Or, if they want a combined file, you can modify the filtering to accept multiple counties and produce a single workbook.

## Example Output

A completed buyer analysis for Knox County, TN sets the quality bar: 134 records with 113 decision-makers found, an 84% identification rate. Reaching that rate takes diverse verification sources (Secretary of State searches, Bizapedia, LinkedIn, company websites, public records, news articles) and consistent formatting of the DecisionMaker columns.

When researching, aim for a similar level of thoroughness — don't just rely on SOS searches. Cross-reference multiple sources to maximize the identification rate.

## Buyer Classification Reference

These thresholds come from `src/buyer_prospector.py` and define how buyers are categorized by investment strategy based on hold period (days between purchase and resale):

| Buyer Type | Hold Period | Strategy |
|------------|------------|----------|
| Wholesale | 0-72 days | Assignment or double close — never touches the property |
| Wholetail | 0-90 days | Quick cosmetic flip — paint, clean, list on MLS |
| Flip | 0-540 days (18 months) | Full rehab cycle — gut reno, permits, contractors |
| Buy-and-Hold | 540+ days | Long-term rental portfolio builder |

### Scoring Weights

Investors are scored on a 0-100 scale using these weighted factors (from `src/buyer_prospector.py`):

| Factor | Weight | What It Measures |
|--------|--------|-----------------|
| Recency | 30% | How recently the buyer last purchased — more recent = more active |
| Frequency | 25% | Total number of purchases — more transactions = higher volume |
| Deal Size | 20% | Average purchase price — larger deals = more available capital |
| Geographic Focus | 15% | How concentrated their purchases are — fewer ZIPs = more predictable |
| Consistency | 10% | Regularity of purchase cadence — steady buyers = more reliable |

**Why this matters for wholesalers:** A buyer with a high score (70+) is actively buying, has capital, and is focused in your area. These are the buyers most likely to close on your next deal. Prioritize outreach to high-score buyers first.

## Data Coverage Notes

The nationwide database contains ~84,000 buyer records across 1,471 counties in all 50 states + DC. Each record represents a buyer with 2+ property purchases in the last 6 months. If a county has no data, it means no buyers met the minimum purchase threshold in that timeframe — the market may have low transaction volume or limited investor activity.
