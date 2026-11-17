# Niche Sequential Presets

Complete preset map and consultative workflow for first-to-market (Tier 1) data marketing.

## What is Niche Sequential?

Niche Sequential marketing targets **first-to-market / Tier 1 data** such as probates, pre-foreclosures, tax sales, code violations, and other courthouse-sourced lists. These records are high-urgency and require manual click-to-dial calling.

### Key Characteristics

| Aspect | Setting |
|---|---|
| Data Type | First-to-market / Tier 1 |
| Calling Method | Manual click-to-dial |
| Urgency | High |
| Common Tags | `courthouse data`, `probate`, `foreclosure` |
| Preset Count | 12 presets (00-11) |

## Preset Map (Base Template)

**Folder Name**: `00 Niche Sequential Marketing`

| # | Preset Name | Purpose |
|---|---|---|
| 00 | Needs Skip Traced | New, unprocessed records -- need skip tracing for phone numbers |
| 01 | Ready to Text | Skip traced with numbers, zero SMS attempts -- send first text |
| 02 | Needs Called Day 1 | Day 1 of 3-day calling cycle -- first call attempt after SMS |
| 03 | Needs Called Day 2 | Day 2 of calling cycle -- second call attempt |
| 04 | Needs Called Day 3 | Day 3 of calling cycle -- final call attempt + final SMS |
| 05 | Needs Mailed | Completed calling cycle (3 attempts), ready for direct mail |
| 06 | Needs Deep Prospecting | Exhausted standard channels -- needs manual research/door knock |
| 07 | Callback Scheduled | Owner requested callback at specific time -- pending appointment |
| 08 | Hot Lead | High-motivation seller identified -- priority follow-up |
| 09 | Not Interested | Owner declined -- quarterly re-engagement eligible |
| 10 | Bad Data | Wrong number, wrong address, or invalid record -- needs cleanup |
| 11 | Completed Cycle | Full marketing cycle complete -- archive or long-term nurture |

## Filter Configurations

### 00. Needs Skip Traced

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include data tag (e.g., `courthouse data`) |
| Property Status | Property Filters | **Do Not Include** -> Sold |
| Params & Others | General | Numbers: No, Skiptraced: No |

### 01. Ready to Text

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include data tag |
| Property Status | Property Filters | **Do Not Include** -> Sold |
| SMS Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Numbers: Yes, Skiptraced: Yes |

### 02-04. Needs Called Day 1, Day 2, Day 3

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include data tag |
| Property Status | Property Filters | **Do Not Include** -> Sold |
| Call Attempts | Marketing | Min: X-1, Max: X-1 (where X = day number, so Day 1 = 0 prior calls) |
| SMS Attempts | Marketing | Min: 1 (has been texted) |
| Params & Others | General | Numbers: Yes |

**3-day cycle logic:** Day 1 (preset 02) = texted but 0 calls. Day 2 (preset 03) = 1 call attempt. Day 3 (preset 04) = 2 call attempts. Day 3 also sends a final SMS if no answer.

**Phone tier dialing order:** Dial First (score 81-100) -> Dial Second (61-80) -> Dial Third (41-60) -> Dial Fourth (21-40) -> Drop (0-20).

### 05. Needs Mailed

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Call Attempts | Marketing | Min: 3 (completed calling cycle) |
| Direct Mail Attempts | Marketing | Min: 0, Max: 0 |
| Property Status | Property Filters | **Do Not Include** -> Sold |
| Params & Others | General | Vacant Mailing: No |
| All Tags (AND) | General | **Do Not Include** -> return mail |

### 06. Needs Deep Prospecting

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Property Status | Property Filters | **Do Not Include** -> Sold |
| Phone Statuses | General | **Do Not Include** -> Correct (all numbers exhausted) |
| Call Attempts | Marketing | Min: 3 |
| Direct Mail Attempts | Marketing | Min: 1 |

Records land here when calling and mailing both failed to produce contact.

### 07. Callback Scheduled

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include -> `callback` |
| Property Status | Property Filters | **Do Not Include** -> Sold |

### 08. Hot Lead

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Property Status | Property Filters | Include -> Qualified |
| Any Tags (OR) | General | Include -> `hot lead` |

### 09. Not Interested

| Filter Block | Category | Settings |
|---|---|---|
| Property Status | Property Filters | Include -> Not Interested |
| Last Updated Field | Property Filters | Field: Status, Date: Prior to 3 months ago |
| Params & Others | General | Numbers: Yes |

Quarterly re-engagement: records show here 3+ months after being marked Not Interested.

### 10. Bad Data

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include -> `bad data`, `wrong number`, `bad address` |
| Property Status | Property Filters | **Do Not Include** -> Sold |

Records needing cleanup: wrong numbers, invalid addresses, duplicate entries.

### 11. Completed Cycle

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Call Attempts | Marketing | Min: 3 |
| Direct Mail Attempts | Marketing | Min: 6 |
| Property Status | Property Filters | **Do Not Include** -> Sold, Qualified, In Progress, Contract |

Records that have gone through the full SMS -> Call -> Mail cycle without converting.

## Consultative Workflow

When helping a user build niche sequential presets:

### Step 1: Discovery
Ask about their specific niches, marketing channels, team structure, data tags, and call attempt cadence.

### Step 2: Design
Start from this base template and customize for their lists, tags, and attempt thresholds.

### Step 3: Present
Deliver the customized preset map as a document for review.

### Step 4: Implementation Guidance
Walk them through building each preset in order, starting with "00. Needs Skipped".
