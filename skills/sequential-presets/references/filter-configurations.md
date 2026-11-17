# Filter Preset Configurations

This document provides the detailed filter block configurations for each preset in both Niche and Bulk Sequential marketing. Use these when building a user's custom preset plan.

## Filter Block Types Explained

Before configuring presets, understand each filter block type available in DataSift:

| Filter Block | How It Works | Example Use |
|---|---|---|
| **Any Lists (OR)** | Record is in ANY of the selected lists | Include "Foreclosure" OR "Probate" OR "Tax Sale" |
| **Any Tags (OR)** | Record has ANY of the selected tags | Include records tagged "Courthouse Data" OR "probate" |
| **All Tags (AND)** | Record has ALL of the selected tags | Exclude records that have BOTH "return mail" AND "mailed" |
| **Property Status** | Include or exclude by status value | "Do not include" -> "Sold", "Dead", "DNC" |
| **Call Attempts** | Filter by number of call attempts (Min/Max) | Min: 0, Max: 0 = never called; Min: 3 = called 3+ times |
| **Direct Mail Attempts** | Filter by number of mail pieces sent (Min/Max) | Min: 0, Max: 0 = never mailed |
| **Last Direct Mailed** | Filter by when the last mail piece was sent | "Prior to Date" -> 1 month ago = not mailed in 30+ days |
| **Phone Statuses** | Filter by phone number disposition | Wrong Number, Dead, DNC, Active, Correct, Correct DNC |
| **Params & Others** | Misc parameters: Numbers (Yes/No), Skiptraced (Yes/No), Vacant Mailing (Yes/No) | Numbers: No + Skiptraced: No = new unprocessed records |
| **Last Updated Field** | Filter by when a specific field was last changed | Field: Status, Date: Prior to 3 months ago |

### Phone Status Values

| Status | Meaning |
|---|---|
| **Correct** | Reached the right person |
| **Correct DNC** | Right person, asked to be on Do Not Call |
| **Wrong** | Wrong number (not the property owner) |
| **Wrong DNC** | Wrong number AND on DNC |
| **Dead** | Disconnected / not in service |
| **DNC** | On Do Not Call list (unverified owner) |
| **Active** | Number exists but no disposition yet |

### Property Status Values

| Status | Meaning |
|---|---|
| **New** | Just imported, no action taken |
| **Lead** | Active lead in pipeline |
| **Not Interested** | Declined, eligible for future re-engagement |
| **Sold** | Property sold -- exclude from ALL marketing presets |
| **Dead** | Dead deal, no future potential |
| **DNC** | Do Not Contact at owner level |

### Critical Rule: Sold Exclusion

**Every single preset (all 21) must include:** Property Status -> "Do not include" -> "Sold"

This prevents marketing to properties that have already sold. The "Sold Property Cleanup" sequence in DataSift auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

---

## Niche Sequential Presets (12 presets)

**Folder:** `00 Niche Sequential Marketing`

### 00. Needs Skip Traced

New records without phone data -- route to skip trace.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include user-defined first-to-market lists (e.g., Foreclosure, Probate, Tax Sale) |
| Any Tags (OR) | General | Include `Courthouse Data` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Call Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Numbers: No, Skiptraced: No |

### 01. Ready to Text

Has phone numbers (Dial First/Second tier), not yet texted. First marketing touch via SMS.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include `Courthouse Data` |
| All Tags (AND) | General | **Do Not Include** -> `sms_sent` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Params & Others | General | Numbers: Yes, Skiptraced: Yes |

### 02. Needs Called Day 1

Texted but not called yet -- first call attempt.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include `sms_sent` |
| All Tags (AND) | General | **Do Not Include** -> `called_day1` |
| Property Status | Property Filters | **Do Not Include** -> `Sold`, `Not Interested` |
| Params & Others | General | Numbers: Yes |

### 03. Needs Called Day 2

Called once, no answer -- second attempt with different script.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include `called_day1` |
| All Tags (AND) | General | **Do Not Include** -> `called_day2` |
| Property Status | Property Filters | **Do Not Include** -> `Sold`, `Not Interested` |
| Params & Others | General | Numbers: Yes |

### 04. Needs Called Day 3

Called twice, final attempt -- urgency-focused script and voicemail.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include first-to-market lists |
| Any Tags (OR) | General | Include `called_day2` |
| All Tags (AND) | General | **Do Not Include** -> `called_day3` |
| Property Status | Property Filters | **Do Not Include** -> `Sold`, `Not Interested` |
| Params & Others | General | Numbers: Yes |

### 05. Needs Mailed

Exhausted 3-day call cycle, ready for direct mail piece.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `called_day3` |
| All Tags (AND) | General | **Do Not Include** -> `mailed` |
| Direct Mail Attempts | Marketing | Min: 0, Max: 0 |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Params & Others | General | Vacant Mailing: No |

### 06. Needs Deep Prospecting

Mail returned or no response after full cycle -- route to intensive research.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `cycle_complete` |
| All Tags (AND) | General | **Do Not Include** -> `dp_complete` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |

### 07. Callback Scheduled

Appointment set during a call -- follow up on the scheduled date.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `callback_scheduled` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |

### 08. Hot Lead

Expressed interest during contact -- route to closer immediately.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `hot` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |

### 09. Not Interested

Declined -- schedule 90-day recycle with different mailer type.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `not_interested` |
| Property Status | Property Filters | Include -> `Not Interested` |
| Last Updated Field | Property Filters | Field: Status, Date: Prior to 3 months ago |
| Params & Others | General | Numbers: Yes |

### 10. Bad Data

Wrong number or address -- route to re-skip trace.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `bad_data` |
| Phone Statuses | General | Include -> `Wrong`, `Wrong DNC`, `Dead` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |

### 11. Completed Cycle

Full 3-day cycle done, no interest expressed -- move to long-term nurture.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include `cycle_complete` |
| All Tags (AND) | General | **Do Not Include** -> `hot` |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |

---

## Bulk Sequential Presets (9 presets)

**Folder:** `01. Bulk Sequential Marketing`

### 00. Bulk Needs Skipped

New bulk records without phone data -- need skip tracing.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include user-defined bulk lists |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Call Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Numbers: No, Skiptraced: No |

### 01. Bulk Skipped NN

Skip traced but yielded no phone numbers -- needs second attempt.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Params & Others | General | Numbers: No, Skiptraced: Yes |

### 02. Bulk Ready to Call

Records with phone numbers ready for the multi-line power dialer.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Call Attempts | Marketing | Min: 0, Max: 0 |
| Params & Others | General | Numbers: Yes |

### 03. Bulk Call Follow Up

Follow-up calls for bulk lists, typically 1-6 attempts range.

| Filter Block | Category | Settings |
|---|---|---|
| Any Lists (OR) | General | Include bulk lists |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Call Attempts | Marketing | Min: 1, Max: 6 (adjustable) |
| Params & Others | General | Numbers: Yes |

### 04. Bulk Needs 1st Mail

Completed bulk calling sequence (6+ attempts), ready for first mail piece.

| Filter Block | Category | Settings |
|---|---|---|
| Call Attempts | Marketing | Min: 7 (or user-defined max attempts + 1) |
| Direct Mail Attempts | Marketing | Min: 0, Max: 0 |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Params & Others | General | Vacant Mailing: No |

### 05. Bulk Mail Monthly

Long-term nurture -- monthly mail piece to uncontacted bulk records.

| Filter Block | Category | Settings |
|---|---|---|
| Direct Mail Attempts | Marketing | Min: 1, Max: 12 |
| Last Direct Mailed | Marketing | Prior to Date -> 1 month ago |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Params & Others | General | Vacant Mailing: No |
| All Tags (AND) | General | **Do Not Include** -> `return mail` |

### 06. Bulk Not Interested

Quarterly re-engagement with previously not-interested bulk owners.

| Filter Block | Category | Settings |
|---|---|---|
| Property Status | Property Filters | Include -> `Not Interested` |
| Last Updated Field | Property Filters | Field: Status, Date: Prior to 3 months ago |

### 07. Exhausted CC -> DP

All phone numbers marked wrong or dead -- send to Deep Prospecting.

| Filter Block | Category | Settings |
|---|---|---|
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Phone Statuses | General | Include -> `Wrong`, `Wrong DNC`, `Dead`, `DNC` |

### 08. Bulk Return Mail -> DP

Mail returned on bulk records -- bad address, needs Deep Prospecting.

| Filter Block | Category | Settings |
|---|---|---|
| Any Tags (OR) | General | Include -> `return mail` |
| Direct Mail Attempts | Marketing | Min: 1 |
| Property Status | Property Filters | **Do Not Include** -> `Sold` |
| Phone Statuses | General | **Do Not Include at least one phone** -> `Correct`, `Correct DNC` |
