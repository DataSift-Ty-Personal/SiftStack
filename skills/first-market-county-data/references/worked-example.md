# Worked County Examples

These two examples show the finished deliverable, not a template. Every cell is filled with real office names, addresses, phones, and portal URLs, then carried all the way through import and handoff. This is the calibration point: when you research a new county, your output should look like one of these tables, fully populated, with no `[result]` placeholders left.

One county is non-judicial with a redeemable tax deed (Knox County, Tennessee). The other is judicial with a two-step tax-lien-then-deed process (Hillsborough County, Florida). Side by side they teach you how the same 7 notice types live in completely different offices depending on the state's foreclosure and tax-sale model.

Verify before use. Portal URLs, office room numbers, phone extensions, and county data-use terms change. Treat every URL below as a live link to confirm, and treat anything marked "verify locally" as a real gap to close before you build an automated pull. See state-law-matrix.md for the full 50-state classification, common-offices.md for the office-name patterns, extraction-normalization-import.md for the pull and CSV steps, and research-prompts.md for the copy-paste research prompts that produce these tables.

## Table of Contents

- [Worked Example A: Knox County, Tennessee](#worked-example-a-knox-county-tennessee)
- [Worked Example B: Hillsborough County, Florida](#worked-example-b-hillsborough-county-florida)
- [What These Two Examples Teach](#what-these-two-examples-teach)
- [End-to-End: Carrying Knox County Through the Full Lifecycle](#end-to-end-carrying-knox-county-through-the-full-lifecycle)

## Worked Example A: Knox County, Tennessee

Classification: non-judicial, deed-of-trust foreclosure state. Tax sale is a redeemable deed (12-month redemption; redemption cost is the bid plus 12 percent per annum interest under T.C.A. 67-5-2701, verify current county practice locally). Statewide legal-notice aggregator: tnpublicnotice.com. Public records statute: Tennessee Public Records Act (T.C.A. 10-7-503), which gives the records custodian 7 business days to produce, deny in writing, or state a written timeline.

| Data Type | Priority | Office Name | Address | Phone | Portal URL | FOIA Email | Difficulty | Update Freq | Cost | Marketing Window | Notes |
|-----------|----------|-------------|---------|-------|------------|------------|------------|-------------|------|------------------|-------|
| foreclosure | A | Trustee sale notices (no court docket). Published in newspapers and on the TN statewide aggregator | n/a (notices are published, not filed in a court index) | n/a | tnpublicnotice.com | n/a (published notices, no records request needed) | Medium | Daily | Subscription to aggregator plus reCAPTCHA solving | Days 1-30 from notice filing, before the sale | Filter for trustee sale language (substitute or successor trustee, notice of trustee sale). TN is non-judicial: there is no lis pendens to chase. First publication runs at least 20 days before the sale |
| probate | A | Probate Division, Knox County Chancery Court (Clerk and Master). Separate from the Chancery Division | 3rd Floor, City-County Building, Suite 352, 400 Main Avenue, Knoxville, TN 37902 | 865-215-2389 (Probate Division), 865-215-2555 (Clerk and Master main) | knoxcounty.org/chancery | verify locally (Clerk and Master records custodian) | Medium | Daily to weekly | Free online search, per-page copy fee | 30-180 days from filing | Contact is the Personal Representative, Executor, or Administrator, not the decedent. Probate records have no property address by default, hand off to probate-property-finder |
| tax_sale | A | Knox County Trustee plus Clerk and Master of Chancery Court (conducts the auction). Tax suit filed in Chancery Court | City-County Building, 400 Main Avenue, Knoxville, TN 37902 | 865-215-2305 (Trustee) | trustee.knoxcounty.org/services/tax-sale and kgis.org/TaxSale | verify locally (Clerk and Master records custodian) | Medium | Annually (tax suit filed Feb 1 to Apr 1, sale follows) | Free list, deposit required to bid | 60-90 days before auction, after list publication | Redeemable deed: former owner has 12 months to redeem by paying the bid plus 12 percent per annum interest (T.C.A. 67-5-2701); verify locally. Sale conducted by Clerk and Master |
| tax_delinquent | A | Knox County Trustee (current delinquencies) and Property Assessor parcel data | City-County Building, 400 Main Avenue, Knoxville, TN 37902 | 865-215-2305 (Trustee), 865-215-2362 (Assessor) | propertytax.knoxcountytn.gov, kgis.org | verify locally (Trustee records custodian) | High | Ongoing | Free lookup | Ongoing (owner owes but no auction date yet) | Verify locally: Knox County property sites carry an explicit anti-data-mining notice. Use the official lookup endpoints, respect the terms, do not assume an open bulk API |
| eviction | B | General Sessions Court, Fifth Sessions (Civil and Detainer) | Old Courthouse, 300 Main Street, Room 318, Knoxville, TN 37902 | 865-215-2518 | knoxcounty.org/circuit/civil_sessions_dockets.php | verify locally (General Sessions Civil records custodian) | Medium | Daily | Free to view, per-page copy fee | Immediate, landlord is motivated now | Detainer warrant equals eviction. Target the plaintiff (landlord), not the defendant (tenant). 2 or more filings in 12 months flags a multiple-eviction landlord |
| code_violation | B | City of Knoxville Codes Enforcement (city level, not county) | City of Knoxville, verify office address locally | verify locally | knoxvilletn.gov codes enforcement | verify locally (City of Knoxville records-request channel; this list is FOIA-only) | High | Weekly to monthly | Free to flat fee, FOIA likely | Before the compliance deadline (10-30 days typical) | Usually city-level. Often requires a public records request, online portals are uncommon |
| divorce | C | Knox County Circuit Court Clerk (Fourth Circuit handles domestic) | City-County Building, 400 Main Avenue, Knoxville, TN 37902 | verify locally | knoxcounty.org/circuit | verify locally (Circuit Court Clerk records custodian) | Medium | Daily | Free to view, per-page copy fee | 60-180 days from filing | Both petitioner and respondent are contacts. Real property listed on the schedule page. Lowest motivation, lowest competition |

Verify-locally flags for Knox County:

- The exact records-custodian email for the Clerk and Master (probate), General Sessions Civil (eviction), and the Circuit Court Clerk (divorce). The offices publish phone numbers, not always a dedicated FOIA inbox.
- The City of Knoxville Codes Enforcement office address and its records-request channel. Code enforcement is city-level here, not county-level, so a county FOIA will not reach it.
- The current Knox County data-use terms. The property tax lookup (propertytax.knoxcountytn.gov) and KGIS (kgis.org) carry an explicit anti-data-mining notice with penalty language. There is no documented open bulk API. Use the official lookup endpoints, respect the posted terms, and do not assume sanctioned bulk scraping.

## Worked Example B: Hillsborough County, Florida

Classification: judicial foreclosure state (Circuit Court, Chapter 702, recorded lis pendens under FS 48.23). Tax sale is a two-step tax-lien then tax-deed process (Chapter 197): annual tax certificate sale on or before June 1, then a 2-year wait before a tax deed application. Statewide legal-notice aggregator: floridapublicnotices.com (Florida Press Association). myfloridalegal.com is the Florida Attorney General consumer site, not a notice aggregator. Public records statute: Florida Sunshine Law (F.S. Ch. 119), with production and fee provisions at 119.07.

| Data Type | Priority | Office Name | Address | Phone | Portal URL | FOIA Email | Difficulty | Update Freq | Cost | Marketing Window | Notes |
|-----------|----------|-------------|---------|-------|------------|------------|------------|-------------|------|------------------|-------|
| foreclosure | A | Hillsborough County Clerk of Circuit Court, foreclosure sales (online auction via RealAuction) | 800 E. Twiggs Street, Tampa, FL 33602 | 813-276-8100 | hillsclerk.com/court-services/foreclosure-sales and hillsborough.realforeclose.com. Case search via HOVER (hover.hillsclerk.com) | n/a (online docket, no records request needed) | Low to Medium | Daily (auctions weekdays 10:00 a.m.) | Free to view, 5 percent deposit to bid | Days 1-30 from lis pendens filing, before the auction | Judicial state: track the recorded lis pendens plus the Clerk foreclosure docket. Do not search for trustee sale language here, there is no trustee sale in FL |
| probate | A | Hillsborough County Clerk of Circuit Court, Probate division | 800 E. Twiggs Street, Tampa, FL 33602 | 813-276-8100 | hillsclerk.com, records via HOVER (hover.hillsclerk.com) | n/a (online docket via HOVER) | Medium | Daily | Free to view, per-page copy fee | 30-180 days from filing | Florida names a Personal Representative (formal or summary administration). Contact the PR, not the decedent |
| tax_sale | A | Tax certificate sale: Hillsborough County Tax Collector (LienHub). Tax deed sale: Clerk of Circuit Court (RealTaxDeed) | Tax Collector: 601 E. Kennedy Blvd, Tampa, FL 33602. Clerk: 800 E. Twiggs Street, Tampa, FL 33602 | Tax Collector 813-635-5200, Clerk 813-276-8100 | Certificate sale: lienhub.com. Tax deed sale: hillsborough.realtaxdeed.com and hillsclerk.com/taxdeeds | n/a (published list and online auction) | Medium | Certificate sale annually on or before June 1. Tax deed sales Thursdays | Free list, deposit to bid | Certificate buyers bid 60-90 days out. Tax deed targets surface after the 2-year wait | Two-step: certificate sale (bid 18 percent down in 0.25 percent steps to lowest rate), then a tax deed application allowed after 2 years if unredeemed |
| tax_delinquent | A | Hillsborough County Tax Collector, delinquent property tax | 601 E. Kennedy Blvd, Tampa, FL 33602 | 813-635-5200 | hillstaxfl.gov | verify locally (Tax Collector public-records channel for the bulk file) | Medium | Annual (delinquent April 1, list advertised 3 consecutive weeks before the June certificate sale) | Free list | Ongoing, between April 1 delinquency and the June certificate sale | Taxes due Nov 1, delinquent after April 1. The advertised delinquent list is the FTM pull window before certificates sell |
| eviction | B | Hillsborough County Clerk of Circuit Court, County Civil (evictions up to $50,000) | 800 E. Twiggs Street, Tampa, FL 33602 | 813-276-8100 | hillsclerk.com/Court-Services/County-Civil, search via HOVER (hover.hillsclerk.com) | n/a (online docket via HOVER) | Low to Medium | Daily | Free to view, per-page copy fee | Immediate, landlord is motivated now | Target the plaintiff (landlord), not the defendant (tenant). HOVER searches by name, case number, or date |
| code_violation | B | City of Tampa Code Enforcement and Hillsborough County Code Enforcement (city and county level) | City of Tampa and Hillsborough County, verify office addresses locally | verify locally | tampa.gov code enforcement, hcfl.gov code enforcement | verify locally (City of Tampa records intake and HCFL.gov AtYourService for the county; FOIA-only) | High | Weekly to monthly | Free to flat fee, FOIA likely | Before the compliance deadline (10-30 days typical) | Split between City of Tampa (incorporated) and county (unincorporated). Often a public records request |
| divorce | C | Hillsborough County Clerk of Circuit Court, Family Law division | 800 E. Twiggs Street, Tampa, FL 33602 | 813-276-8100 | hillsclerk.com, records via HOVER (hover.hillsclerk.com) | n/a (online docket via HOVER) | Medium | Daily | Free to view, per-page copy fee | 60-180 days from filing | Both petitioner and respondent are contacts. Real property appears on the financial affidavit or marital asset schedule. Lowest motivation, lowest competition |

Verify-locally flags for Hillsborough County:

- The current public-records request channel (HCFL.gov AtYourService for the county, separate City of Tampa records intake for city code enforcement). Confirm the exact custodian inbox before filing.
- The City of Tampa versus county code-enforcement split. Incorporated Tampa addresses go to City of Tampa Code Enforcement, unincorporated addresses go to Hillsborough County Code Enforcement. You will pull from both to cover the whole county.
- Tax deed deposit terms and the exact tax deed sale day, which RealTaxDeed publishes per sale.

## What These Two Examples Teach

| Dimension | Knox County, TN (non-judicial) | Hillsborough County, FL (judicial) |
|-----------|-------------------------------|-------------------------------------|
| Foreclosure signal | Trustee sale notice published in newspapers and tnpublicnotice.com | Lis pendens recorded plus Clerk of Court foreclosure docket (RealAuction) |
| Foreclosure office | Trustee (private), no court index | Clerk of Circuit Court, Chapter 702 |
| Tax sale model | Redeemable deed, 12-month redemption at bid plus 12 percent per annum (T.C.A. 67-5-2701), verify locally | Tax lien then deed: certificate sale by June 1, 2-year wait, then tax deed |
| Tax sale venue | Clerk and Master of Chancery Court conducts the auction | Tax Collector (LienHub certificates), Clerk (RealTaxDeed deeds) |
| Probate venue | Probate Division of Chancery Court (Clerk and Master) | Clerk of Circuit Court, probate division |
| Eviction venue | General Sessions Court, Fifth Sessions (detainer warrant) | County Civil, Clerk of Circuit Court |
| Statewide aggregator | tnpublicnotice.com | floridapublicnotices.com (not myfloridalegal.com) |
| Records statute | Tennessee Public Records Act (T.C.A. 10-7-503) | Florida Sunshine Law (F.S. Ch. 119) |

The practical takeaway: in a non-judicial state you chase a published trustee sale notice and filter for the trustee sale language (see the INCLUDE and EXCLUDE phrase lists in extraction-normalization-import.md). In a judicial state there is no trustee sale notice to search for, so you track the recorded lis pendens plus the Clerk's foreclosure docket instead. Sending a judicial-state pull to look for "trustee sale" language returns nothing. Use state-law-matrix.md to classify any new county before you pick the office.

Anything marked verify locally above is a real gap: confirm the exact records-custodian email, the city versus county code-enforcement split, and the current Knox County data-use terms before building an automated pull.

## End-to-End: Carrying Knox County Through the Full Lifecycle

The CSV is the midpoint, not the finish line. Here is Knox County data carried all the way to a dialer-ready, campaign-routed list. Each named skill is a sibling reference you hand off to.

### Step 1: Extract

Pull each Priority A source first. Foreclosure and probate come from tnpublicnotice.com (the live web scraper runs those two saved searches). Tax sale, tax delinquent, eviction, code violation, and divorce come from the offices in the table above (some via portal, some via a Tennessee Public Records Act request with the 7 business day clock). Filter the foreclosure pull with the trustee sale INCLUDE and EXCLUDE phrase lists: exclusions are checked first and win over inclusions, and the generic phrase "notice of sale" is accepted only when the word "trustee" also appears in the notice text. Non-foreclosure notice types pass through unfiltered. See extraction-normalization-import.md for the full 20-phrase INCLUDE list and 9-phrase EXCLUDE list.

Keep all 7 canonical notice types: foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce. The project config lists only foreclosure and probate because the web scraper runs just those two saved searches, but the full taxonomy across the photo pipeline and the CRM is 7 types. Do not reduce to 2.

### Step 2: Normalize

Map every record to one notice type and standardize the columns: property address split into street, city, state, zip, owner or contact name split into first and last, and the contact mailing address. For deceased owners the contact mailing address is the decision maker's, not the property's. For probate, capture decedent_name, personal_representative, and filing_date even though there is no property address yet. See extraction-normalization-import.md for the full CSV column spec.

A finished normalized record is the actual hand-off deliverable, so here is what one looks like for two Knox County notice types. The names, addresses, and dates below are illustrative placeholders showing the shape, not real people, but the column mapping is exactly what you emit.

Foreclosure row (living owner, contact is the owner, mailing falls back to the property address if no separate mailing address exists):

| Column | Value |
|--------|-------|
| property_street | 123 Oak Ridge Dr |
| property_city | Knoxville |
| property_state | TN |
| property_zip | 37918 |
| owner_first_name | John |
| owner_last_name | Sample |
| mailing_street | 123 Oak Ridge Dr |
| mailing_city | Knoxville |
| mailing_state | TN |
| mailing_zip | 37918 |
| tags | Courthouse Data;foreclosure;Knox;2026-06;has_auction |
| lists | Foreclosure |
| notes | Substitute trustee sale, auction 2026-07-15, owner executed the deed of trust |

Probate row (deceased owner, contact is the Personal Representative at the PR mailing address, NOT the decedent or the property; property_street stays blank until probate-property-finder fills it):

| Column | Value |
|--------|-------|
| property_street | (blank, hand to probate-property-finder) |
| property_city | (blank) |
| property_state | TN |
| property_zip | (blank) |
| owner_first_name | Mary |
| owner_last_name | Example |
| mailing_street | 456 Birchwood Ln (the PR's address, where creditors send claims) |
| mailing_city | Knoxville |
| mailing_state | TN |
| mailing_zip | 37919 |
| tags | Courthouse Data;probate;Knox;2026-06;deceased |
| lists | Probate |
| notes | Estate opened 2026-05-20; PR is Mary Example; decedent Robert Example; no property address yet |
| owner_deceased | TRUE |
| decedent_name | Robert Example |
| personal_representative | Mary Example |

The probate row shows the single most common import mistake avoided: the mailing address is the PR's, never the decedent's old address and never the property address, because mailing a dead person at a vacant property burns the lead.

### Step 3: List Hygiene and Deliverability

Before import, scrub the list so you do not burn postage and dials on dead records:

- NCOA scrub. Run National Change of Address. It covers USPS moves in the last 48 months. Skipping it can push undeliverable rates toward 20 percent.
- UAA and DSF processing. Up to 40 percent of moves are never filed with USPS, so NCOA alone misses them. UAA (undeliverable as addressed) and DSF (Delivery Sequence File) flag the rest.
- Suppression. Scrub phones against the federal Do Not Call registry every 31 days, and suppress against a Do Not Mail file for any direct mail drop.

### Step 4: Import and Tag Courthouse Data

Import the normalized CSV to the DataSift CRM and tag every record `Courthouse Data`. That tag routes the records into the `00 Niche Sequential Marketing` folder (12 presets), which is the first-to-market funnel, and is what prioritizes first-to-market records over bulk data in the filter presets. Records WITHOUT the `Courthouse Data` tag route to `01. Bulk Sequential Marketing` (9 presets) instead. Also carry the notice_type as a list so each record lands in its niche list (Foreclosure, Probate, Tax Sale, and so on).

### Step 5: Enrich and Skip Trace

Run Enrich Property Information to add property data (beds, baths, value, sale history), with Enrich Owners and Swap Owners OFF so the enrichment does not overwrite your Personal Representative and decision-maker contact mapping. Then Skip Trace to pull phone numbers and emails.

### Step 6: Hand Off Address-less Probate to probate-property-finder

Knox probate records have a decedent name and a Personal Representative but no property address. Hand those records to the **probate-property-finder** skill, which runs a 3-tier lookup (assessor name search, then executor family search, then people search) to find the parcel. Get a property address before any deep prospecting.

### Step 7: Route Deceased and Entity Owners to deep-prospecting

For deceased owners, LLC or trust or corporate owners, or any record where skip trace returned no usable phone, hand off to the **deep-prospecting** skill for heir and decision-maker research. It runs the L1-L3 research flow (with L4 curative-title escalation when all heirs are deceased through 3 generations or title is clouded), the heir verification loop, and the DOD sanity check (reject an obituary whose date of death is more than 3 years before the filing date). The output is a living decision-maker with a contact path.

### Step 8: Score Phones with phone-validator

Send every returned phone to the **phone-validator** skill (Trestle activity score), then dial in tier order:

| Activity Score | Tag |
|----------------|-----|
| 81-100 | Dial First |
| 61-80 | Dial Second |
| 41-60 | Dial Third |
| 21-40 | Dial Fourth |
| 0-20 | Drop |

Tier-scored dialing lifts connect rates substantially over dialing an unscored list, which multiplies the first-to-market timing advantage.

### Step 9: Route via sequential-presets

Finally, run the records through the **sequential-presets** skill so the niche funnel processes them automatically: SMS first, then call, then mail, then deep prospecting. Because every record carries the `Courthouse Data` tag, the 12 niche presets in `00 Niche Sequential Marketing` pick them up ahead of bulk data.

Chain summary: extract and filter (THIS SKILL), normalize, list hygiene, import and tag `Courthouse Data`, enrich and skip trace, then probate-property-finder (address-less probate), then deep-prospecting (deceased and entity owners, the L1-L3 research flow with L4 curative-title escalation), then phone-validator (Trestle scoring with the tier bands), then sequential-presets (niche sequential marketing). The county pull is the front of a value chain, not an endpoint.

This worked example assumes the Data Priority Pyramid: exhaust Tier 1 first-to-market county data (this skill) before paying for Tier 2 nationwide aggregated data (DataSift, PropStream, BatchLeads) or Tier 3 AI-enhanced and predictive data. Self-scraped first-to-market records run roughly $0.50-$2.00 each plus $0.10-$0.15 for skip trace, against roughly $4.00-$8.00 for aggregated and $8.00-$15.00 for AI or predictive data (illustrative ranges, verify current vendor pricing).
