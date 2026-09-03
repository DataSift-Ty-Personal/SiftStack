# Remove Sold Properties From Marketing

## Overview

This SOP sets up the sold-property suppression loop in a DataSift account: a sequence that flips any record tagged "recently sold" to Sold status, a SiftMap county filter that finds every real sale, a one-time backlog pull, and daily auto-add so new sales suppress themselves. Use it when standing up a new account or market so no marketing dollars are spent on properties that already sold.

## Parameters

- **county** (required): The county to suppress, as SiftMap names it (e.g. "Knox County, TN")
- **price_floor** (optional, default: "1000"): Minimum last sale price in dollars
- **years_back** (optional, default: "3"): How far back the last-sold-date window reaches
- **tag_name** (optional, default: "recently sold"): The tag shared by the preset and the sequence
- **sequence_name** (optional, default: "Sold Property Removal"): Name for both the sequence and the saved filter

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined

## Steps

### 1. Build the Sequence

Create a sequence named {sequence_name} in the Transactions folder: trigger on property tags added, condition tag {tag_name}, action change property status to Sold.

**Constraints:**
- You MUST create the sequence before the filter because auto-added records must land on an already-armed trigger
- You MUST use exactly {tag_name} as the condition tag because the SiftMap preset applies this same tag and any mismatch means the sequence never fires
- You MUST set the action's status to Sold, not leave it on Default
- You MUST verify the sequence appears in the Transactions folder after saving
- You MUST record the sequence name and folder in progress.md

### 2. Build the SiftMap Filter

In SiftMap, search {county} and set exactly two filters under the More tab: last sale price minimum {price_floor}, and last sold date from January 1 of {years_back} years ago through today. Apply, then save the filter as {sequence_name} with the {tag_name} tag and auto-add OFF.

**Constraints:**
- You MUST filter at the county level because zip or neighborhood scoping misses sales and multiplies presets
- You MUST NOT set any other filters because extra criteria silently shrink the sold universe the suppression is supposed to cover
- You MUST NOT drop the price floor because sales under it are mostly interfamily transfers and inherited properties, not market sales
- You MUST confirm the applied result count is plausible for the market (tens of thousands for a metro county over {years_back} years) and stop to report if it is near zero
- You MUST leave auto-add OFF at save time because auto-add only catches new matches and the backlog has to be pulled first
- You MUST record the result count in progress.md

### 3. Pull the Backlog

Add every matching property to the account in batches: select the maximum 10,000, add with the {tag_name} tag, owners protected, no lists, and repeat until the full count from Step 2 is in.

**Constraints:**
- You MUST keep "Do not replace owners of existing Properties" ON because existing acquisition records carry researched owner data that a sold-suppression pull must never overwrite
- You MUST apply {tag_name} on every batch because an untagged batch lands silently and never gets suppressed
- You MUST NOT add any lists because the system attaches classification lists automatically
- You MUST repeat batches until the cumulative added count covers the Step 2 result count, and log each batch in progress.md
- You SHOULD verify adds are landing via the Activity page rather than assuming, because bulk adds process async and a stalled queue looks identical to success

### 4. Enable Auto-Add

Edit the saved preset {sequence_name}: toggle Auto-Add New Records ON, confirm {tag_name} is still applied by the preset, and save.

**Constraints:**
- You MUST NOT enable auto-add before Step 3 is complete because auto-add only captures new sales going forward
- You MUST confirm the tag is still on the preset after editing because the tag is the only link between the preset and the sequence
- You MUST read the preset back after saving and verify auto-add shows enabled

### 5. Verify the Daily Flow

On the next day, confirm the loop runs end to end: an auto-upload appears in Activity, its clean records carry status Sold, and one spot-checked property's history shows a real recent sale.

**Constraints:**
- You MUST check the Activity page for the auto-upload entry and record its record count in progress.md
- You MUST open at least one clean record and verify its status is Sold
- You MUST spot-check one property's sale history to confirm the suppression caught a real sale
- If clean records arrive without Sold status, You MUST compare the preset tag and the sequence condition tag character by character and fix the mismatch
- You SHOULD note that Incomplete records among the daily adds are normal because entity-owned buyers were never marketable records

## Examples

### Example Input
```
county: Knox County, TN
years_back: 3
```

### Example Output
```
Sequence "Sold Property Removal" live in Transactions.
Preset saved: Knox County, $1,000+ sale price, sold since 2023-01-01.
Backlog: 32,786 properties pulled in 4 batches, all tagged recently sold.
Auto-add ON. Next-day verification: 92 records auto-added, clean records
at status Sold, spot-check showed an active Pending resale. progress.md updated.
```

## Troubleshooting

### Daily adds arrive but statuses never change
The preset tag and sequence condition tag do not match exactly. Fix one so both are identical, then add the tag manually to one affected record to confirm the sequence fires.

### Result count near zero after applying filters
A stray filter is set outside the More tab. Clear all filters and rebuild with only the county, the price floor, and the date window.

### Old sold properties still receiving marketing
Auto-add was enabled before the backlog pull. Re-run Step 3; auto-add does not backfill.
