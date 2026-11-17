# Trestle, phone intelligence

Scores a phone number so you know what to dial first, and flags the numbers you
should not dial at all.

- **Key:** `TRESTLE_API_KEY`
- **Cost:** about $0.015 per number
- **Used by:** `phone-validator`, and the scoring pass at the end of every skip
  trace chain

## What you get back

The endpoint is `phone_intel`. The parts that drive decisions:

| Field | Why it matters |
|---|---|
| Activity score | The core ranking signal. Drives the tier |
| Line type | Mobile beats landline for both SMS and pickup |
| Connected status | Disconnected numbers leave the list permanently |
| Contact grade | Confidence that this number belongs to this person |
| Litigator flag | The one you cannot reproduce for free |

## The five dial tiers

These are fixed across every skill in the library. Keep them even if you score
by other means, because the rest of the system reads them.

| Score | Tier |
|---|---|
| 81 to 100 | Dial first |
| 61 to 80 | Dial second |
| 41 to 60 | Dial third |
| 21 to 40 | Dial fourth |
| 0 to 20 | Drop |

Measured effect of dialing in tier order rather than list order: about **4.75x**
the connect rate. That number is the entire argument for the $0.015.

## Rules that matter more than the score

**Score every number, then dedupe.** Skip trace sources overlap heavily and the
same number arrives from several. Scoring after the union means you pay once
per unique number instead of once per source hit.

**Cross-confirmation beats score.** A number that appeared in two independent
sources outranks a single-source number at the same score. Carry a
`confirm_count` alongside the score; it is free and it is a better signal than
the last ten points of score.

**On a shared household line, the owner rule wins.** If a number belongs to
both the owner and a relative, label it with source and tier only, never with
the relationship. Otherwise the dial sheet tells a caller that the owner's own
landline is "Husband", and the call opens wrong.

**The litigator flag is the reason to pay.** Activity and line type you can
approximate for free. TCPA and litigator risk you cannot. If you dial at
volume, this check pays for itself the first time it fires.

## Two keys

`TRESTLE_FREE_API_KEY` and `TRESTLE_PAID_API_KEY` exist in the example env.
The free tier is for validating your integration. Do not run production
scoring through it and conclude the data is thin.

## Without the key

[Phone scoring without Trestle](../setup/no-api-playbook.md#phone-scoring-without-trestle).
You can recover most of the ordering value from line type, recency and
cross-source confirmation. You cannot recover litigator screening.
