# Niche Sequential Preset Map (Base Template)

This document provides the standard preset structure for a **Niche Sequential** marketing workflow targeting first-to-market courthouse data. Use this as the starting point for building a user's customized plan.

## What is Niche Sequential?

Niche Sequential marketing targets **first-to-market / Tier 1 data** such as probates, pre-foreclosures, tax sales, code violations, evictions, and divorces. These records are high-urgency and require manual click-to-dial calling (not power dialer).

### Key Characteristics

| Aspect | Setting |
|---|---|
| Data Type | First-to-market / Tier 1 (courthouse-sourced) |
| Calling Method | Manual click-to-dial |
| Urgency | High |
| Primary Tag | `Courthouse Data` |
| Preset Count | 12 presets (00-11) |
| Folder Name | `00 Niche Sequential Marketing` |
| Cycle Structure | 3-day cycle: Text -> Call x3 -> Mail -> Deep Prospecting |

### Cost Pendulum (per touch)

| Channel | Cost | Stage |
|---|---|---|
| SMS | ~$0.01/message | Day 1 (first touch) |
| Cold Call | ~$0.03-0.06/attempt | Days 1-3 |
| Direct Mail | $0.50-2.00/piece | After calling exhausted |
| Deep Prospecting | $1.50-4.00/record | After mail exhausted/returned |

## 3-Day Cycle Flow

The niche sequential workflow follows a tight 3-day cadence:

```
Day 1: Text (SMS) -> Call (Script A) -> Trigger mailer
Day 2: Call (Script B) -> Text variation
Day 3: Final call (urgency script) -> Final text -> Mailer arrives
```

Records that complete the cycle without contact move to mail, then deep prospecting.

## Preset Map

**Folder Name**: `00 Niche Sequential Marketing`

| # | Preset Name | Purpose | Action |
|---|---|---|---|
| 00 | Needs Skip Traced | New records without phone data | Run skip trace (Tracerfy batch -> Trestle phone scoring) |
| 01 | Ready to Text | Has phone (Dial First/Second tier), not yet texted | Send Day 1 SMS via Launch Control / REISimpli |
| 02 | Needs Called Day 1 | Texted, not called yet | First call attempt, leave voicemail, log disposition |
| 03 | Needs Called Day 2 | Called once, no answer | Second attempt with different script |
| 04 | Needs Called Day 3 | Called twice, final attempt | Urgency voicemail, final text |
| 05 | Needs Mailed | Exhausted 3-day call cycle | Export mail-ready CSV, send handwritten letter ($1.75) |
| 06 | Needs Deep Prospecting | Mail returned / no response after full cycle | Route to deep prospecting (Level 1-3 research) |
| 07 | Callback Scheduled | Appointment set during a call | Call at scheduled time, update disposition |
| 08 | Hot Lead | Expressed interest during contact | Immediate closer assignment, schedule appointment |
| 09 | Not Interested | Declined | Tag for 90-day follow-up, rotate to different mailer |
| 10 | Bad Data | Wrong number/address | Remove bad data, re-run skip trace |
| 11 | Completed Cycle | Full 3-day cycle done, no interest | Move to nurture list, schedule monthly touch |

## Record Flow Diagram

```
New Record (Courthouse Data tag)
    |
    v
[00. Needs Skip Traced] -- skip trace --> phone found?
    |                                        |
    | (no phone)                             | (has phone)
    v                                        v
  Re-skip or manual research         [01. Ready to Text]
                                        |
                                        v (SMS sent)
                                  [02. Needs Called Day 1]
                                        |
                                        v (no answer)
                                  [03. Needs Called Day 2]
                                        |
                                        v (no answer)
                                  [04. Needs Called Day 3]
                                        |
                        +---------------+---------------+
                        |               |               |
                   (no answer)    (interested)    (not interested)
                        |               |               |
                        v               v               v
               [05. Needs Mailed] [08. Hot Lead] [09. Not Interested]
                        |                               |
                        v (no response)                 v (90 days)
               [06. Needs Deep Prospecting]        Re-engage
                        |
                        v (cycle complete)
               [11. Completed Cycle] --> nurture list

Side tracks:
  - Any call -> callback set -> [07. Callback Scheduled]
  - Any stage -> wrong number -> [10. Bad Data] -> re-skip
```

## Customization Points

When designing for a specific user, adjust these elements:

1. **Lists**: Replace "first-to-market lists" with their actual DataSift lists (e.g., "Foreclosure", "Probate", "Tax Sale")
2. **Tags**: Confirm their primary data tag (default: `Courthouse Data`)
3. **Call attempts**: 3 days is standard; some users prefer 4-5
4. **Mail timing**: After calls (standard) or concurrent with calls
5. **Deep prospecting trigger**: After mail returned, after X mail pieces with no response, or both
6. **Team assignment**: Solo operator vs. round-robin across callers

## Consultative Workflow

### Step 1: Discovery
Ask about their niches, marketing channels, team structure, data tags, and call attempt cadence.

### Step 2: Design
Start from this base template and customize for their lists, tags, and attempt thresholds.

### Step 3: Present
Deliver the customized preset map for review. If rejected, ask which specific presets need changes -- do not restart from scratch.

### Step 4: Implementation
Build each preset in DataSift in order (00 through 11), configuring filter blocks per `references/filter-configurations.md`.
