# Bulk Sequential Preset Map (Base Template)

This document provides the standard preset structure for a **Bulk Sequential** marketing workflow targeting stacked/aggregated data. Use this as the starting point for building a user's customized plan.

## What is Bulk Sequential?

Bulk Sequential marketing targets **Tier 2/3 data** such as stacked lists, AI-enriched data, and older purchased lists. These records are lower-urgency and use multi-line power dialers for volume efficiency.

### Key Characteristics

| Aspect | Setting |
|---|---|
| Data Type | Tier 2/3 (stacked lists, AI-enriched, aged data) |
| Calling Method | Multi-line power dialer |
| Urgency | Low to Medium |
| Primary Tag | `dataflik` |
| Secondary Tag | `stacked niche` (niche data also in bulk lists) |
| Preset Count | 9 presets (00-08) |
| Folder Name | `01. Bulk Sequential Marketing` |

### Cost Pendulum (per touch)

| Channel | Cost | Stage |
|---|---|---|
| Cold Call (power dialer) | ~$0.03-0.06/attempt | Primary channel, high volume |
| Direct Mail | $0.50-2.00/piece | After calling exhausted |
| Deep Prospecting | $1.50-4.00/record | After phone/mail exhausted |

Note: Bulk data typically skips SMS (lower urgency does not justify texting costs) and goes straight to power dialer calls.

## Preset Map

**Folder Name**: `01. Bulk Sequential Marketing`

| # | Preset Name | Purpose | Action |
|---|---|---|---|
| 00 | Bulk Needs Skipped | New bulk records without phone data | Run batch skip trace |
| 01 | Bulk Skipped NN | Skip traced but no numbers | Second skip trace attempt or manual research |
| 02 | Bulk Ready to Call | Records with numbers, zero call attempts | Load into multi-line power dialer |
| 03 | Bulk Call Follow Up | Follow-up calls (1-6 attempts) | Continue power dialer sessions |
| 04 | Bulk Needs 1st Mail | Completed calling (6+ attempts), no contact | Export mail-ready CSV, send postcard/letter |
| 05 | Bulk Mail Monthly | Uncontacted after first mail | Monthly mail piece (long-term nurture) |
| 06 | Bulk Not Interested | Declined during contact | Quarterly re-engagement (every 90 days) |
| 07 | Exhausted CC -> DP | All phone numbers wrong/dead | Route to Deep Prospecting |
| 08 | Bulk Return Mail -> DP | Mail returned (bad address) | Route to Deep Prospecting |

## Record Flow Diagram

```
New Bulk Record (dataflik / stacked niche tag)
    |
    v
[00. Bulk Needs Skipped] -- skip trace --> phone found?
    |                                         |
    | (no phone)                              | (has phone)
    v                                         v
[01. Bulk Skipped NN]                 [02. Bulk Ready to Call]
  (re-skip or discard)                        |
                                              v (power dialer sessions)
                                       [03. Bulk Call Follow Up]
                                         (1-6 attempts)
                                              |
                              +---------------+---------------+
                              |               |               |
                         (no answer)    (interested)    (not interested)
                              |               |               |
                              v               v               v
                    [04. Bulk Needs    [Lead Pipeline]  [06. Bulk Not
                     1st Mail]                          Interested]
                              |                               |
                              v                               v (90 days)
                    [05. Bulk Mail Monthly]              Re-engage
                      (monthly pieces)
                              |
                    +---------+---------+
                    |                   |
               (mail returned)    (no response)
                    |                   |
                    v                   v
            [08. Bulk Return    [07. Exhausted CC
             Mail -> DP]         -> DP]
```

## Key Differences from Niche Sequential

| Aspect | Niche (12 presets) | Bulk (9 presets) |
|---|---|---|
| SMS stage | Yes (preset 01) | No -- skip SMS, go straight to calls |
| Call structure | 3 separate day presets (02-04) | 1 follow-up range preset (03) |
| Call attempts before mail | 3 (one per day) | 6 (power dialer volume) |
| Hot Lead preset | Yes (08) | No -- leads go to pipeline directly |
| Callback preset | Yes (07) | No -- managed in dialer |
| Bad Data preset | Yes (10) | No -- handled by Exhausted CC (07) |
| Completed Cycle preset | Yes (11) | No -- records stay in mail nurture |

## Customization Points

When designing for a specific user, adjust these elements:

1. **Lists**: Replace "bulk lists" with their actual DataSift lists
2. **Tags**: Confirm their bulk data tag (default: `dataflik`; stacked niche data uses `stacked niche`)
3. **Call attempt range**: 1-6 is standard for preset 03; some users prefer 1-8 for higher volume
4. **Mail transition threshold**: 7+ attempts is default; adjust based on call attempt range
5. **Power dialer**: Confirm which dialer they use (e.g., Mojo, PhoneBurner, BatchDialer)
6. **Team assignment**: Solo vs. round-robin across multiple callers

## Consultative Workflow

### Step 1: Discovery
Ask about their bulk data sources, dialer setup, team structure, tags, and call attempt thresholds.

### Step 2: Design
Start from this base template and customize for their lists, tags, and attempt ranges.

### Step 3: Present
Deliver the customized preset map for review. If rejected, ask which specific presets need changes -- do not restart from scratch.

### Step 4: Implementation
Build each preset in DataSift in order (00 through 08), configuring filter blocks per `references/filter-configurations.md`.
