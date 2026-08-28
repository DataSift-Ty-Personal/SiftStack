# Walkthrough To Offer

## Overview

This SOP turns walkthrough media for a single property into one Excel workbook whose front page answers one question: what do we offer the seller. It reads the walk videos frame by frame to scope condition, pulls sold comps inside a drawn boundary to set a conservative ARV, reads the comps' own listing remarks to decide the finish level, prices one rehab number against that finish, and renders a wholesale offer built on the percent-of-ARV rule. Use it the hour after walking a house, before any price is discussed with the seller.

## Parameters

- **property_address** (required): Street address of the subject, e.g. "1342 Grainger Ave"
- **media_folder** (required): Folder holding the walkthrough videos and stills
- **zip_code** (required): Subject ZIP, used for the comp pull
- **city** (optional, default: "Knoxville"): Subject city
- **boundary_polygon** (optional): Path to a JSON file of [lat, lon] pairs, or a hand-drawn map to trace. Without one the comp set is the whole ZIP, which is almost never right
- **rule_pct** (optional, default: "0.70"): Percent-of-ARV rule that sets the buyer's maximum
- **assignment_fee** (optional, default: "15000"): Our flat wholesale fee
- **arv_override** (optional): Force the ARV instead of taking the engine's figure

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined

## Steps

### 1. Extract frames from the walk media

Probe every video for duration and audio, then cut frames at 2 second intervals with ffmpeg (`fps=1/2,scale=900:-1`) into a scratch folder. Convert any HEIC stills with `-map 0:v:0 -frames:v 1`.

**Constraints:**
- You MUST check for an audio track and transcribe it only if speech is present, because walk videos are frequently silent or carry only ambient conversation with the seller, and a transcript of ambient talk yields nothing worth paying for
- You MUST treat the frames as the primary evidence, because narration cannot be relied on
- You MUST write frames to a scratch directory, NOT the repo, since they are large and disposable
- Artifact: a frame directory, roughly 190 frames for 6 minutes of video

### 2. Read every frame and record the condition

Look at the frames in batches and write down what is actually visible: roof, siding, foundation, mechanicals, kitchen, baths, flooring, wall and ceiling finishes, and any water damage or structural failure.

**Constraints:**
- You MUST record only what a frame or a document shows, because this scope becomes a real offer on a real house
- You MUST NOT infer a cost, a measurement or a condition that no frame proves, because someone will act on this number and an invented defect is as expensive as a missed one
- You SHOULD zoom into any ambiguous area with a cropped ffmpeg extract at the relevant timestamp rather than guessing
- Artifact: a written condition list keyed to frame numbers

### 3. Pin the subject facts from the county, not the aggregator

Query the county tax API for the parcel, then the assessor card for beds, baths, living area, year built and exterior wall code. Cross-check against Zillow and the CRM record.

**Constraints:**
- You MUST treat the county card as authoritative over Zillow and over the CRM, because aggregators get bed and bath counts wrong and the bed count moves the ARV by six figures
- You MUST note any plumbing fixture count that exceeds what the recorded bath count implies, because uncounted upstairs plumbing is common in old houses and it makes an added bath far cheaper
- Artifact: a subject facts block including parcel id

### 4. Draw and validate the comp boundary

Turn the drawn map into a polygon of [lat, lon] pairs and test it against a sold cache before trusting it.

**Constraints:**
- You MUST use a polygon rather than a bounding box, because a pocket is bounded by roads, highways and rail, not by a rectangle
- You MUST print every street and address that fell inside and compare it against the drawn map before accepting any ARV
- You MUST test two or three boundaries and report how the ARV moves, because a boundary that bleeds into a stronger sub-market silently inflates the number
- Artifact: `output/<deal>_polygon.json`

### 5. Pull comps and set a conservative ARV

Run `src/post_walkthrough.py` with the polygon to pull sold comps and save a pack. Then judge the ARV rather than accepting it.

**Constraints:**
- You MUST inspect which comps the ARV was built on and reject the figure if a thin same-bed set puts it on the market's most expensive streets
- You SHOULD widen from the same-bed clamp to size peers when the same-bed set has fewer than five comps, and You MUST say so in the basis string when you do
- You MUST anchor the ARV against the nearest sale on the subject's own street, because a pocket median can be a different street's number
- You MUST measure the bath gap across the peer set and state it, since a one-bath house does not earn a two-bath price
- Artifact: `output/<deal>_pack.json` and a written ARV basis

### 6. Read the comps' listing remarks to set the finish

Pull `property-details-address` for the closest peer comps and read the descriptions and resoFacts.

**Constraints:**
- You MUST scope the renovation to the finishes the comps actually sold with, because a finish tier chosen off a menu is an opinion while a finish read off the comps is evidence
- You MUST NOT budget a full gut when the comps sell on preserved original character, since that spends money the market does not pay back
- You MUST record the evidence in the walk file's `comp_finish_basis` field, quoting named comps and their prices
- Artifact: a finish recipe traceable to named comps

### 7. Write the walkthrough JSON with exactly one scenario

Start from `python src/post_walkthrough.py --walkthrough-template`, then fill in condition, priced flags, gates and a single `single_scenario` block.

**Constraints:**
- You MUST set `single_scenario` and You MUST NOT emit the four-scenario matrix, because the operator wants one rehab number rather than a menu
- You MUST set every flag's `scenarios` to `[]` when using `single_scenario`, because a flag carrying a non-matching key such as `["mid","gut_t2"]` is DROPPED SILENTLY and can delete tens of thousands of dollars of real work
- You MUST give any cost awaiting a signed bid a realistic value with `"placeholder": true` rather than a blank or a zero, so the total stays a true number
- You MUST record anything that can prevent a closing, such as an unopened probate, in `gates`
- Artifact: `walk_<deal>.json`

### 8. Render the workbook

Run `src/offer_sheet.py` with the pack and the walk file to build the five tab workbook: Offer, The House, Repair Detail, Comps, Buyers.

**Constraints:**
- You MUST pass `--walk` explicitly and You MUST NOT rely on the copy inside the pack, because a saved pack is a snapshot that does not track later edits and has already served stale rehab totals and a stale ARV
- You MUST pass `--arv` when the engine figure was overridden, since an explicit value beats the pack
- Artifact: `<Address>_Offer.xlsx`

### 9. Verify by recalculating, not by reading

Load the saved workbook with the `formulas` package and compute it the way Excel would.

**Constraints:**
- You MUST assert the offer figure and assert zero formula errors, because a workbook that opens is not a workbook that is correct
- You MUST change the rule percentage and confirm the offer, the buyer profit and every return move together, which proves the page is live rather than static
- You MUST confirm no tab scrolls horizontally and that no em or en dash appears anywhere
- You MUST state the assignment math out loud: the buyer maximum, the offer, and the buy price needed to clear the fee. If the numbers do not support a deal, You MUST say so plainly rather than tuning inputs until they do
- Artifact: a verification result naming the computed offer

## Examples

**1342 Grainger Ave, Knoxville TN 37917 (the worked example).** A 1920 four bed, one bath, 1,332 sf estate property, vacant, probate not yet opened. Two videos totalling 6:14 with no usable narration, reviewed as 187 frames. The county card corrected Zillow. The drawn polygon held 27 sold comps, and the engine's same-bed ARV of $481,000 was rejected because two of its three comps sat on the pocket's premium streets; the size-peer read with the bath gap measured landed at $348,000. Listing remarks on the comps showed preserved original trim, doors and windows, so the scope became a renovation rather than a gut, at $134,680. Result: buyer maximum $108,920, assignment fee $15,000, **offer to seller $93,920**.

```bash
python src/post_walkthrough.py --address "1342 Grainger Ave" --city Knoxville --zip 37917     --beds 4 --baths 1 --sqft 1332 --year-built 1920 --months 24     --polygon output/1342_grainger_polygon.json --walkthrough walk_1342_grainger.json     --save-pack output/1342_grainger_pack.json

python src/offer_sheet.py --pack output/1342_grainger_pack.json     --walk walk_1342_grainger.json --out "1342_Grainger_Ave_Offer.xlsx"
```

## Troubleshooting

**The rehab number looks wrong or matches an older run.** The pack is stale. Recompute from the walk file and pass `--walk` explicitly; never read the serialized totals.

**A cost you wrote in the walk file is missing from the workbook.** A flag carries a `scenarios` list that does not include the `single_scenario` key. Set every flag to `[]`.

**The ARV is far above the nearest sale on the subject's street.** The same-bed clamp has landed on a thin set concentrated on better streets. Widen to size peers and say so in the basis.

**The Comps tab is empty.** The comps were not revived from the pack into live objects. This is a wiring fault, not a thin market.

**`PermissionError` on save.** The workbook is open in Excel, which holds an exclusive lock. The renderer writes a `_PENDING_` copy instead; close Excel and rename it.

**A tab scrolls sideways.** Column widths were set before autofit rather than after. Autofit sizes off the longest string in a column and the full-width paragraphs live in column A.
