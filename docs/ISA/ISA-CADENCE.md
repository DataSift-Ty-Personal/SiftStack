# ISA Cadence — Dial-First, High-Velocity Outreach

**Audience:** Kareem.
**Purpose:** Day-by-day dial cadence per preset. Replaces the mail/SMS-heavy cadence in [SOP-OUTREACH-CADENCE.md](../SOP-OUTREACH-CADENCE.md), which remains Mike's playbook for the SMS channel. Kareem's channel is **phone first**.

---

## The shift from the old cadence

The previous cadence (designed for Mike, SMS-first) put the first phone attempt on **Day 4**. That's three days of cold-aging on a record that's been losing equity, time, and emotional bandwidth every hour. Kareem inverts that — **dial Day 1, dial Day 2, dial Day 3, dial Day 5, dial Day 7.** SMS becomes a **support channel** for his calls (sent before/after, not instead of).

Why dial-first works for distress ICP:
- A 90-second voice conversation builds more trust than 10 SMS exchanges
- Distress homeowners are often phone-fatigued from predators — a calm Ohio voice cuts through pattern-match
- Voice captures motivation category, urgency, ballpark, condition (UMBC) on the first connect; SMS doesn't
- TARP only works on voice — it cannot be set or matched over text

---

## Velocity targets (calibrated to Kareem's schedule)

Targets ramp over four weeks. Dials, not connects. Connect rate target: 25-35% (industry-typical for distress).

| Week | Dials/day | Connects/day | Handoffs to Danielle/day |
|---|---|---|---|
| 1 (training) | 50-60 | 12-18 | 0-1 |
| 2 | 70-80 | 18-25 | 1-2 |
| 3 | 90-100 | 22-30 | 2-3 |
| 4+ (cruise) | 100-120 | 25-40 | 2-4 |

**Dial budget by day length:**

| Day | Active hours | Dial-block hours | Cruise dial target |
|---|---|---|---|
| Mon (12-6) | 6 | ~4 | 60-75 |
| Tue / Wed / Thu (9-6) | 9 | ~6 | 100-120 |
| Fri (9-5) | 8 | ~5.5 | 90-100 |
| Sat (1st + 3rd, 9-12) | 3 | ~2 | 30-40 |

**Aaron can override these once he sees Week 1 data.** Targets are a starting point, not a contract.

---

## Universal cadence framework

Every distress lead Kareem works gets this base cadence unless they STOP, convert, or hard-decline:

```
Day 0   Record lands in DataSift overnight (autonomous)
Day 1   DIAL #1 + VM if missed + same-day SMS (Mike's day-1 SMS already fires; Kareem's call confirms it)
Day 2   DIAL #2 (different time of day from Day 1 — morning/afternoon swap)
Day 3   DIAL #3 + tested VM variant
Day 5   DIAL #4 (only Tier 1-2 phones at this point)
Day 7   DIAL #5 + nurture SMS
Day 14  DIAL #6 + check whether mail piece #1 landed
Day 30  DIAL #7 (status check)
Day 60  DIAL #8 + check mail piece #2
Day 90  Recycle through the cadence (only for status=Not-Yet, never for Sold/DNC)
```

**Cadence overrides per preset:** see each preset section below.

---

## When to STOP the cadence permanently

Stop dialing **forever** for any of these:

| Trigger | Tag in DataSift | Action |
|---|---|---|
| Reply STOP via SMS / verbal "do not call" | `do_not_contact` | Never dial again. TCPA compliance. |
| Status: Sold (someone else bought it) | `Sold` | DataSift Sold Property Cleanup sequence fires automatically |
| Status: Closed (we closed the deal) | `Closed` | Move to post-close workflow |
| Hard decline + concrete reason ("son's moving in," "wife inherited") | `not_interested` + reason in Notes | Recycle in 90 days only if reason changes |
| Bad number confirmed (3 dials, all "wrong number") | `bad_number` | Drop. Note for SiftStack pipeline review. |
| Returned mail (Mike's flag) + 3 dial misses | `bad_address` | Drop. |

---

## Distress-state-driven recycle rules

These override the standard cadence. Kareem tags the dominant distress state during the connect; recycle timing follows the state.

| Distress state | Recycle in | Reasoning |
|---|---|---|
| #1.1 Shame | 14 days | They need a moment to process the call. Coming back in 2 weeks lets the shame thaw. |
| #1.2 Denial ("bank's working with me") | 30 days | The bank conversation usually stalls within 30 days. Re-approach when reality has caught up. |
| #1.3 Learned helplessness | 21 days | Energy returns slowly. Don't push, but don't disappear. |
| #1.4 Suspicion-of-vultures | 7 days (with SMS first) | Trust thaws faster if Kareem is consistent + transparent. Send a warm SMS Day 5, dial Day 7. |
| #1.5 Decision fatigue | 14 days | Wait for cognitive bandwidth to return. |
| #1.6 Grief (probate) | 30-60 days | Patience compounds. The probate runway is on our side. |
| #1.7 Fear of judgment | 14 days | Re-approach with explicit confidentiality reframe. |
| #1.8 Magical thinking ("brother's lending me money") | 30 days | The Hail Mary almost always doesn't pan out. 30 days = enough time for reality to set in. |

**Tag the distress state in DataSift so the next dial picks up where Kareem left off.** Suggested tag format: `distress_shame`, `distress_denial`, `distress_helpless`, `distress_suspicion`, `distress_fatigue`, `distress_grief`, `distress_judgment`, `distress_magical`.

---

# Preset-Specific Cadences

## §1 Lis Pendens (Foreclosure Just Filed)

**Why dial-heavy:** the prospect has 4-12 weeks before sheriff sale. We have time and we want to be the calm voice they hear early — before the predators start calling.

| Day | Action | Notes |
|---|---|---|
| Day 1 (AM) | DIAL #1 + LP opener | Mike's Day 1 SMS already fired; Kareem's call references that "you may have seen my colleague's text" |
| Day 1 (PM) | If missed: DIAL #1.5 + VM | Different time of day, same opener |
| Day 2 | DIAL #2 | Alternate time-of-day from Day 1 |
| Day 3 | DIAL #3 + tested VM variant | If still no connect, send confirmation SMS: "Hey [first], Kareem from WHO again — couple texts/calls about [property]. No pressure. [number]" |
| Day 5 | DIAL #4 | Tier 1-2 phones only (skip Tier 3+ at this point) |
| Day 7 | DIAL #5 + nurture SMS | "Hey [first], FYI Ohio has a 30-day right-to-cure window after lis pendens — quick rundown if helpful: [link]. [number]" |
| Day 14 | DIAL #6 | Reference any prior contact: "Wanted to circle back — has a sheriff sale date been set yet?" |
| Day 21 | DIAL #7 (status check) | Sale-date check is the load-bearing question |
| Day 30 | DIAL #8 | "Things change — wanted to see where you landed" |
| Day 60 | DIAL #9 (final pre-recycle) | If no engagement: tag `lp_recycle_q90` |
| Day 90+ | Quarterly recycle | Re-enter cadence Day 1 |

**Escalate to Aaron if:**
- Property worth >$300K with significant equity
- Lead wants to close within 21 days
- Lead has multiple properties
- Lead asks "what's your offer?" (Boxing Objections rule kicks in)

## §2 Sheriff Sale (>21 days to auction)

**Why dial-heavy:** sale is on the calendar. Every day matters. Mail/SMS aren't fast enough.

| Day | Action | Notes |
|---|---|---|
| Day 1 (AM) | DIAL #1 + SS opener | Standard urgency posture |
| Day 1 (PM) | If missed: DIAL #1.5 + VM | |
| Day 2 (AM) | DIAL #2 | |
| Day 2 (PM) | DIAL #2.5 + tested VM | |
| Day 3 | DIAL #3 | |
| Day 5 | DIAL #4 + SMS deadline math | "Hey [first], [auction] is X days out. Quick chat?" |
| Day 7 | DIAL #5 | |
| Day 10 | DIAL #6 | |
| Day 14 | DIAL #7 + mail piece check | Cross over into <21-day urgency mode at Day 14 |

## §3 Sheriff Sale (≤21 days to auction — URGENCY MODE)

| Day | Action | Notes |
|---|---|---|
| Day 1 (AM + PM) | 2x DIAL + tested VM each | Two attempts same day |
| Day 2 (AM + PM) | 2x DIAL + tested VM | |
| Day 3 (AM) | DIAL + email if available | |
| Day 5 | DIAL + SMS deadline math | "[first], [auction] = X days. Last chance. [number]" |
| Day 7 | DIAL + final VM | |
| Day until sale | Daily DIAL + daily SMS | "Day X before [auction]. Still time. [number]" |

**Escalate to Aaron if:**
- Sale ≤14 days + any willingness signal
- Equity above payoff (Zestimate vs. payoff math)
- Lead says "yes" to a Danielle call

## §4 Probate

**Why dial-light vs. foreclosure:** grief deserves space. Probate is a 6-9 month process. Patience compounds.

| Day | Action | Notes |
|---|---|---|
| Day 1 | DIAL #1 (probate opener) | If they're in active grief (#1.6) — back off, send SMS summary, 30-day recycle |
| Day 3 | DIAL #2 only if Day 1 didn't connect | Skip if Day 1 ended in grief back-off |
| Day 7 | Nurture SMS | "[first], hope the week's been manageable. No pressure on [property] — just wanted to check in." |
| Day 14 | DIAL #3 (gentler probate opener) | "Wanted to circle back — how's the estate process going?" |
| Day 30 | DIAL #4 + mail piece check | Reference court timeline naturally |
| Day 45 | DIAL #5 | "Things settling down at all?" |
| Day 60 | DIAL #6 | |
| Day 90 | DIAL #7 (probate-closing check) | "Where's the estate at — closing soon?" |
| Day 120+ | Quarterly nurture | Until probate closes or status changes |

**Escalate to Aaron if:**
- PR has Letters of Authority + says "yes, ready to sell"
- Property is vacant + costing money to maintain
- Multiple heirs aligned on selling
- Property worth >$300K

## §5 Redemption Window (Post-Sheriff-Sale)

**Why dial-heavy and time-compressed:** redemption windows are short — typically 0-30 days. Every dial is a clock-watching event.

| Day | Action | Notes |
|---|---|---|
| Day 1 (AM + PM) | 2x DIAL + tested VM | |
| Day 2 (AM) | DIAL + educational SMS | "[first], here's a quick guide on Ohio redemption rights: [link]. [number] for questions." |
| Day 3 | DIAL | |
| Day 5 | DIAL + deadline math SMS | |
| Day 7 | DIAL + email if available | |
| Day until window expires | Daily DIAL + daily SMS | "[first], X days left to act on [address]. [number]." |

**Escalate to Aaron immediately if:**
- Lead says "yes, help me redeem"
- Window has <14 days remaining
- Property has clear equity above sale price (always — that's free money)

---

---

# Bulk Top-250 Distressed List Cadence (the second queue)

In addition to the FTM courthouse cadence above, Kareem also dials a **rotating Top-250 distressed list per county** (Franklin / Montgomery / Greene = 750 total active records on the bulk side at any time). This list is refreshed monthly and represents the highest-distress records DataSift surfaces — vacant, tax-delinquent, high-equity, absentee, code-violation, multi-distress overlap.

## How Bulk Top-250 differs from FTM

| Dimension | FTM (courthouse) | Bulk Top-250 |
|---|---|---|
| Source | SiftStack scrapers (probate / foreclosure / redemption) | DataSift bulk distressed-data filters |
| Volume | ~30-80 records/day across all presets | 750 active records (250 × 3 counties) |
| Distress signal | Specific event (probate filed, sheriff sale scheduled) | Multi-factor score (vacancy + equity + tax + absentee) |
| Per-record context | Deep — case number, fiduciary, decedent, auction date | Shallow — just the property + owner name + tags |
| Cadence intensity | High touch, long runway (90+ days) | Lighter touch, faster cycle (3-week rotations) |
| Conversion rate | Higher per dial (event-driven motivation) | Lower per dial, but volume compensates |

## The 3-week rotation cycle (per county)

Each county's 250 records get worked over 3 weeks before the list refreshes:

| Week | What Kareem does | Touches per record |
|---|---|---|
| **Week 1** | First-pass dial + VM + same-day SMS | 1 dial + 1 VM + 1 SMS |
| **Week 2** | Second-pass dial only on Tier 1-2 phones (skip Tier 3+) | 1 dial + 1 VM if needed |
| **Week 3** | Final-pass dial on Tier 1 only + nurture SMS to all | 1 dial + nurture SMS |

After Week 3, the record is recycled into the **monthly nurture pool** (1 SMS per month from Mike, no Kareem dial) until the bulk list refreshes and a new Top-250 takes its place.

## Daily Bulk Top-250 dial budget

Bulk dials happen in the **afternoon block** (after the FTM AM block). Cruise targets:

| Day | FTM dials (AM) | Bulk dials (PM) | Total |
|---|---|---|---|
| Mon (12-6) | 25-30 | 30-40 | 55-70 |
| Tue/Wed/Thu (9-6) | 35-45 | 55-75 | 100-120 |
| Fri (9-5) | 30-40 | 50-60 | 90-100 |
| Sat (1st/3rd, 9-12) | 20-30 | 10-15 | 30-45 |

## Bulk Top-250 script differences

Same framework (TARP, four options, UMBC) but the **opener changes** because the prospect doesn't have a single specific event. Use this opener:

> "Hi, is this [first_name]? Kareem with Wright Home Offer in [county] County. I'm a local Ohio buyer and I'm calling because your property at [property_address] popped up on a list of homes that match what I usually buy — vacant or distressed properties where the owner might be open to a fast sale. I have three or four options for folks in your spot, and not all of them require selling. Worth five minutes to walk through them?"

**Why this works:**
- "Popped up on a list" is honest — beats pretending we know something specific
- "Vacant or distressed properties" — they self-identify which one they are
- Same four-options framing carries through

## Bulk Top-250 priority order within the afternoon block

When Kareem opens the bulk queue, work in this order:

1. **Vacant + High-Equity** (the goldmine — owner has wealth and a non-occupying property)
2. **Tax-Delinquent + Absentee** (clock running on tax sale + remote owner = motivated)
3. **Code Violation + Vacant** (city pressure + non-occupying)
4. **Long-Term Absentee** (5+ years out of state)
5. **Multi-distress overlap** (any record with 3+ distress flags)
6. **Single-distress** (just one flag — vacant only, or absentee only)

DataSift's filter presets in the "01 Bulk Sequential Marketing" folder match these — Kareem opens that folder and works the presets top-to-bottom.

## Bulk Top-250 escalation triggers

Same as FTM — escalate to Aaron when:
- Property worth >$300K with significant equity
- Owner says "yes, send Danielle" on the first call (rare on bulk — flag it)
- Multi-property owner (could be a portfolio buyout opportunity)
- Lead asks "what's your offer?" — same Boxing Objections rule

---

# Phone tier discipline (which records get dialed which days)

DataSift records carry phone tier scores from the Trestle / Tracerfy enrichment step. Tier dictates dial intensity:

| Tier | Score | Treatment |
|---|---|---|
| Tier 1 | 81-100 | 100% same-day dial. Multi-attempt aggressive cadence. Every dial day. |
| Tier 2 | 61-80 | Dial Day 1, 2, 3, 5, 7. Then tier-2 nurture pace. |
| Tier 3 | 41-60 | Dial Day 1, 3, 7 only. SMS Mike's lane. |
| Tier 4 | 21-40 | Mail-only. Kareem does NOT dial. |
| Tier 5 | 0-20 | Drop. Litigator-flag risk. SiftStack should auto-tag. |

**Top-of-day priority rule:** every preset has 50+ records on a given morning. Kareem's first 2 hours go to the **top 2 phone-tier records per preset**, not bottom-tier sweeps. The top 2 of 50 are where the day's handoff yield lives.

---

## Smrtphone power-dial setup

Kareem's dial budget is impossible without power-dialing. Smrtphone is integrated into REISift and supports multi-line parallel dial.

- **Default:** 3-line power dial during dial blocks
- **Drop to 2-line** when working sheriff-sale URGENCY records — those need full attention on the connect
- **Drop to 1-line (single-call)** when calling a probate Day 1 — never power-dial a grieving family

Voicemail drop should be enabled with a tested, neutral pre-recorded VM as fallback when Kareem is mid-conversation on another line.

---

## What a typical dial day looks like

(Full detail in [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md). Quick version here.)

**9:00–9:30** — DataSift pull, preset queue review, top-2 prioritization per preset
**9:30–11:30** — Dial Block #1 (Tier 1-2 records, top 2 of every preset)
**11:30–12:00** — Hot-handoff window: anyone Kareem connected with in Block #1 who needs Danielle, REISimpli webform + group text the team thread
**12:00–12:30** — Lunch
**12:30–2:30** — Dial Block #2 (Day 2/3/5/7 follow-ups)
**2:30–3:00** — Recycle queue (records due for re-attempt today)
**3:00–4:30** — Dial Block #3 (afternoon attempts on records that didn't connect AM)
**4:30–5:30** — DataSift hygiene: tag updates, status updates, callback scheduling
**5:30–6:00** — End-of-day note in DataSift Activity tab + tomorrow's prep

---

## See also

- [ISA-CALL-SCRIPTS.md](ISA-CALL-SCRIPTS.md) — the scripts each dial uses
- [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md) — full day-shape with hour blocks
- [ISA-QUALIFICATION-HANDOFF.md](ISA-QUALIFICATION-HANDOFF.md) — what happens when a dial converts to a handoff
- [SOP-OUTREACH-CADENCE.md](../SOP-OUTREACH-CADENCE.md) — Mike's SMS-channel cadence (parallel track, coordinated via tags)
