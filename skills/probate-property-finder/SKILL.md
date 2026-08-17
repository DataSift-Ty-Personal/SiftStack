---
name: probate-property-finder
description: >
  Find real property owned by probate decedents when you only have the case filing (PR number,
  decedent name, executor details) but no property address. Browses county assessor/CAD sites,
  deed records, and aggregators to discover every parcel the decedent owned, then outputs a
  Sift-ready CSV with formatted addresses and property data. Works on single records or bulk
  CSV. Use whenever someone says "find the property for this probate", "what did the decedent
  own", "complete this probate list", "enrich probate records", "fill in missing property
  addresses", "probate property lookup", "property discovery", or uploads a CSV of probate
  filings missing property addresses. Also use when someone has a decedent name and county
  but no property address. Even if the user just says "I have probates but no addresses" or
  "find the properties for these" — use this skill.
---

# Probate Property Finder

When a real estate investor pulls probate filings from a county, they often get only the case
number, decedent name, date of death, and executor contact information — but no property
address. This skill bridges that gap by researching what real property the decedent actually
owned and producing a clean, upload-ready CSV for REI Sift.

This skill focuses exclusively on **property discovery and data enrichment**. It does NOT
identify decision-makers, skip trace heirs, or do deep prospecting — the `deep-prospecting`
skill handles that separately once you have an address.

## Critical Principle: Actually Browse the Sites

The single most important thing this skill does is **use browser tools to navigate county
assessor and recorder websites and execute searches**. Do not merely identify URLs or list
them as resources — you must actually open the browser, navigate to the site, type in search
terms, read the results, and extract data from the pages.

If a county assessor site is broken, unresponsive, or returns no results, immediately pivot
to the county recorder/deed search. If that fails, use aggregator sites. But always start by
actually browsing the official county data source.

Aggregator sites (Zillow, NeighborWho, Spokeo, etc.) are for cross-referencing only — never
treat them as primary sources. They often have stale or incorrect ownership data. The county
assessor is the source of truth for current ownership, and deed records are the source of
truth for ownership history.

## Input Formats

The skill accepts two input modes:

### Single Record
The user provides details about one probate case in conversation:
- Decedent name (required)
- County/state where probate was filed (required)
- Case/file number (helpful but optional)
- Executor name and address (helpful for cross-referencing)
- Date of death (helpful for narrowing searches)

### Bulk CSV
The user uploads a CSV file containing multiple probate records. The CSV may have varying
column names, but typically includes some combination of:
- File/case number
- Deceased/decedent name
- Date of death/deceased
- Executor/PR name
- Executor address (street, city, state, zip — may be separate columns or combined)

Parse the CSV flexibly — column names vary between counties and users. Map whatever columns
exist to the internal fields: `decedent_name`, `case_number`, `date_of_death`, `county`,
`state`, `executor_name`, `executor_address`, `executor_city`, `executor_state`, `executor_zip`.

If the county is not in a column, check the filename, the user's message, or ask.

## Property Lookup Tier Order

The automated pipeline uses a three-tier approach. Follow this same order when doing manual
lookups — it is the most efficient path from most-likely to least-likely to produce a match.

### Tier 1: County Tax/Assessor Name Search (Primary)

Search the county assessor or tax API by the decedent's name. This is the highest-value
lookup because assessors index properties by owner name.

**Name matching uses token overlap scoring.** The system compares name tokens (words) between
the search name and the assessor's owner field, ignoring order and noise words:

- Tokenize both names: `"JOHN MICHAEL SMITH"` becomes `{JOHN, MICHAEL, SMITH}`
- Remove noise: `&, JR, SR, II, III, IV, THE, ESTATE, OF`
- Score = overlapping tokens / max(search tokens, owner tokens)
- Example: `"JOHN MICHAEL SMITH"` vs `"SMITH JOHN M"` — overlap is `{JOHN, SMITH}` = 2, max tokens = 3, score = 0.67

**Name search variations** (try in order until results found):
1. Full name as-is
2. Full name without suffixes (JR, SR, II, III, IV, ESQ)
3. "LAST FIRST" format (how most county APIs store names)
4. "LAST FIRST MIDDLE" format
5. "FIRST LAST" only (drop middle name)
6. Maiden name variant for 4+ part names (e.g., "LULA ELIZABETH MASSIE JONES" try "MASSIE LULA")

### Tier 2: Executor Family Search (Secondary)

If Tier 1 returns nothing, search the assessor by the **executor's** name. Look for
properties where the decedent's last name appears in the owner field. This catches cases
where property was jointly owned or transferred within the family:

- Search by executor name variations (same as Tier 1)
- For each result, check if the decedent's last name appears in the owner field
- Skip properties at the executor's known mailing address (that is the executor's home)

### Tier 3: People Search (Fallback)

If both assessor tiers fail, search people-search sites (TruePeopleSearch, FastPeopleSearch)
for the decedent's last known address. These sites aggregate public records and often show
current and past addresses. Match addresses containing the target county/city.

### When to Use Each Tier

| Tier | Source | Best For | Limitation |
|------|--------|----------|------------|
| 1 | County assessor/tax API | Direct owner match | Only works if name on file matches decedent |
| 2 | County assessor by executor name | Family property transfers | Requires executor name; only finds same-county properties |
| 3 | People search sites | Decedent's last address | May return outdated data; no ownership verification |

## Confidence Tiers

Confidence levels correspond to name match quality and verification depth:

| Confidence | Token Overlap Score | Criteria |
|------------|-------------------|----------|
| **HIGH** | >= 0.8 | Exact or near-exact name match, verified on county assessor. All significant name tokens match. |
| **MEDIUM** | 0.4 - 0.79 | Partial match (e.g., middle name differs, initial vs full name), or verified via deed records only without assessor confirmation. |
| **LOW** | < 0.4 | Common name with ambiguous match, single aggregator source, unverified, or executor family search without direct name match on title. |

**Decision-maker confidence** (from obituary enricher, separate from property confidence):
- HIGH: DM verified living (obituary search confirmed no obituary exists)
- MEDIUM: DM unverified (could not confirm alive or deceased)
- LOW: All identified heirs confirmed deceased, or no usable heirs found

**DOD Sanity Check:** When matching obituaries, the system rejects matches where the date of
death is more than 3 years before the probate filing date (`MAX_DOD_GAP_YEARS = 3`). Probate
is typically filed within 1-2 years of death. This prevents matching a 2014 obituary to a
2025 court filing (wrong person with the same name). DOD in the future relative to notice
filing is also rejected.

## Research Workflow

For each record, execute these steps in order. Every step involves actually browsing the
web — not just identifying sites to check later.

### Step 1: Parse and Prepare the Record

Extract all available fields from the input. Note:
- The executor's address is NOT the subject property — it's where the executor lives (often
  a different city or state entirely). However, it may sometimes BE a property the decedent
  owned (e.g., the executor is a family member living in the decedent's house). You MUST
  verify this on the county assessor, not assume.
- The county where probate was filed is the most likely location for the decedent's property,
  but not guaranteed — search all mentioned geographies.
- Clean up name formatting: "LAST, FIRST MIDDLE" should become "First Middle Last" for
  search purposes. Handle suffixes (Jr., Sr., II, III) gracefully.

### Step 2: County Assessor / CAD Search (Primary)

This is the highest-value lookup because county assessors index properties by owner name.
You must actually navigate to the site and execute the search using browser tools.

**How to find the right assessor site:**
1. Google: `{County Name} county {State} property tax search` or
   `{County Name} county {State} assessor property search by owner name`
2. Look for the official county website — NOT third-party aggregators
3. Many counties use common platforms:
   - DevNet/Wedge (e.g., `champaignil.devnetwedge.com`) — often address/parcel search only
   - Tyler Technologies / iasWorld
   - Esri-based GIS portals
   - State-specific systems (e.g., Texas uses county appraisal districts like `brazoscad.org`)

**Important: Some county sites only search by address or parcel number, NOT by owner name.**
If the main assessor site doesn't have owner-name search, look for alternative portals:
- Community data sites (e.g., `cu-citizenaccess.org` for Champaign County IL)
- Tax year databases that allow owner name lookup
- GIS portals with owner-name search capability
- The county may have separate "property search" and "tax inquiry" sites — try both

#### State-Specific Assessor/Court Systems

Probate filings live in the court system. Property records live in the assessor/auditor
system. These are ALWAYS separate systems — never assume one site has both.

| State | Probate Court | Property Records | Notes |
|-------|--------------|-----------------|-------|
| **Tennessee** | Chancery Court (probate division) | County assessor: KGIS (Knox), TPAD via TN Comptroller (Blount, others) | KGIS uses ArcGIS Maps interface; TPAD is state-wide with jurisdiction codes |
| **Ohio** | Probate Court (separate from Common Pleas) | County Auditor (property search) | Each county auditor has own site; many use Tyler/iasWorld |
| **Florida** | Circuit Court (Probate Division) | Property Appraiser (each county) | Large counties (Miami-Dade, Broward) have robust online search |
| **Texas** | County Court / Statutory Probate Court | County Appraisal District (CAD) | CADs are independent entities, not county offices |
| **California** | Superior Court (Probate Division) | County Assessor | Some counties charge for online access |
| **General rule** | Probate FILING in court system | Property RECORDS in assessor/auditor/appraiser system | Always identify BOTH systems for a county |

**Search strategy:**
1. Try the decedent's full name: "Last, First Middle" and "Last, First"
2. If too many results, add middle initial
3. If no results, try maiden name if known (e.g., "Tomlinson" for "Tomlinson Howell")
4. Try name variants: with/without suffix, middle name vs initial, hyphenated vs not
5. Record ALL parcels returned — decedents often own multiple properties

#### Timeout and Error Recovery for Assessor Sites

County assessor websites are unreliable. Follow these rules strictly:

**Site not loading (timeout):**
- Allow 30 seconds maximum for initial page load
- If no response in 30 seconds: refresh once, wait another 15 seconds
- If still not loading: try an alternative URL (many counties have both a primary and backup site, or a separate "tax inquiry" portal)
- If no alternative exists: log the failure and move immediately to Step 3 (Deed Records)
- NEVER wait more than 60 seconds total on a single site load attempt

**Site returns an error page (500, 503, "temporarily unavailable"):**
- Note the error type and timestamp
- Do NOT retry more than once — these errors rarely resolve within a session
- Move to the next lookup tier immediately
- Document: "Knox County KGIS returned 503 at 2:15pm — moved to deed records"

**Search returns no results:**
- This does NOT mean the decedent owns no property — try all name variations listed above
- After exhausting name variations, proceed to Tier 2 (executor search) then Tier 3 (people search)
- Common reasons for false negatives: name stored differently (maiden name, middle initial vs full middle name), property in trust/LLC, assessor records lag behind ownership changes

**Site requires CAPTCHA or login:**
- If reCAPTCHA or similar: note for manual follow-up, do NOT attempt to solve automatically
- If login required: check if the county offers a free public access portal (many do)
- Document: "Blount TPAD required CAPTCHA — flagged for manual lookup"
- Move to next tier immediately

**Always document which sites were checked and their status**, even for failures:
```
Sites checked:
- Knox KGIS: searched "SMITH JOHN" — 0 results, retried "SMITH J" — 0 results
- Knox Tax API: searched 5 name variations — 0 results
- Knox Deed Records: searched "SMITH, JOHN" as grantee — 1 warranty deed found
```

**For each parcel found, capture:**
- Property address (full street address)
- City, State, Zip
- Parcel ID / APN / PIN
- Assessed value (land + improvements if available)
- Property type (residential, commercial, vacant land, farm land, etc.)
- Square footage, lot size, year built, bedrooms, bathrooms (if shown)
- Acreage (especially for rural/farm parcels)

**If the county assessor site is broken, not loading, or returns no results**, do NOT give
up — proceed IMMEDIATELY to Step 3 (Deed Records). County websites go down often; this is
not an excuse to stop searching.

### Step 3: Deed / Recorder Search (Critical Fallback)

This step is ESSENTIAL when the assessor site fails or returns nothing. County recorder
offices maintain deed records that show every property transfer. Navigate to the recorder's
site and search.

**How to find the recorder site:**
- Google: `{County Name} county {State} deed records search` or
  `{County Name} county {State} official records search`
- Texas: Many counties use `{county}.tx.publicsearch.us` (e.g., `brazos.tx.publicsearch.us`)
- Illinois: Check `{county}countyclerk.com` or the county clerk's website
- Other states: Search for the county recorder of deeds or county clerk website

**Search strategy:**
1. Navigate to the recorder/clerk's online search portal
2. Search by the decedent's last name: "Howell, Irene" or "Weber, Virginia"
3. Look for documents where the decedent is the **GRANTEE** (they received the property)
   — these are the properties they owned
4. Also check where they are the **GRANTOR** — if there's a deed transferring property
   AFTER the date of death, the property has already been moved out of the estate
5. Look for document types: WARRANTY DEED, QUIT CLAIM DEED, GRANT DEED, DEED OF TRUST
6. Extract the legal description from the deed (e.g., "WOODBROOK 00003 000T 00583 0156")
7. Cross-reference the legal description with the assessor to get a street address

**Analyzing deed results:**
- If the most recent deed shows the decedent as grantee (received property) with no
  subsequent transfer, the property is likely still in the estate
- If a TRUSTEE'S DEED or SUBSTITUTE TRUSTEE'S DEED shows the property going FROM the
  decedent TO a bank/savings institution, the property was likely foreclosed
- If a WARRANTY DEED shows the decedent transferring the property to someone else before
  death, they no longer owned it
- Note the dates of all transactions — this tells the story of ownership

### Step 4: Executor Address Verification

Before moving to aggregators, check whether the executor's address is actually owned by
the decedent. This is extremely common in probate — the surviving spouse or adult child
(now executor) often lives in the decedent's house.

1. Navigate to the county assessor site for the executor's county
2. Search by the executor's street address (or parcel search if available)
3. Check the owner name on file:
   - If it shows the DECEDENT's name: the executor's address IS the estate property
   - If it shows the EXECUTOR's name: it's the executor's own home, NOT estate property
   - If it shows BOTH names (joint ownership): it's likely an estate property
4. Record the parcel data if it's the decedent's property

This verification must be done on the actual county assessor, not aggregator sites.
Aggregators often show outdated or inaccurate ownership data and will lead to wrong
conclusions (e.g., showing someone as "resident" when they're actually not the owner).

### Step 5: Aggregator Cross-Reference (Supplemental Only)

Use aggregator sites ONLY to supplement or cross-reference data already found in Steps 2-4.
Never use these as the sole basis for a property finding.

1. **Zillow**: Search the decedent's name + city/state for sold/off-market listings
2. **Redfin**: Similar search
3. **County GIS Portal**: Separate mapping portal with parcel data

If aggregators show a property NOT found in assessor/deed searches, note it as LOW
confidence and flag for manual verification.

### Step 6: Google Dorking (Fallback Discovery)

When assessor and recorder searches come up empty, use targeted Google searches:

```
"{Decedent Full Name}" property {County} {State}
"{Decedent Full Name}" deed {County}
"{Decedent Full Name}" obituary (obituaries often say "of [address]" or "[Name], [age], of [City]")
"{Decedent Full Name}" "property tax" {County}
site:{county-assessor-domain} "{Decedent Last Name}"
```

Also try:
- Searching obituary sites (Legacy.com, local newspaper obits) — they frequently include
  the decedent's city of residence or street
- Searching the decedent's name in neighboring counties
- Searching name variants (maiden name, middle name, with/without suffix)

### Step 7: Multi-County Sweep

Search for properties in ALL geographies mentioned in the record, in this priority order:

1. **Probate filing county** (highest priority — most likely property location)
2. **Decedent's last known address county** (if different from probate filing)
3. **Executor's county** (property may have been transferred or executor may manage remote property)
4. **Any other mentioned counties/states** (discovered during obituary, deed, or executor research)

Start with #1 and work down. Stop expanding only when all mentioned geographies have been searched.

This is not optional. Even when the executor lives out of state, they may be managing
property in a completely different location. A probate filed in County A with an executor
in State B means you search BOTH County A and the executor's county in State B.

### Step 8: Compile and Validate

For each property discovered:
1. Verify the owner name on title matches the decedent (accounting for name variants,
   middle initials, suffixes)
2. Confirm the property has NOT been transferred after the date of death (check for
   recent deeds or sales)
3. Note if property was foreclosed or sold before death (still include in output with note)
4. Flag any properties where ownership is uncertain (partial name match, common name, etc.)
5. If the decedent died recently and the property appears in prior-year tax records but
   not the current year, it may have been transferred to the executor/estate — check the
   prior year's assessor data too

## Current-Year vs Prior-Year Tax Record Guidance

Deceased owners create a gap between reality (person is dead) and tax records (assessor
hasn't updated yet). This lag is a common source of false negatives in property lookups.

**Key rules:**

1. **Always check BOTH current and prior year records** when searching by decedent name.
   Many assessor sites let you toggle between tax years — use this feature.

2. **Assessor records typically lag 6-12 months behind ownership changes.** A person who
   died in January 2025 will likely still appear as owner on the 2025 tax roll. By 2026,
   the assessor may have updated to show "ESTATE OF [NAME]" or the executor's name.

3. **"Estate of" or "Personal Rep" in the owner field** means the assessor knows about
   the death — this IS your property. Search for "ESTATE OF SMITH" or "EST OF SMITH"
   as additional name variations.

4. **Prior-year records are especially important when:**
   - Death occurred more than 12 months ago (assessor may have already updated)
   - Current year shows no results for the decedent's name
   - You know the probate has been open for a while (estate may have been partially settled)

5. **Tax records showing a sale after the DOD** usually mean the estate already sold the
   property. Still document it but note: "Property sold by estate on [DATE] — no longer
   available."

## Property Type Disambiguation Rules

When a name search returns multiple parcels, you need to determine which are relevant and
how to prioritize them.

**Verifying the right property:**
- Match by address first, not just name — if you have any address hint (from obituary,
  people search, executor address), compare it against the returned parcels
- If the decedent's name is common ("John Smith"), require a second verification factor
  (address match, DOD/age consistency, county match) before accepting

**Multiple properties for the same decedent:**
- Document ALL parcels — each is a potential deal for the investor
- The primary residence is usually the highest-value residential parcel
- Investment properties, rental units, and vacant lots are all valuable leads
- Create separate output rows for each parcel, linked by case number

**Identifying property types from legal descriptions:**
- "Section/Township/Range" format (e.g., "SEC 14, TWP 9S, RNG 8E") = undeveloped land
  - Usually lower priority for REI investors focused on residential
  - BUT still valuable for land investors and certain exit strategies (subdivide, develop)
- "LOT [X], BLOCK [Y], [SUBDIVISION]" = platted subdivision lot (usually residential)
- "UNIT [X], BUILDING [Y]" = condo or townhouse

**Vacant land parcels:**
- Do NOT skip vacant land — it has value for certain strategies:
  - Subdivide and sell lots
  - Infill development
  - Novations to builders
  - Owner-finance to buyers
- Flag as property type "Vacant Land" so the investor can filter as needed
- Still capture assessed value, acreage, and parcel ID

## Probate Preset Logic

The automated pipeline uses a "probate preset" shortcut when courthouse records already
name a Personal Representative (PR) or executor. Understanding this helps avoid redundant
work:

- **When it triggers:** Notice has both `owner_name` (PR/executor) and `decedent_name`,
  and they are different people
- **What it does:** Sets the PR/executor as the decision-maker directly, skips obituary
  search entirely (prevents wrong obituary from overriding court-named executor)
- **Why this matters for property lookup:** The PR name is court-verified — use it
  confidently for Tier 2 (executor family search) without needing independent verification

## Handling Edge Cases

### No Property Found
If no property can be found after ALL steps (assessor, recorder/deeds, executor address
check, aggregators, Google dorking):
- Mark the record as "NO PROPERTY FOUND" in the output
- Note which sources were actually checked (not just identified)
- Common reasons: decedent was a renter, property was sold before death, property is in
  a trust or LLC name, property is in a different state entirely, property was foreclosed

### Multiple Properties Found
Include ALL properties as separate rows in the output CSV, each linked back to the same
case number so the user knows they came from the same probate filing. This is very common
with rural/farm properties — one decedent may own multiple parcels.

### Common Names
When a name is very common (e.g., "John Smith"), use these disambiguation strategies:
- Cross-reference with the executor's address or the probate county
- Use the date of death to eliminate active owners
- Check if middle names or initials narrow the results
- Flag the result as LOW confidence if ambiguity remains

### Executor Address IS the Property
Sometimes the executor lives at the decedent's property (common with surviving spouses
or adult children). You MUST verify this by checking the county assessor for the owner
name on the executor's address. Do NOT assume the executor's address is the property
just because the executor lives there — verify that the DECEDENT is listed as owner on
the assessor. If the assessor shows the EXECUTOR as owner (not the decedent), then it
is the executor's property, not the estate's.

### Deceased Owners Disappearing from Current Tax Year
When a property owner dies, their name may be removed from the CURRENT tax year's
assessor records and replaced with the executor or estate name. If searching the current
year returns nothing, check the PRIOR tax year's records. Many assessor sites let you
toggle between tax years. Also try searching "ESTATE OF [LAST NAME]" as the assessor
may have already re-titled the property.

### Trust / LLC Ownership
The decedent may have held property in a trust or LLC. If the direct name search returns
nothing:
- Try searching for "[Decedent Last Name] Trust" or "[Decedent Last Name] Living Trust"
- Try "[Decedent Last Name] LLC" or "[Decedent Last Name] Properties"
- Check if the obituary or any court records mention a trust

### Foreclosed / Previously Transferred Property
If deed records show the property was foreclosed (trustee's deed to a bank) or transferred
before death, the decedent no longer owns it. Still include it in the output but note:
"Property foreclosed [DATE] — no longer in estate" or "Transferred to [GRANTEE] on [DATE]"

### Minor / Invalid Records
Some rows in the CSV may not be valid probate cases (e.g., "Minor - Didn't log"). Skip
these records and note them in the output as "SKIPPED - [reason]".

### CAPTCHA-Protected Assessor Sites
Some county sites have added CAPTCHA challenges to their search pages. When encountered:
- Note the site and CAPTCHA type in the output notes
- Do NOT attempt automated CAPTCHA solving
- Move immediately to the next lookup tier
- Flag for manual follow-up: "Knox KGIS required CAPTCHA — needs manual lookup"

## Output Format

### Primary Output: Sift-Ready CSV

The CSV must have these columns in this order:

```
Case Number, Decedent Name, Property Address, City, State, Zip, Parcel ID, Assessed Value, Property Type, Bedrooms, Bathrooms, Sq Ft, Lot Size, Year Built, Executor Name, Executor Address, Executor City, Executor State, Executor Zip, Confidence, Notes
```

**Column definitions:**
- `Case Number`: Original case/file number from input
- `Decedent Name`: Cleaned decedent name (First Middle Last format)
- `Property Address`: Street address ONLY (no city/state/zip) — properly formatted with
  standard abbreviations (St, Ave, Blvd, Dr, Ln, Ct, etc.)
- `City`: City name
- `State`: Two-letter state abbreviation
- `Zip`: 5-digit zip code
- `Parcel ID`: County parcel/APN number (blank if not found)
- `Assessed Value`: Total assessed value in dollars (blank if not found)
- `Property Type`: SFR, Multi-Family, Condo, Townhouse, Vacant Land, Farm Land, Commercial
- `Bedrooms`: Number (blank if not found)
- `Bathrooms`: Number (blank if not found)
- `Sq Ft`: Living area square footage (blank if not found)
- `Lot Size`: Lot size in sq ft or acres (blank if not found)
- `Year Built`: 4-digit year (blank if not found)
- `Executor Name`: From input record
- `Executor Address`: From input record
- `Executor City`: From input record
- `Executor State`: From input record
- `Executor Zip`: From input record
- `Confidence`: HIGH / MEDIUM / LOW
  - HIGH = Owner name token overlap score >= 0.8, verified on county assessor
  - MEDIUM = Token overlap score 0.4-0.79, or deed-record-only verification
  - LOW = Score < 0.4, common name, ambiguous match, aggregator-only, or unverified
- `Notes`: Brief note on research outcome (e.g., "Verified on Champaign Co. assessor -
  2 parcels, 80 acres farm land", "Foreclosed 1987 — no longer in estate",
  "Executor address verified as executor's own property, not decedent's",
  "KGIS returned 503 — found via deed records instead")

**Address Formatting Rules:**
- Capitalize first letter of each word (123 Main St, not 123 MAIN ST or 123 main st)
- Use standard USPS abbreviations: St, Ave, Blvd, Dr, Ln, Ct, Cir, Pl, Rd, Way, Ter
- No periods in abbreviations (St not St.)
- Include unit/apt numbers where applicable (123 Main St Apt 4B)
- No trailing commas or extra whitespace
- For farm/rural parcels without a street address, use the legal description or parcel
  location (e.g., "Section 9, Township 20N, Range 9E" or just note "Rural parcel - no
  street address" and include the Parcel ID)

### Summary Report (Markdown)

After the CSV, also produce a brief markdown summary:

```
## Probate Property Finder — Results Summary

- **Records processed**: X
- **Properties found**: X (across Y parcels)
- **No property found**: X
- **Skipped**: X
- **Multi-property records**: X
- **Foreclosed/transferred**: X

### Sites Checked
[List each county site accessed, its status (working/down/CAPTCHA), and search outcome]

### Records Needing Attention
[List any LOW confidence matches or records where the user should verify manually]
```

## Processing Strategy for Bulk CSVs

When processing a CSV with many records:

1. **Parse the entire CSV first** — understand the column mapping and total record count
2. **Group records by county** — batch lookups by county to minimize site navigation
3. **Process county by county** — for each county:
   a. Navigate to the assessor/CAD site once
   b. Search each decedent name from that county
   c. Record all findings
   d. Move to the next county
4. **Check executor addresses** on the assessor for possible estate properties
5. **Search deed/recorder records** for any records that came up empty on the assessor
6. **Google dorking** as final fallback for stubborn records
7. **Compile the full output CSV** with all records

This county-batching approach is much faster than processing record-by-record because
you only have to figure out each county's assessor site once.

### Bulk Processing Performance Guidelines

**Sequential processing is recommended.** County assessor sites may rate-limit or block
rapid successive requests. Do not attempt parallel lookups against the same site.

**Batch sizing and progress saves:**
- Process in groups of 10-20 records, then save intermediate results to the CSV
- This protects against browser crashes, site outages, and session timeouts
- If the browser crashes mid-batch, you can resume from the last saved record

**Time estimates for planning:**
- Small batch (1-10 records): 5-15 minutes total
- Medium batch (10-50 records): 30-90 minutes total
- Large batch (50-100 records): 1.5-4 hours total
- Very large batch (100+ records): Plan for multiple sessions; estimate 3-8 minutes per record on average (includes retries and tier fallbacks)

**Rate limiting between requests:**
- Wait 2-3 seconds between individual searches on the same county site
- Wait 5-10 seconds between switching counties (different site, cold start)
- If a site starts returning errors or slow responses, back off to 10-15 second intervals

**Session management for large batches:**
- Save progress after every 10 records
- If processing will take more than 2 hours, plan for a break point
- Document where you stopped so the next session can pick up: "Completed records 1-45 of 120; resume at record 46 (SMITH, ROBERT)"

## Important Reminders

- The executor's mailing address is NOT the subject property in most cases. Verify on the
  assessor before assuming. The assessor's owner field is the source of truth.
- Always search by the DECEDENT's name, not the executor's name, when looking for property
  ownership (executor name search is Tier 2, only after decedent name fails).
- Properties may have been sold or transferred before death — check deed history.
- Some decedents may not own any real property at all (they rented). This is a valid finding.
- Accuracy matters more than speed. A wrong address wastes the investor's marketing dollars.
  When in doubt, flag it as LOW confidence rather than guessing.
- Save the output CSV to the workspace/Desktop folder so the user can download it.
- When a county assessor site is down or broken, pivot to deed records immediately — do
  not stop searching.
- Always check prior tax year records when a recently deceased owner doesn't appear in
  the current year.
- Document every site checked and its status, even failures — this helps the user know
  what has and hasn't been tried.
