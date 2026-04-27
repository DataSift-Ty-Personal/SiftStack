# SOP — Lead Qualification & Escalation

**Audience:** Mike + future caller. **Audience for review:** Aaron (deal closer).
**Purpose:** Decision framework for moving leads through the pipeline. When to escalate, when to drop, when to keep nurturing. Don't waste Aaron's time on bad leads, don't waste your time on dead leads.

---

## The 4 Pillars of Motivation (the gating framework)

Every lead is scored on these 4 dimensions. **A lead is "qualified" only if at least 3 are positive.** This is the gate before anything reaches Aaron.

### 1. Timeline (do they need to move fast?)

Look for:
- **Probate:** court deadlines, family pressure, vacancy costs
- **Foreclosure:** sheriff sale date, missed mortgage payments
- **Tax sale:** auction date proximity
- **Code violation:** city compliance deadline
- **Personal:** divorce settlement, job relocation, health issue

**Strong signal:** "I need this done by [date]" or "I can't keep paying the [tax/mortgage/insurance]"
**Weak signal:** "We're just exploring options"

### 2. Pain (is the property a burden?)

Look for:
- Vacant property losing value or being vandalized
- Repairs they can't afford or don't have time for
- Tenants they can't manage (eviction filings)
- Estate property that's blocking distribution
- Out-of-state owner with no boots on the ground

**Strong signal:** "It's been sitting empty for 18 months" / "I can't deal with the tenants anymore"
**Weak signal:** "Yeah it's fine, my brother lives there"

### 3. Capability (can they actually sell?)

Look for:
- **Probate:** Court has issued Letters of Authority to the PR (executor has authority to sell)
- **Foreclosure:** Property is in their name (not a co-signer or LLC complication)
- **All:** They are the sole or majority decision-maker (no other heirs/owners to convince)

**Strong signal:** "I'm the only executor, court approved last month, I can sign tomorrow"
**Weak signal:** "My sister has to agree" / "We're still waiting on the court"

### 4. Decision-maker on the call

Are you talking to the person who can say yes? Or are they running it past someone else?

**Strong signal:** "Yes, I can make this decision"
**Weak signal:** "I need to talk to my [spouse/lawyer/sibling]"

---

## The qualification score

Quick mental math during/after each call:

| Score | Action |
|---|---|
| 4/4 (all green) | **Tag `hot` → escalate to Aaron immediately** — drop other work, this gets a same-day callback |
| 3/4 | Stay in standard cadence; tag the lead `qualified`; aim for follow-up call within 48 hrs |
| 2/4 | Continue niche sequential cadence (Day 2 SMS, Day 3 SMS, mailer) |
| 1/4 | Tag `low_motivation`, drop to monthly mail nurture |
| 0/4 | Tag `not_interested`, schedule 90-day recycle |

---

## Escalation triggers (when to ping Aaron in Slack)

**Same-day escalation (drop everything):**
- Lead says "yes, make me an offer" or names a price near MAO
- Probate PR with Letters of Authority + vacant property + ready to sign
- Foreclosure homeowner with sheriff sale in <30 days who agrees to meet
- Any "I want to close this week" conversation

**24-hour escalation:**
- Lead is qualified (3-4/4) but says "let me think about it"
- Multiple heirs identified — Aaron may need to handle the multi-party negotiation
- Property has unusual situation (mineral rights, code violations, IRS lien) that affects offer

**No escalation needed:**
- "Send me information" — keep them in nurture
- "Not interested" — recycle in 90 days
- "Already working with someone" — recycle in 60 days (deals fall through)

---

## Pipeline status definitions

| Status (DataSift pill color) | When to use |
|---|---|
| **New** (default) | Just landed in DataSift, hasn't been touched |
| **Contacted** | First SMS sent or first call attempted (even VM counts) |
| **Interested** | Lead engaged positively but hasn't committed (mid-conversation) |
| **Appointment** | Calendar date set with Aaron or caller |
| **Offer** | Verbal offer made, awaiting response |
| **Under Contract** | Signed purchase agreement |
| **Closed** | Deal closed, deed recorded |
| **Dead** | Lost to competitor, owner pulled out, sale fell through |
| **Sold** | Property sold to someone else (not you) — triggers Sold Property Cleanup sequence |

**Update the status as the relationship moves.** This is what feeds DataSift's reporting and what tells Aaron what's worth his time.

---

## Quick-reject filters (don't even try)

Stop the cadence and tag these as `disqualified`:

- **Property is owned by an LLC/Corp/Trust** AND `entity_research` couldn't identify a person → too much friction
- **Multiple competing heirs** with active legal dispute (visible from court docket) → wait for resolution
- **Property is already pending sale** (MLS Status = Pending) — they're already under contract
- **Property is already SOLD** (MLS Status = Sold, recent sale date) — pipeline missed the dedup
- **Phone tier 5** (0-20) — confirmed bad number or litigator risk
- **Owner deceased without heir map** — we couldn't identify a decision maker; needs deep prospecting before contact

---

## The "callback ladder" — when to call back

If a lead doesn't reject you outright but doesn't commit either:

| Today | Day 7 | Day 30 | Day 90 |
|---|---|---|---|
| Initial contact | First nurture call | Second nurture call | Recycle through cadence |

**Probate exception:** Probate leads are time-sensitive. If they say "let me think," call back in 3-5 days, not 7. Estates close fast.

**Foreclosure exception:** If sheriff sale is approaching, ignore the standard ladder — call/SMS daily until sale date. After sale, the deal options narrow but redemption-period plays still exist for ~30 days.

---

## What to do when a lead converts (closing handoff to Aaron)

When Aaron takes over:

1. **In DataSift:** tag the lead `hot` and set status to `Interested` or `Appointment`
2. **In Slack:** ping Aaron with the property address, owner name, and 1-line summary of why this is qualified ("Montgomery probate, executor confirmed sole authority, vacant property, wants to close in 30 days")
3. **Add a note** in the DataSift record with the call summary so Aaron has context before his call
4. **Don't keep working the lead** — Aaron now owns it. You can monitor the record in DataSift but don't reach out unless asked.

After Aaron's call:
- If deal closes → he tags `Sold` → cleanup sequence fires → you don't see it again
- If deal dies → he tags `Dead` and adds a note → you can recycle in 90 days if appropriate

---

## Daily KPIs to track (informally — Mike doesn't need a dashboard)

By the end of each day, mentally track:

- **Total leads worked:** How many SMS sent, calls dialed, mail flagged?
- **Connection rate:** Of calls dialed, how many resulted in a conversation? (Target: 30%+)
- **Qualification rate:** Of conversations, how many scored 3-4/4? (Target: 15-25%)
- **Escalation count:** How many `hot` tags went to Aaron? (Target: 1-3 per day)
- **Recycle/dead count:** How many you closed out today?

These don't need to be perfect — they're for self-calibration. If your connection rate drops below 20% for a few days, it's a signal that phone tiers are off or call timing is wrong.

---

## When you're stuck — check these resources first

| Question | Where to look |
|---|---|
| "What do I say to this objection?" | [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) Objection Handling section |
| "How do I tag this in DataSift?" | [SOP-DATASIFT-NAVIGATION.md](SOP-DATASIFT-NAVIGATION.md) "Add a tag" section |
| "Why does this preset show 0 records?" | [SOP-RED-FLAGS.md](SOP-RED-FLAGS.md) Section 4 |
| "Should I escalate this one?" | This doc, "Escalation triggers" section |
| "What does this tag mean?" | [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md) Section 1 |

If still stuck after checking the SOPs → ping Aaron in Slack with the question. Better to ask than to guess.

---

## See also

- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md)
- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md)
- [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md)
- [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md) — redemption-window leads auto-bump on Timeline + Pain pillars; same-day escalation triggers are different from standard records
