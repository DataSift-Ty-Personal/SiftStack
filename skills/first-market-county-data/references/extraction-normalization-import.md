# Extraction, Normalization, and Import

This reference holds the deep operational detail that supports the lean SKILL.md router: how to recognize each county vendor system, how to navigate the common portals, how to extract from scanned PDFs and courthouse photos, how to normalize a raw pull into clean records, how to scrub a list for deliverability, how to file a public records request, and how to format the final CSV for CRM import.

Load this when: you have identified the offices and portals for a county (via common-offices.md) and now need to actually pull, clean, and import the data, or when a portal is unfamiliar and you need to recognize the vendor, or when you are drafting a FOIA request, or when you are mapping columns for the CRM upload.

## Table of Contents

- [Portal Systems Field Guide](#portal-systems-field-guide)
- [Portal Navigation Click-Paths](#portal-navigation-click-paths)
- [PDF and OCR Extraction](#pdf-and-ocr-extraction)
- [Data Normalization Steps](#data-normalization-steps)
- [List Hygiene and Deliverability](#list-hygiene-and-deliverability)
- [FOIA Request Template](#foia-request-template)
- [CRM Import CSV Column Spec](#crm-import-csv-column-spec)

Related references: state-law-matrix.md (per-state foreclosure, tax-sale, and public-records statutes), common-offices.md (which office holds each notice type), worked-example.md (two fully filled-in county pulls).

## Portal Systems Field Guide

Before you build a pipeline, identify which vendor system the county runs. The system tells you whether bulk extraction is possible, how hard it will be, and which access path to use. Recognize the system first, then pick your method. Treat every portal URL below as a pattern to confirm, not a fixed address: vendors rebrand and counties migrate, so verify before use.

### Court Records (foreclosure, probate, eviction, divorce, civil)

| System | Used for | How to recognize | Bulk export? | Access tips |
|--------|----------|------------------|--------------|-------------|
| Tyler Odyssey / Enterprise Justice Portal | Court case management (probate, civil, eviction, foreclosure dockets) in 600+ counties across 21 states | Public "Portal" or "Case Search" with party name, case number, date range, and case type filters. Often branded "Odyssey Portal" or "Public Access" | No self-serve CSV. Public access is per-case search only. Standing bulk feeds require a court-approved bulk data agreement with the clerk or state court administration | Search by case type and date range for daily pulls. For volume, ask the clerk about a bulk data agreement. Do not assume CSV download exists |
| re:SearchTX (Tyler) | Statewide Texas court records, e-filed documents and case info across all 254 counties (150+ participating) | research.txcourts.gov. Free subscription tier. Searches by party, case number, attorney, date | No public bulk. Same agreement path as Odyssey | One login covers most of Texas. Best single source for TX probate and civil. Filter by court type (probate, district, county court) |
| ASP.NET legal-notice aggregator (tnpublicnotice.com pattern) | State-level legal notices: foreclosures, probates, tax sales, tax delinquent across many counties | URL has session GUID in the path, navigation uses page postbacks with ViewState, dropdown saved searches trigger postbacks, 50 rows per page, reCAPTCHA on each detail page | No official export. Extraction is automated scraping plus CAPTCHA solving (about $0.003 per solve via 2Captcha, verify current pricing) | Highest-value single source where it exists. Paginate at 50/page, open each record, solve CAPTCHA per detail page. Reuse session cookies |

### Recorder and Land Records (deeds, mortgages, liens, lis pendens)

| System | Used for | How to recognize | Bulk export? | Access tips |
|--------|----------|------------------|--------------|-------------|
| Landmark Web (Pioneer Technology Group, owned by Catalis) | Official records search: deeds, mortgages, liens, judgments | URL path contains /LandmarkWeb | Index search usually free. Images and bulk are gated or fee-based, county by county | Search the index for free to find recent liens and deeds. For bulk, ask the recorder about a data agreement. Not a Fidlar or Avenu product |
| Fidlar (Laredo, Tapestry, Monarch) | County land records, deed and mortgage search | Laredo or Tapestry branding, ava.fidlar.com portals, fidlar.com. Tapestry needs no login | Yes, through Monarch (sanctioned bulk document retrieval) | Tapestry for occasional pay-per-search. Laredo is a county subscription with a contract. Monarch is the bulk path. Pricing is set per county |
| Kofile | Recorder and clerk land records, digitized official records | URL on psearch.kofile.com, fsearch.kofile.com, or gasearch.kofile.com. Heavy presence in Georgia counties | Index search public. Bulk and images fee-based or by request | Use the county-specific fsearch path. For volume, contact Kofile or the county for a bulk quote |
| Avenu Insights (Avenu Records, Clearview 20/20+) | Recorder and records management, e-certified recording | avenuinsights.com hosted portals, Avenu branding | Public index search varies by county. Bulk by request | Separate vendor from Landmark Web and Fidlar. Treat as its own system. Ask for electronic delivery on any records request |

### Tax and Assessor Data (tax sale, tax delinquent, parcel and owner lookup)

| System | Used for | How to recognize | Bulk export? | Access tips |
|--------|----------|------------------|--------------|-------------|
| Schneider Geospatial Beacon and qPublic.net | Assessor and GIS parcel lookup, CAMA, owner, legal description, taxing district | beacon.schneidercorp.com or qpublic.schneidercorp.com. Built on Esri ArcGIS | No public bulk. Parcel-by-parcel lookup only | Search by name, address, or parcel ID to confirm owner and value. For bulk parcel files, request a data extract from the county assessor |
| Tyler iasWorld | Assessor CAMA plus tax billing and collections back end | Often labeled "Public Access" on the assessor or treasurer site, Oracle-based, with E-File and Inquiry/Appeals add-ons | No public bulk. Per-parcel search where Public Access is enabled | Use Public Access to pull parcel and tax status. For tax-delinquent lists in volume, request the file from the treasurer or trustee |
| Catalis Tax and CAMA (includes Manatron GRM) | Full property tax lifecycle: valuation, assessment, billing, collection, delinquents | Catalis or Manatron branding on the treasurer, collector, or assessor system | Varies. Tax-delinquent lists are often a county-published file or by request | The delinquent and tax-sale list usually lives with the treasurer, collector, or trustee. Ask for the published list and its release schedule |

### FOIA and Public Records Request Portals (for data with no online index)

| System | Used for | How to recognize | Bulk export? | Access tips |
|--------|----------|------------------|--------------|-------------|
| Granicus GovQA (Records Request Management) | Submitting and tracking public records requests, receiving records back | GovQA or Granicus branding on the request portal. Largest hosted FOIA platform | No dataset feed. It is a request channel. Output is usually PDF | Use for code violations, evictions, and utility shut-offs that have no online index. Always request electronic format (CSV or Excel) |
| NextRequest (owned by CivicPlus) | Modern public records request portal, plus an open view of already-released records | URL is {agency}.nextrequest.com | Staff have bulk document actions. Public can browse and download already-released records in the open view | Check the open/published records first, the answer may already be posted. Otherwise file a request and ask for electronic delivery |

### Quick recognition cheat sheet

- URL has /LandmarkWeb: Pioneer/Catalis recorder search.
- URL has schneidercorp.com (Beacon or qPublic): Schneider assessor and GIS.
- URL has {agency}.nextrequest.com: NextRequest FOIA portal (CivicPlus).
- GovQA or Granicus branding on a request form: Granicus FOIA intake.
- research.txcourts.gov: re:SearchTX, statewide Texas court search (Tyler).
- Session GUID in the path plus page postbacks plus reCAPTCHA: ASP.NET legal-notice aggregator (scrape it).
- Laredo or Tapestry or ava.fidlar.com: Fidlar land records (use Monarch for bulk).
- Catalis or Manatron branding on a treasurer or assessor site: Catalis tax and CAMA.
- "Public Access" on an Oracle-based assessor or tax site: Tyler iasWorld.

### Two corrections to keep accurate

1. Landmark Web is a Pioneer Technology Group product, and Pioneer was acquired by Catalis in 2019. It is not a Fidlar or Avenu product. Fidlar (Laredo, Tapestry, Monarch) and Avenu Insights are three separate vendors.
2. Tyler court portals (Odyssey, re:SearchTX) do not offer self-serve CSV bulk export to the public. Public access is per-case search. Bulk court data requires a court-approved data agreement.

## Portal Navigation Click-Paths

These are the repeatable click-paths for the systems you will hit most. Field labels and screen layouts change between releases and counties, so read the labels on the actual page and adapt. Confirm each live URL before you depend on it.

### ASP.NET legal-notice aggregator (scrape pattern)

This is the tnpublicnotice.com family: a state public-notice portal built on ASP.NET WebForms.

1. Log in. The session ID lands inside the URL path (a GUID segment), so keep the same browser session for the whole run.
2. Open the Smart Search or saved-search dashboard. Selecting a saved search from the dropdown triggers a server-side page postback (ViewState), not a normal link, so a headless browser is required. Plain HTTP requests would have to hand-manage ViewState and EventValidation.
3. Set the date range for a daily pull (notices filed since the last run) or a wider window for a backfill.
4. Page through results at 50 rows per page. Track the last page so you do not miss records.
5. Open each notice detail page. A reCAPTCHA v2 challenge appears on every single detail page even while logged in. There is no CAPTCHA on login, search, or results pages.
6. Solve the CAPTCHA. Automated solving runs about $0.003 per solve via 2Captcha (approximate current cost, verify before budgeting). This per-detail-page CAPTCHA is the main throughput bottleneck, on the order of 10-30 seconds per record.
7. Parse the free-text notice body. There are no structured HTML fields: address, owner, dates, and auction date are all embedded in the notice text and must be pulled with patterns.
8. Apply rate limiting (a couple of seconds of random delay between requests) and retry transient failures a few times.

### Tyler Odyssey and re:SearchTX case search

1. Open the public Portal or Case Search page (Odyssey) or research.txcourts.gov (re:SearchTX, one login covers most of Texas).
2. Choose the search type. For probate, search by case type or case category and pick the probate or estate category. For foreclosure in a judicial state, search the civil docket.
3. Set the date range to the filing window you want.
4. Run the search and open each case. Pull party names, case number, filing date, and the case type.
5. There is no public CSV export. Record each case as you read it, or for volume, contact the clerk or state court administration about a court-approved bulk data agreement. Do not assume a download button exists.

### Schneider Beacon and qPublic

1. Open beacon.schneidercorp.com or qpublic.schneidercorp.com for the target county.
2. Select the county and the assessor application if prompted.
3. Search by owner name, situs address, or parcel ID.
4. Read the parcel card: owner of record, mailing address, legal description, assessed and appraised value, taxing district, and often sale history.
5. There is no public bulk export. Use this to confirm and enrich one parcel at a time. For a bulk parcel file, request a data extract from the county assessor.

### Fidlar (Laredo, Tapestry, Monarch)

1. Identify the access tier. Tapestry is pay-per-search with no login (good for occasional lookups). Laredo is a county subscription with a contract (good for steady index and image access). Monarch is the sanctioned bulk path.
2. For an occasional check, use Tapestry: search the index by name or document type, then pay per index or image view.
3. For recurring bulk (for example all deeds recorded in a month), use Monarch under the county contract. Pricing is set per county.
4. Pull deeds, mortgages, liens, and lis pendens to cross-reference distress signals against ownership.

### Landmark Web (Pioneer/Catalis recorder)

1. Open the recorder site whose URL path contains /LandmarkWeb.
2. Accept any disclaimer, then choose Official Records or Document Search.
3. Search by party name, document type (deed, mortgage, lien, lis pendens), and date range.
4. The index search is usually free. Document images and bulk are gated or fee-based county by county.
5. For bulk, ask the recorder about a data agreement. This is a Pioneer/Catalis product, not Fidlar or Avenu.

### Granicus GovQA and NextRequest (CivicPlus) FOIA portals

1. For NextRequest ({agency}.nextrequest.com), first browse the open/published records view. The answer may already be posted, which costs nothing.
2. If not posted, create an account and submit a new request.
3. Describe the records precisely (record type, date range, county or city), and request the data in its existing electronic format (CSV or Excel).
4. Include a cost-cap line so you are notified before any charge above your threshold.
5. Track the request in the portal. GovQA and NextRequest both return records through the portal, usually as PDF unless you asked for an existing electronic export. Use these portals for code violations, evictions, and utility shut-offs that have no online index.

## PDF and OCR Extraction

Some counties only publish scanned PDFs, and some data only exists as a photo taken at a courthouse terminal. Both paths need optical character recognition (OCR) before you can parse fields.

### Scanned PDF handling

1. Render each PDF page to a high-resolution image.
2. Correct page orientation if the scan is rotated.
3. Run OCR on each page to get raw text (Tesseract is the common free engine; Adobe Acrobat's built-in OCR works for one-off batches).
4. Parse the raw text into fields with the same patterns you use on web notices: address, owner, dates, auction date, and notice-type-specific fields.
5. Validate the parse: if key fields are missing or malformed, flag the record for manual review rather than importing garbage.

### Courthouse terminal photos

Phone photos of courthouse public-access terminals are harder than clean scans because the screen introduces a moire (interference) pattern that wrecks naive OCR. Hard-won rules:

- The moire pattern from terminal screens is the single biggest OCR killer. Standard adaptive-threshold and contrast-equalization preprocessing produces garbage on these photos.
- Use a bilateral filter to remove the moire while preserving text edges, then apply an automatic (Otsu) binary threshold. This pairing is what makes terminal photos readable.
- Use a page-segmentation mode tuned for a single column of variable text, not the single-uniform-block mode that research guides often recommend. The single-block mode fails in practice on terminal screens.
- Let the photo's own embedded orientation metadata handle rotation. Do not run a separate orientation-detection pass on raw phone photos: it often misfires and rotates a correct image sideways.
- After OCR, pass the text through a language-model parse to assign the right notice type and pull the structured fields, then validate before import.

### CAPTCHA note

Where the source is a live web portal rather than a file, expect a CAPTCHA on the record detail pages (see the ASP.NET pattern above). Automated solving runs about $0.003 per solve via 2Captcha, an approximate current cost to verify before you budget a large run. CAPTCHA solving is the throughput bottleneck, not the parsing.

## Data Normalization Steps

Run this six-step procedure on every raw pull before list hygiene and CRM import. The goal is one clean, deduplicated record per property with the correct contact attached.

1. Standardize the address. Run each property address through an address-standardization service (for example Smarty, the free USPS Web Tools Address API, or Google Geocoding) so "123 Main St" and "123 Main Street" collapse to one canonical form. Capture the standardized street, city, state, ZIP, and the delivery-point validation match code. A clean address is what makes deduplication and mail deliverability work.
2. Split the owner name. Parse the owner or party name into first and last name. For entities (LLC, trust, estate, corporation), keep the full entity name and flag it for entity research downstream. Match the right party to the right notice type: for probate the party of interest is the Personal Representative, not the decedent, and for eviction it is the plaintiff (landlord), not the defendant (tenant).
3. Map notice_type to the CRM list. Assign each record one of the seven canonical notice types and map it to the matching CRM list name (see the mapping in the CSV spec below). The notice type drives which niche sequence the record enters.
4. Deduplicate by address. The same property can appear in more than one notice. Deduplicate on the standardized address and keep the most recent notice. Do not blindly drop duplicates: if two notices are different types on the same property (for example tax delinquent and probate), that is a stronger lead, so preserve both signals in the notes.
5. Apply contact logic. Set the contact (decision maker) per notice type. For living owners, the contact is the property owner at the owner mailing address (fall back to the property address if no separate mailing address exists). For deceased owners, the contact is the decision maker (Personal Representative or heir) at the decision maker's mailing address, not the property address. For eviction, the contact is the landlord. For divorce, both parties are contacts.
6. Format for the CRM. Emit the standardized fields into the CRM import CSV column layout (below), set the Lists column from the notice type, add the required tags including Courthouse Data, and write a contextual note per record (notice type, key dates, amounts, and any deceased or auction flags).

After normalization, run List Hygiene and Deliverability, then import.

## List Hygiene and Deliverability

Run list hygiene between normalization and CRM import so you do not mail dead addresses or call suppressed numbers.

- NCOA scrub: Run National Change of Address before mailing through any USPS-licensed NCOALink processor or a list-hygiene vendor (for example TrueNCOA or AccuZIP). Skipping it can push undeliverable rates toward 20 percent. NCOA covers USPS moves in the last 48 months and is required to keep First Class and Marketing Mail postal discounts.
- Catch what NCOA misses: Up to 40 percent of moves are never filed with USPS. Use UAA (undeliverable as addressed) and DSF (Delivery Sequence File) processing, available through the same NCOALink processors, to flag the rest.
- DNC and Do Not Mail suppression: Scrub phone numbers against the federal Do Not Call registry at least every 31 days. Use a DNC-scrub vendor (for example DNC.com) or register as a telemarketer directly with the free FTC National Do Not Call Registry. Suppress against the Do Not Mail file for direct mail.
- Outcome: higher deliverability, lower postage, fewer returns, and better response rates.

The DNC scrub interval (at least every 31 days) is also a compliance requirement, not just a hygiene nicety. Calls offering to buy someone's property are telephone solicitations under the TCPA and DNC rules. See SKILL.md Compliance and Legal Guardrails for the full picture.

## FOIA Request Template

Use this template for any notice type that has no online index (most commonly code violations, evictions, and utility shut-offs). Fill in the bracketed fields. Use a single consistent cost threshold for the whole request. For the correct statute name, citation, and response window for your target state, see state-law-matrix.md. Cite the statute by name so the custodian knows you know the law.

```
To: [Records Custodian / FOIA Officer], [County or City] [Office Name]
From: [Your Name], [Your Company]
Date: [Date]
Re: Public Records Request under [State Public Records Statute, e.g. Tennessee Public Records Act, T.C.A. 10-7-503]

Dear Records Custodian,

Under [State Public Records Statute and citation], I request access to and a copy of
the following public records:

[Describe the records precisely. Example: All [notice type, e.g. code violation]
records filed or opened between [START DATE] and [END DATE] in [County or City],
including, for each record: property address, owner or responsible party name and
mailing address, [violation type / case type], filing or open date, and
[compliance deadline / hearing date].]

Format: Please provide these records in the electronic format in which they are
already maintained (CSV, Excel, or an existing database export). Please do NOT
convert, reformat, or run custom programming to fulfill this request, since that
can add special service charges. If the data already exists as a report or extract,
that existing file is acceptable.

Fees: I am a commercial requestor and understand a public-interest fee waiver likely
does not apply. I am willing to pay reasonable copy or service fees. However, if
fulfilling this request will cost more than $[AMOUNT], please notify me with an
itemized estimate before proceeding so I can confirm or narrow the request.

Response: Please respond within the time required by [State Public Records Statute].
If any portion of this request is denied, please cite the specific exemption and
release all reasonably segregable non-exempt portions.

Thank you for your assistance.

Sincerely,
[Your Name]
[Phone] | [Email] | [Mailing Address]
```

Notes on the template:

- Use the same $[AMOUNT] cost threshold throughout. A common default is $100, but set it to whatever spend you will approve without a second look.
- The electronic-format request matters: copy fees are tied to paper pages, so asking for an existing electronic export usually avoids per-page printing charges entirely.
- Fee waivers are reserved for non-commercial, public-interest disclosures and almost never apply to an investor pulling distress lists. Several states do not allow fee waivers at all. The realistic protection is the cost cap above, not a waiver. See state-law-matrix.md for which states cap fees and which response window applies.

## CRM Import CSV Column Spec

The normalized records are emitted as a CSV for upload to the DataSift CRM. The spec has a Core column set (always present) and an Extended set (the enrichment and notice-specific fields). Every record carries the Courthouse Data tag, and the notice type maps to a DataSift list name.

### Core columns

| Column | Description |
|--------|-------------|
| property_street | Standardized property street address |
| property_city | Property city |
| property_state | Property state (2-letter) |
| property_zip | Property ZIP |
| owner_first_name | Owner or contact first name |
| owner_last_name | Owner or contact last name |
| mailing_street | Contact mailing street (where mail is sent) |
| mailing_city | Contact mailing city |
| mailing_state | Contact mailing state |
| mailing_zip | Contact mailing ZIP |
| tags | Comma-separated tags. Always includes Courthouse Data plus notice type, county, date, and status flags |
| lists | The DataSift list name, mapped from notice_type (see mapping below) |
| notes | Contextual note: notice type, key dates, amounts, and any deceased or auction flags |

### Extended columns (enrichment and notice-specific)

| Column | Description | Applies to |
|--------|-------------|------------|
| estimated_value | Estimated property value | all |
| owner_deceased | Boolean: owner is deceased | probate and any deceased-owner record |
| date_of_death | Date of death | deceased owners |
| personal_representative | Named PR / Executor / Administrator | probate |
| parcel_id | Assessor parcel ID | all (when known) |
| auction_date | Foreclosure or tax-sale auction date | foreclosure, tax_sale |
| tax_delinquent_amount | Delinquent tax balance | tax_sale, tax_delinquent |
| mls_status | MLS / listing status | all |
| owner_street | Owner mailing street (when separate from contact) | all |
| owner_city | Owner mailing city | all |
| owner_state | Owner mailing state | all |
| owner_zip | Owner mailing ZIP | all |
| equity_percent | Estimated equity percentage | all |
| latitude | Geocoded latitude | all |
| longitude | Geocoded longitude | all |
| dpv_match_code | Delivery-point validation match code from address standardization | all |
| decedent_name | Name of the deceased | probate |
| obituary_url | Source obituary URL | probate / deceased owners |
| decision_maker_name | Resolved decision maker (PR or heir) | deceased owners |

### Deceased-owner contact-address rule

For a deceased owner, the contact mailing address (mailing_street, mailing_city, mailing_state, mailing_zip) is the decision maker's address (the Personal Representative or verified heir), NOT the property address and NOT the decedent's old address. The decision_maker_name carries the resolved contact, and owner_deceased plus date_of_death plus decedent_name flag the record for the downstream heir and decision-maker workflow. This is the single most common import mistake to avoid: mailing a dead person at a vacant property burns the lead.

### Courthouse Data tag and Lists mapping

Every first-to-market record gets the Courthouse Data tag. That tag routes the record into the niche funnel (the 00 Niche Sequential Marketing preset folder, 12 presets) and is what prioritizes first-to-market records over bulk data in the filter presets. Records WITHOUT the Courthouse Data tag route to 01. Bulk Sequential Marketing (9 presets) instead. Keep them separate so first-to-market leads stay prioritized.

The notice_type maps to the DataSift list name as follows:

| notice_type | DataSift list name |
|-------------|--------------------|
| foreclosure | Foreclosure |
| tax_sale | Tax Sale |
| tax_delinquent | Tax Delinquent |
| probate | Probate |
| eviction | Eviction |
| code_violation | Code Violation |
| divorce | Divorce |

DataSift auto-creates lists from the CSV, so the list name in the Lists column is enough to file the record into the right niche sequence.

### After import

Once the CSV is imported and tagged Courthouse Data, the lifecycle continues outside this file: enrich property data and skip trace inside the CRM (keep owner-swap off to protect the PR and decision-maker mapping), then hand off in order to probate-property-finder (address-less probate), deep-prospecting (heirs and decision makers, the L1-L3 research flow with L4 curative-title escalation), phone-validator (Trestle phone scoring with the tier bands), and sequential-presets (niche sequential marketing). See SKILL.md for the full handoff chain and worked-example.md for two end-to-end county pulls.
