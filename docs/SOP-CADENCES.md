# SOP — Cadences & DataSift Preset Specifications

**Audience:** Mike (preset + sequence builder), ISA, Aaron.
**Purpose:** Single reference for **what fires on what day across what channel** for each distress type. Mike uses this doc to build the DataSift filter presets and sequences. ISA uses it to know what's already happened on a record before they call.

This is the operational glue between:
- **What scrapers tag** ([SOP-TAG-FLOW.md](SOP-TAG-FLOW.md))
- **What gets sent** ([SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md), [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md))
- **What it costs** ([SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md))

---

## How DataSift sequences work (refresher)

A **preset** = filter logic. Records matching the filter appear in the preset's record list.

A **sequence** = automated cadence triggered when a record enters the preset. Each sequence step has:
- **Day offset** (Day 0 = entry into preset, Day N = N days later)
- **Action** (Send SMS / Send Mail via OpenLetter / Schedule Call / Add Tag / etc.)
- **Condition** (only fire if record still matches X — e.g. status still New)

Sequences fire automatically. Mike doesn't manually push them — he builds them once and the system fires touches per record per the timeline.

### The two layers (read this once, then refer back)

There are TWO sequence layers running in parallel for every record:

1. **`Mail_On_Entry`** (global, fires Day 0) — sends ONE first-touch mail piece to every new mailable record, regardless of which preset they're in. Branches on `Notice Type` to pick Probate-1 / SS-1 / LP-1 / RW-1. Tags `mail_entry_sent` after firing. See [MIKE-MAIL-ENTRY-SETUP.md](MIKE-MAIL-ENTRY-SETUP.md) for the build.

2. **Preset cadences** (per-preset: `FTM_Probate_Cadence`, `FTM_SS_Cadence`, `FTM_LP_Cadence`, `FTM_RW_Cadence`) — the existing sequences that handle Day 1 SMS, Day 1/2/3 ISA call, Day 30/60/90 follow-up mail, etc.

**The Day 1 mail step in EACH preset cadence is gated** — it only fires if the record does NOT have `mail_entry_sent` tag. This prevents duplicate sends. If a record hits `Mail_On_Entry`, the preset's Day 1 mail step skips. If a record somehow misses `Mail_On_Entry` (mailable=no, missing DPV, etc.), the preset cadence fires the Day 1 mail as a backup.

Bottom line: every mailable record gets exactly ONE Day 0 mail piece, then the preset cadence picks up at Day 30 onwards.

---

## Universal stop-cadence rules (apply to ALL sequences)

These conditions halt remaining cadence steps for a record:

- Status = `Sold` / `Closed` / `Dead` / `Under Contract` / `Offer` / `Appointment`
- Tag `do_not_mail` applied
- Tag `bankruptcy_stay` applied
- Tag `co_owner_blocked` applied
- 3+ returned-mail count

Build these as cadence-wide filter exclusions, not per-step. DataSift typically supports a "sequence-level exit condition" — set it to: status in [Sold, Closed, Dead, Under Contract, Offer, Appointment] OR tag in [do_not_mail, bankruptcy_stay, co_owner_blocked] OR returned_mail_count >= 3.

---

## 1. Probate cadence — `FTM_Probate_OH_<county>`

**Highest converting niche per industry data + lowest urgency. Build first.**

### Preset filter

Folder: `00 Niche Sequential Marketing`
Build 3 presets (one per county):

| Preset name | County filter |
|---|---|
| `FTM_Probate_Franklin` | County = Franklin |
| `FTM_Probate_Montgomery` | County = Montgomery |
| `FTM_Probate_Greene` | County = Greene |

**Common filter logic** (applies to all three):
- Tag: `ftm-probate` (REQUIRED)
- Tag: `Courthouse Data` (REQUIRED — narrows to direct courthouse-sourced records)
- Status: `New` OR `Contacted`
- Tag: NOT `Sold`
- Tag: NOT `do_not_mail`
- Custom field: `Owner Deceased` = "yes"
- Custom field: `DM 1 Status` = "verified_living" (don't run cadence on records where we couldn't verify the executor is alive)
- Custom field: DPV match = `Y` (mailable)

**Sort:** `Date Added` DESC (freshest first).

**Expected daily volume:** 5-15 records/day across 3 counties combined. Spikes when probate court has busy filing weeks.

### Sequence — `FTM_Probate_Cadence`

| Day | Channel | Action | Stop if |
|---|---|---|---|
| 0 | DialForce | Bulk dial (already happens — no preset action needed) | n/a |
| 0 | SMS | Send Probate Day-1 SMS via Mike's tool | status changed |
| 0 | OpenLetter (`Mail_On_Entry`) | Probate-1 yellow letter — fires from global Mail_On_Entry sequence; tags `mail_entry_sent` | mailable, DPV=Y |
| 1 | ISA | Schedule call (Trestle Tier 0–3 phones only) | status changed |
| 2 | SMS | Send Probate Day-2 SMS | status changed |
| 3 | OpenLetter (preset cadence) | Send Probate-1 mail — BACKUP only, fires only if record does NOT have `mail_entry_sent` (catches records that bypassed Mail_On_Entry) | status changed AND NOT mail_entry_sent |
| 3 | ISA | Second call attempt | status changed |
| 5 | SMS | Send Probate Day-3 SMS final | status changed |
| 7 | ISA | Third call attempt | status changed |
| 14 | ISA | Status check call — if no engagement, mark `low_motivation` | n/a |
| 30 | OpenLetter | Send Probate-2 mail | status not New|Contacted |
| 30 | ISA | Re-engagement call | n/a |
| 60 | OpenLetter | Send Probate-3 mail (final direct) | status not New|Contacted |
| 90 | SMS | Send "still interested?" SMS | status not New|Contacted |
| 120 | OpenLetter | Send Probate-4 nurture mail | status not New|Contacted |
| 210 | OpenLetter | Send Probate-4 nurture mail (every 90 days) | status not New|Contacted |
| 300 | OpenLetter | Send Probate-4 nurture mail | status not New|Contacted |

**Total touches over 6 months:** 14 (4 mail, 4 SMS, 6 calls).
**Steady-state mail spend per probate record:** 4 pieces × $1.75 ≈ $7 (then $1.75/quarter for nurture).

---

## 2. Lis Pendens cadence — `FTM_LP_OH_<county>`

For records caught at the Common Pleas foreclosure case-filing stage. Long runway (4-12 weeks until sheriff sale), so cadence stays measured but progressively urgent.

### Preset filter

| Preset name | County filter |
|---|---|
| `FTM_LP_Franklin` | County = Franklin |
| `FTM_LP_Montgomery` | County = Montgomery |
| `FTM_LP_Greene` | County = Greene |

**Common filter:**
- Tag: `ftm-lp` (REQUIRED)
- Tag: `Courthouse Data`
- Status: `New` OR `Contacted`
- Tag: NOT `Sold`
- Tag: NOT `do_not_mail`
- DPV match = `Y`

**Sort:** `Date Added` DESC.

**Expected daily volume:** 3-10 records/day across 3 counties (more after Franklin court busy days).

### Sequence — `FTM_LP_Cadence`

| Day | Channel | Action | Stop if |
|---|---|---|---|
| 0 | DialForce | Bulk dial | n/a |
| 0 | SMS | Foreclosure Day-1 SMS (per [SOP-CALL-SCRIPTS](SOP-CALL-SCRIPTS.md)) | status changed |
| 0 | OpenLetter (`Mail_On_Entry`) | LP-1 yellow letter — fires from global Mail_On_Entry sequence; tags `mail_entry_sent` | mailable, DPV=Y |
| 1 | OpenLetter (preset cadence) | LP-1 BACKUP only, fires only if record does NOT have `mail_entry_sent` | NOT mail_entry_sent |
| 1 | ISA | Live call attempt | status changed |
| 2 | SMS | Foreclosure Day-2 SMS | status changed |
| 3 | SMS | Foreclosure Day-3 SMS | status changed |
| 5 | ISA | Second call attempt | status changed |
| 14 | ISA | Re-engagement call | status not New|Contacted |
| 30 | OpenLetter | Send LP-2 mail | status not New|Contacted |
| 30 | ISA | Call attempt | status not New|Contacted |
| 60 | OpenLetter | Send LP-3 mail | status not New|Contacted |
| 60 | SMS | "Auction approaching" SMS | status not New|Contacted |
| 90 | OpenLetter | Send LP-4 mail (final pre-auction) | status not New|Contacted |

**Stage transition:** when this record's auction date appears (RealAuction scraper picks up the sheriff sale listing), it gets the `ftm-ss` tag. The LP cadence keeps running, but the SS cadence ALSO fires from Day 0 of the SS preset entry. Records can be in both presets simultaneously — that's intentional, the urgency layered on top of the long-runway nurture.

**Total touches over 90 days:** ~12 (4 mail, 4 SMS, 4 calls).

---

## 3. Sheriff Sale cadence — `FTM_SS_OH_<county>`

For records where the auction is scheduled. Cadence compresses to days, not weeks. Auction date is the deadline.

### Preset filter

| Preset name | County filter |
|---|---|
| `FTM_SS_Franklin` | County = Franklin |
| `FTM_SS_Montgomery` | County = Montgomery |
| `FTM_SS_Greene` | County = Greene |

**Common filter:**
- Tag: `ftm-ss` (REQUIRED)
- Tag: `Courthouse Data`
- Tag: NOT `redemption_open` AND NOT `redemption_closing` AND NOT `redemption_closed`
  (Once the sale has happened, records flow into FTM_RW_* presets. They leave the SS preset automatically.)
- Status: `New` OR `Contacted`
- Tag: NOT `Sold`
- Custom field: `Foreclosure Date` (auction date) is set AND ≥ today

**Sort:** `Foreclosure Date` ASC (closest auction first — most urgent on top).

**Expected daily volume:** 5-25 records/day depending on auction-week scheduling.

### Sequence — `FTM_SS_Cadence`

| Day from preset entry | Channel | Action | Stop if |
|---|---|---|---|
| 0 | DialForce | Bulk dial | n/a |
| 0 | SMS | Foreclosure Day-1 SMS | status changed |
| 0 | OpenLetter (`Mail_On_Entry`) | SS-1 yellow letter — fires from global Mail_On_Entry sequence; tags `mail_entry_sent` | mailable, DPV=Y |
| 1 | OpenLetter (preset cadence) | SS-1 BACKUP only, fires only if record does NOT have `mail_entry_sent` | NOT mail_entry_sent |
| 1 | ISA | Live call | status changed |
| 2 | SMS | Foreclosure Day-2 SMS | status changed |
| 3 | SMS | Foreclosure Day-3 SMS (urgency) | status changed |
| 5 | ISA | Second call | status changed |
| 7 | OpenLetter | Send SS-2 mail (concrete offer) | status not New|Contacted |
| 14 | ISA | Re-engagement | status not New|Contacted |

**Time-to-auction urgency triggers** (these fire based on `Foreclosure Date`, not preset entry date):
| Days to auction | Channel | Action |
|---|---|---|
| 14 | SMS | "2 weeks to auction" reminder |
| 7 | OpenLetter | Send SS-3 mail (final urgency) |
| 7 | ISA | Daily call attempt |
| 3 | SMS | Daily SMS until auction |
| 1 | SMS + ISA | Final SMS + final ISA call |

**Total touches per record (from preset entry to auction, 4-8 weeks typical):** 12-18 (3 mail, 6-10 SMS, 4-5 calls).

---

## 4. Redemption Window cadences — `FTM_RW_OH_*`

The crown jewel niche per [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md). Two presets — one for normal-urgency open windows, one for closing-fast (≤14 days) windows.

### Preset 1: `FTM_RW_OH_Redemption_Open`

**Filter:**
- Tag: `ftm-rw` (REQUIRED)
- Tag: `redemption_open` (REQUIRED)
- Tag: NOT `redemption_closing` (this preset is the >14-day-out group)
- Status: `New` OR `Contacted`
- Tag: NOT `Sold`
- Tag: NOT `do_not_mail`
- Tag: NOT `bankruptcy_stay`

**Sort:** `Redemption Days Remaining` ASC.

### Preset 2: `FTM_RW_OH_Redemption_Closing`

**Filter:**
- Tag: `ftm-rw` AND `redemption_closing` (REQUIRED — both)
- Status: `New` OR `Contacted` OR `Interested`
- Tag: NOT `Sold`
- Tag: NOT `bankruptcy_stay`

**Sort:** `Redemption Days Remaining` ASC.

**This preset is Mike's morning Priority #1.** Records here have ≤14 days until the court closes the window. This preset gets worked before any other preset every single morning.

### Sequence — `FTM_RW_Cadence` (fires for BOTH presets)

The cadence is identical across both presets — what differs is the mail vendor's shipping speed (standard mail for `Open`, FedEx 2-day for `Closing`). OpenLetter handles that branching internally based on the record's tag.

| Day from preset entry | Channel | Action | Notes |
|---|---|---|---|
| 0 | DialForce | Bulk dial | already happens |
| 0 | SMS | Redemption Day-1 SMS | per [SOP-REDEMPTION-WINDOW](SOP-REDEMPTION-WINDOW.md) |
| 0 | ISA | Live call attempt | Trestle Tier 0–3 phones |
| 0 | OpenLetter (`Mail_On_Entry`) | RW-1 — standard mail OR FedEx 2-day if `redemption_closing` tag present; tags `mail_entry_sent` | mailable, DPV=Y |
| 1 | OpenLetter (preset cadence) | RW-1 BACKUP only, fires only if record does NOT have `mail_entry_sent` | NOT mail_entry_sent |
| 2 | SMS | Redemption Day-2 SMS | reinforce legal right |
| 3 | ISA | Second call | |
| 5 | SMS | Redemption Day-5 SMS | window-narrowing message |
| 5 | ISA | Third call | |
| 7 | ISA | Fourth call | |
| 10 | SMS | Redemption Day-10 SMS | urgency escalation |
| 10 | ISA | Fifth call | |
| 14 | SMS + ISA | Final SMS + escalation call | last shot before confirmation |

**Door-knock day** (deferred pending Aaron + Travis alignment per [SOP-REDEMPTION-WINDOW](SOP-REDEMPTION-WINDOW.md)). When/if approved, slot in at Day 5–7 of the cadence.

**Time compression for `Closing` records:** if a record enters the FTM_RW_Closing preset with only 5 days until confirmation, the cadence above is too slow. Build a SECOND sequence variant `FTM_RW_Cadence_Compressed` that fires when `Redemption Days Remaining ≤ 7`:
- All touches collapsed into days available
- Mail goes FedEx Overnight (vs 2-day)
- ISA calls daily until window closes

DataSift may need a sequence-condition that auto-switches. If not, Mike adds a manual check each morning: any record in `Closing` preset with `Redemption Days Remaining ≤ 7` gets manually moved to compressed cadence.

**Total touches per record over 14 days:** ~12 (1-2 mail pieces, 5-6 SMS, 5-6 calls).

---

## Cadence interaction matrix

What happens when the same record qualifies for multiple presets:

| Stage progression | Preset(s) record sits in | Behavior |
|---|---|---|
| Lis pendens filed | `FTM_LP_*` | LP cadence runs |
| Auction listed (still pre-sale) | `FTM_LP_*` AND `FTM_SS_*` | BOTH cadences run in parallel — long nurture from LP + urgency from SS |
| Auction held (sale day passed, redemption open) | `FTM_RW_OH_Redemption_Open` ONLY (filter excludes redemption-tagged from FTM_SS) | RW cadence runs; SS cadence stopped automatically |
| Confirmation hearing scheduled ≤14 days | `FTM_RW_OH_Redemption_Closing` (in addition to Open via the dual-tag rule) | Both presets show the record; both cadences fire — same touches but DataSift dedups so each touch only fires once |
| Sale confirmed | NONE — `redemption_closed` tag retires from all presets | All cadences stop; record retires |
| Status flipped to Sold/Closed/Dead | NONE | All cadences stop universally |

---

## Pre-launch checklist (Mike, ~3 hours total)

Before the first daily run hits Mike's desk, build everything in this order:

### 1 — Filter presets (~45 min)
- [ ] `FTM_Probate_Franklin` / `_Montgomery` / `_Greene` (3 presets, ~5 min each)
- [ ] `FTM_LP_Franklin` / `_Montgomery` / `_Greene` (3 presets)
- [ ] `FTM_SS_Franklin` / `_Montgomery` / `_Greene` (3 presets)
- [ ] `FTM_RW_OH_Redemption_Open` (1 preset, statewide)
- [ ] `FTM_RW_OH_Redemption_Closing` (1 preset, statewide)

### 2 — OpenLetter mail templates (~2 hours)
Per [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md):
- [ ] Probate-1, Probate-2, Probate-3, Probate-4 (4 templates)
- [ ] LP-1, LP-2, LP-3, LP-4 (4 templates)
- [ ] SS-1, SS-2, SS-3 (3 templates)
- [ ] RW-1 (1 template, but 2 shipping triggers — standard vs FedEx)

### 3 — Sequence builds (~45 min total)
- [ ] **`Mail_On_Entry`** (global, not preset-attached) — builds per [MIKE-MAIL-ENTRY-SETUP.md](MIKE-MAIL-ENTRY-SETUP.md). Branches on `Notice Type` to send Probate-1 / SS-1 / LP-1 / RW-1. Tags `mail_entry_sent` after firing.
- [ ] `FTM_Probate_Cadence` (link to Probate preset) — Day 1 mail step gated on `NOT mail_entry_sent`
- [ ] `FTM_LP_Cadence` (link to LP preset) — Day 1 mail step gated on `NOT mail_entry_sent`
- [ ] `FTM_SS_Cadence` (link to SS preset) — Day 1 mail step gated on `NOT mail_entry_sent`
- [ ] `FTM_RW_Cadence` (link to BOTH RW presets) — Day 1 mail step gated on `NOT mail_entry_sent`
- [ ] `FTM_RW_Cadence_Compressed` (manual trigger fallback, see RW section above)

### 4 — SMS template setup (~15 min)
Load the SMS templates from [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) and [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md) into Launch Control / REISimpli (Mike's SMS tool). Reference the cadence Day-N triggers above.

### 5 — Verify
- [ ] Send a test record through each preset manually (Mike picks one record from each list, watches the cadence trigger Day 0 actions)
- [ ] Verify variable substitution works (does `{{first_name}}` populate, does `{{confirmation_date}}` show on RW pieces?)
- [ ] Verify stop-cadence: change a test record's status to `Sold`, confirm remaining sequence steps cancel

---

## Steady-state daily counts (estimates for capacity planning)

What Mike sees on a typical morning, 30 days into Phase 1 launch:

| Preset | Records | Mike's actions |
|---|---|---|
| `FTM_RW_Closing` | 0–5 | **PRIORITY #1** — work first, every morning |
| `FTM_RW_Open` | 5–15 | Priority #2 |
| `FTM_Probate_*` (3 counties combined) | 30–60 active | Priority #3 |
| `FTM_SS_*` (3 counties combined) | 50–150 active | Priority #4 |
| `FTM_LP_*` (3 counties combined) | 80–200 active | Priority #5 (nurture-paced) |

Mike's daily working capacity: ~30-50 active touch-points (calls + SMS replies + lead qualification). The "best 250 per county" lead-scoring layer (queued for build per [MASTER-PLAN.md](MASTER-PLAN.md)) will narrow this to a manageable ~35-process/day target matching the Master Planner goal.

---

## When something looks wrong

- **A preset is empty for >2 days when it should have records:** see [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) §13 (specifically for redemption window) and §4 (general tag drift).
- **A sequence is not firing on Day-0 entry:** check that the record actually entered the preset (manual filter test); if yes, check sequence is enabled + linked to the correct preset.
- **Variable substitution shows literal `{{first_name}}` in sent mail:** OpenLetter variable name mismatch — re-check against the variable reference in [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md).

---

## See also

- [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md) — full template body text + OpenLetter variable mapping
- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — SMS Day-1/2/3 + phone openers + voicemail + objection handlers
- [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md) — full operational guide for the redemption niche
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — 4-Pillars + escalation rules
- [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md) — what tag drives what preset
- [SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md) — economics + ROI math
- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — Mike's morning playbook (priority order matches the Steady-state table above)
