# Mike — Mail On Entry Sequence Setup

**Time required:** ~1 hour. Foreclosure templates Aaron is writing himself (placeholder OK to start).
**When to do this:** Today (or whenever Aaron's foreclosure mail copy is ready).

This sheet sets up automated entry mail — every new SiftStack record gets ONE mail piece on the day it lands in DataSift, regardless of which preset it goes into. Captures the first-to-market window before any nurture cadence kicks in.

---

## What this does + why

When a new record uploads to DataSift each morning, it lands in a preset (FTM_Probate, FTM_SS, FTM_LP, FTM_RW) and that preset's sequence fires Day 0 SMS + ISA call etc. **The new piece you're adding fires a single mail piece on Day 0 too**, before any other cadence does anything.

This works in PARALLEL to the existing preset sequences. Once the entry mail fires, the existing preset cadences pick up the Day 30 / 60 / 90+ follow-up mail pieces normally. No duplicate sends — the entry mail tags the record so other cadences skip the Day 1 mail step.

Why mail on entry: industry data + Tyler's teaching. Records contacted within 7 days of public filing convert at 2–3x the rate of records first touched 14+ days later. SiftStack already gets us mail-ready within 24 hours of the filing — this captures that window automatically.

---

## What you're building (one new sequence)

**Sequence name:** `Mail_On_Entry`
**Folder:** create or use existing "DIRECT MAIL" folder
**Linked to:** ALL preset records (no preset attachment needed — it triggers on tag conditions, not preset membership)

### Trigger conditions (record must satisfy ALL)

- Tag IS `Courthouse Data`
- `mailable` = `yes` (custom field — the pipeline computes this)
- `dpv_match_code` = `Y` (custom field — Smarty USPS confirmation)
- Tag IS NOT `mail_entry_sent`
- Tag IS NOT `do_not_mail`
- Tag IS NOT `bankruptcy_stay`
- Status IN: `New`, `Contacted`

### Action — branch on Notice Type

The `Notice Type` custom field tells the sequence which template to fire. Set up 4 conditional branches:

| If `Notice Type` is | Send template | Format |
|---|---|---|
| `probate` | **Probate-1** (already in OpenLetter — handwritten yellow letter) | Yellow letter, standard mail |
| `foreclosure` AND tag `ftm-rw` is present | **RW-1** | Standard letter, FedEx 2-day if tag `redemption_closing` also present |
| `foreclosure` AND tag `ftm-rw` is NOT present AND tag `ftm-ss` is present | **SS-1** (Aaron's writing this one) | Yellow letter, standard mail |
| `lis_pendens` | **LP-1** (Aaron's writing this one) | Yellow letter, standard mail |
| `tax_sale` | (skip for now — orphan niche, no template yet) | n/a |

### After the mail fires

Apply two tags to the record (action steps in the sequence):

1. `mail_entry_sent` — prevents this sequence from re-firing AND prevents the preset cadence from sending its Day 1 mail (avoids duplicate)
2. `mail_entry_<YYYY-MM-DD>` (e.g. `mail_entry_2026-04-29`) — audit trail of when the entry mail went

---

## Adjustments to existing cadences (the duplicate-prevention step)

The existing `FTM_Probate_Cadence`, `FTM_SS_Cadence`, `FTM_LP_Cadence`, `FTM_RW_Cadence` sequences have a Day 0 / Day 1 mail step that needs to be GATED:

For each of those 4 cadence sequences, add this condition to the Day 1 mail step:

> Only fire if record does NOT have tag `mail_entry_sent`

This way:
- Records that hit `Mail_On_Entry` get their Day 1 mail from THERE
- Records that somehow miss `Mail_On_Entry` (mailable=no, missing DPV, etc.) still get their Day 1 mail from the preset cadence as backup
- No duplicate sends ever

---

## Templates needed in OpenLetter

These are template bodies you'll paste into OpenLetter. Bodies live in [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md). **Aaron is writing the foreclosure SS-1 and LP-1 templates himself — use placeholders for now, swap when he sends final copy.**

Required templates:

| Template | Status | Notes |
|---|---|---|
| `Probate-1` | ✅ READY (in SOP-MAIL-TEMPLATES.md) | Aaron's already mapped this — paste into OpenLetter as-is |
| `RW-1` | ✅ READY (in SOP-MAIL-TEMPLATES.md) | Redemption-window piece, two shipping triggers (standard vs FedEx 2-day) |
| `SS-1` | ⏳ PENDING — Aaron writing | Foreclosure auction-stage piece |
| `LP-1` | ⏳ PENDING — Aaron writing | Lis pendens early-acknowledgment piece |

You can build the `Mail_On_Entry` sequence with placeholder body text for SS-1 and LP-1 right now, then swap the bodies when Aaron sends the final copy. The branching logic + tag application is what matters — the words can update.

---

## Build checklist (~1 hour)

- [ ] Confirm `Probate-1` and `RW-1` templates already exist in OpenLetter (load from SOP-MAIL-TEMPLATES.md if missing)
- [ ] Add placeholder `SS-1` and `LP-1` templates (body to be filled by Aaron)
- [ ] Build new sequence `Mail_On_Entry` with 4 conditional branches per the Notice Type table above
- [ ] Add 2 tag-apply actions after each mail send (`mail_entry_sent` + dated audit tag)
- [ ] Save + activate the sequence
- [ ] Adjust existing cadence sequences (`FTM_Probate_Cadence`, `FTM_SS_Cadence`, `FTM_LP_Cadence`, `FTM_RW_Cadence`) — add `NOT mail_entry_sent` condition to their Day 1 mail step
- [ ] Verify on a test record: upload one fresh Courthouse Data record, watch sequence fire, confirm mail_entry_sent tag applies, confirm preset cadence's Day 1 mail does NOT also fire on that record
- [ ] Update Aaron when SS-1 and LP-1 templates need to be swapped from placeholder to final

---

## What you'll see after this is live

### In DataSift activity log per record:

```
2026-05-01 07:35  Record uploaded by SiftStack with tags: Courthouse Data, ftm, ftm-probate, montgomery, ...
2026-05-01 07:36  Mail_On_Entry sequence triggered → Probate-1 sent via OpenLetter
2026-05-01 07:36  Tags applied: mail_entry_sent, mail_entry_2026-05-01
2026-05-01 09:15  FTM_Probate_Mont sequence Day 0 — SMS triggered (mail step skipped: mail_entry_sent present)
2026-05-01 09:30  ISA call fired
2026-05-31 07:00  FTM_Probate_Mont sequence Day 30 — Probate-2 mail fires (cadence resumes)
```

### In Slack daily report:

The next-day Slack should show:
```
Mail On Entry sent today: 47 records
  Probate: 12, Foreclosure (SS): 18, LP: 14, Redemption: 3
```

(That's a future enhancement Aaron may add to the Slack notifier — for now the count is visible in OpenLetter's send log.)

---

## Why no new presets needed

The original idea was sub-tiering presets (FTM_Probate_Tier1_Mont etc.) to differentiate mail spend by motivation. Aaron's instinct — and the Tyler/REISift teaching it aligns with — is that the highest-leverage mail moment is the FIRST touch. By mailing every mailable record on entry automatically, we capture that window without complicating the preset structure.

Sub-tiering presets remains a Phase 2 option if response data later shows a clear motivation gradient worth segmenting on. For now: one entry mail to all, then existing preset cadences handle the follow-up.

---

## Cost forecast

At steady state (Day 7+ after dedup hits):
- ~50 NEW records/day × 22 weekdays = ~1,100/month entry mail
- × $1.75 handwritten yellow letter = ~$1,925/month
- + Existing preset cadence pieces (~$500–800/month for Day 30 / 60 / 90 touches on records that haven't converted)
- **Total ~$2,400–2,800/month** — within the $3k Phase 1 budget

If response rate hits 2–4% target, expect 22–44 conversations/month from entry mail alone, before counting existing SMS / ISA / DialForce contributions.

---

## See also

- [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md) — exact template bodies (Probate-1, RW-1 ready; SS-1 + LP-1 pending Aaron)
- [SOP-CADENCES.md](SOP-CADENCES.md) — preset cadence specs (Day 30 / 60 / 90 follow-up structure)
- [MIKE-MASTER-GUIDE.md](MIKE-MASTER-GUIDE.md) — full operational reference
