# ISA Qualification + Handoff to Danielle

**Audience:** Kareem.
**Purpose:** Replaces the 4-Pillars qualification gate. Defines what counts as a qualified opportunity, what data Kareem captures, how the REISimpli webform handoff works, and what Danielle expects to receive.

---

## What "qualified" means here

A lead is **qualified to hand off to Danielle** when Kareem captures all four UMBC dimensions during a connect AND the lead reaches one of three outcomes:

| Outcome | Definition |
|---|---|
| **A — Cash-sale qualified** | Urgency present, motivation real (mapped to RD category), ballpark in our cash range or extractable, livable or fixable property |
| **B — Novation seed** | Urgency present, low-distress + ballpark ~90% of market + livable. Plant novation language; flag in handoff. |
| **C — Listing-pivot qualified** | Urgency present, ballpark well over our cash range, has 60+ days, livable. Aaron handles via his Ohio license. |

**A lead is NOT qualified when:**
- UMBC is incomplete (one or more dimensions missing) — schedule callback to capture
- Motivation is shallow / face-value (re-press with Socratic follow-ups before handoff)
- Distress state is grief (#1.6) or active denial (#1.2) — recycle, don't handoff
- Owner is not the decision-maker AND the actual DM hasn't been confirmed yet

Kareem does **not** judge "is this a good lead" by gut. UMBC + outcome category is structural. The framework decides; Kareem captures.

---

## The UMBC Capture Sheet (Kareem fills this during/after every connect)

Before submitting the REISimpli webform, Kareem must have all of these. If any are missing, the call ends with a callback scheduled to fill the gap, not a handoff.

### U — Urgency
- [ ] Is there a court-driven deadline? (Sheriff sale date, redemption window expiry, probate calendar)
- [ ] Is there a life-driven deadline? (Relocation, health, divorce settlement, family pressure)
- [ ] How many days until the most-pressing deadline?
- [ ] What does "urgency" feel like to them right now — high pressure, simmering, distant?

### M — Motivation
- [ ] Which RD Motivation Category? (Financial / Emotional / Relationship / Physical / Mental / Time / Goal-Blocking / Extreme Pleasure / Spiritual / No Motivation)
- [ ] Orientation: **Away From Pain** or **Toward Pleasure**?
- [ ] What's the specific quote that revealed real motivation? (Capture verbatim if possible)
- [ ] Did Kareem use ≥2 Socratic follow-ups before accepting the answer?

### B — Ballpark
- [ ] What number did the seller name? ($____)
- [ ] How did Kareem ask? (1st, 2nd, 3rd attempt — 3x rule)
- [ ] If they refused all 3, what's the partner-fear frame they responded to?
- [ ] **Did Kareem ever quote a price, range, or hypothetical?** (If yes — hard flag, escalate to Aaron immediately for coaching)

### C — Condition
- [ ] Livable right now? Anyone living there?
- [ ] Major systems issues? (Roof, foundation, plumbing, HVAC, mold, hoarding)
- [ ] Last update / renovation timeframe?
- [ ] Any disclosed defects? (Lawsuits, code violations, IRS liens)

### Distress State Tag
- [ ] Which of the 8 states (or blend) was dominant on this call? (Drives recycle timing if not handed off)

---

## The REISimpli Webform — manual lead submission

Wright Home Offer's REISimpli account has a **Web Form Lead** intake. Kareem submits one webform per qualified handoff. **No API push, no Zapier — manual entry, on purpose, so attribution and notes are clean.**

### Field-by-field guide

Kareem opens the REISimpli webform URL (Aaron will share the bookmarked URL during onboarding) and fills:

| REISimpli field | What Kareem enters | Source |
|---|---|---|
| **Lead Source** | `SiftStack ISA — [preset]` (e.g., `SiftStack ISA — Sheriff Sale Montgomery`) | Standardized — drives Danielle's pipeline filtering |
| **First Name** | `[first_name]` | DataSift `Owner First Name` |
| **Last Name** | `[last_name]` | DataSift `Owner Last Name` |
| **Phone** | The verified phone Kareem just connected on | Smrtphone call log |
| **Email** | If captured during call | Otherwise leave blank |
| **Property Address** | Full street, city, state, ZIP | DataSift `Property Street/City/State/ZIP` |
| **Mailing Address** | If different from property | DataSift `Mailing Street/City/State/ZIP` |
| **Owner Status** | Living / Deceased | DataSift `Owner Deceased` field |
| **Notes** (the long field) | See template below | Kareem types |

### Notes field template (copy-paste, fill in)

```
SOURCE: SiftStack ISA — [preset name]
DATE/TIME OF CONNECT: [YYYY-MM-DD HH:MM ET]
CALL DURATION: [Smrtphone-reported minutes]
DISTRESS STATE: [from 8-state map — primary, plus blend if applicable]

UMBC SUMMARY:
- Urgency: [court-driven deadline + days remaining; life-driven deadline if any]
- Motivation: [RD category — Away From Pain / Toward Pleasure — verbatim quote: "..."]
- Ballpark: $[X] (asked [N] times; partner-fear frame [used / not used])
- Condition: [livable y/n — major issues — last update]

OUTCOME REACHED:
[ ] A — Cash-sale qualified
[ ] B — Novation seed
[ ] C — Listing pivot

HANDOFF TARP SET WITH SELLER:
- Day/Time of Danielle's call: [day, date, time ET]
- Backup time: [day, date, time ET]
- Time promise stated: [X minutes]
- Agenda stated: [verbatim — what Kareem told the seller Danielle would cover]
- Result stated: [verbatim — what Kareem told the seller they'd know by end]
- Permission-to-say-no stated: [Y/N]
- Callback time stated 3 times: [Y/N]
- Confirmation SMS sent to seller: [Y/N — timestamp]

OBJECTIONS RAISED + HOW HANDLED:
- [verbatim objection] → [framework used: Deflect / Go-For-No / Boxing / Multiple Options] → [outcome]
- (one bullet per objection that came up)

FAMILY / DECISION-MAKER NOTES:
- Are there siblings, spouse, attorney, co-owner involved?
- Has Kareem talked to all of them, or just this one?
- Any conflict signals?

KAREEM'S READ — what to expect on Danielle's call:
[1-3 sentences — what Danielle should walk in knowing. Tone, energy, what they're scared of, what they're hopeful about, what's likely to come up as the real objection.]
```

### Submission timing

- **Submit within 15 minutes** of the call ending. While the call is fresh, the notes are accurate.
- After submission, **group text the team thread immediately** (tag Danielle by name) with: property address + 1-line summary + the day/time of her scheduled call.
- **Send the seller a confirmation SMS within 5 minutes of the call ending.** Template:

> "Hey [first_name], Kareem from Wright Home Offer — confirming Danielle will call you [day] at [time]. About 10 minutes, walking through the property and offer numbers. If anything comes up before then, my number's [number]. Talk soon."

---

## TARP alignment — Kareem → Danielle

This is the load-bearing rule. **Whatever Kareem promises about Danielle's call must be exactly what Danielle delivers.**

| Kareem's Closing TARP | Danielle's Opening TARP must match |
|---|---|
| "About 10 minutes" | Open with "I'll keep this to about 10 minutes" |
| "She'll go through the property condition and offer numbers" | Open by going through property condition and offer numbers (not paperwork, not legal stuff) |
| "By the end you'll know exactly where you stand" | Open with that promise restated |
| "If you decide it's not the right fit, that's totally fine" | Permission-to-say-no in opening TARP |

If Danielle opens with a different time / agenda / result than what Kareem promised, the seller's nervous system flags the mismatch — trust drops, ghost rate spikes. **Kareem's notes give Danielle exactly what to say.**

---

## What happens after Kareem hands off

1. Kareem submits the REISimpli webform
2. Group text fires to the team thread tagging Danielle
3. Confirmation SMS goes to the seller
4. **Kareem stops working the lead.** Danielle owns it from this point.
5. Kareem can monitor the record's status in DataSift but **does not call, text, or email the seller again** unless Danielle explicitly asks.
6. After Danielle's call:
   - **If deal advances:** Danielle moves the REISimpli status forward; Kareem sees it advance; commission is tracked when the deal closes
   - **If deal dies:** Danielle moves status to `Dead` or `Long-term Nurture` in REISimpli, adds a note explaining why; Kareem can recycle in 90 days if appropriate
   - **If seller no-shows the call:** Danielle texts Kareem on the team thread; Kareem re-dials within 24 hours with a re-engagement opener (don't lead with shame)

---

## Re-engagement after no-show (Kareem's fallback playbook)

When a seller commits to a Danielle call and no-shows, the standard rep response is to feel rejected and stop. **Kareem instead re-dials within 24 hours with a calm reset.**

> "Hey [first_name], Kareem with Wright Home Offer. We had Danielle scheduled to chat with you yesterday at [time] and missed each other. No big deal — happens all the time. Want to reset for [tomorrow / day after] at [time]? Same agenda — about 10 minutes, property + offer numbers, you'll know where things stand by the end. Or if today's not the right time at all, just let me know and I'll back off."

**The reset call gives them dignity.** No guilt. No "you missed our call." Half of no-shows reschedule on the reset.

If they no-show **twice**, Verify The No, recycle in 30 days, tag distress state.

---

## What Danielle expects to see — every time, no exceptions

When Danielle opens a SiftStack-ISA lead in REISimpli, she should see:

1. Lead Source = `SiftStack ISA — [preset]`
2. Notes field filled with the full template above
3. Group-text message from Kareem on the team thread with property address + 1-liner
4. SMS confirmation visible in Smrtphone log to the seller
5. Scheduled callback time (matches the group-text message)

**If any of those are missing, Danielle pings Kareem to fix before the call.** Aaron audits this compliance weekly.

---

## Edge cases

### "Seller wants Aaron specifically, not Danielle"
Some sellers ask for Aaron by name (recurring contact, referral, etc.). In that case Kareem hands off to Aaron via the same REISimpli flow but tags Aaron in the team group text instead of Danielle. Notes template is identical.

### "Seller is the type Aaron handles directly" (>$300K, multi-property, complex multi-heir)
Kareem flags in the notes ("KAREEM'S READ: high-value / multi-property / multi-heir — recommend Aaron handle"). Group text the team thread tagging Aaron. Aaron decides whether to take it himself or route to Danielle.

### "Seller is a listing-pivot opportunity" (Outcome C)
Kareem submits the REISimpli webform with Lead Source = `SiftStack ISA — Listing Referral`. Group text the team thread tagging Aaron (not Danielle, since Danielle handles cash buys). Aaron's licensed-Realtor flow takes over.

### "Seller is mid-call urgent — needs Danielle now, not tomorrow"
Rare but it happens (sheriff sale tomorrow, redemption expires Friday). Kareem texts Danielle on the team thread: "URGENT — [property address], [reason]. Can you take a 3-way call right now?" If Danielle is available, conference her in. If not, Kareem captures full UMBC, books fastest possible callback, escalates priority in REISimpli notes.

---

## Compliance audit (Aaron, weekly)

Aaron audits a sample of Kareem's handoffs every Friday. Pass/fail criteria:

- [ ] All UMBC dimensions captured in notes
- [ ] Distress state tagged
- [ ] TARP set (4 elements) and matches Danielle's opening TARP
- [ ] Confirmation SMS sent within 5 minutes
- [ ] Team-thread group text sent within 15 minutes
- [ ] No price, range, or hypothetical quoted by Kareem (hard flag if violated)
- [ ] "Other options" never replaced with "other offers" (hard flag if violated)
- [ ] Verify-The-No executed if seller declined handoff (hard flag if missed)

Hard-flag violations: same-day coaching session. See WHO OS Sales scorecard for full Results-Driven rubric.

---

## See also

- [ISA-SALES-FRAMEWORK.md](ISA-SALES-FRAMEWORK.md) — the UMBC framework + RD methodology
- [ISA-CALL-SCRIPTS.md](ISA-CALL-SCRIPTS.md) — the closing-TARP scripts that feed this handoff
- [ISA-CADENCE.md](ISA-CADENCE.md) — when callbacks get scheduled
- WHO OS Sales `02_departments/sales/scorecard.md` — the Results Driven scorecard Kareem is graded against
