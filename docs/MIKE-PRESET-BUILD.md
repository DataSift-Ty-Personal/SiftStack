# Mike — Preset Build & Adjust Sheet

**Time required:** ~30 minutes total.
**When to do this:** Today. New records will start flowing into preset #10/11/12 tomorrow morning at 9 AM.

---

## Part 1 — BUILD 3 new presets (redemption window)

Add these to the `00. Niche Sequential` folder.

### #10 — `FTM_RW_Mont`

**Filter:**
- Tag IS `ftm-rw`
- Tag IS `redemption_open`
- Tag IS `montgomery`
- Tag IS NOT `Sold`
- Tag IS NOT `do_not_mail`
- Tag IS NOT `bankruptcy_stay`
- Tag IS NOT `redemption_closed`
- Status IN: New, Contacted, Interested

**Sort:** `Redemption Days Remaining` ASC (most urgent first)

### #11 — `FTM_RW_Franklin`

Same filter as #10 — change `montgomery` to `franklin`.

### #12 — `FTM_RW_Greene`

Same filter as #10 — change `montgomery` to `greene`.

---

## Part 2 — ADJUST 6 existing presets (add one exclusion to each)

Once a foreclosure record enters the redemption window, it should NOT also keep showing up in the sheriff sale or lis pendens preset. Add these exclusions so records show up in exactly one place.

### Adjust the 3 sheriff-sale presets

For each of these:
- **#4 `FTM_SS_Mont`**
- **#5 `FTM_SS_Franklin`**
- **#6 `FTM_SS_Greene`**

Add ONE filter rule:
- Tag IS NOT `ftm-rw`

That's it for SS adjustments. Records in the redemption window will now flow exclusively to the FTM_RW preset for that county.

### Adjust the 3 lis pendens presets

For each of these:
- **#1 `FTM_LP_Mont`**
- **#2 `FTM_LP_Franklin`**
- **#3 `FTM_LP_Greene`**

Add ONE filter rule:
- Tag IS NOT `ftm-rw`

Same logic — records past the auction shouldn't keep appearing in the lis pendens nurture cadence.

---

## Part 3 — Build 1 sequence (`FTM_RW_Cadence`)

Attach this sequence to all 3 new presets (#10, #11, #12). Day 0 = day record enters preset.

| Day | What fires | Channel |
|---|---|---|
| 0 | Day-1 redemption SMS | Mike sends |
| 0 | Live ISA dial (Trestle Tier 0–3 phones) | ISA |
| 1 | Mail piece RW-1 — **FedEx 2-day if record has `redemption_closing` tag, otherwise standard mail** | OpenLetter |
| 2 | Day-2 SMS | Mike |
| 3 | ISA dial #2 | ISA |
| 5 | Day-5 SMS + ISA dial #3 | Mike + ISA |
| 7 | ISA dial #4 | ISA |
| 10 | Day-10 SMS + ISA dial #5 | Mike + ISA |
| 14 | Final SMS + ISA escalation | Mike + ISA |

**Stop conditions (entire sequence halts):** status changes to Sold / Closed / Dead / Under Contract / Offer / Appointment, OR tag `do_not_mail` / `bankruptcy_stay` / `co_owner_blocked` / `redemption_closed` is applied.

**Templates:** SMS scripts in [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md). Mail body (RW-1) in [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md).

---

## Verification (after build, ~30 min later)

- [ ] All 3 new presets exist and show 0 records (correct — they populate tomorrow)
- [ ] Open one record in `FTM_SS_Franklin` → verify it does NOT have `ftm-rw` tag (otherwise the filter won't exclude it)
- [ ] `FTM_RW_Cadence` sequence is attached to all 3 new presets

## What you'll see tomorrow morning (~9 AM)

The first daily run with the new code will:
1. Re-scrape all foreclosure auctions from the past 35 days (catches the same records you've already worked + any new ones)
2. Check Common Pleas dockets for each — applies redemption tags
3. Records where the sheriff sale already happened will show up in the new `FTM_RW_*` presets
4. Same records will NO LONGER appear in `FTM_SS_*` (because of the exclusion you just added)

If the FTM_RW presets are empty after tomorrow's 9 AM run, ping Aaron — that means either the watcher didn't find any active redemption windows, or there's a tagging issue.

---

## Quick reference: tag flow

| Tag on a record | Lives in which preset |
|---|---|
| `ftm-lp` (no `ftm-ss`, no `ftm-rw`) | `FTM_LP_*` (nurture cadence) |
| `ftm-lp` + `ftm-ss` (no `ftm-rw`) | `FTM_LP_*` AND `FTM_SS_*` — both cadences fire (intentional — long nurture + auction urgency) |
| `ftm-ss` (no `ftm-rw`) | `FTM_SS_*` (auction-stage cadence) |
| `ftm-ss` + `ftm-rw` + `redemption_open` | `FTM_RW_*` only (excluded from SS by the new filter rule) |
| `ftm-ss` + `ftm-rw` + `redemption_closed` | None — record retires from active cadence |

That's the whole picture. 30 min to build + adjust, then it runs autonomously every morning.
