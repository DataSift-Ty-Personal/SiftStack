# SOP — Direct Mail Roll-out Plan

**Audience:** Aaron (decision-maker on spend), Mike (executor of the day-to-day flow).
**Purpose:** Phased plan to roll out direct mail cadences without burning $5K+/month on bad data. Mail is expensive — get the data right before you press the button.

---

## The economic reality

| Metric | Number |
|---|---|
| Mail cost per piece (handwritten, personalized) | ~$1.75 |
| Daily SiftStack record volume (steady state) | ~150 records |
| Daily mail spend if 100% mailability | ~$262/day |
| Monthly mail spend if 100% | ~$5,250/month |
| **Realistic mail spend at 70% mailability + once-per-record** | **~$3,500/month** |
| Industry response rate on cold direct mail | 0.5%–2% |
| Required deals to break even (assume $20K avg margin) | ~1 deal every 6 months at $5K spend |

**Translation:** Mail is profitable IF the data is clean AND the mailing matches the right segment. **Don't blast every record.** Mail is the most expensive channel — make it the most selective.

---

## The 4-week ramp plan

### Week 1 — Verify autonomous daily flow (NO MAIL)

**Today through next Friday.**

Goal: confirm the daily 6 AM autonomous run produces clean data for 7 consecutive days.

Activities:
- Mike works leads via SMS + phone, NOT mail
- Aaron monitors data quality via Friday Weekly Review
- Verify the 5 mailability filters (below) on every record's data

**Mailability filters to validate:**

| Filter | Field | Expected value | If wrong: |
|---|---|---|---|
| **Address validated** | `dpv_match_code` | `Y` (USPS confirmed) | Drop from mail; Smarty rejected |
| **Mailable flag** | `mailable` | `yes` | Pipeline computed; if `""`, drop |
| **First/last name parsed** | `Owner First Name`, `Owner Last Name` | non-empty | "Dear LONG, WILLIAM" looks like spam — drop |
| **Mailing address present** | `Mailing Street Address` | non-empty | Mail goes to executor's mailing addr (not property) |
| **Decision maker verified living** | `decision_maker_status` | `verified_living` | Don't mail dead people; Tracerfy + obituary cross-check |

**Decision point at end of Week 1:**
- If <80% of records pass all 5 filters → fix the gap before mailing (Week 2 task)
- If 80%+ pass → green light Week 2 mail prep

### Week 2 — Fix gaps + soft-launch infrastructure (NO MAIL YET)

Goal: Hit 95%+ mailability on the daily batch.

Likely fixes needed (we already know):
1. **Name parser** — yesterday's run had 58/144 records (40%) with unparsed first/last names. Phase 11 task.
2. **Tax Sale records** — 40 records/week with no preset assignment. Decide: build `FTM_TaxSale_*` presets OR filter at scraper level.
3. **Lis Pendens scrapers** — Mike has empty `FTM_LP_*` presets waiting for data. Phase 9 candidate.
4. **Direct mail vendor selection** — pick one (see "Vendor decision matrix" below).

Infrastructure tasks:
- Set up direct mail vendor account
- Design the mail piece (handwritten yellow letter is the standard for distress mail)
- Set up the mail-trigger sequence in DataSift (likely "Day 5+ no SMS response → flag mailable → auto-route to mail vendor")
- Hire the caller (per Aaron's plan) — onboard them on SOPs

### Week 3 — Soft-launch (50 pieces, 1 segment)

Goal: Validate response rate on a small batch before scaling spend.

**Pick:** Montgomery County probate (highest volume + highest motivation segment).

**Filter:**
- Tag: `ftm-probate` AND `montgomery`
- DPV match: Y
- Mailable: yes
- First/Last name: parsed
- Tier (phone): 1, 2, or 3 (don't mail Tier 4-5 until you trust the data)
- Status: New OR Contacted (not Interested+ — those need calls, not mail)

**Cadence:**
- 1 piece per record
- Handwritten yellow letter, addressed to Decision Maker (executor)
- Reference the decedent's name in the body
- Include Aaron's number + Wright Home Offer brand

**Spend:** 50 × $1.75 = **$87**

**Track:**
- Calls received from mail (have a tracking number on the piece)
- Letters returned undeliverable (DPV failure rate)
- Conversion to "Interested" status within 21 days
- Any negative responses (don't mail again)

### Week 4 — Scale the proven segment (200-300 pieces, 1-2 segments)

If Week 3 metrics:
- **Response rate >0.5%:** Scale Montgomery probate to all daily records, add Franklin probate
- **Response rate 0.25%-0.5%:** Stay at Montgomery probate, refine the message
- **Response rate <0.25%:** Stop mailing, debug the mail piece + targeting

Scale to:
- All probate records (Mont + Franklin) — ~30/week
- All Tier 1+2 foreclosure records — ~80/week
- Mail spend: ~$200/week = $800/month

### Week 5+ — Full ramp (continuous)

Steady-state mail program:
- All FTM_* preset matches that pass mailability filters
- ~150 pieces/week × $1.75 = ~$1,050/week = **~$4,200/month**
- Coordinate with Mike's SMS + caller's phone work — mail is the 3rd touch in the niche sequential flow
- Adjust messaging by segment (probate vs foreclosure vs tax sale)

---

## Vendor decision

**Use OpenLetter** (DataSift's native integrated mail house). It's the right choice for SiftStack from Day 1, not a future migration step:

- **Native DataSift integration** — sequences trigger mail directly from records; no CSV export/upload friction
- **Variable substitution from DataSift custom fields** — Sheriff Sale Held Date, Confirmation Hearing Date, Decedent Name, etc. populate automatically
- **Tracking flows back into DataSift activity** — response calls, returned mail, opt-outs all log against the record
- **Cadence automation** — Day-N triggers fire from the sequence, no manual reupload
- **A/B variants** — supported within a single cadence for testing message variants

The earlier plan to start with YellowLetters.com (CSV upload) and migrate later was inferior — OpenLetter is already inside Sift, no migration needed. **All mail templates and cadences live in [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md).**

(For reference only — the alternatives we considered before OpenLetter became the chosen path:)

| Vendor | Cost/piece | Min order | Handwritten? | API integration | Best for |
|---|---|---|---|---|---|
| **OpenLetter (chosen)** | per DataSift contract | per contract | Yes (varies by template) | Native DataSift integration | All SiftStack volume |
| YellowLetters.com | $1.65-1.95 | 50 | Yes (real ink) | CSV upload only | (deferred) |
| Click2Mail | $0.85-1.15 | 1 | No (printed) | Full REST API | (deferred — high volume) |

---

## Mail templates and cadences

All mail templates and per-distress cadences (probate, foreclosure lis pendens, foreclosure sheriff sale, redemption window) live in **[SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md)**.

That doc is the source of truth for:
- Per-distress cadence design (which piece fires on which day)
- Full template body text (copy-paste into OpenLetter)
- DataSift custom-field → OpenLetter variable mapping
- Stop-mail triggers (status changes, returned mail, opt-outs)
- A/B variant testing patterns

This doc (SOP-DIRECT-MAIL-PLAN) stays focused on economics: spend ramp, mailability filters, ROI math, response-rate targets, vendor decision.

> **Active model (2026-04-29):** Entry-mail-first via global `Mail_On_Entry` sequence. Every mailable record gets ONE first-touch mail piece on Day 0 (captures first-to-market window — Tyler/REISift teaching). Preset cadences handle Day 30 / 60 / 90+ follow-ups on records that don't convert. See [MIKE-MAIL-ENTRY-SETUP.md](MIKE-MAIL-ENTRY-SETUP.md) for build instructions. Spend math below revised to fit this model.

### Revised spend math (entry-mail-first model)

At steady state (~Day 7+ post-dedup):
- Entry mail: ~50 NEW records/day × 22 weekdays × $1.75 = **~$1,925/month**
- Preset cadence follow-up (Day 30 / 60 / 120 mail on non-converted records): ~300–500 pieces/month × $1.50 = **~$450–750/month**
- **Total: ~$2,375–2,675/month** — fits the $3k Phase 1 budget

Trade-off vs the original "mail only top-tier records" model: same total spend, BUT every mailable record gets a first-to-market touch automatically. Higher response rate from Day 0 timing offsets not-targeting-only-top-tier. Sub-tier presets remain a Phase 2 option if data later shows clear motivation gradient worth segmenting on.

---

## Tracking & ROI math

For every batch of mail sent, track in a Google Sheet:

| Date | Segment | Pieces | Spend | Calls received | Conversations | Offers made | Closed | Cost/Closed |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

**The real number that matters:** Cost per closed deal.

If $87 of mail (Week 3) generates 1 closed deal at $20K margin → **ROI = 230x**. That's the unit economics.
If $87 of mail generates 0 closed deals over 90 days → bad targeting or bad message; reduce or pause.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Mailing dead people | DOD sanity check (3-yr window) + obituary verification before mail; require `decision_maker_status = verified_living` |
| Mailing wrong address | Smarty DPV match = Y required; reject S/N matches |
| Mailing too soon (annoying recipients) | 14-day delay between SMS sequences and first mail piece; 30-day delay between mail pieces to same record |
| Spending too much before validating | Hard cap at $1,000 mail spend until first deal closes; review weekly |
| Negative response (recipient angry) | Tag `do_not_mail` permanently; immediate respect; respond personally if they call/email |

---

## Stop-mail criteria (when to pause)

Pause the mail program if any of these hit:

- **3+ DPV failures** in a single batch — data quality issue, fix before continuing
- **Response rate <0.25% over 200 pieces** — message or targeting is wrong
- **>1% negative response** ("stop mailing me") — segmentation is too aggressive or list is contaminated
- **Tracerfy/Trestle credits exhausted** — fix enrichment first; mail without verified addresses is wasted spend
- **Mike or caller can't keep up with inbound** — mail-driven calls require capacity

---

## See also

- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — daily flow context
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — qualification gating BEFORE mail
- [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md) — weekly review picks up mail metrics
- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — script for inbound calls from mail responders
- [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md) — redemption-window mail piece (Phase 2 only; FedEx 2-day for `redemption_closing` records, NOT standard mail)
