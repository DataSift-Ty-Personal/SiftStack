# Common Office Names by Data Type

Office names vary by jurisdiction. A "foreclosure" notice can live with a Trustee in one state and a Circuit Court Clerk in another. A "tax sale" can be run by a County Treasurer, a Tax Collector, or a Clerk and Master of Chancery Court depending on the state. Use the lists below as search starting points, then confirm the exact office name for your target county before you build a pull.

This file gives you, per data type:

- Common office names (what the office is usually called)
- What to search (the exact query strings and notice-title language to look for)
- Contact and access notes (who the decision maker is, and how the records are usually released)

For the full per-state breakdown of foreclosure process (judicial vs nonjudicial), security instrument (deed of trust vs mortgage), tax-sale type with redemption window, and public-records statute with response window, see state-law-matrix.md. This file keeps the quick office lists; state-law-matrix.md holds the authoritative per-state detail.

---

## Table of Contents

1. [Priority A: Core Lists](#priority-a-core-lists)
   - [Foreclosure](#foreclosure-priority-a)
   - [Probate](#probate-priority-a)
   - [Tax Sale](#tax-sale-priority-a)
   - [Tax Delinquent](#tax-delinquent-priority-a)
2. [Priority B: Standard Lists](#priority-b-standard-lists)
   - [Eviction](#eviction-priority-b)
   - [Code Violation](#code-violation-priority-b)
3. [Priority C: Extended Lists](#priority-c-extended-lists)
   - [Divorce](#divorce-priority-c)
   - [Extended Type Office Names](#extended-type-office-names)
4. [State-Specific Quick Notes](#state-specific-quick-notes)
   - [State-Level Aggregator Portals](#state-level-aggregator-portals)
5. [The 7 Canonical Notice Types and Contact Logic](#the-7-canonical-notice-types-and-contact-logic)
6. [Foreclosure Filtering Rules (Critical)](#foreclosure-filtering-rules-critical)
7. [Where This Goes Next (Handoff Chain)](#where-this-goes-next-handoff-chain)
8. [Related References](#related-references)

---

## Priority A: Core Lists

Priority A is the highest-motivation, most-distressed data. Pull these first in your primary county before adding Priority B or Priority C. The contact (decision maker) and marketing window are noted per type and match the canonical taxonomy.

### Foreclosure (Priority A)

The office depends entirely on whether your state forecloses in court (judicial) or out of court (nonjudicial). See state-law-matrix.md for the full 50-state-plus-DC classification. The short version:

- Nonjudicial states (deed of trust, power of sale): the first public record is a recorder-filed Notice of Default or a published trustee sale notice. Pull the recorder feed or the state public-notice aggregator.
- Judicial states (mortgage): the first public record is a court-filed lis pendens or foreclosure complaint. Pull the court civil docket.

Common office names:

- County Recorder, Register of Deeds, or County Clerk (nonjudicial states, where the trustee sale notice or Notice of Default is recorded)
- Substitute or Successor Trustee (private party in nonjudicial states such as Tennessee, Texas, California; there is no court index, the notice is published). Georgia is also nonjudicial and power-of-sale, but its security instrument is a security deed (deed to secure debt, O.C.G.A. 44-14), not a deed of trust; the foreclosing party is the secured creditor or its attorney rather than a separate trustee
- County Public Trustee (Colorado only: a county government office that handles the Notice of Election and Demand)
- Clerk of the Circuit Court or Clerk of Superior Court (judicial states such as Florida, Illinois, New York, Ohio, where the foreclosure complaint and lis pendens are filed)
- State or county public-notice aggregator (where one exists, see state-law-matrix.md)

What to search (notice-title language and query strings):

- "notice of trustee's sale"
- "substitute trustee's sale"
- "successor trustee's sale"
- "notice of substitute trustee's sale"
- "notice of foreclosure sale"
- "notice of default and foreclosure sale"
- "lis pendens" (judicial states, the recorded notice of pending suit)
- "foreclosure complaint" or "complaint to foreclose mortgage" (judicial states)
- "notice of election and demand" (Colorado)
- "power of sale" (deed-of-trust states, the clause that authorizes nonjudicial foreclosure)

Contact and access notes:

- Decision maker: the property owner (the grantor or borrower who executed the deed of trust or mortgage). In foreclosure notices the owner name typically appears after "executed by" in the deed-of-trust language.
- Marketing window: Days 1-30 from notice filing, before the auction date.
- Apply the foreclosure filter (see "Foreclosure Filtering Rules" below) so you do not pay to market non-foreclosure records mixed into a raw courthouse foreclosure docket.
- Judicial-state caution: do not search for trustee sale language as the primary signal in a mortgage state. There is no trustee sale. Track the recorded lis pendens and the Clerk of Court foreclosure docket instead.

### Probate (Priority A)

Common office names:

- Probate Court (standalone probate court, common in Georgia, Texas, Alabama, Connecticut)
- Surrogate's Court (New York, New Jersey)
- Orphans' Court (Pennsylvania, Maryland)
- Register of Wills (Pennsylvania, Maryland, Delaware: the office that admits the will and opens the estate)
- Clerk of the Circuit Court, Probate Division (Florida, Virginia, Illinois)
- Chancery Court, Probate Division, Clerk and Master (Tennessee: probate runs through Chancery in many counties)
- Superior Court, Probate Division (California, North Carolina)

What to search:

- "notice to creditors" (the published notice that opens the claim period; note this phrase is an EXCLUDE term in the foreclosure filter but is the right search for probate)
- "letters testamentary" (issued to an executor named in a will)
- "letters of administration" (issued to an administrator when there is no will)
- "petition for probate" or "petition to administer estate"
- "estate of [decedent name]"
- "personal representative" or "executor" or "administrator"

Contact and access notes:

- Decision maker: the Personal Representative, Executor, or Administrator, NOT the deceased. This is the single most common beginner error. The notice names a decedent and a PR; you market to the PR.
- Marketing window: 30-180 days from filing.
- Probate notices do not contain the decedent's property address by default. The PR mailing address is captured instead (where creditors send claims). A separate property lookup fills the property address. Hand address-less probate records to the probate-property-finder skill for that step.

### Tax Sale (Priority A)

The office depends on the state's tax-sale mechanism (tax lien, tax deed, or redeemable deed). See state-law-matrix.md for the full per-state mechanism and redemption window.

Common office names:

- County Treasurer (most tax-lien and many tax-deed states)
- County Tax Collector (Florida, California, and most Southern and Western states)
- Clerk and Master of the Chancery Court (Tennessee: conducts the auction after the County Trustee handles delinquent collection and a tax suit is filed in Chancery)
- County Trustee (Tennessee: handles delinquent collection before suit)
- Sheriff (some states route the tax sale through the Sheriff)
- Comptroller or Revenue Commissioner (some Southern states)

What to search:

- "notice of tax sale" or "delinquent tax sale"
- "tax lien certificate sale" or "tax certificate sale" (lien states and Florida's first step)
- "tax deed sale" (deed states and Florida's second step)
- "notice of sale of land for delinquent taxes"
- "tax foreclosure" (some county-by-county deed states such as parts of Ohio)
- "redemption period" or "right of redemption" (to confirm the window in redeemable-deed states)

Contact and access notes:

- Decision maker: the property owner of record.
- Marketing window: 60-90 days before auction, after the list is published.
- The list is usually published as a newspaper legal notice, often once a week for 3-4 successive weeks before the sale. The same list is typically available from the treasurer, collector, or chancery clerk as a published file. Ask for the file and its release schedule.
- Florida is a two-step process: a tax certificate (lien) sale first, then a tax deed sale only if the lien is unredeemed after roughly 2 years. Texas is a redeemable deed state (6 months non-homestead, 2 years homestead and agricultural), not a clean deed state. See state-law-matrix.md.

### Tax Delinquent (Priority A)

Common office names:

- County Treasurer (delinquent real property tax)
- County Tax Collector
- County Trustee (Tennessee)
- Property Assessor or Appraiser (parcel data and owner of record, used to cross-reference the delinquent list)
- Revenue Commissioner or Comptroller (some Southern states)

What to search:

- "delinquent tax list" or "delinquent property tax"
- "tax delinquent properties"
- "past due property taxes"
- "list of delinquent taxpayers" (some states publish an annual list)

Contact and access notes:

- Decision maker: the property owner of record.
- Marketing window: ongoing. The owner has bills owed but no auction date has been set yet, which is exactly why this is an early, low-competition signal.
- Some county property sites carry an explicit anti-data-mining notice with civil or criminal penalty language and no documented public developer API. Use the official lookup endpoints, respect the terms, and verify the current data-use terms before you assume bulk extraction is sanctioned.

---

## Priority B: Standard Lists

Priority B data is strong but a step below Priority A on either motivation or accessibility. Pull these after your Priority A pipelines are running in your primary county.

### Eviction (Priority B)

Common office names:

- General Sessions Court (Tennessee: the civil and detainer division handles evictions)
- County Civil division of the Clerk of the Circuit Court (Florida: evictions up to a dollar threshold)
- Justice of the Peace Court or Justice Court (Texas: JP courts handle evictions)
- District Court or County Court (varies by state)
- Magistrate Court or Small Claims Court (some states)
- Landlord-Tenant Court or Housing Court (large metros such as New York City)

What to search (the legal term for eviction varies widely by state, search all of these):

- "forcible entry and detainer" (the classic common-law term, used in many states)
- "unlawful detainer" (California, Virginia, and others)
- "detainer warrant" (Tennessee)
- "forcible detainer" (Texas, Illinois)
- "eviction" or "eviction complaint"
- "writ of possession" (the order that actually removes the tenant)
- "summary ejectment" (North Carolina)
- "summary process" (Massachusetts, Connecticut)

Contact and access notes:

- Decision maker: the plaintiff (the landlord), NOT the defendant (the tenant). You are marketing to the property owner who is dealing with a problem tenant, not to the tenant being removed.
- A landlord with 2 or more eviction filings in 12 months is a high-value multiple-eviction landlord (a likely tired landlord who may sell).
- Marketing window: immediate. The landlord is motivated now.

### Code Violation (Priority B)

Common office names:

- City Codes Enforcement or Code Enforcement Division (this data is usually city-level, not county-level)
- Department of Code Compliance
- Building and Safety Department or Department of Buildings
- Neighborhood Services or Property Standards
- County Code Enforcement (only for unincorporated areas outside any city limits)

What to search:

- "notice of violation" or "code violation"
- "property maintenance violation"
- "unsafe structure" or "condemned property"
- "compliance deadline" or "notice to comply"
- "abatement" or "nuisance abatement"
- "demolition order"

Contact and access notes:

- Decision maker: the owner of record. Pull the current owner from the assessor, because the violation record may list a stale owner.
- Data is usually city-level, not county-level. In a split metro you may need both the city and the county. There is often no online index, so a public records request (FOIA) is common. See research-prompts.md for the FOIA request language.
- Marketing window: before the compliance deadline, typically 10-30 days.

---

## Priority C: Extended Lists

Priority C is lower motivation or higher effort. Add these after you have added more counties for Priority A and B. Add Priority C last.

### Divorce (Priority C)

Common office names:

- Circuit Court Clerk, Domestic Relations or Family division (Tennessee, Virginia, Illinois)
- Family Court (many states have a dedicated family court)
- Superior Court, Family Law division (California, Georgia, North Carolina)
- District Court (states without a separate family court)
- Clerk of the Circuit Court, Family Law division (Florida)
- Chancery Court (some Southern states route divorce through Chancery)

What to search:

- "petition for divorce" or "complaint for divorce"
- "dissolution of marriage" (note: "divorce" and "dissolution" are EXCLUDE terms in the foreclosure filter, but they are the correct search for this data type)
- "legal separation"
- "marital settlement agreement" or "marital dissolution agreement"
- "financial affidavit" or "marital asset schedule" (where the real property is listed)

Contact and access notes:

- Decision maker: both the petitioner and the respondent. Real property appears on the schedule page (the financial affidavit or marital asset schedule).
- Marketing window: 60-180 days from filing.
- Lowest motivation and lowest competition of the seven core types. Many investors never pull this, which is part of the value.

### Extended Type Office Names

Beyond the seven core types, these extended distress signals are worth pulling once your core pipelines run. Most are FOIA-only or city-level with no online index. See state-law-matrix.md for the public-records statute and response window per state, and research-prompts.md for the request language.

| Extended Type | Common Office | What to Search |
|---------------|---------------|----------------|
| Condemned and unsafe structures | City Code Enforcement, Building Department | "condemned", "unsafe structure", "demolition order" |
| Mechanic's liens | County Recorder, Register of Deeds | "mechanic's lien", "notice of lien", "claim of lien" |
| IRS and state tax liens | County Recorder, Secretary of State | "federal tax lien", "notice of state tax lien" |
| HOA and condo liens | County Recorder | "HOA lien", "assessment lien", "claim of lien" |
| Utility shut-offs | City Utility or Water Department (FOIA) | "shut-off list", "delinquent utility account" |
| Lis pendens (general) | County Recorder, Clerk of Court | "lis pendens", "notice of pendency of action" |
| Building permits | City Building Department | "permit issued", "permit expired", "expired permit" |
| Mold, asbestos, lead | City or County Health Department (FOIA) | "lead hazard", "asbestos abatement", "mold remediation" |
| Fire and storm damage | Fire Marshal, County Emergency Management (FOIA) | "fire damage report", "storm damage assessment" |
| Sinkhole reports | County or State Geological Survey, insurer filings | "sinkhole claim", "subsidence report" |
| Medicaid estate recovery liens | State Medicaid agency, County Recorder | "estate recovery lien", "TEFRA lien" |
| Child-support liens | County Recorder, State child-support agency | "child support lien", "support judgment lien" |
| Quiet-title actions | Clerk of Court, civil docket | "quiet title", "action to quiet title" |

---

## State-Specific Quick Notes

These are quick starting points. For the full per-state breakdown of foreclosure process, security instrument, tax-sale type with redemption, and public-records statute with response window, see state-law-matrix.md.

### State-Level Aggregator Portals

Some states aggregate county legal notices into a single site. Check for one first, because it lets you pull many counties from one source.

| State | Aggregator | Data Types |
|-------|-----------|------------|
| Tennessee | tnpublicnotice.com | Foreclosures, probates, tax sales, tax delinquent (statewide) |
| Florida | floridapublicnotices.com | Foreclosure sale notices, notices of default, probates, tax deed sales (statewide, run by the Florida Press Association) |
| Texas | No statewide aggregator. Start at the county clerk and recorder, or use re:SearchTX for court records | Court records statewide via re:SearchTX |
| California | No statewide aggregator. Start at the county recorder for trustee sale notices | County recorder |
| Ohio | County-level portals only | County clerk of courts |

Caution: myfloridalegal.com is the Florida Attorney General consumer-protection site, not a legal-notice aggregator. Do not use it as a data source. The correct Florida statewide aggregator is floridapublicnotices.com.

Verify before use: portal URLs and county data-use terms change. Confirm any live URL above is current, and read the county data-use terms before building an automated pull.

For the full classification of every state's foreclosure path, security instrument, tax-sale mechanism, and records statute, do not restate partial lists here. Open state-law-matrix.md, which carries the authoritative tables.

---

## The 7 Canonical Notice Types and Contact Logic

There are seven canonical notice types. All extraction, normalization, and CRM import maps to one of these. Do not reduce the skill to two types.

| Priority | Notice Type | Key Fields | Contact (decision maker) | Marketing Window |
|----------|-------------|-----------|--------------------------|------------------|
| A | foreclosure | address, owner_name, auction_date, trustee (informational) | property owner (grantor or borrower who executed the deed of trust) | Days 1-30 from notice filing, before the auction date |
| A | tax_sale | address, owner_name, auction_date, delinquent_amount | property owner of record | 60-90 days before auction, after list publication |
| A | tax_delinquent | address, owner_name, delinquent_amount, years_delinquent | property owner of record | Ongoing: owner has bills but no auction date yet |
| A | probate | decedent_name, personal_representative, filing_date | Personal Representative, Executor, or Administrator, NOT the deceased | 30-180 days from filing |
| B | eviction | plaintiff_name (landlord), defendant_name (tenant), property_address | plaintiff (landlord), NOT the defendant (tenant). 2 or more filings in 12 months marks a high-value multiple-eviction landlord | Immediate: the landlord is motivated now |
| B | code_violation | owner_name, violation_type, compliance_deadline, property_address | owner of record. Data is usually city-level, not county-level | Before the compliance deadline, typically 10-30 days |
| C | divorce | petitioner_name, respondent_name, property_address | both petitioner and respondent. Property appears on the schedule page | 60-180 days from filing |

Note on the count: a config file in the source project lists only foreclosure and probate, because the live web scraper currently runs just those two saved searches. The project-wide taxonomy via the photo pipeline and the CRM is the 7 types above. Use 7, not 2.

The `trustee` field on the foreclosure row is informational (the named trustee on the notice), not a separately scored pipeline field. The load-bearing foreclosure fields are address, owner_name, and auction_date.

---

## Foreclosure Filtering Rules (Critical)

Not all notices from a "Foreclosure" saved search or a raw courthouse foreclosure docket are actual first-to-market foreclosures. Filter for real trustee sale language. These lists match the canonical foreclosure filter (INCLUDE_PHRASES and EXCLUDE_PHRASES) verbatim. Matching is case insensitive against the full notice text.

### Filter logic (order matters)

1. Non-foreclosure notice types (tax_sale, tax_delinquent, probate, eviction, code_violation, divorce) pass through unfiltered.
2. Check EXCLUDE phrases first. Exclusions take priority over inclusions. If any exclude phrase appears, drop the notice even when it also matches an include phrase.
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

Default and foreclosure sale:

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

---

## Where This Goes Next (Handoff Chain)

Pulling the right office is the front of a value chain, not the end. After you pull and normalize first-to-market county data, route it in this exact order:

1. Run the data through List Hygiene and Deliverability: NCOA scrub (USPS moves in the last 48 months; skipping it can push undeliverable rates toward 20 percent), UAA and DSF processing to catch the up-to-40-percent of moves never filed with USPS, and DNC suppression every 31 days plus Do Not Mail suppression for direct mail.
2. Import to the DataSift CRM and tag every record `Courthouse Data`, which routes it to the `00 Niche Sequential Marketing` folder (12 presets). The `Courthouse Data` tag is what prioritizes first-to-market records over bulk data in the filter presets; records WITHOUT that tag route to `01. Bulk Sequential Marketing` (9 presets) instead.
3. Enrich property data and skip trace inside the CRM (adds phones and emails).
4. Hand address-less probate records to the probate-property-finder skill to discover the parcel before deep prospecting.
5. Hand deceased owners, entity or LLC owners, and any record where skip trace returns no usable phone to the deep-prospecting skill for heir and decision-maker research (the L1-L3 research flow with L4 curative-title escalation, heir verification loop, DOD sanity check of 3 years).
6. Score every phone with the phone-validator skill (Trestle activity score), then dial in tier order: Dial First (81-100), Dial Second (61-80), Dial Third (41-60), Dial Fourth (21-40), Drop (0-20).
7. Run the records through the sequential-presets skill so the niche funnel (SMS, then call, then mail, then deep prospecting) processes them automatically.

---

## Related References

- research-prompts.md: copy-paste research and FOIA prompts per data type and priority tier. Load this when you have the office name and need the exact query to run or the FOIA request language to file.
- state-law-matrix.md: the authoritative per-state tables for foreclosure process (judicial vs nonjudicial), security instrument (deed of trust vs mortgage), tax-sale mechanism with redemption window, and public-records statute with response window. Load this when your target state is not Tennessee or you need the full classification rather than the quick office list.
- worked-example.md: fully filled-in Knox County, Tennessee and Hillsborough County, Florida examples showing every column for all seven data types. Load this when you want a concrete model of a finished source list, including the non-judicial vs judicial foreclosure split in practice.
