# Research Prompts for County Data Sources

Copy-paste research prompts for every notice type, plus FOIA request prompts and portal-down fallback prompts. Drop the prompt into any capable research model (or hand it to a human researcher), replace the placeholders, and run it. Office names, portal URLs, and statutes vary by jurisdiction, so treat every result as a search starting point and verify before use.

Placeholder convention: square brackets are fill-in variables. Replace `[COUNTY]`, `[STATE]`, `[NOTICE_TYPE]`, `[AMOUNT]`, and similar before running. Do not leave brackets in a live request. The fully filled-in output for a real county lives in worked-example.md, not here. This file shows the prompt templates with `[result]`-style placeholders; worked-example.md shows the completed rows.

Cross-reference companions:
- common-offices.md: the office-name lists, "what to search" query strings, judicial vs non-judicial routing, tax-sale mechanism by state, and portal vendor field guide.
- state-law-matrix.md: the 50-state-plus-DC foreclosure process table, tax-sale buckets and redemption windows, and the per-state FOIA response windows and fee rules.
- worked-example.md: two fully completed county pulls (Knox County TN, non-judicial, and Hillsborough County FL, judicial) with every column filled in.

## Table of Contents

- [Priority A: Core Lists](#priority-a-core-lists)
- [Priority B: Standard Lists](#priority-b-standard-lists)
- [Priority C: Extended Lists](#priority-c-extended-lists)
- [FOIA Request Prompts](#foia-request-prompts)
- [Fallback and Troubleshooting Prompts](#fallback-and-troubleshooting-prompts)

The 7 canonical notice types are foreclosure, tax_sale, tax_delinquent, probate (Priority A), eviction, code_violation (Priority B), and divorce (Priority C). A live config may list only foreclosure and probate because the web scraper runs just those two saved searches, but the project-wide taxonomy across the photo pipeline and the CRM is the full 7 types. Use 7, not 2. Pull Priority A in your primary county first, then Priority B, then add counties before reaching for Priority C.

---

## Priority A: Core Lists

Highest seller motivation, most distress. Pull these first.

### Foreclosure

```
Act as a public-records researcher. For [COUNTY] County, [STATE], find where new foreclosure
notices first appear and exactly how to pull them.

First classify the state: judicial or non-judicial foreclosure (see state-law-matrix.md).
- Non-judicial / deed-of-trust state: the first public signal is a recorder-filed Notice of
  Default or a published trustee sale notice. Tell me the recorder feed and any statewide
  legal-notice aggregator (for example tnpublicnotice.com in Tennessee). Do NOT send me to look
  for a court lis pendens; there is no court docket to chase.
- Judicial / mortgage state: the first public signal is a court-filed lis pendens or
  foreclosure complaint. Tell me the Clerk of Court civil docket and the recorded lis pendens
  office. Do NOT tell me to search for "trustee sale" language; there is no trustee sale.
- "Either" state (lender picks the instrument): tell me to check BOTH the recorder
  trustee-sale feed and the court docket until the county's common path is confirmed.

Return: exact office name, physical address, phone, online portal URL (label it "verify before
use"), public-records contact, the vendor/portal system if known (Tyler Odyssey, Landmark Web,
Fidlar, etc., see common-offices.md), bulk-export availability, update frequency, access cost,
and the marketing window.

Marketing window: Days 1-30 from the notice filing, before the auction date.
Key fields to capture: address, owner_name, auction_date, trustee (trustee name is
informational, not a required pipeline field).
Contact / decision maker: the property owner (the grantor or borrower who executed the deed of
trust). In a judicial state, the named defendant borrower.
```

After extraction, apply these foreclosure filtering rules (these match the canonical filter, see SKILL.md for the grouped source list):

- EXCLUDE first (exclusions take priority). Drop the notice if it contains any of: "non-resident notice", "non resident notice", "nonresident notice", "order of publication", "notice to creditors", "notice of lien", "order to sell", "divorce", "dissolution".
- INCLUDE only notices containing real trustee sale language. The full canonical list is 20 phrases across five families: substitute trustee variants ("substitute trustee's notice of sale", "substitute trustee's sale", "substitute trustee's notice of foreclosure sale", "substitute trustee sale", "substituted trustee's sale", "substituted trustee sale", "notice of substitute trustee's sale", "notice of substitute trustee sale"), successor trustee ("successor trustee's notice of sale", "successor trustee's sale", "successor trustee sale"), general trustee sale ("notice of trustee's sale", "notice of trustee's foreclosure sale", "notice of trustee sale", "trustee's sale", "trustee sale"), default and foreclosure sale ("notice of default and foreclosure sale", "foreclosure sale notice", "notice of foreclosure sale"), and the generic "notice of sale".
- The generic "notice of sale" is accepted ONLY if the word "trustee" also appears somewhere in the text.
- If no include phrase matches, drop the notice.

Full canonical phrase reference (matches the filter exactly, case insensitive against the full notice text):

INCLUDE (20 phrases):

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

EXCLUDE (9 phrases):
- "non-resident notice"
- "non resident notice"
- "nonresident notice"
- "order of publication"
- "notice to creditors"
- "notice of lien"
- "order to sell"
- "divorce"
- "dissolution"

Filter precedence in one sentence: exclusions are checked first and take priority over inclusions, so a notice matching any exclude phrase is dropped even if it also matches an include phrase; the generic "notice of sale" is accepted only if the word "trustee" also appears in the text; non-foreclosure notice types pass through the filter unfiltered. Without this filter you waste marketing dollars on non-foreclosure records mixed into raw courthouse foreclosure dockets.

### Probate

```
Act as a public-records researcher. For [COUNTY] County, [STATE], find where newly opened
probate / estate cases are recorded and exactly how to pull them.

Return: exact court name (probate court, surrogate's court, orphans' court, or chancery
probate division depending on the state), physical address, phone, online case-search portal
URL (label "verify before use"), public-records contact, the vendor/portal system if known
(Tyler Odyssey, re:SearchTX, etc.), bulk-export availability, update frequency, access cost,
and the marketing window. Tell me the exact case-type filter to select (for example "Estate"
or "Probate") and the date-range field.

Marketing window: 30-180 days from filing.
Key fields to capture: decedent_name, personal_representative, filing_date.
Contact / decision maker: the Personal Representative, Executor, or Administrator, NOT the
deceased. Marketing aimed at the decedent is wasted; the PR controls the estate and the
property.
```

Probate notices almost never contain the decedent's property address. The PR mailing address is captured instead (where creditors send claims). A separate property lookup fills the property address, so hand probate records to the probate-property-finder skill for that step before deep prospecting.

### Tax Sale

```
Act as a public-records researcher. For [COUNTY] County, [STATE], find where the upcoming
tax sale list is published and exactly how to pull it.

First classify the tax-sale mechanism (see state-law-matrix.md): tax lien (a lien certificate
is sold, owner keeps the property and a redemption window), tax deed (the property is sold,
usually no post-sale redemption), or redeemable deed (a deed is sold but redeemable with a
penalty). The mechanism tells me what is actually being sold and how long the owner can buy it
back.

Return: exact office holding the list (County Treasurer or Tax Collector for most states; in
Tennessee the Clerk and Master of the Chancery Court conducts the auction), physical address,
phone, portal URL (label "verify before use"), public-records contact, the newspaper
legal-notice publication requirement (commonly once a week for 3-4 successive weeks before the
sale), update frequency, list cost, deposit-to-bid amount, and the marketing window.

Marketing window: 60-90 days before the auction, after the list is published.
Key fields to capture: address, owner_name, auction_date, delinquent_amount.
Contact / decision maker: the property owner of record (still in title until the sale, so
still reachable and motivated).
```

### Tax Delinquent

```
Act as a public-records researcher. For [COUNTY] County, [STATE], find the current delinquent
property tax list (owners who owe back taxes but have NO auction date scheduled yet) and
exactly how to pull it.

Return: exact office (County Treasurer, Tax Collector, or county Trustee), physical address,
phone, lookup portal URL (label "verify before use"), public-records contact, whether the
delinquent file is published or available by request, update frequency, access cost, and any
data-use terms. Flag any anti-data-mining or terms-of-use restriction on the property tax
site, because some county tax sites carry explicit penalty language; use the official lookup
endpoints, respect the terms, and do not assume an open bulk API.

Marketing window: ongoing. The owner has bills but no auction date yet, so this is a long,
steady list.
Key fields to capture: address, owner_name, delinquent_amount, years_delinquent.
Contact / decision maker: the property owner of record.
```

---

## Priority B: Standard Lists

Strong motivation, slightly more work to source. Pull after Priority A.

### Eviction

```
Act as a public-records researcher. For [COUNTY] County, [STATE], find where eviction filings
(forcible entry and detainer, unlawful detainer, or detainer warrant depending on the state)
are recorded and exactly how to pull them.

Return: exact court name (General Sessions, County Civil, Justice Court, or district court
depending on the state), physical address, phone, case-search portal URL (label "verify
before use"), public-records contact, the vendor/portal system if known, update frequency,
access cost, the exact search terms to use ("forcible entry detainer", "unlawful detainer",
"detainer warrant"), and the marketing window.

Marketing window: immediate. The landlord is motivated now.
Key fields to capture: plaintiff_name (the landlord), defendant_name (the tenant),
property_address.
Contact / decision maker: the PLAINTIFF (the landlord), NOT the defendant (the tenant). The
landlord owns the property and is the seller lead. Flag any landlord with 2 or more eviction
filings in the last 12 months as a high-value multiple-eviction landlord; those owners are
tired of the asset and convert well.
```

### Code Violation

```
Act as a public-records researcher. For [CITY or COUNTY], [STATE], find where code enforcement
violations (condemned or unsafe structures, overgrowth, nuisance, junk, open permits) are
recorded and exactly how to pull them.

Note up front: code violation data is usually CITY-level, not county-level, and online portals
are uncommon. Tell me whether the city runs an online code-enforcement search or whether this
requires a public records request, and name the records custodian for the request.

Return: exact department name (city code enforcement, building department, or neighborhood
services), physical address, phone, portal URL if one exists (label "verify before use"),
public-records contact, update frequency, access cost, and the marketing window.

Marketing window: before the compliance deadline, typically 10-30 days.
Key fields to capture: owner_name, violation_type, compliance_deadline, property_address.
Contact / decision maker: the owner of record.
```

---

## Priority C: Extended Lists

Lower competition, more sourcing effort, often a FOIA request. Pull these only after Priority A and B are running and you have capacity. Each of these uses the same role framing: open with "Act as a public-records researcher," classify the source office, return office name, address, phone, portal URL labeled "verify before use," public-records contact, update frequency, access cost, and the marketing window.

### Divorce

```
Act as a public-records researcher. For [COUNTY] County, [STATE], find where divorce and
dissolution cases are filed and exactly how to pull them.

Return: exact court name (Circuit Court, Family Law division, or district court), physical
address, phone, case-search portal URL (label "verify before use"), public-records contact,
update frequency, access cost, and the marketing window.

Marketing window: 60-180 days from filing.
Key fields to capture: petitioner_name, respondent_name, property_address (real property
appears on the schedule page, financial affidavit, or marital asset schedule).
Contact / decision maker: BOTH the petitioner and the respondent. Either spouse may need to
sell. Lowest motivation and lowest competition of the 7 types.
```

### Extended types (use this template for each)

```
Act as a public-records researcher. For [CITY or COUNTY], [STATE], find where [EXTENDED_TYPE]
records are maintained and exactly how to pull them. Most of these are not on a public online
index and require a public records or FOIA request.

Return: exact office or department holding the record, physical address, phone, online portal
URL if any (label "verify before use"), the records custodian for a FOIA request, update
frequency, access cost, the recommended search terms, the likely decision-maker contact, and
the marketing window. If no online source exists, say so plainly and route me to the FOIA
prompt below.
```

Run that template for each of the following. The bracketed note is the source office and the contact logic to capture:

1. Condemned / unsafe structures: city building or code department. Owner of record. The structure is a liability; owner is highly motivated. Window: before the demolition or repair deadline.
2. Mechanic's liens: county recorder. The property owner is the debtor on the lien. Signals cash-flow distress. Window: ongoing while the lien is open.
3. IRS tax liens (federal): county recorder (federal liens are filed locally). Owner of record. Signals broad financial distress. Window: ongoing.
4. State tax liens: county recorder or the state department of revenue. Owner of record. Window: ongoing.
5. HOA / condo association liens: county recorder, sometimes the association directly. Owner of record. Can move to HOA foreclosure quickly in some states. Window: before the HOA sale.
6. Medicaid estate recovery liens: county recorder or the state Medicaid agency. The estate or heirs (overlaps with probate). Pair with the probate list. Window: during estate administration.
7. Child support liens: county recorder or the state child-support enforcement agency. Owner of record. Signals personal financial distress. Window: ongoing.
8. Lis pendens (pending litigation, including judicial foreclosure): county recorder or Clerk of Court. The named defendant or property owner. In judicial states this IS the first foreclosure signal. Window: Days 1-30 from filing.
9. Utility shut-offs: city or county utility provider. Usually requires a FOIA request. Owner or account holder. Signals occupancy and cash-flow distress. Window: immediate.
10. Building permits (especially expired or stalled permits): city building department. Owner of record. Stalled rehab equals a motivated seller. Window: when a permit lapses.
11. Mold / asbestos / lead remediation orders: city or county environmental health. Owner of record. Expensive to cure; high motivation. Window: before the remediation deadline.
12. Fire / storm damage: fire marshal, building department, or insurance-adjacent public records. Owner of record. Damaged property owners often sell as-is. Window: 30-180 days after the event.
13. Sinkhole / structural reports: county or state geological or building records. Owner of record. Very expensive to cure; strong as-is sellers, especially in Florida. Window: ongoing.
14. Quiet title actions: Clerk of Court civil docket. The plaintiff clearing title (often already an investor) and any named owner. Useful for both leads and buyer research. Window: 60-180 days from filing.

---

## FOIA Request Prompts

Use these when a record type has no online index and you must file a public records or FOIA request. Set expectations to the correct statutory response window for your state (see state-law-matrix.md): Tennessee is 7 business days under the Tennessee Public Records Act (T.C.A. 10-7-503), Georgia and Missouri are 3 business days, Pennsylvania and New York are 5 business days, Texas and California are about 10, and several states have no fixed day count (reasonable or prompt). When you do not know the state's window, treat it as a reasonable time, commonly 10 business days, and verify.

One consistent cost variable: across the template and the draft prompt below, the cost-threshold fill-in is always `$[AMOUNT]`. Pick one number (a common default is $100) and use the same value in both places. Commercial requestors (real estate investors) should expect to pay copy and service fees; the public-interest fee waiver almost never applies to a commercial requestor, and several states (including Alabama, Arizona, California, North Carolina, and others) do not allow fee waivers at all. The realistic protection is a cost cap, not a waiver.

### Prompt to identify the right custodian and statute

```
Act as a public-records researcher. I need to file a public records / FOIA request for
[NOTICE_TYPE] records in [COUNTY] County, [STATE].

Return:
1. The exact records custodian (office name and the request-intake email or portal).
2. The controlling public records statute, by name and citation.
3. The statutory response window in business days for this state.
4. Whether the agency accepts requests by email, online portal, or only in writing.
5. The expected copy or service fee, and whether a fee waiver is available (assume I am a
   commercial requestor, so most waivers will not apply).
6. Whether the data exists in an electronic / native format I can ask for to avoid per-page
   printing charges.
Label any portal URL "verify before use."
```

### Prompt to draft the FOIA request letter

```
Act as a public-records researcher. Draft a public records / FOIA request letter to the
[OFFICE / CUSTODIAN] of [COUNTY] County, [STATE], for [NOTICE_TYPE] records covering the date
range [START_DATE] to [END_DATE].

Requirements for the letter:
- Cite the controlling public records statute by name and citation.
- Request the records in their existing electronic or native format (CSV, Excel, or an existing
  database export), phrased as "in the electronic format in which it is already maintained,"
  and ask the agency NOT to convert, reformat, or run custom programming (several states allow
  extra special-service charges for extensive IT or programming time).
- Specify the exact fields I need: [LIST FIELDS, for example owner name, property address,
  violation type, compliance deadline].
- Include this cost-cap line verbatim: "If fulfilling this request will cost more than
  $[AMOUNT], please notify me with an itemized estimate before proceeding." Use the SAME
  $[AMOUNT] value I set in the template above.
- Ask the agency to acknowledge receipt and state the date by which records will be produced.
- Keep the tone professional and concise. Do not claim a fee waiver; I am a commercial
  requestor.
```

---

## Fallback and Troubleshooting Prompts

Use these when a portal is down, when you are not sure a source is current, or when a URL needs re-checking before a pull.

### Find alternative data sources

```
Act as a public-records researcher. The primary source for [NOTICE_TYPE] in [COUNTY] County,
[STATE] is unavailable (portal down, login broken, or no online index). Find every alternative
path to the same data, ranked easiest to hardest:

0. Call the county clerk directly and ask for a manual lookup or an emailed PDF of recent
   filings. Many clerks will email a PDF of the docket or list on request, which beats waiting
   for a portal to come back.
1. A statewide legal-notice aggregator (for example tnpublicnotice.com in Tennessee or
   floridapublicnotices.com in Florida, run by the state press association). Confirm whether
   one exists and covers this county.
2. The newspaper of record that publishes legal notices for this county.
3. A neighboring access path: recorder vs court vs tax office that holds an overlapping record.
4. A vendor portal (Tyler Odyssey, Landmark Web, Fidlar Laredo/Tapestry, Kofile, Schneider
   Beacon/qPublic) that mirrors the same data.
5. A legacy or backup portal: a county may run both a modern Tyler portal and an older in-house
   system, so check whether an older lookup still resolves.
6. A public records / FOIA request as the last resort.

If the portal is simply down (not retired), treat it as a temporary maintenance window: most
county portal outages are short. Document the outage (date, what was down, what you tried) and
retry in 24-48 hours before committing to a heavier fallback.

For each option give the URL (label "verify before use"), the access cost, and whether bulk
export is possible. Do NOT send me to a non-aggregator site (for example a state Attorney
General consumer page is not a legal-notice aggregator).
```

### Verify data currency

```
Act as a public-records researcher. For the [NOTICE_TYPE] source I am using in [COUNTY] County,
[STATE] (URL: [URL]), confirm how fresh the data actually is:

1. Find the most recent filing date visible in the source right now.
2. Compare it to today's date and tell me the lag in days.
3. State the real update cadence (daily, weekly, monthly, annually) based on what you observe,
   not what the site claims.
4. Flag any sign the source is stale or frozen (no new records in an unusually long stretch).
5. Recommend a re-pull frequency that matches the cadence so I neither miss records nor
   re-pull stale data.
```

### Source re-verification loop

```
Act as a public-records researcher. Re-verify this county data source before I trust it for a
production pull. Source: [OFFICE NAME], [NOTICE_TYPE], [COUNTY] County, [STATE], URL: [URL],
FOIA contact: [CONTACT].

Run this checklist and report each item:
1. Live URL check: confirm the portal URL is live right now and loads the search interface
   (not a parked page, redirect, or error). If it moved, find the new URL and label it "verify
   before use."
2. Update-frequency re-confirmation: re-confirm the stated update frequency against the actual
   most recent courthouse filing dates visible in the source, not the site's marketing claim.
3. Custodian / contact re-confirmation: confirm the records-custodian email or FOIA intake is
   still valid and monitored.
4. Terms-of-use check: confirm the site has no new anti-data-mining or bulk-access restriction
   that changed since the last pull.
5. Re-pull cadence: set a re-verification cadence (for example re-check daily-feed portals
   weekly and annual lists monthly) because county portals, URLs, and FOIA contacts change.
   Output the next re-verification date.
```

---

## Expected Output Table

Each prompt returns one row per office. This table is illustrative; the placeholders below show the shape of the result, not real values. The fully filled-in version, with real office names, addresses, portal URLs, and statutes for a complete county pull, lives in worked-example.md (Knox County TN and Hillsborough County FL).

| Data Type | Priority | Office Name | Address | Phone | Portal URL | FOIA Email | Difficulty | Update Freq | Cost | Marketing Window | Notes |
|-----------|----------|-------------|---------|-------|------------|------------|------------|-------------|------|------------------|-------|
| foreclosure | A | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | Days 1-30 from filing, before auction | [filter logic, judicial vs non-judicial] |
| tax_sale | A | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | 60-90 days before auction | [lien / deed / redeemable] |
| tax_delinquent | A | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | Ongoing | [data-use terms] |
| probate | A | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | 30-180 days from filing | Contact is the PR, not the decedent |
| eviction | B | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | Immediate | Plaintiff/landlord; flag 2+ filings in 12 months |
| code_violation | B | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | Before deadline (10-30 days) | Usually city-level; FOIA likely |
| divorce | C | [result] | [result] | [result] | [result, verify before use] | [result] | [Low/Med/High] | [result] | [result] | 60-180 days from filing | Both petitioner and respondent |

After the table is built and verified, the lifecycle continues: import the normalized list to the DataSift CRM tagged `Courthouse Data` (which routes it to the `00 Niche Sequential Marketing` folder, 12 presets; records WITHOUT the `Courthouse Data` tag route to `01. Bulk Sequential Marketing`, 9 presets, so the tag is what prioritizes first-to-market records over bulk data), then enrich and skip trace, then hand off in order to probate-property-finder (address-less probate), deep-prospecting (heirs and decision-makers, the L1-L3 research flow with L4 curative-title escalation), phone-validator (Trestle phone scoring: 81-100 Dial First, 61-80 Dial Second, 41-60 Dial Third, 21-40 Dial Fourth, 0-20 Drop), and sequential-presets (niche sequential marketing). See worked-example.md for the completed pull and common-offices.md for the office and portal details.
