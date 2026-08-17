# Bulk Sequential Presets

Complete preset map and consultative workflow for bulk/stacked data marketing.

## What is Bulk Sequential?

Bulk Sequential marketing targets **Tier 2/3 data** such as stacked lists, AI-enriched data, and older purchased lists. These records are lower-urgency and use multi-line power dialers for efficiency.

### Key Characteristics

| Aspect | Setting |
|---|---|
| Data Type | Tier 2/3 (stacked, AI-enriched) |
| Calling Method | Multi-line power dialer |
| Urgency | Low to Medium |
| Common Tags | `dataflik`, `stacked niche` |
| Preset Count | 9 presets (00-08) |

## Preset Map (Base Template)

**Folder Name**: `01. Bulk Sequential Marketing`

| # | Preset Name | Purpose |
|---|---|---|
| 00 | Bulk Needs Skipped | New, unprocessed bulk records — need skip tracing |
| 01 | Bulk Skipped NN | Skip traced but no numbers — needs second attempt |
| 02 | Bulk Ready to Call | Records with phone numbers ready for multi-line dialer |
| 03 | Bulk Call Follow Up | Follow-up calls (1-6 attempts range) |
| 04 | Bulk Needs 1st Mail | Completed calling sequence (6+ attempts), ready for mail |
| 05 | Bulk Mail Monthly | Long-term nurture — monthly mail piece |
| 06 | Bulk Not Interested | Quarterly re-engagement with not-interested owners |
| 07 | Exhausted CC → DP | All phone numbers wrong/dead — send to Deep Prospecting |
| 08 | Bulk Return Mail → DP | Mail returned — needs Deep Prospecting |

## Filter Configurations

### 00. Bulk Needs Skipped

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Property Status | Property Filters | **Do Not Include** → Any Statuses |
| Call Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Numbers: No, Skiptraced: No |

### 01. Bulk Skipped NN

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Params & Others | General | Numbers: No, Skiptraced: Yes |

### 02. Bulk Ready to Call

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Property Status | Property Filters | **Do Not Include** → Any Statuses |
| Call Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Numbers: Yes |

### 03. Bulk Call Follow Up

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Property Status | Property Filters | **Do Not Include** → Any Statuses |
| Call Attempts | Marketing | Min: 1, Max: 6 (adjustable) |
| Params & Others | General | Numbers: Yes |

### 04. Bulk Needs 1st Mail

| Filter Block | Category | Settings |
|---|---|---|
| Call Attempts | Marketing | Min: 7 (or max attempts + 1) |
| Direct Mail Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Vacant Mailing: No |

### 05. Bulk Mail Monthly

| Filter Block | Category | Settings |
|---|---|---|
| Direct Mail Attempts | Marketing | Min: 1, Max: 12 |
| Last Direct Mailed | Marketing | Prior to Date → 1 month ago |
| Params & Others | General | Vacant Mailing: No |
| All Tags (AND) | General | **Do Not Include** → return mail |

### 06. Bulk Not Interested

| Filter Block | Category | Settings |
|---|---|---|
| Property Status | Property Filters | Include → Not Interested |
| Last Updated Field | Property Filters | Field: Status, Date: Prior to 3 months ago |

### 07. Exhausted CC → DP

| Filter Block | Category | Settings |
|---|---|---|
| Property Status | Property Filters | **Do Not Include** → Any Statuses |
| Phone Statuses | General | Include → Wrong, Wrong DNC, Dead, DNC |

### 08. Bulk Return Mail → DP

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include → return mail |
| Direct Mail Attempts | Marketing | Min: 1 |
| Phone Statuses | General | **Do Not Include at least one phone** → Correct, Correct DNC |

## Consultative Workflow

When helping a user build bulk sequential presets:

### Step 1: Discovery
Ask about their bulk data sources, dialer setup, team structure, tags, and call attempt thresholds.

### Step 2: Design
Start from this base template and customize for their lists, tags, and attempt ranges.

### Step 3: Present
Deliver the customized preset map as a document for review.

### Step 4: Implementation Guidance
Walk them through building each preset in order, starting with "00. Bulk Needs Skipped".
