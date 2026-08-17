---
name: sequential-presets
description: Design, build, and manage sequential marketing filter presets in DataSift (REI Sift). Use when the user needs help setting up filter presets for first-to-market (Niche) or bulk data, organizing their marketing funnel from skip tracing through SMS, calling, mail, and deep prospecting, or customizing workflows based on their specific niche, marketing channels, and team structure. This skill provides both consultative guidance and hands-on implementation support.
---

# Sequential Presets Skill

This skill makes you a world-class preset consultant AND implementor. You guide users through a consultative process to design optimized sequential marketing filter presets in DataSift, then help them build each preset step-by-step in the DataSift UI.

## Execution Mode Detection

Before starting, detect the execution environment:

**Check 1:** Does `scripts/manage_presets.py` exist in this skill's directory?
**Check 2:** Is Playwright available? Run: `python -c "from playwright.sync_api import sync_playwright; print('OK')"`
**Check 3:** Are credentials set? Check for `DATASIFT_EMAIL` and `DATASIFT_PASSWORD` in `.env` or environment.

### Automated Mode (Claude Code CLI — all 3 checks pass)

Run preset management directly:

```bash
# Discover all existing presets
python scripts/manage_presets.py --discover

# Add Sold exclusion to all 21 presets
python scripts/manage_presets.py --add-sold-exclusion

# Full workflow: discover + update all
python scripts/manage_presets.py --all
```

The script handles login, filter panel navigation, preset discovery, and Sold exclusion updates automatically using Playwright browser automation. After running, proceed to the consultative design section below.

### Manual Mode (Co-Work or no Playwright — any check fails)

Follow the step-by-step UI walkthrough instructions below. Claude will guide you through each click in the DataSift interface.

## Core Concepts

### Niche vs. Bulk Sequential Marketing

The primary distinction is the data source and marketing approach. Determine which strategy the user is employing first -- this dictates the entire preset structure.

| Aspect | Niche Sequential | Bulk Sequential |
|---|---|---|
| **Data Type** | First-to-market/Tier 1 (courthouse data) | Tier 2/3 (stacked lists, AI-enriched) |
| **Calling Method** | Manual click-to-dial | Multi-line power dialer |
| **Urgency** | High | Low to Medium |
| **Primary Tag** | `Courthouse Data` | `dataflik` |
| **Preset Count** | 12 presets (00-11) | 9 presets (00-08) |
| **Folder Name** | `00 Niche Sequential Marketing` | `01. Bulk Sequential Marketing` |

**Total: 21 presets across both folders.** All 21 presets exclude Sold status (Property Status: "Do not include" -> "Sold").

### Data Tag Taxonomy

Tags drive which preset folder processes a record:

| Tag | Meaning | Routes To |
|---|---|---|
| `Courthouse Data` | First-to-market county data (probate, foreclosure, etc.) | Niche presets |
| `dataflik` | Aggregated/bulk data source | Bulk presets |
| `stacked niche` | Niche data that also appears in bulk lists | Both folders (prioritize niche) |

### The Pendulum Theory of Marketing

Marketing activities are sequenced from lowest to highest cost per touch for maximum ROI:

| Channel | Cost Per Touch | Notes |
|---|---|---|
| 1. SMS | ~$0.01/message | Cheapest, highest volume |
| 2. Cold Calling | ~$0.03-0.06/attempt | VA cost, not platform fee |
| 3. Direct Mail | $0.50-2.00/piece | Postcard ($0.50) to handwritten letter ($1.75) |
| 4. Deep Prospecting | $1.50-4.00/record | Skip trace + research time |
| 5. Door Knocking | $0/knock | Labor only, highest conversion but not scalable |

Your preset design should guide records through this cost-effective pendulum.

### The 3 Core Questions of Workflow

Every preset system answers three questions:

1. What new data needs to be processed (skip traced)?
2. What data is ready for its first marketing touch?
3. What data has been marketed to but requires follow-up?

### Round-Robin Assignment

Round-robin assigns incoming records to team members in rotating order, so multiple callers/managers share a lead pool evenly. Configure in DataSift: Records -> Assign -> Round Robin. Ask the user if they want round-robin during discovery -- it affects how presets feed records to team members.

## The Consultative Workflow

Follow this four-step process. Do not skip steps.

### Step 1: Discovery & Requirement Gathering

Ask clarifying questions before designing anything:

- **Strategy**: "Are you focusing on **Niche Sequential** (first-to-market, courthouse data) or **Bulk Sequential** (stacked, older data)?"
- **Niche Lists**: "What specific first-to-market niches are you targeting? (e.g., Probate, Pre-Foreclosure, Tax Sale, Code Violations, Eviction, Divorce)"
- **Marketing Channels**: "Which marketing channels will you use, and in what order? (e.g., SMS only, SMS + Calling, Full Pendulum)"
- **Team Structure**: "Who handles each part? (Solo operator, VA for calling, dedicated Lead Manager, round-robin team)"
- **Data Tags**: "What tag identifies your primary marketing list? (e.g., `Courthouse Data`, `dataflik`)"
- **Attempt Cadence**: "How many call days before moving to the next channel? (Standard is 3 days for niche)"

### Step 2: Design the Preset Map & Configuration

Based on the user's answers, design a complete preset map:

1. **Choose a Base Template**: Read the appropriate reference file:
   - For Niche Sequential: `references/niche-sequential-map.md`
   - For Bulk Sequential: `references/bulk-sequential-map.md`

2. **Customize the Map**: Adjust the base template for the user's niches, channels, and team structure. For detailed filter settings, read:
   - `references/filter-configurations.md`

3. **Document the Plan**: Create a clear step-by-step document including:
   - The preset folder name
   - A numbered table of every preset in order
   - Exact filter blocks and settings for each preset

### Step 3: Present the Plan for Confirmation

Present the preset plan for review. Be concise:

> "Based on our discussion, here is your customized sequential preset plan with complete filter configurations. Please review and let me know if you approve, or if any presets need adjustment."

**Wait for explicit approval before proceeding.**

**If the user rejects the plan:** Do NOT restart from scratch. Ask which specific presets need changes. Common rejection reasons include wrong call attempt thresholds, wrong mail timing, or a missing stage. Iterate on just the rejected elements.

### Step 4: Implementation

Once approved, walk the user through building each preset in DataSift. You can either guide them step-by-step or, if you have browser automation access, build the presets directly.

#### DataSift UI Implementation Steps

For each preset in order (always start with 00):

1. **Open the filter panel** (left sidebar on the Records page)
2. **Scroll to the bottom** of the filter panel -- the Filter Presets section is at the very bottom
3. **Expand "Filter Presets"** by clicking the section header
4. **Expand the target folder** (e.g., "00 Niche Sequential Marketing")
   - Folder names may have case variations ("00 Niche" vs "00 NICHE") -- use case-insensitive comparison
5. **Click the preset name** to load it (or click "Save New" to create a new one)
6. **Configure each filter block** according to the plan (see filter-configurations.md)
7. **Add the Sold exclusion**: Property Status -> "Do not include" -> "Sold" (required on ALL 21 presets)
8. **Save the preset**: Click "Save" (NOT "Save New") to update an existing preset, then confirm the overwrite dialog

#### Preset Naming Convention

All preset names follow the pattern `^\d{2}\.` (two-digit number, period, space, name):
- `00. Needs Skip Traced`
- `01. Ready to Text`
- `02. Needs Called Day 1`
- etc.

#### Key UI Patterns

- The filter panel is a scrollable `<div>`, not the browser viewport -- standard scroll methods may not work
- Styled-Components dropdowns (not native `<select>`) are used for all filter controls
- Multiple Select dropdowns exist per panel -- target the correct one by context
- After modifying a preset, always use "Save" (overwrites) not "Save New" (creates duplicate)

## 12 Niche Preset Names (from source code)

These are the canonical preset names. Do not deviate from these:

| # | Preset Name | Purpose |
|---|---|---|
| 00 | Needs Skip Traced | New records without phone data -- route to skip trace |
| 01 | Ready to Text | Has phone (Dial First/Second tier), not yet texted |
| 02 | Needs Called Day 1 | Texted, not called yet -- first call attempt |
| 03 | Needs Called Day 2 | Called once, no answer -- second attempt, different script |
| 04 | Needs Called Day 3 | Called twice, final attempt -- urgency-focused |
| 05 | Needs Mailed | Exhausted calls, ready for direct mail piece |
| 06 | Needs Deep Prospecting | Mail returned / no response after full cycle |
| 07 | Callback Scheduled | Appointment set during a call -- follow up on schedule |
| 08 | Hot Lead | Expressed interest during contact -- route to closer |
| 09 | Not Interested | Declined -- schedule 90-day recycle |
| 10 | Bad Data | Wrong number/address -- route to re-skip trace |
| 11 | Completed Cycle | Full 3-day cycle done, move to nurture |

## 9 Bulk Preset Names

| # | Preset Name | Purpose |
|---|---|---|
| 00 | Bulk Needs Skipped | New bulk records -- need skip tracing |
| 01 | Bulk Skipped NN | Skip traced but no numbers -- needs second attempt |
| 02 | Bulk Ready to Call | Records with numbers ready for multi-line dialer |
| 03 | Bulk Call Follow Up | Follow-up calls (1-6 attempts range) |
| 04 | Bulk Needs 1st Mail | Completed calling (6+ attempts), ready for mail |
| 05 | Bulk Mail Monthly | Long-term nurture -- monthly mail piece |
| 06 | Bulk Not Interested | Quarterly re-engagement with not-interested owners |
| 07 | Exhausted CC -> DP | All phone numbers wrong/dead -- Deep Prospecting |
| 08 | Bulk Return Mail -> DP | Mail returned -- needs Deep Prospecting |

---

## How to Create These Presets in DataSift

### Implementation Order

Build niche presets first (they handle your highest-value first-to-market data), then bulk.

1. **Niche presets (00-11)** → folder "00 Niche Sequential Marketing"
2. **Bulk presets (00-08)** → folder "01. Bulk Sequential Marketing"

### Step-by-Step UI Walkthrough

1. **Open the filter panel** — Click "Filter Records" on the records page
2. **Scroll to the bottom** of the filter panel (it's a scrollable `<div>`, not the main viewport — use JS `scrollIntoView` if automating)
3. **Expand "Filter Presets"** section — click the arrow/chevron to reveal preset folders
4. **Expand the target folder** — "00 Niche Sequential Marketing" or "01. Bulk Sequential Marketing" (folder names may have case variations — use case-insensitive matching)
5. **Click "+ New Preset"** or click an existing preset to modify
6. **Configure filter blocks** for each preset:
   - Set the filter conditions per `references/filter-configurations.md`
   - Every preset must include: Property Status → "Do not include" → "Sold"
   - Preset names follow the pattern `XX. Preset Name` (e.g., "00. Needs Skip Traced")
7. **Save** — Click "Save" (not "Save New") if modifying an existing preset, then confirm overwrite

### Filter Block Setup (per preset)

For each preset, configure these filter blocks as specified in `references/filter-configurations.md`:

- **Any Tags (OR)** — which tags trigger inclusion (e.g., "Courthouse Data" for niche)
- **All Tags (AND)** — which tags must ALL be present
- **Property Status** — always exclude "Sold"
- **Phone Statuses** — filter by Correct, Wrong, Dead, DNC as needed
- **Call Attempts (Min/Max)** — track marketing progress
- **Direct Mail Attempts (Min/Max)** — track mail stages
- **Params & Others** — Numbers Yes/No, Skiptraced Yes/No

### Verification Checklist

After building all presets, verify:

- [ ] All 12 niche presets created in "00 Niche Sequential Marketing" folder
- [ ] All 9 bulk presets created in "01. Bulk Sequential Marketing" folder
- [ ] Every preset excludes "Sold" status
- [ ] Preset 00 (Needs Skip Traced) catches new records with no phone numbers
- [ ] Preset 01 (Ready to Text) only shows records WITH phone numbers in Dial First/Second tiers
- [ ] Records flow from one preset to the next as tags are applied (test with 1 record)
- [ ] No records appear in multiple presets simultaneously (filters are mutually exclusive)

### Common Issues

| Problem | Cause | Fix |
|---------|-------|-----|
| Record appears in 2 presets | Overlapping tag conditions | Add exclusion tag to the earlier preset |
| Preset shows 0 records | Filter too restrictive | Check each filter block individually |
| New records don't appear | Missing "Courthouse Data" or "dataflik" tag | Verify CSV upload includes correct tags |
| Records stuck in one preset | Tags not being applied after action | Check that your calling/mailing workflow adds the progression tag |
