---
name: vendor-directory-builder
description: >-
  Build a vetted, filterable directory of local service providers (general
  contractors, subcontractors, trade crews, or any vendors) for a target market by
  mining a community source (a Facebook group, forum, subreddit, or referral list) and
  cross-checking every name against public records. Use whenever someone wants to find,
  vet, rank, or organize providers in a place: "build me a contractor/sub list for X
  county," "find a reliable flip crew in Y," "who can do Z here," "vet these vendors,"
  "build a service-provider or referral directory," or wants to geography-check or
  expand an existing list. Works for any trade or vendor type and any market: defaults
  to real-estate contractor crews but generalizes. ALSO use when the user hands you a
  raw or AI-generated provider list to verify and clean up. Trigger even if they never say
  "directory": if the need is "find good local people who can do a thing and tell me
  which to actually call," use this skill.
---

# Vendor Directory Builder

## What this produces

A filterable Excel workbook the user can actually work from:

- **Directory tab**: one row per provider, with contact info, service area, a "Serves
  <target market>" flag, ratings, license/credential status, strengths, the source
  signal (who recommended them / how they surfaced), cautions, a confidence read, and a
  top-pick star.
- **Top Picks tab**: the single best, verified, market-serving option per category,
  plus runners-up and a small summary block.
- **Methodology tab**: how the list was built and a reusable vetting playbook, so the
  user (or their team) trusts the list and can keep filling it.
- **Optional reference tab(s)**: domain-specific "obscure but critical" data the job
  needs (e.g., the utility districts that own the water main and set tap fees, permit
  offices, licensing boards). Add these whenever the use case has a hidden layer.

`scripts/build_directory.py` generates all of this from one JSON config, so the format
is consistent every time and you never hand-format a spreadsheet.

## Why this method works (read this, it drives every decision)

Two weak signals combine into one strong one:

1. **Community recommendations are social proof from people who actually paid.** When
   an investor in a local Facebook group answers "who's a good plumber?" by naming a
   company, that's a referral backed by a real transaction, far better than an ad. The
   single strongest signal is **cross-validation**: when two or more different people
   independently name the same provider, call that one first.
2. **Public records are independent verification.** Reviews, license boards, BBB, and
   the company's own site confirm the provider exists, is licensed where required, is
   still in business, and serves the target area, none of which a forum post proves.

Neither alone is enough. A forum name with no paper trail might be someone's cousin; a
5-star Google result might be a national lead-gen shop that never picks up. The value of
this skill is doing **both** and being honest about what couldn't be confirmed.

## The workflow

Six phases. Don't skip Phase 0, and don't let Phase 2 (verification) slide: an
unverified directory is worse than no directory because someone will act on it.

### Phase 0: Scope it (ask before building)

Use `AskUserQuestion` to pin down what actually changes the work. At minimum:

- **Market / geography**: the county, city, or metro (and any secondary counties). This
  becomes the "Serves <market>" column and the removal test in Phase 3.
- **The use case**: what is this directory *for*? This determines the **role/category
  taxonomy** you must cover (a house-flip crew is not a rental-turnover bench, and
  neither is a wedding vendor list). See `references/use-case-taxonomies.md`.
- **The community source(s)** to mine: which Facebook group / forum / subreddit / Slack
  / referral thread, and whether you can reach it (a private group needs the user's
  logged-in browser; confirm access or fall back to public sources).
- **Deliverable + vetting priorities**: usually the Excel directory; ask what to weight
  (experience, reliability/reviews, price, licensing) so ranking reflects their world.

If the session is unattended or the user says "just go," make the reasonable call, state
your assumptions at the top of your response, and proceed. Don't block on a question no
one is there to answer.

### Phase 1: Source from the community

Mine the named community first, because its recommendations are the highest-signal
input. Full techniques in **`references/sourcing-playbook.md`**; the essentials:

- Use the community's **in-group search**, once per category/trade term, rather than
  scrolling the feed. Harvest two things: providers advertising their own services
  (contact is right in the post) and **recommendation-request threads** where members
  name their people in the comments (open those threads to read the comments, because
  that's where the gold is).
- Capture for each: name/company, contact, category/trade, who recommended them, and
  sentiment. Flag anyone named by **two or more** people.
- If the group is thin for a category (specialty trades often are), note it and lean on
  public sourcing in Phase 2. That's expected, not a failure.

### Phase 2: Cross-verify against public records

This is the phase that makes the list trustworthy. Fan this out with subagents (one per
category cluster) so it's fast and each keeps a clean context. Full checklist in
**`references/vetting-checklist.md`**; for each provider confirm: real business, best
phone/email, website, **service area covers the target market**, rating + review *count*
(a 5.0 on 3 reviews is not a 4.8 on 300), license/credential status on the relevant
board, BBB, red flags, and a confidence read.

**The non-negotiable rule: never fabricate.** People will dial these numbers and sign
contracts off this sheet. If a phone, license number, email, or rating can't be
verified, write **"not found"**, never a plausible guess. Accuracy beats completeness
every time. This applies doubly when the user hands you a **found or AI-generated list**:
treat every field as a claim to verify, not a fact. In practice these lists routinely
contain wrong phone numbers, overstated capabilities, or businesses that don't exist in
the target market, and catching those *is* the deliverable.

### Phase 3: Market sweep, gap analysis, and the niche layer

Three checks that turn a pile of names into a usable bench:

- **Geography sweep.** Confirm each provider actually serves the target market. Cut the
  ones that don't (out-of-area, too far to realistically cover a secondary county).
  Mark metro-local providers whose coverage of a secondary area isn't published as
  "Confirm <area>" rather than dropping them: likely fine, just verify on the call.
- **Gap analysis.** Compare what you have against the full role taxonomy for the use case
  (`references/use-case-taxonomies.md`). Name the missing categories and fill them from
  public sources. Cover the whole job, not just the obvious trades.
- **The niche / obscure layer.** Most use cases have a hidden layer the obvious list
  misses: the specialty vendors *and* the non-vendor bodies that gate the work
  (licensing boards, permit offices, HOAs, and for utilities, the districts that own the
  main and set tap fees). Surfacing this is often the most valuable part. Put reference
  data like this in its own tab.

### Phase 4: Build the deliverable

Assemble everything into one JSON config and run the builder, and don't hand-format a
spreadsheet:

```bash
python scripts/build_directory.py <config.json> <output.xlsx>
```

The config schema and a runnable example are in `assets/` (`config_schema.md`,
`example_config.json`). The script produces the Directory + Top Picks + Methodology tabs
and any reference tabs you define, with consistent formatting, a filter, confidence and
"serves-market" color coding, and computed summary counts.

### Phase 5: Verify and deliver

Spot-check a few phone numbers and any license claims, sanity-check that nothing
fabricated slipped in, then deliver the file. Offer the natural next steps: expand a thin
category, geography-check against a new sub-market, or turn the top picks into a one-page
call sheet.

## A note on scale

Match effort to the ask. "Find me a couple good plumbers" is a light pass; "build my
whole flip crew for this county and vet everyone" warrants the full six phases with
fan-out verification. When in doubt on a sourcing/vetting task, lean thorough: the cost
of a wrong number in this deliverable is a real person's wasted afternoon.

## Reference material

Read these as the phase calls for them (don't front-load them all):

| File | Read it when | What's in it |
|---|---|---|
| `references/sourcing-playbook.md` | Phase 1 (and to keep a list fresh) | Community-mining mechanics (in-group search, recommendation-thread harvest, cross-validation), plus cutting-edge sourcing tactics (sub off an active operator, supplier pro-desks, permit data, adjacent communities). |
| `references/vetting-checklist.md` | Phase 2 & 3 | The per-provider verification checklist, where to find license/credential boards by domain, the geography sweep, how to catch AI-hallucinated lists, red flags, and the vetting-call questions. |
| `references/use-case-taxonomies.md` | Phase 0 & 3 | Ready-made role/category taxonomies (full flip crew, rental turnover, new build, general local-vendor sourcing), how to build one for any use case, and prompts for finding the niche/regulatory layer. |
| `assets/config_schema.md` | Phase 4 | The JSON config the builder reads, field by field. |
| `assets/example_config.json` | Phase 4 | A small worked example you can run to see the output shape. |
