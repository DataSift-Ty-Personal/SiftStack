# Vetting Checklist: turning leads into a trustworthy directory

Phase 2 and 3 work. The output of this file is a directory the user can act on without
getting burned. Fan the work out with subagents (one per category cluster) so it's fast
and each subagent keeps a clean context; give each the leads for its cluster plus this
checklist.

## The prime directive: never fabricate

Someone will dial these numbers and sign contracts off this sheet. Every field is either
**verified from a real source** or marked **"not found."** Never write a plausible-looking
phone number, license number, email, or rating you didn't confirm. A directory that's 80%
filled and 100% honest is far more valuable than one that's 100% filled and quietly wrong.
If a subagent can't confirm something, it says so. That's a feature.

## Per-provider verification (fill each column honestly)

For every lead, confirm and record:

1. **Real business + best contact.** Confirm the company exists; get/verify the phone and
   email from the company's own site or a first-party listing. Mark whether the phone is
   **verified** (matches an independent listing) vs. **from their site** vs. **unconfirmed**.
2. **Service area covers the target market.** The most common silent failure. Check the
   company's service-area/locations page and Google Business area. Record which target
   county/city they actually cover.
3. **Rating + review COUNT.** Always capture the count, not just the stars. "4.9" means
   nothing without knowing if it's 4 reviews or 400. Name the source (Google, BBB,
   HomeAdvisor, Birdeye, Angi) since counts vary by platform.
4. **License / credential status.** Look up the relevant board for the domain (see below).
   Record the license number + active status when the work legally requires it, or note
   "likely not required for small jobs" / "not found, verify." Don't assert a license you
   didn't see.
5. **BBB + red flags.** Accreditation/rating if any; and scan for complaints, "closed"
   listings, out-of-state name collisions, lead-gen fronts, and lapsed licenses.
6. **Confidence.** A plain read: High = safe to call today; Low = a real lead but do the
   small-test-job filter first.

## Where to find license / credential boards (by domain)

Licensing is domain- and jurisdiction-specific. Find the authoritative board rather than
trusting a directory aggregator:

- **Construction trades (US, by state):** each state has a contractor licensing board;
  search "<state> contractor license lookup" (many are at a `.gov` verification portal).
  Thresholds matter. For example, many states only require a state license above a dollar amount
  (often ~$25k), below which trade or local licenses apply. Note the classification
  (building, mechanical/plumbing, electrical) covers the work.
- **Plumbing / HVAC / electrical:** often a separate trade license or a classification
  under the contractor board; small jobs may run under a limited/journeyman or municipal
  license.
- **Specialty regulated work:** septic installers (state environmental/health dept
  certification), pest/termite "WDO" inspectors (state agriculture dept charter; the
  entity issuing a real-estate "termite letter" must hold one), asbestos/lead, well
  drilling, electrical.
- **General businesses:** state Secretary of State entity search confirms a registered
  business; BBB and the company site fill in reputation.
- **Non-construction vendors:** the relevant professional board or registry for that field
  (e.g., a state bar, medical board, insurance dept), or simply verified reviews +
  business registration when no license applies.

When in doubt, tell the user which board to confirm on and give them the number. Don't
overstate certainty.

## Catching AI-generated / found lists (a common trigger for this skill)

When the user hands you a list from ChatGPT, a VA, or an old spreadsheet, treat it as a
set of **claims**, not facts. These lists routinely contain:

- **Wrong phone numbers**: a digit off, or a call-tracking number, or an unrelated
  same-name business. Verify every number against the company's own site.
- **Overstated scope**: "advertises water-line excavation" when the site only shows
  concrete work. Check what the company actually says it does.
- **Businesses that don't exist in the target market**: a real-sounding name that,
  on inspection, is out of state or unfindable. If there's no independent evidence a
  business exists in the market, say **"UNVERIFIED, no independent evidence found"** and
  flag it; don't launder the claim into the directory as fact.

Catching these is the highest-value thing the skill does on a found-list task. Show the
user exactly what you corrected (e.g., "their real number is X, not Y").

## The geography sweep (Phase 3)

The geography rule: everyone on the final sheet should serve the whole target market. For each
provider decide: **serves it** (based in or lists the area), **confirm** (metro-local,
almost certainly covers a secondary area but doesn't publish it: keep, flag to confirm on
the call), or **remove** (out of area / too far to realistically serve). Don't silently
keep an out-of-area provider because they have great reviews. Note it and cut or flag.

## The vetting-call questions (put these in the Methodology tab)

Give the user the questions that separate a good vendor from a bad one on the first call:

- "Do you service <the secondary area> as well as <the primary area>?" (for "confirm" rows)
- "Do you work with <the user's type: investors/flippers/property managers/etc.>, and can
  you give volume/repeat pricing?"
- "What's a realistic timeline, and will you hold that bid in writing?"
- "Do you pull permits, and is that in the price?" (where relevant)
- "Can you send a certificate of insurance (COI) and your license number?"
- "Payment terms: draws on completion, or money up front?" (heavy up-front deposits are a
  red flag).

## What "done" looks like

Every lead is either a verified row with honest fields or clearly flagged as unverifiable,
each provider is tagged for whether it serves the market, and any corrections to a found
list are surfaced to the user.
