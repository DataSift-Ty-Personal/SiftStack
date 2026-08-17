---
name: first-market-county-data
description: Find WHERE to pull county distress lists (probate, foreclosure, tax sale, tax delinquent, eviction, code violation, divorce) for any U.S. county, and HOW to extract, filter, normalize, and import them to a CRM. Use when a user asks where to get courthouse or county data, how to identify the right clerk, recorder, trustee, or assessor office, when a county portal is down and needs a workaround, when filing a public records or FOIA request for county data, or when you have a raw pulled list to OCR, dedupe, standardize addresses, map to the 7 canonical notice types, run NCOA and DNC hygiene, and format the CSV for CRM import. Distinct from probate-property-finder (which locates a parcel for a named decedent), sift-market-research (ZIP scoring and Market Finder reports), and deep-prospecting (which researches the living heir or decision-maker once you have a record). This skill stops at pulling and normalizing the raw county list.
---

# First-to-Market County Data Research

Find the exact offices, portals, and processes to pull first-to-market distress lists for any U.S. county, then extract, filter, normalize, and hand the data into the marketing pipeline.

## Table of Contents

Router sections:
- The Harder to Acquire Equals More Money Principle
- Moat Math: One Deal Pays For Everything
- Data Priority Pyramid
- When to Use This Skill
- Canonical Notice Types (7)
- Core Workflow
- Reference Files
- Foreclosure Filtering Rules (Critical)
- Difficulty and Freshness Framing
- Compliance and Legal Guardrails
- Handoff: Where This Data Goes Next
- Important Notes

## The Harder to Acquire Equals More Money Principle

The harder a data source is to acquire, the more money it makes you. CAPTCHAs, FOIA requests, courthouse visits, portal quirks, and data normalization are barriers, and every barrier is a moat. The investors who will not solve them never reach the seller. Pulling data directly from the county puts you 30-90 days ahead of the buyers who wait for that same record to show up in PropStream or BatchLeads. Reach the motivated seller first and you negotiate against far less competition.

## Moat Math: One Deal Pays For Everything

Frame these as illustrative ranges to verify, not fixed prices. Vendor pricing and competition shift over time.

| Source | Cost per Lead | Competition Level |
|--------|---------------|-------------------|
| Self-scraped first-to-market county data | $0.50-$2.00 (plus $0.10-$0.15 skip trace per record) | Very low |
| Nationwide aggregated data (DataSift, PropStream, BatchLeads) | $4.00-$8.00 | High |
| AI-enhanced / predictive data | $8.00-$15.00 | Moderate |

Worked break-even: pulling and skip tracing 1,000 first-to-market records costs roughly ($0.50-$2.00 x 1,000) plus ($0.10-$0.15 x 1,000), about $600-$2,150 all-in. A single wholesale assignment of $8,000-$15,000 repays the whole campaign many times over. Roughly one deal per 1,000 records makes the pipeline profitable. Tier-scored dialing then compounds the edge: dialing best numbers first lifts connect rates roughly 4-5x (from about 2-3% to about 9.5%) per the phone-validator skill, so reaching the seller first AND dialing the best number multiplies the advantage into more closed deals per dollar.

## Data Priority Pyramid

| Tier | Data Source | Examples |
|------|-------------|----------|
| Tier 1 (THIS SKILL) | First-to-market county data pulled directly from the county | Probate, foreclosure, tax sale, tax delinquent, eviction, code violation, divorce |
| Tier 2 | Nationwide aggregated data | DataSift, PropStream, BatchLeads |
| Tier 3 | AI-enhanced / predictive data | AI-scored and predictive lead products |

Always exhaust Tier 1 before moving to Tier 2 or Tier 3.

## When to Use This Skill

Trigger this skill when a user says things like:
- "Where do I pull foreclosure (or probate, tax sale, tax delinquent, eviction, code violation, divorce) leads for [county]?"
- "Which clerk, recorder, trustee, or assessor office holds [data type]?"
- "Is [state] judicial or nonjudicial, and where does the foreclosure record show up?"
- "The county portal is down. How else do I get this list?"
- "Draft a public records or FOIA request for county [data type]."
- "I pulled a raw list. Clean it, filter the real foreclosures, and get it ready for CRM import."

## Canonical Notice Types (7)

These seven types are the project-wide standard. All extraction, normalization, and CRM import maps to one of them. Note: a config.py NOTICE_TYPES list may show only foreclosure and probate, because the live web scraper currently runs just those two saved searches. The full taxonomy via the photo pipeline and the CRM is the 7 types below. Use 7, not 2. For office names per type, see `references/common-offices.md`.

| Priority | Notice Type | Key Fields | Contact (decision maker) | Marketing Window |
|----------|-------------|-----------|--------------------------|------------------|
| A | foreclosure | address, owner_name, auction_date, trustee (informational, not a pipeline-extracted field) | property owner (grantor/borrower who executed the deed of trust) | Days 1-30 from notice filing, before the auction date |
| A | tax_sale | address, owner_name, auction_date, delinquent_amount | property owner | 60-90 days before auction, after list publication |
| A | tax_delinquent | address, owner_name, delinquent_amount, years_delinquent | property owner | Ongoing: owner has bills but no auction date yet |
| A | probate | decedent_name, personal_representative, filing_date | Personal Representative / Executor / Administrator, NOT the deceased | 30-180 days from filing |
| B | eviction | plaintiff_name (landlord), defendant_name (tenant), property_address | plaintiff (landlord), NOT the defendant (tenant). 2 or more filings in 12 months marks a high-value multiple-eviction landlord | Immediate: the landlord is motivated now |
| B | code_violation | owner_name, violation_type, compliance_deadline, property_address | owner of record. Data is usually city-level, not county-level | Before the compliance deadline, typically 10-30 days |
| C | divorce | petitioner_name, respondent_name, property_address | both petitioner and respondent. Property appears on the schedule page | 60-180 days from filing |

Probate notices do not contain the decedent's property address. The PR mailing address is captured instead (where creditors send claims), and a separate property lookup fills the property address. Hand probate records to the probate-property-finder skill for that step.

## Core Workflow

1. Collect the target location: state, county, and city if code violations or municipal data are in scope.
2. Select the notice types to pull. Start with Priority A in the primary county, then add Priority B, then add counties before adding Priority C.
3. Look up the correct office per type in `references/common-offices.md` (clerk, recorder, trustee, assessor, or city code enforcement). Confirm judicial vs nonjudicial routing for foreclosure.
4. Run the matching prompt from `references/research-prompts.md` to find the live portal, FOIA contact, difficulty, and update frequency. Verify each URL before use.
5. Extract the records (portal scrape, published list, or FOIA export). Pick the execution mode that fits your volume and skills: (a) manual per-record lookup, the slowest path but no code required and fine for low volume; (b) the SiftStack scraper or an Apify-style actor; or (c) hire a developer or VA to build the scrape. CAPTCHA solving and OCR apply only to the automated path. See `references/extraction-normalization-import.md` for portal navigation steps.
6. Normalize to the canonical schema: one row per property, mapped to one of the 7 notice types, with the key fields for that type.
7. List hygiene and deliverability: run NCOA (National Change of Address, covers USPS moves in the last 48 months; skipping it can push undeliverable rates toward 20 percent), then UAA (Undeliverable As Addressed) and DSF (Delivery Sequence File) processing to catch the up-to-40-percent of moves never filed with USPS, then DNC (Do Not Call) suppression every 31 days and Do Not Mail suppression for direct mail. See `references/extraction-normalization-import.md` for the how.
8. Import to the CRM and tag every record `Courthouse Data` so it routes to the niche funnel.
9. Enrich property data and skip trace (adds phones and emails). Keep Enrich Owners and Swap Owners OFF to protect the PR/DM contact mapping.
10. Hand off down the chain (see Handoff section).

## Reference Files

- `references/research-prompts.md`: copy-paste research prompts by Priority A/B/C, plus portal-down fallback and FOIA drafting prompts. Load when you need ready prompts to run.
- `references/common-offices.md`: office names per data type, judicial vs nonjudicial routing, tax-sale mechanism by state, state aggregator portals. Load when you need to name the right office.
- `references/state-law-matrix.md`: full 50-state-plus-DC foreclosure process, tax-sale mechanism with redemption periods, and public-records statutes with response windows and fees. Load when working a state not covered in the worked examples.
- `references/extraction-normalization-import.md`: portal systems field guide, extraction methods, the normalization procedure, list hygiene detail, the CSV column spec, and CRM import steps. Load when you have raw data to clean and import.
- `references/worked-example.md`: two fully filled-in county examples (Knox County TN nonjudicial, Hillsborough County FL judicial). Load when you want a real model with every column populated.

## Foreclosure Filtering Rules (Critical)

Not all notices from a "Foreclosure" saved search or a raw courthouse foreclosure docket are actual first-to-market foreclosures. Filter for real trustee sale language. These lists match the canonical INCLUDE_PHRASES and EXCLUDE_PHRASES verbatim. Matching is case insensitive against the full notice text.

### Filter logic (order matters)

1. Non-foreclosure notice types (tax_sale, tax_delinquent, probate, eviction, code_violation, divorce) pass through unfiltered.
2. Check EXCLUDE phrases first. Exclusions take priority over inclusions. If any exclude phrase appears, drop the notice even when an include phrase also matches.
3. Check INCLUDE phrases. If one matches, keep the notice.
4. Special guard: the generic phrase "notice of sale" is accepted ONLY if the word "trustee" also appears somewhere in the notice text.
5. If no include phrase matches, drop the notice by default.

### INCLUDE: real first-to-market foreclosures (20 phrases)

Substitute trustee variants:
- "substitute trustee's notice of sale"
- "substitute trustee's sale"
- "substitute trustee's notice of foreclosure sale"
- "substitute trustee sale"
- "substituted trustee's sale"
- "substituted trustee sale"
- "notice of substitute trustee's sale"
- "notice of substitute trustee sale"

Successor trustee variants:
- "successor trustee's notice of sale"
- "successor trustee's sale"
- "successor trustee sale"

General trustee sale:
- "notice of trustee's sale"
- "notice of trustee's foreclosure sale"
- "notice of trustee sale"
- "trustee's sale"
- "trustee sale"

Default and foreclosure sale (no trustee guard; these are full INCLUDE matches on their own):
- "notice of default and foreclosure sale"
- "foreclosure sale notice"
- "notice of foreclosure sale"

Generic (accepted only if "trustee" also appears in the text):
- "notice of sale"

### EXCLUDE: not first-to-market foreclosures (9 phrases)

- "non-resident notice"
- "non resident notice"
- "nonresident notice"
- "order of publication"
- "notice to creditors"
- "notice of lien"
- "order to sell"
- "divorce"
- "dissolution"

Without this filter you waste marketing dollars on non-foreclosure records mixed into raw courthouse foreclosure dockets.

## Difficulty and Freshness Framing

Harder equals higher value. A "High" difficulty source means fewer competitors are pulling that data, which is exactly why it is worth the effort. Rate each source by access friction (open portal, login required, CAPTCHA, FOIA only, in-person only) and let high difficulty signal low competition, not a reason to skip. Use this rubric for the Low/Medium/High rating the output tables ask for:

| Rating | What makes a source this rating |
|--------|---------------------------------|
| Low | Online portal with an open search or a bulk export, no account or special request needed |
| Medium | Account registration, limited or per-record export, or an in-person pickup of an otherwise online list |
| High | Affidavit, physical courthouse visit, FOIA request, paywall, or an anti-data-mining restriction |

Freshness drives the marketing window. County portals, URLs, FOIA contacts, and publication schedules change. Re-verify each live URL and stated update frequency against actual courthouse filing dates on a regular cadence. Nonjudicial foreclosures move fast (roughly 60-180 days), so re-pull often. Judicial foreclosures can run well over 1,000 days, so a lead stays actionable far longer. Match pull cadence to process type.

Expected update cadence by notice type (a quick reference for how often each list refreshes):

| Notice type | Expected cadence |
|-------------|------------------|
| foreclosure, probate, eviction | Daily (court filing portals and published notice feeds) |
| code_violation | Weekly to monthly (city code dockets) |
| tax_delinquent | Monthly (rolling delinquent list) |
| tax_sale | Annual (sale list published 60-90 days before the auction) |
| divorce | Daily to weekly (court filing portal) |
| utility shut-offs and other FOIA-only data | On request (no live feed; pull via a records request) |

When a portal is down or a source looks stale, the ranked fallback list (call the clerk for a manual lookup or emailed PDF, check for a legacy backup portal, document a temporary outage and retry in 24-48 hours, then aggregators, newspaper of record, neighboring office, vendor mirror, and FOIA) lives in `references/research-prompts.md`.

## Compliance and Legal Guardrails

General information, not legal advice. Confirm current rules with a licensed attorney in the target state. Detail lives in `references/state-law-matrix.md`.

- FDCPA: Real estate investors buying property are generally not debt collectors under the FDCPA. In Obduskey v. McCarthy and Holthus LLP (2019), the Supreme Court held a business engaged in no more than nonjudicial foreclosure is not an FDCPA debt collector except for the limited purpose of section 1692f(6). Do not claim a blanket exemption. The analysis is fact specific.
- TCPA and DNC: Calls offering to buy someone's real estate are telephone solicitations subject to the National Do Not Call Registry. Scrub phone lists against the federal DNC registry at least every 31 days. Prerecorded or autodialed calls need prior express written consent. Statutory damages run $500-$1,500 per call. The FCC's one-to-one consent rule, scheduled for January 27, 2025, was vacated by the Eleventh Circuit on January 24, 2025 (Insurance Marketing Coalition Ltd. v. FCC) before it took effect, and the FCC then repealed it. Bundled prior express consent remains permissible as of this writing, but TCPA rules change, so confirm the current FCC rule and any state mini-TCPA before autodialing or texting.
- State foreclosure-consultant and equity-purchaser laws: Foreclosure outreach is regulated at the state level. Examples: California Civil Code 2945 (foreclosure consultants) and 1695 (Home Equity Sales Contracts Act), and Minnesota Chapter 325N (foreclosure solicitors and equity purchasers). Verify the target state's rules before mailing or calling foreclosure owners.

## Handoff: Where This Data Goes Next

After you pull and normalize first-to-market county data, the data does NOT stop here. Hand it off in this exact order so the chain works:

1. Run List Hygiene and Deliverability before import: NCOA scrub (USPS moves in the last 48 months; skipping it can push undeliverable rates toward 20 percent), then UAA and DSF processing to catch the up-to-40-percent of moves never filed with USPS, then DNC suppression every 31 days plus Do Not Mail suppression for direct mail.
2. Import to the DataSift CRM and tag every record `Courthouse Data` so it routes to the `00 Niche Sequential Marketing` folder (12 presets). The `Courthouse Data` tag is what prioritizes first-to-market records over bulk data in the filter presets; records WITHOUT that tag route to `01. Bulk Sequential Marketing` (9 presets) instead.
3. Enrich property data and skip trace inside the CRM (adds phone numbers and emails).
4. For probate records that have a decedent name but no property address, hand off to the probate-property-finder skill to discover the parcel (assessor name search, then executor family search, then people search). Get an address before deep prospecting.
5. For deceased owners, entity or LLC owners, or any record where skip trace returns no usable phone, hand off to the deep-prospecting skill for heir and decision-maker research (the L1-L3 research flow with L4 curative-title escalation, heir verification loop, DOD sanity check of 3 years).
6. Score every phone with the phone-validator skill (Trestle activity score), then dial in tier order: Dial First (81-100), Dial Second (61-80), Dial Third (41-60), Dial Fourth (21-40), Drop (0-20).
7. Run the records through the sequential-presets skill so the niche funnel (SMS, then call, then mail, then deep prospecting) processes them automatically.

Chain summary: pull and normalize (THIS SKILL), list hygiene and deliverability, import and tag `Courthouse Data`, enrich and skip trace, then probate-property-finder, then deep-prospecting, then phone-validator, then sequential-presets.

## Important Notes

- Deceased owners: the contact is the decision maker, not the deceased. Use the decision maker's first/last name and mailing address. Use probate-property-finder to locate the property, then deep-prospecting to find the living decision maker.
- Probate has no property address by default. Capture the PR mailing address (where creditors send claims) and route to probate-property-finder to fill the property address before deep prospecting.
- Verify every live URL before use, and confirm county data-use terms. Some county property sites carry an explicit anti-data-mining notice. Use official lookup endpoints, respect the terms, and do not assume an open bulk API.
