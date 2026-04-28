# Mike's Master Guide to SiftStack

**This is the one doc Mike needs to understand the whole system.** Read it once end-to-end, then keep it open during the morning workflow as a reference.

It answers: What is SiftStack doing every night? Why do some records have phones and others don't? Why are some "deceased" with heir data and others aren't? What do I check first thing in the morning? What signals trouble?

---

## 1. The 30-second elevator pitch

Every weekday morning at 7:00 AM, a cloud robot called SiftStack wakes up and does this:

1. **Scrapes 9 Ohio public-records websites** — probate court, foreclosure auction, lis pendens (Common Pleas) for Franklin, Montgomery, and Greene counties
2. **Cleans and enriches each record** — verifies addresses, looks up property values, finds obituaries for deceased owners, identifies heirs and decision-makers, pulls phone numbers, scores each phone for dial priority
3. **Uploads everything to DataSift** — pre-tagged so it lands directly in your filter presets, ready for you to work
4. **Posts a summary in Slack** — usually around 8:30–9:00 AM

By the time you open DataSift at 9, the records are already sorted into the right preset buckets, tagged with priority signals, and ready to dial / SMS / mail. **Your job is to work them, not pull them.**

---

## 2. The full pipeline (what happens between 7 AM and 9 AM)

```
7:00 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 1 — Scrape 9 OH portals                                │
         │ Franklin/Montgomery/Greene × probate/foreclosure/lis pendens│
         │ + Montgomery + Greene RealAuction (foreclosure backstop)   │
         │ Output: ~300-400 raw records                                │
         └────────────────────────────────────────────────────────────┘
                                ↓
7:25 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 2 — Dedup against memory of prior runs                 │
         │ "Have we seen this case before? Yes → carry forward         │
         │  enrichment we already did. No → flag as NEW."              │
         │ Output: ~30-50 NEW records, ~300 carried forward            │
         └────────────────────────────────────────────────────────────┘
                                ↓
7:30 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 3 — Redemption-window watch (foreclosure only)         │
         │ For each foreclosure case: check the Common Pleas court     │
         │ docket. Has a sheriff sale happened? Confirmation hearing   │
         │ scheduled? Already confirmed? Apply the right tag.          │
         └────────────────────────────────────────────────────────────┘
                                ↓
7:35 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 4 — Enrichment (only on NEW records)                   │
         │   • Smarty: USPS-validate the address, get ZIP+4, geocode   │
         │   • Zillow: pull property value, MLS status, sqft, bed/bath │
         │   • County Auditor: find probate decedent's property        │
         │   • Obituary search: is the owner deceased? When did they   │
         │     die? Who are the heirs? (PROBATE-relevant only)         │
         │   • Entity research: if owner is an LLC/Corp, find the      │
         │     person behind it                                         │
         │   • Filter out: vacant land, business owners, commercial    │
         │     properties, dead numbers                                 │
         └────────────────────────────────────────────────────────────┘
                                ↓
8:15 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 5 — Skip trace + phone scoring                         │
         │   • Tracerfy: pull phones + emails for new records          │
         │   • Trestle: score each phone Tier 0-5 (priority for dialing)│
         │ Cached records skip both — phones already known.            │
         └────────────────────────────────────────────────────────────┘
                                ↓
8:25 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 6 — Build DataSift CSV with all tags + custom fields   │
         │ Upload to DataSift via Playwright automation                 │
         │ DataSift's built-in skip trace also runs (free unlimited     │
         │ plan) and adds any phones we missed                          │
         └────────────────────────────────────────────────────────────┘
                                ↓
8:45 AM  ┌────────────────────────────────────────────────────────────┐
         │ STEP 7 — Slack daily report                                 │
         │ "Today: X new records, Y carried forward, breakdown by      │
         │  county/type, redemption windows opening, spend report."    │
         └────────────────────────────────────────────────────────────┘
                                ↓
9:00 AM  ─── MIKE OPENS DATASIFT ───
```

---

## 3. Why some records are "deep prospected" and others aren't

You'll notice some records arrive with full heir info, executor name, decision-maker confidence score, and a PDF report. Others just have basic owner + address + phone. This is **by design**, not a bug:

| Distress type | Has decision-maker analysis? | Why |
|---|---|---|
| **Probate** | YES | The property owner is dead. We need to find the executor / heir who can actually sign a contract. Without DM analysis, you'd be sending mail to a corpse. |
| **Foreclosure (sheriff sale)** | NO | The owner is the homeowner being foreclosed on — they're alive. They sign their own contract. No heir analysis needed. |
| **Lis pendens** | NO | Same reason — the defendant is alive. |
| **Tax sale** | NO | Same — the owner is alive. |
| **Redemption window** | NO | Foreclosure stage — homeowner still alive, still the decision-maker. |

So when you open a foreclosure record and see no DM/heir/executor data — that's correct. The contact is `Owner First/Last Name`. When you open a probate record, you should see the executor's name in `Decision Maker`, with a confidence rating, and ideally their address in `Mailing Address` (different from the property's address).

**Important note (current state, late April 2026):** The probate property-lookup step has a known bug that's preventing some probate records from coming through with addresses. Aaron is fixing this. Until then, you may see the probate count in the daily Slack lower than expected. Don't sweat it — when fixed, probate volume will jump.

---

## 4. Tag glossary (everything you'll see on a record)

Every record arrives with multiple tags. Some are ROUTING tags (drive which preset they appear in). Others are SIGNAL tags (give you info at a glance).

### Routing tags (drive preset filtering)

| Tag | Meaning | Goes into preset |
|---|---|---|
| `ftm-probate` | Probate court filing | `FTM_Probate_*` |
| `ftm-ss` | Sheriff sale (foreclosure auction stage) | `FTM_SS_*` |
| `ftm-lp` | Lis pendens (foreclosure case filing, pre-auction) | `FTM_LP_*` |
| `ftm-rw` | Redemption window (post-auction, ORC §2329.33 still allows redemption) | `FTM_RW_*` |
| `ftm-ts` | Tax sale | (no preset yet — orphaned today) |
| `ftm` | First-to-market generic — every courthouse-direct record gets this |
| `Courthouse Data` | Always applied — confirms the record came from public records, not a list broker |

### Stage / status signal tags

| Tag | Meaning |
|---|---|
| `redemption_open` | Sheriff sale happened, court hasn't confirmed yet — the seller still has rights |
| `redemption_closing` | Confirmation hearing within 14 days — URGENT, work this first |
| `redemption_closed` | Court confirmed the sale — window is gone, record retires from FTM_RW |
| `deceased` | Owner confirmed dead (probate flag) |
| `living` | Owner alive (default for foreclosure / LP / tax sale) |
| `has_auction` | Auction date is in the future |
| `has_dm_address` | Decision maker's mailing address found (probate) |
| `dm_verified` | We confirmed the named DM is alive |
| `tax_delinquent` | Property has unpaid taxes |
| `franklin` / `montgomery` / `greene` | County tag |
| `YYYY-MM` | Month tag (when record entered) |

### Quality / phone tier tags

| Tag | Meaning | What it tells you |
|---|---|---|
| `Dial First` | Tier 0–1 phone, score 81-100 | Highest connect probability — call immediately |
| `Dial Second` | Tier 2 phone, score 61-80 | Strong number, prioritize |
| `Dial Third` | Tier 3 phone, score 41-60 | Medium quality, dial after Tier 1-2 work |
| `Dial Fourth` | Tier 4 phone, score 21-40 | Low quality, mail-only worthwhile |
| `Drop` | Tier 5 phone, score 0-20 | Litigator-flagged or junk — don't dial |
| `skip_traced_YYYY-MM` | When the record's phones were last skip-traced |

### Operational / control tags

| Tag | Meaning | Action |
|---|---|---|
| `do_not_mail` | Recipient asked to stop | Mail program halts; calls only |
| `bankruptcy_stay` | Active Ch 7/13 stay | All outreach halts |
| `co_owner_blocked` | A co-owner refuses to participate | Deal is dead; tag and recycle |
| `mail_returned_1` / `_2` / `_3+` | Mail came back undeliverable | After 2: pause sequence; 3+: do_not_mail |

---

## 5. Custom fields glossary (the columns you'll see in DataSift)

In addition to tags, records carry these custom fields:

| Field | Source | Example |
|---|---|---|
| `Notice Type` | Pipeline | "foreclosure" / "probate" / "lis_pendens" / "tax_sale" |
| `County` | Pipeline | "Franklin" / "Montgomery" / "Greene" |
| `Date Added` | Pipeline | When SiftStack first scraped this record |
| `Decedent Name` | Probate scraper | The deceased owner (probate only) |
| `Date of Death` | Obituary | YYYY-MM-DD from confirmed obit |
| `Owner Deceased` | Pipeline | "yes" or blank |
| `Decision Maker` | Heir analysis | Top-ranked heir/executor (probate only) |
| `DM Relationship` | Heir analysis | "executor" / "spouse" / "son" / "daughter" |
| `DM Confidence` | Heir analysis | "high" / "medium" / "low" |
| `DM 2/3 Name` + Relationship | Heir analysis | Backup decision-makers |
| `Foreclosure Date` | Scraper | Auction date |
| `Sheriff Sale Held Date` | Redemption watcher | When the auction actually happened |
| `Confirmation Hearing Date` | Redemption watcher | When the court confirms (window closes) |
| `Redemption Window Status` | Redemption watcher | "open" / "closing" / "closed" |
| `Redemption Days Remaining` | Redemption watcher | Countdown integer |
| `Tax Auction Date` | Tax scraper | Tax sale date |
| `Estimated Value` | Zillow | Zestimate |
| `Equity Percentage` | Zillow | (Zestimate - mortgage) / Zestimate |
| `Parcel ID` | County auditor | Stable identifier |
| `Source URL` | Scraper | Direct link to the public record (audit trail) |
| `Obituary URL` | Obit search | Audit trail for deceased confirmation |

---

## 6. Mike's morning workflow (the daily checklist)

Open DataSift at 9:00 AM. Work the presets in this exact priority order:

### Priority 1 — `FTM_RW_*` Redemption windows (URGENT)

Records here are racing a 7-30 day legal deadline. If you don't reach them before the confirmation hearing, the deal is gone forever.

**Open in this order:**
1. `FTM_RW_Mont` (#10)
2. `FTM_RW_Franklin` (#11)
3. `FTM_RW_Greene` (#12)

Records sorted by `Redemption Days Remaining` ASC. Top 3-5 records in each preset are the urgent ones — work those first. SMS Day-1 + ISA call within an hour of arrival. If `Redemption Days Remaining ≤ 7`, drop everything else.

### Priority 2 — `FTM_Probate_*` (highest converting niche)

These have full DM/heir info and mailing addresses. Probate is the segment with the best mail response rate.

1. `FTM_Probate_Mont` (#7)
2. `FTM_Probate_Franklin` (#8)
3. `FTM_Probate_Greene` (#9)

Sort by `Date Added` DESC (freshest first — strike while motivation is highest).

### Priority 3 — `FTM_SS_*` Sheriff sale (auction approaching)

1. `FTM_SS_Mont` (#4)
2. `FTM_SS_Franklin` (#5)
3. `FTM_SS_Greene` (#6)

Sort by `Foreclosure Date` ASC — auctions closest to today come first.

### Priority 4 — `FTM_LP_*` Lis pendens (long nurture)

1. `FTM_LP_Mont` (#1)
2. `FTM_LP_Franklin` (#2)
3. `FTM_LP_Greene` (#3)

Sort by `Date Added` DESC. These have the longest runway — work last, slower cadence.

---

## 7. What to check on each record (validation pass — do this on first 5 records in each preset)

Before you start dialing/SMS'ing, do a quick spot-check on 5 records per preset to make sure the data flow is healthy.

For each record, open it and verify:

- [ ] **Tags include the routing tag** (`ftm-rw` for redemption, `ftm-ss` for sheriff sale, etc.)
- [ ] **County tag matches** (`montgomery` / `franklin` / `greene`)
- [ ] **Property address is populated** (Property Street, City, ZIP)
- [ ] **At least one phone is populated** (Phone 1-9)
- [ ] **For probate records:** Decision Maker name + Relationship are filled, Decedent Name is filled, Mailing Address is the DM's (NOT the property's)
- [ ] **For redemption records:** Sheriff Sale Held Date + Confirmation Hearing Date are populated, Redemption Days Remaining shows a number
- [ ] **Phone tier tag exists** (Dial First / Second / Third / Fourth — drives your priority)

If 4+ out of 5 spot-checked records pass, the data is healthy → start working. If fewer pass, ping Aaron — something in the pipeline broke.

---

## 8. Working a record (the cadence)

Each preset has an automated cadence (the FTM_RW_Cadence sequence for redemption, similar sequences for the others). DataSift fires SMS, schedules calls, and triggers OpenLetter mail automatically as records progress through the days.

**Your job:**
- Send the SMS templates from [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) when the sequence flags Day 1 / Day 2 / Day 3 SMS due
- Take inbound calls from records who respond — qualify per [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) (the 4 Pillars: Timeline, Pain, Capability, Decision-maker)
- Tag `hot` and ping Aaron in Slack for any 4/4 qualified lead OR any redemption-window lead with positive engagement
- Tag `Sold` / `Dead` / `not_interested` when a record's status is final (this stops the cadence and prevents Mike-and-mailer from re-touching them)

You do NOT manually dial cold calls — that's DialForce's job. You handle SMS replies + qualification + warm transfers.

---

## 9. What signals trouble (escalate to Aaron)

| Signal | What it means | What to do |
|---|---|---|
| No Slack daily report by 10:00 AM | Pipeline failed before completing | Slack Aaron — "Daily run didn't fire" |
| `FTM_RW_*` empty for 2+ days during foreclosure season | Redemption watcher is broken | Slack Aaron — "FTM_RW empty for X days" |
| Spot-check fails on 3+ of 5 records (missing tags/fields) | Data flow broken upstream | Slack Aaron with the broken record's case number |
| Probate records have no Decision Maker or Mailing Address | Probate property lookup or obit search broken | Slack Aaron — "Probate records missing DM" |
| Tier 0/1 phones are 0% in today's Slack | Phone scoring or skip trace broken | Slack Aaron — "Phone tiers look wrong" |
| You're seeing the same record twice in different presets | Tag exclusion rules broken | Slack Aaron with both preset names |
| OpenLetter mail bounced 3+ times for one record | Bad mailing address | Tag `do_not_mail` permanently |

---

## 10. FAQ

**Q: Why does today's Slack say "366 scraped, 360 NEW" when many of these are records I've seen before?**

A: Today (April 28) was the very first run with the new dedup system. The state file was empty so every record looked "new." Tomorrow (April 29) the dedup will actually kick in — expect "30-50 NEW, 300+ carried forward." That's the savings: cached records skip Smarty/Zillow/obituary/Tracerfy because the pipeline already enriched them last time.

**Q: Why are some records in two presets at once?**

A: Intentionally. A foreclosure case might be in both `FTM_LP_*` (because the case was filed) AND `FTM_SS_*` (because the auction is now scheduled). Long-runway nurture from LP runs in parallel with auction-urgency cadence from SS. Once the sale happens and redemption tags appear, the new preset filter rules route the record EXCLUSIVELY to `FTM_RW_*`.

**Q: Why doesn't every record have phones?**

A: Most do. If Tracerfy didn't find phones, DataSift's built-in skip trace runs after upload and may find them. If neither does, the owner has a stale or unlisted number — flag those as `mail-only` and don't waste call time.

**Q: What if I tag a record `Sold` and then the cadence still fires?**

A: It shouldn't. The sequences have stop conditions on status changes. If a sequence fires after `Sold`, that's a bug — note the case number and ping Aaron.

**Q: Can I add my own tags?**

A: Yes. Common Mike-applied tags: `callback_scheduled`, `info_sent`, `not_interested`, `low_motivation`, `qualified`, `hot`. These trigger or pause sequences.

**Q: Why are some records marked deceased but no DM info?**

A: Probate property lookup is currently broken (asyncio bug). When fixed, all confirmed-deceased records will have DM data. Until then, treat any deceased-but-no-DM record as "needs Aaron's attention" — there's a real estate but we don't have the contact.

---

## 11. Cheat sheet — daily 9 AM open-DataSift sequence

1. Open Slack — read the daily SiftStack report. Note: total records, breakdown, anything in red flags
2. Open DataSift → `FTM_RW_Closing` records (if any) → work URGENT first
3. `FTM_RW_Open` → urgent but slightly slower
4. `FTM_Probate_Mont` → `FTM_Probate_Franklin` → `FTM_Probate_Greene`
5. `FTM_SS_Mont` → `FTM_SS_Franklin` → `FTM_SS_Greene`
6. `FTM_LP_Mont` → `FTM_LP_Franklin` → `FTM_LP_Greene`
7. End of day: clear out any Status changes (Sold/Dead/Interested) that came in during the day so cadences halt correctly

---

## See also

- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — exact SMS templates and phone scripts for each scenario
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — the 4-Pillars qualification framework + when to escalate
- [SOP-REDEMPTION-WINDOW.md](docs/SOP-REDEMPTION-WINDOW.md) — deeper context on the redemption niche
- [SOP-MAIL-TEMPLATES.md](docs/SOP-MAIL-TEMPLATES.md) — full mail piece library (probate, foreclosure, redemption)
- [MIKE-PRESET-BUILD.md](MIKE-PRESET-BUILD.md) — the build sheet (presets 10-12, adjustments to 1-6)
