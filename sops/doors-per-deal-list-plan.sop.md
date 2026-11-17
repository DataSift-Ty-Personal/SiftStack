# Doors per Deal: Pick and Size Our Lists

## Overview

This SOP turns one county's Doors per Deal data into a ranked, sized list plan for outbound marketing. The agent reads the county's market share ladder from the public DataSift data file, marks each list as pull today in SiftMap or pull at the source from the courthouse, computes net new doors and expected deals per rung, sets a cut line from the team's capacity, and writes the plan with a SiftMap filter recipe for every pull-today list. Use it before starting or resizing marketing in a county; a person then pulls the lists into DataSift by hand.

## Parameters

- **county** (required): The county to plan, by name, for example "Knox".
- **state** (required): The two-letter state code, for example "TN".
- **doors_budget** (required): How many homes the team can work on every channel. The acquisitions manager sets it.
- **ladder_view** (optional, default: "ai"): "ai" reads the All lists + AI score ladder; "lists" reads the All lists ladder. Use "lists" when the account has no AI scores.
- **record_allowance** (optional, default: "unknown"): Records left in the SiftMap Monthly Limit, read from the SiftMap sidebar.
- **buy_box_min** (optional, default: 1): Lowest home value to pull, in dollars.
- **buy_box_max** (optional, default: 700000): Highest home value to pull, in dollars.
- **output_dir** (optional, default: "output/list_plans"): Folder for the plan file.

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined
- You MUST accept doors_budget as a whole number of homes and strip commas before using it

## Steps

### 1. Load the county ladder

Map {state} to its two-digit US state FIPS code, fetch `https://learn.datasift.ai/capture-data/<state_fips>.json`, and find the county object whose `name` matches {county}.

**Constraints:**
- You MUST read the ladder from `capture-data`, not `county-data`, because `county-data` scores each list on its own rather than in pull order and its current build under-counts June 2026
- You MUST match {county} against the `name` field case-insensitively inside the state file; the keys of that file are five-digit county FIPS codes
- You MUST NOT guess a FIPS code or pick the closest name when there is no exact match because a wrong county produces a plan that looks right and is not
- You MUST stop and report the list of county names in the state file when {county} is not found
- You MUST record the county object's `built` date and `window` in `{output_dir}/{county}_{state}_list_plan.md` because the page data refreshes and the plan has to say which build it came from

### 2. Read the baseline

Compute the county baseline from the county object.

**Constraints:**
- You MUST compute baseline doors per deal as `base_supply ÷ deals` and round it to one decimal
- If `deals` is zero, You MUST stop and report it because a county with no investor sales has no baseline to rank against
- You MUST write the baseline, `deals` and `base_supply` to the plan file

### 3. Read the rungs in order

Take the rungs from `variants[{ladder_view}]` exactly in the order they appear.

**Constraints:**
- You MUST keep the rungs in file order because that order is the pull order, and each rung's numbers assume every rung above it was pulled
- You MUST NOT re-sort the rungs by doors per deal because the numbers are marginal and a re-sorted ladder counts the same deals twice
- If {ladder_view} is "ai" and the county has no `ai` variant, You MUST fall back to `lists` and say so in the plan file
- For each rung, You MUST read `label`, `plan`, `priority`, `marg_dpd`, `marg_deals`, `median_gross`, `cum_share`, `cum_supply`, `thin`, `est` and `keys`

### 4. Mark where each rung comes from

Classify every rung as pull today (SiftMap), pull at the source (the courthouse), or deep prospecting.

**Constraints:**
- You MUST mark a rung "pull at the source" when its label names a court or county record: Notice of Default, Probate, Foreclosure, Lis Pendens, Final Judgment, Tax Sale, Eviction, Code Violation or Divorce
- You MUST mark an Obituary rung "deep prospecting, after month 3" because families file probate 30 to 90 days after the death and nobody should be called before that
- You MUST mark every other rung "pull today"
- You MUST flag a rung with `thin: true` as "small sample, verify as you work it"
- You MUST NOT drop a thin rung from the plan because a small sample means thin provider coverage in SiftMap, not a weak list
- You MUST flag a rung with `est: true` as "estimate, giant list sampled rather than matched record by record"

### 5. Compute net new doors and expected deals

For each rung, work out how many homes it adds and how many deals it should reach in 6 months.

**Constraints:**
- You MUST compute net new doors as the rung's `cum_supply` minus the previous rung's `cum_supply`, with zero for the first rung's previous value
- You MUST NOT use `cum_supply` as the size of one list because it includes every rung above it
- You MUST use `marg_deals` as the expected new deals for the rung, because it is the page's New deals reached figure
- You MUST show `marg_dpd` as the rung's doors per deal and `median_gross` as its typical gross

### 6. Set the cut line

Walk down the rungs and stop where the running total would pass the team's capacity.

**Constraints:**
- You MUST add rungs in order while `cum_supply` stays at or under {doors_budget}
- You MUST place every Priority 1 rung that fits before any Priority 2 rung, and You MUST NOT add a Priority 3 rung because Priority 3 buys market share at a steep cost per deal
- If {record_allowance} is a number and the pull-today rungs above the cut line add up to more than it, You MUST move the cut line up until they fit and say why in the plan
- You MUST write the cut line rung, its `cum_supply` and its `cum_share` to the plan file

### 7. Plan the monthly adds

Estimate how many new homes join the lists above the cut line each month.

**Constraints:**
- You MUST compute monthly adds as the cut line `cum_supply` × 0.075, and show the 5% to 10% range next to it
- You MUST NOT use 0.0075 because lists turn over 5% to 10% a month and 0.75% understates the monthly adds by ten times

### 8. Write the SiftMap recipe for each pull-today rung

For every pull-today rung above the cut line, write the settings a person enters in SiftMap.

**Constraints:**
- You MUST write: county "{county} County, {state}"; Price on the Est. Value tab from {buy_box_min} to {buy_box_max}; Property Types "Single Family Res."; Homeowner and Property Distressors set to Custom Combination with All Selected
- You MUST write "Equity % from 100 (under Financial Details)" for any rung whose label includes Free & Clear, because Free & Clear is not a distressor in the SiftMap filter panel
- You MUST name Senior as "Senior Homeowners" and Vacant as "Vacant Properties", and for any other signal You MUST write the rung's own signal name plus its `keys` and mark it "match by name in SiftMap"
- You MUST NOT invent a SiftMap distressor label because a wrong label silently changes who ends up on the list
- For an AI score rung, You MUST write "confirm the AI Scores filter settings with Ty" because no verified recipe exists yet
- You MUST write the tag "Priority 1" or "Priority 2" to match the rung, Auto-Add New Records on, and the saved filter name "{county} - Priority <n> - <label>" with the description "<marg_dpd> Doors per Deal"

### 9. Write and report the plan

Write the finished plan and summarize it.

**Constraints:**
- You MUST write the plan to `{output_dir}/{county}_{state}_list_plan.md` with: the header (county, state, view, `built`, `window`, baseline), a rung table (rung, list, plan tag, priority, pull from, net new doors, doors per deal, expected deals, typical gross, flags), the cut line, the monthly adds, and one SiftMap recipe per pull-today rung
- You MUST NOT add records to any DataSift account or turn on any auto-add because pulling spends the account's record allowance and is a person's decision
- You MUST end your reply with the file path, the cut line, the total doors above it and the expected deals above it

## Examples

### Example Input

county: Knox
state: TN
doors_budget: 1500
ladder_view: ai

### Example Output

`output/list_plans/Knox_TN_list_plan.md`, built 2026-08-13, window 2026-01 to 2026-06, baseline 165.8 (153,675 homes ÷ 927 deals).

| Rung | List | Pull from | Net new doors | Doors per deal | Expected deals | Typical gross | Flags |
|------|------|-----------|---------------|----------------|----------------|---------------|-------|
| 1 | Notice of Default | At the source | 75 | 10.7 | 7 | $66,799 | small sample |
| 2 | Probate | At the source | 44 | 14.7 | 3 | $46,000 | small sample |
| 3 | Free & Clear + Senior + Vacant | Pull today | 337 | 19.8 | 17 | $131,000 | |
| 4 | AI score 95+ | Pull today | 1,001 | 20.0 | 50 | $142,053 | |

Cut line after rung 4: 1,457 total doors, 8.3% market share, 77 expected deals. Monthly adds: 1,457 × 0.075 = about 109 (73 to 146 at 5% to 10%). Rung 3 recipe: Knox County, TN; Est. Value $1 to $700,000; Single Family Res.; Custom Combination, All Selected: Senior Homeowners, Vacant Properties; Equity % from 100; tag Priority 1; Auto-Add New Records on; name "Knox - Priority 1 - Free & Clear + Senior + Vacant"; description "19.8 Doors per Deal".

## Troubleshooting

### The county is not in the state file
Report the county names the state file does contain and ask which one was meant. Some places are independent cities with their own entry.

### The ladder is very short
Counties with too little investor activity show state-adapted benchmarks instead of their own ranking. Write the plan, and say at the top that the order is a guide rather than a proven local ladder.

### The numbers do not match the page
The page and the data file refresh together. Re-fetch the file, compare its `built` date with the page, and rebuild the plan from the newer data.
