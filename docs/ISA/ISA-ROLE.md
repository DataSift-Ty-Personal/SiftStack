# ISA Role — Kareem

**Audience:** Kareem (the new ISA), Aaron (manager), Danielle (deal closer / handoff target).
**Purpose:** Single-page definition of what Kareem owns, what he doesn't, and the day-shape of the role.

---

## What Kareem is

Kareem is a **distress-property appointment setter** for Wright Home Offer. He calls two queue types every day:

1. **FTM (First-To-Market) records** — courthouse data scraped overnight by SiftStack: foreclosure / probate / redemption filings in Franklin, Montgomery, Greene. Niche-sequential, high quality, low volume (~30-80/day across all presets). These are the records Mike works on SMS in parallel.
2. **Top 250 distressed list per county** — bulk distressed-property data, the highest-distress 250 records per county across the three Ohio counties. Higher volume, broader distress (vacant, tax-delinquent, equity, absentee, code violations, etc.), lower per-record context than FTM. Lighter touch per record, faster cadence.

His job is **not** to qualify-and-reject. His job is to **help, earn the conversation, and hand the helpable ones to Danielle as warm appointments.**

Every record Kareem sees — FTM or bulk — is already our ICP. The acquisition cost is sunk. A "no" on first call does not mean disqualified — it usually means we caught them at the wrong moment of a hard month. Recycle, re-approach, re-help.

---

## The KPI Kareem is hired against

| Metric | Target | Notes |
|---|---|---|
| **Signed contracts per month** | **4** | The North Star. This is what success looks like. |
| **Set appointments per week** | **7-10** | The leading indicator. Implied 9-14% appointment-to-contract conversion (reasonable for distress). |
| Appointments per day (cruise pace) | 1.5-2 | Across both FTM and bulk Top-250 queues |
| Dials per day (cruise) | 100-120 | Combined across FTM + bulk |
| Connect rate | 25-35% | Industry-typical for distress dial |

**The math:** to hit 4 signed contracts, Kareem needs ~30-43 set appointments per month. At 7-10/week that's the floor; at 12+/week he gives Danielle margin to lose a few without missing the contract goal. Mike + Kareem + the SiftStack pipeline all serve this number.

Aaron grades the role against contracts and appointments — not dials. Volume is a means to that end, not the end itself.

---

## Schedule (ET)

| Day | Hours |
|---|---|
| Monday | 12:00 PM – 6:00 PM |
| Tuesday – Thursday | 9:00 AM – 6:00 PM |
| Friday | 9:00 AM – 5:00 PM |
| 1st + 3rd Saturday | 9:00 AM – 12:00 PM |

Weekly active dial windows are scoped in [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md). All callbacks Kareem schedules with prospects must land inside these windows or get rerouted to Danielle directly.

---

## What Kareem owns

- All inbound and outbound seller phone calls inside DataSift / Smrtphone
- **Two daily queues:**
  - **FTM queue** — top 2 of 50 prioritized per courthouse preset (see [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md))
  - **Bulk Top-250 queue** — three lists (Franklin / Montgomery / Greene), worked in cadence over 2-3 week cycles
- Daily collaboration with Mike (SMS channel owner) — see [ISA-MIKE-COLLAB.md](ISA-MIKE-COLLAB.md)
- TARP discipline on every call (set + match the next-call TARP)
- Help-framed openers for foreclosure / probate / redemption (see [ISA-CALL-SCRIPTS.md](ISA-CALL-SCRIPTS.md))
- UMBC capture on every connect (Urgency / Motivation / Ballpark / Condition)
- DataSift status + tag updates the same hour the call happens
- Warm handoff to Danielle via REISimpli webform when a record qualifies (see [ISA-QUALIFICATION-HANDOFF.md](ISA-QUALIFICATION-HANDOFF.md))
- Voicemail discipline — every dial that goes to VM gets a tested VM, not a hangup
- Daily end-of-day note on the team group text + DataSift Activity tab summarizing connects + handoffs

## What Kareem does NOT own

- SMS outreach — that's Mike's lane (the two roles touch the same records but on different channels — coordinate via DataSift tags, not by texting prospects directly)
- Mail outreach — automated, no manual action needed
- Pricing or offer-making — Kareem has **no pricing authority**, by design, and uses that as leverage on every call ("I'm not the partner who sets the number — my job is to make sure they have what they need to fight for you")
- Closing the deal — Danielle owns from the warm handoff onward
- Lead generation, scraping, enrichment, DataSift uploads — SiftStack does all of that overnight
- Determining whether a lead is "qualified" via judgment-only — qualification is structural (UMBC + readiness signals), not a vibe call

---

## The mental model — why we built this role

Distress homeowners are pitched relentlessly by wholesalers, foreclosure-rescue scammers, and predatory cash buyers. They have an immune response to the phrase "I want to buy your house." What separates Wright Home Offer is that **Kareem leads with help, not with intent to buy.** The first thing out of his mouth is options. The second thing is his patience. The third thing — only if they invite it — is what we can actually do.

That posture flips the conversation:

- They expect a vulture. They get a calm advisor.
- They expect to be sold. They get to choose between four real paths (see [ISA-SALES-FRAMEWORK.md](ISA-SALES-FRAMEWORK.md) — Four Options).
- They expect pressure. They get TARP — explicit time, agenda, and permission to say no.

When Kareem does this consistently, the people who are actually ready to sell self-identify, and the people who aren't yet ready remember us when their situation worsens.

---

## What "good" looks like in Week 1

- Every connect has a logged UMBC outcome (Qualified / Novation / Listing-pivot / Not-yet — never just "no")
- Every call opens with TARP and closes with TARP that matches the next callback
- Zero dials end without either a follow-up scheduled, a verified no with a referral ask, or a handoff
- Zero records get tagged "Dead" — we re-status, never kill
- 100% of warm handoffs to Danielle land in REISimpli within 15 minutes of the call ending

---

## Escalation map

- **Hot lead, ready to talk to Danielle:** Submit REISimpli webform → group text Danielle on the team thread with property address + 1-line summary. See [ISA-QUALIFICATION-HANDOFF.md](ISA-QUALIFICATION-HANDOFF.md).
- **Stuck on an objection / unusual situation:** Group text Aaron (team thread) with the property address + transcript snippet. Don't sit on it — Aaron would rather be texted than have a deal stall.
- **Tech issue (Smrtphone / DataSift / REISimpli):** Group text the team thread. Kareem keeps dialing on whichever channel still works.
- **DialForce / scheduling / pay questions:** Direct to DialForce ops. Aaron is not the right escalation path for those.

---

## See also

- [ISA-SALES-FRAMEWORK.md](ISA-SALES-FRAMEWORK.md) — the psychology + RD methodology
- [ISA-CALL-SCRIPTS.md](ISA-CALL-SCRIPTS.md) — preset-by-preset openers + objection branches
- [ISA-CADENCE.md](ISA-CADENCE.md) — dial-velocity playbook by preset
- [ISA-QUALIFICATION-HANDOFF.md](ISA-QUALIFICATION-HANDOFF.md) — UMBC gate + REISimpli handoff
- [ISA-ONBOARDING.md](ISA-ONBOARDING.md) — Week 1 → Week 4 ramp
- [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md) — hour-by-hour day shape
