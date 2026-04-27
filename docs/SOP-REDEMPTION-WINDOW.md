# SOP — Redemption Window: Mike's Build Guide

**For Mike.** Add 3 new DataSift presets to capture the redemption-window niche. The autonomous SiftStack pipeline already auto-tags records during the daily 7am run — your job is to build the 3 county presets that surface them.

---

## What you already have (audit, confirmed from the daily Apify run)

Your existing FTM presets (numbered, in `00. Niche Sequential` folder):

| # | Preset | What flows in |
|---|---|---|
| 1 | `FTM_LP_Mont` | Montgomery lis pendens (foreclosure case filings, weeks–months pre-sale) |
| 2 | `FTM_LP_Franklin` | Franklin lis pendens |
| 3 | `FTM_LP_Greene` | Greene lis pendens |
| 4 | `FTM_SS_Mont` | Montgomery sheriff sale (auction listings) |
| 5 | `FTM_SS_Franklin` | Franklin sheriff sale |
| 6 | `FTM_SS_Greene` | Greene sheriff sale |
| 7 | `FTM_Probate_Mont` | Montgomery probate filings |
| 8 | `FTM_Probate_Franklin` | Franklin probate filings |
| 9 | `FTM_Probate_Greene` | Greene probate filings |

These all work today — the autonomous run drops records into them every morning.

---

## What's missing — 3 redemption presets to add

Add these following the same numbered convention:

| # | Preset name | County |
|---|---|---|
| **10** | `FTM_RW_Mont` | Montgomery |
| **11** | `FTM_RW_Franklin` | Franklin |
| **12** | `FTM_RW_Greene` | Greene |

These capture properties in the **post-auction redemption window** (the 7–30 day period between sheriff sale and court confirmation, where the homeowner can still legally redeem under ORC §2329.33).

---

## Filter logic (same for all 3 presets — change only the county tag)

**Required tags** (record must have ALL):
- `ftm-rw`
- `redemption_open`
- County tag — `montgomery` for #10, `franklin` for #11, `greene` for #12

**Excluded tags** (record must NOT have ANY):
- `Sold`
- `do_not_mail`
- `bankruptcy_stay`
- `redemption_closed`

**Status:** `New`, `Contacted`, OR `Interested`

**Sort:** `Redemption Days Remaining` (custom field) ASCENDING

The sort is the key trick — records with `redemption_closing` tag (≤14 days to confirmation) automatically rise to the top of each county's list. So you don't need separate "panic list" presets. Each morning, the top 3–5 records in each county preset are the urgent ones; everything below is normal-pace cadence.

---

## What auto-flows in (so you can verify it's working)

When the daily run finishes, redemption-window records carry these tags + custom fields:

**Tags applied automatically:**
- `ftm-rw` — the routing tag for these presets
- `redemption_open` — sale held, hearing not yet imminent
- `redemption_closing` — hearing within 14 days (also keeps `redemption_open` as backstop)
- `redemption_closed` — confirmation entered (record drops out of preset automatically)
- `franklin` / `montgomery` / `greene` — county
- `ftm-ss` — base sheriff sale tag (still present; same record may also show in FTM_SS_*)
- `Courthouse Data`, `ftm`, `foreclosure`

**Custom fields populated by the pipeline:**
- `Sheriff Sale Held Date` — auction date
- `Confirmation Hearing Date` — when court confirms (window closes)
- `Redemption Window Status` — "open" / "closing" / "closed"
- `Redemption Days Remaining` — countdown integer

If you open a record in the FTM_RW preset and don't see these fields, ping Aaron — pipeline isn't pushing them through correctly.

---

## How records flow from scrape into your boards

```
6:00 AM    Apify scrapes Franklin/Montgomery/Greene foreclosure auctions
            (35-day lookback — captures past 5 weeks of sale days)
6:30 AM    Redemption watcher checks Common Pleas dockets per case
            ├─ "RETURN OF SALE FILED"   → tag redemption_open
            ├─ "CONFIRMATION HEARING"   → tag redemption_closing if ≤14 days
            └─ "ENTRY CONFIRMING SALE"  → tag redemption_closed (retires from preset)
7:00 AM    Records uploaded to DataSift with all redemption tags + custom fields
9:00 AM    Mike opens DataSift → FTM_RW presets show records sorted urgent-first
```

### How records from PRIOR daily runs flow into the new presets

The autonomous pipeline handles existing records automatically — no manual re-import needed. Once the redemption code deploys, here's what happens to records already in DataSift from prior runs:

1. **The 35-day RealAuction lookback** re-scrapes past sheriff sales each morning, including ones we already had records for from yesterday and today's runs (Montgomery 70 records on Apr 26, Franklin/Montgomery 166 records on Apr 27)
2. **Persistent case state** carries every active foreclosure record across runs (file: `output/foreclosure_case_state.json`)
3. **Watcher runs against the merged set** — both today's fresh scrape AND records carried over from prior runs
4. **DataSift upload deduplicates by property address / parcel_id** — when SiftStack re-uploads a record we've seen before, DataSift MERGES the new tags onto the existing record rather than creating a duplicate
5. **The same record now appears in BOTH its original FTM preset AND the new FTM_RW preset** — `ftm-ss` tag stays so it remains visible in `FTM_SS_*`; the new `ftm-rw` + `redemption_open` tags surface it in `FTM_RW_*`

**Practical result:** within 1–2 daily runs of the redemption code deploying, every Montgomery + Franklin foreclosure record that's still in its redemption window (sale held, hearing pending) will simultaneously appear in:
- Their original sheriff-sale preset (`FTM_SS_Mont` or `FTM_SS_Franklin`) — still visible in standard cadence
- The new redemption preset (`FTM_RW_Mont` or `FTM_RW_Franklin`) — surfaced for the urgent 14-day cadence

No data migration step needed. No manual re-tagging. Mike just builds the 3 new presets and waits for the next morning's run to populate them.

---

## Sequence to wire (one sequence, attached to all 3 county presets)

Build a sequence named `FTM_RW_Cadence`. Day 0 = day record enters the preset.

| Day | Channel | Action |
|---|---|---|
| 0 | DialForce | Bulk dial (already running automatically — no setup needed) |
| 0 | SMS | Redemption Day-1 SMS |
| 0 | ISA call | Live attempt on Trestle Tier 0–3 phones |
| 1 | OpenLetter | RW-1 mail piece. **Use FedEx 2-day if record has `redemption_closing` tag**, otherwise standard mail |
| 2 | SMS | Day-2 follow-up |
| 3 | ISA call | Second attempt |
| 5 | SMS + ISA call | Day-5 narrowing message + third attempt |
| 7 | ISA call | Fourth attempt |
| 10 | SMS + ISA call | Urgency escalation + fifth attempt |
| 14 | SMS + ISA call | Final SMS + last attempt before confirmation |

**SMS templates:** see [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) (redemption section).
**Mail body:** see [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md) §"Redemption window cadence" — RW-1 template.

**Stop conditions** (apply to entire sequence): status changes to `Sold` / `Closed` / `Dead` / `Under Contract` / `Offer` / `Appointment` OR tag `do_not_mail` / `bankruptcy_stay` / `co_owner_blocked` / `redemption_closed` applied.

---

## Build checklist for Mike (estimated 30 min total)

- [ ] Build preset #10 `FTM_RW_Mont` (filter logic above, county tag `montgomery`)
- [ ] Build preset #11 `FTM_RW_Franklin` (county tag `franklin`)
- [ ] Build preset #12 `FTM_RW_Greene` (county tag `greene`)
- [ ] Build sequence `FTM_RW_Cadence` per the table above
- [ ] Attach `FTM_RW_Cadence` to all 3 presets
- [ ] Verify: open any one of the 3 presets — should be empty for now (pipeline code not deployed yet). Once Aaron deploys the redemption code, records will start landing within 1–2 daily runs.
- [ ] When records appear, spot-check one: confirm tags include `ftm-rw` + `redemption_open` and custom fields `Sheriff Sale Held Date` + `Confirmation Hearing Date` are populated.

---

## Daily workflow once live

Morning sequence after FTM_RW goes live:

1. **First** — open `FTM_RW_Mont`, `FTM_RW_Franklin`, `FTM_RW_Greene` (in any order)
2. Records sorted by `Redemption Days Remaining` ASC — work top-down
3. Anything ≤7 days remaining is **drop-everything urgent** — these are records about to lose all their rights at the confirmation hearing
4. Then proceed to your normal FTM_Probate / FTM_SS / FTM_LP rotation

The redemption window is the highest-conversion niche — short timeline, maximum motivation, minimum competition. Treat the morning FTM_RW pass as Priority #1 every day.
