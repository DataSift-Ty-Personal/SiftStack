# KPI Catalog - every metric, formula, benchmark, and source

The complete KPI vocabulary for a cold-calling REI operation on DataSift. Benchmarks come from the DataSift 5-Day Deal Flow Challenge (Day 5: Scaling and Operations) and a live Tennessee operation's baselines (July 2026). **Everything here is a baseline range to tune to your market, not a universal rule.**

## Volume

| KPI | Formula / source event | Baseline |
|---|---|---|
| Dials | count of `owner.call.made` events (1 dial = 1 phone number) | floor 150/caller/day, target 200; bulk dialing 250-350 |
| Full attempt | every number on the record + voicemail + text | definition |
| Records touched | distinct records dialed | - |
| Dial hours | first-to-last-call window | 4-6 hrs/day on dialer |
| Weekly team dials | sum Mon-Fri | 5,500+ |

## Connection and conversation (three rates, never collapsed)

| KPI | Formula | Baseline |
|---|---|---|
| Answer rate | answered / dials (includes voicemail pickups) | loosest signal |
| Conversation rate | answered >= 60s / dials | 5-8 conversations/caller/day |
| Meaningful conversation rate | answered >= 120s / dials | - |
| Contact (right-party) rate | correct numbers / dials | strong ops reach ~47% of records over time |
| Duration bands | <30s VM, 30-60s brief, 60-120s conversation, 120s+ deep | - |

Never report a single "connect rate": answer, conversation, and contact rates measure different things and diverge hard in practice.

## Phone quality (Trestle scoring, pairs with the phone-validator skill)

| KPI | Value |
|---|---|
| Dial tiers | 81-100 Dial First, 61-80 Second, 41-60 Third, 21-40 Fourth, 0-20 Drop |
| Connect lift | ~4.75x: 2-3% blind -> ~9.5% dialing tiers 1-2 only (tiers 1-2 held 92% of connects in 48% of the list) |
| Dials per correct number | ~9 phone-scored, ~32 blind |

## Dispositions

Phone-status events (deduped per phone, latest wins): CORRECT / CORRECT_DNC = correct number; WRONG / WRONG_DNC; DEAD; NO_ANSWER; DNC.
Property-status events (deduped per record): lead statuses (below), not_interested, follow_up, Dead Lead, dnc / opt_out.

## Leads

- **A record becomes a lead the moment its status changes to a lead status.** First-touch leads land in `new_lead` / `No Contact New Lead` - count those, not just Cold/Warm/Hot Lead, or your lead KPI reads zero while callers produce 2-3/day.
- Default lead-status set: Cold Lead, Warm Lead, Hot Lead, new_lead, New Lead, No Contact New Lead, Nurture New Lead (edit for custom statuses).
- Qualification = 4 Pillars of Motivation (Reason, Timeline, Condition, Price). 2+ hot pillars = hot lead straight to the closer; 1 = warm; 0 = cold.
- **Baseline: 2-3 leads per caller per dialing day** at the 150-dial floor.

## Funnel and pacing

| Ratio | Baseline | Notes |
|---|---|---|
| Dials : correct number | 32:1 blind, 8-10:1 phone-scored | |
| Conversations : appointment | 5:1 | |
| Appointment take on pushed-over leads | ~25% (round to whole appointments) | "go see the property" model |
| Appointment : offer | 1.5:1 | |
| Offer : contract | 4:1 (25%+ close; below 20% = comps are off) | |
| Correct numbers : deal | 100:1 over 12 months (30-50 for strong cohorts) | the signature ratio |
| Leads : contract | 15-20:1 | sequential-flow baseline |
| Worked example | 67,421 dials -> 2,049 correct -> 870 leads -> 327 qualified -> 108 offers -> 16 contracts -> 12 closed | real funnel |

## Role KPIs

- **Cold caller / prospector:** 150-200 dials/day, 5-8 conversations, 3-5 appointments set, 4-6 dial hours. The one metric: appointments set per dial hour.
- **Lead manager:** 150 dials/day floor, ~25 conversations/day, push 15-20% of conversations (2-5 send-to-acquisitions/day), speed to lead under 1 minute (~400% conversion lift). The one metric: lead-to-offer.
- **Closer:** 5-10 offers/day, hot leads contacted within 1 hour, offer-to-contract 25%+. The one metric: offer-to-contract ratio.

## Data source notes

- The DataSift per-record activity log is the only date-accurate, caller-attributed source of calling KPIs (dashboard widgets are not script-accessible).
- Callers self-identify on call events; disposition events carry their author. No static roster needed.
- Log timestamps are UTC; bucket days in your local timezone.
- Exclude admin/owner logins from caller tables so testing activity does not pollute the numbers.
