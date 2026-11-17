---
name: text-touch-builder
description: Generate a four-text-touch SMS sequence for your hottest DataSift records, personalized per record and varied like cold email so messages never look mass-blasted. Writes the four texts into DataSift custom fields (Text Touch 1-4) via CSV export and re-import, so callers can copy the next touch into their dialer right before calling. Use when the user wants SMS templates for ready-to-call records, a pre-call text strategy, drip texting for cold calling queues, or asks about "text touches".
---

# Text Touch Builder

You are an SMS-for-cold-calling strategist and implementor. You build a **four-text-touch sequence** for every record in a user's ready-to-call queue and store the four messages in DataSift custom fields, so the caller copies the next touch into their texting tool right before dialing.

## The Strategy (explain this to the user first)

Cold emailers rotate slightly different copy on every send because identical mass messages get flagged and ignored. This skill applies the same principle to SMS:

1. **Every record gets 4 short personalized texts** (Text Touch 1-4), drawn from pools of handwritten variants.
2. **Variant selection is seeded by the record itself**, so no two records send an identical 4-message sequence, and regenerating produces the same output (safe to re-run as the queue grows).
3. **The texts live in custom fields on the record.** The caller sends Touch 1 before call attempt 1, Touch 2 before attempt 2, and so on. Texting first, then calling, dramatically lifts answer rates because the number is no longer a stranger.

### The Four Touches (principles in `references/message-recipe.md`)

| Touch | Job | Never |
|-------|-----|-------|
| 1 | Identity check only: "is \<address\> yours?" Warm, positive | Pitch anything |
| 2 | The drip: "not sure my text went through" | Guilt or pressure |
| 3 | Soft ask: "ever thought about selling?" + offer a quick call | Price talk |
| 4 | Breakup: "did you decide to keep it instead?" | Negativity |

Every message: under ~160 characters (320 hard cap), no links, never mentions the distress list (never say foreclosure, probate, tax), signed with the **assigned caller's first name**, and aims at one goal: get them on the phone.

**It must read like a person wrote it, or it does not go out.** No em dashes or en dashes (the clearest tell that text was machine-written), no semicolons, no emoji, no ALL CAPS, and none of the giveaway phrases or AI vocabulary listed under "Sound human, or do not send it" in `references/message-recipe.md`. The script enforces this on every generated message **and** on the variant pools themselves, and refuses rather than warns. Audit the pools any time you edit them:

```bash
python scripts/build_text_touches.py --check-pools
```

## Workflow

### Step 1: Create the custom fields (one time, DataSift UI)

Settings -> Custom Fields -> add four **Text** fields to any group (Misc. works): `Text Touch 1`, `Text Touch 2`, `Text Touch 3`, `Text Touch 4`.

### Step 2: Export the target records

Open the filter preset for the queue (e.g. your "Ready to Call" preset) -> select all -> Export CSV. The export needs at least the property street address; owner first name, city, and Assigned To make the texts better.

### Step 3: Generate the touches

**Scripted (preferred when Python is available):**

```bash
python scripts/build_text_touches.py exported_records.csv --out text_touches_import.csv
python scripts/build_text_touches.py exported_records.csv --sender Maria   # fallback signer if no Assigned To column
```

The script auto-detects common DataSift export column names (override with `--col-street`, `--col-city`, `--col-first`, `--col-assigned`). It prints sample messages and writes an import-ready CSV: address identity columns + Text Touch 1-4.

**Manual (no Python):** read `references/message-recipe.md`, then generate the four touches yourself for each record following its rules exactly: pick variants so consecutive records differ, merge in first name + street address + city + caller name, keep every message under 160 characters, and output the same import CSV shape. Show the user 3 sample records for approval before doing the full list.

### Step 4: Review before upload

Always show the user samples (and offer the full CSV) BEFORE importing. Check: names render correctly (no "Hi E A!"), no message over 320 chars, sign-offs match the assigned callers, and **read three of them out loud** - if any sentence sounds like a brochure, rewrite the variant.

Records whose messages trip the human-voice check are skipped and listed in the run report rather than silently shipped.

### Step 5: Import back into DataSift

Upload File -> Add Data -> choose the **existing list** the records belong to (this upserts by address instead of duplicating) -> upload the generated CSV -> in the column-mapping step, drag `Text Touch 1-4` onto the matching custom fields (custom fields never auto-map) -> Finish Upload.

### Step 6: Operate it

- Caller opens the record, copies the next Text Touch into the dialer/texting tool, sends, then calls.
- Drip cadence: touches on separate days (e.g. Mon/Tue/Wed pattern), aligned with call attempts 1-4.
- When a seller replies, STOP the sequence and respond like a human. The three questions that matter: Do I have the right person? Have you considered selling? Do you have a price in mind? Then get them on the phone.
- Tag records that reply STOP or hostile as DNC immediately.

## Compliance guardrails (always mention)

Text from your own business number, one-to-one. Honor opt-outs instantly, keep quiet hours (8am-9pm local), and remind the user that texting laws (TCPA and state rules) apply to them; volume texting platforms have their own registration requirements. This skill personalizes copy; it does not make bulk texting legal where it otherwise is not.

## Customizing the pools

Encourage users to rewrite the variant pools in their own voice (edit the `TOUCH*` lists at the top of the script, or the tables in `references/message-recipe.md`). Structure and rules stay; wording should sound like the actual person sending. Seasonal or day-of-week openers (a Kind Investor signature move) work great when the user regenerates fields weekly.
