---
name: deep-prospecting-v4
description: Deep prospect real estate leads to identify the heirs/decision-makers and exactly who must sign to sell when an owner is deceased or skip tracing fails. Use when the user provides a property address, filing, probate docket, foreclosure notice, or any distress record and needs the correct owner/heir/executor plus their contact info. PREFERRED path is API-based heir resolution (Enformion/Endato Person Search -> required-signer gating -> phone dedupe -> Trestle scoring), which is deterministic and far more reliable than manual research; the TruePeopleSearch/FastPeopleSearch/CyberBackgroundChecks browser waterfall is the fallback when no API access. Delivers a family tree, a who-must-sign table, scored phone numbers, and emails.
---

# Deep Prospecting

Deep prospecting is the process for identifying and verifying the heirs/decision-makers (owner/heir/executor) behind a distress lead, and determining **who must actually sign for the property to sell**. Core philosophy: **"When everyone hits a wall, we bring a shovel."**

> **Updated edition (v4).** Productionized against a live Enformion/Endato integration. Hardening since v3:
> - Detect API failure by **HTTP status**, never by the always-present `error` object.
> - **Never infer "child" from a surname match alone.** `relativeLevel "ab"` is closest kin, which also includes the spouse, siblings, and parents. Use `relativeType`, and match the surname as a **whole last-name token** (not a substring: "Maxwell" is not a "Well").
> - Recover the **year** from masked/partial dates (`9/XX/1955`, `3/XX/2026`) and dict-shaped death fields.
> - Anchor the **DOD sanity check to the notice's PUBLICATION date**, not the date you ingested the record.
> - **Surface, never silently resolve**, a death-index vs obituary DOD conflict.
> - Ships a runnable, dependency-light reference script (`scripts/enformion_person_search.py`) and a full field schema (`references/enformion-person-search.md`).

## Two Paths - Pick API-First

| Path | When | Reliability | Cost/record |
|------|------|-------------|-------------|
| **Primary: API-Based Heir Resolution** (recommended) | You have Enformion/Endato + Trestle API access | HIGH - heir set comes from the provider's relationship graph; deterministic; nothing inferred | ~$2.30-3.00 |
| **Fallback: 3-Site Browser Waterfall** | No API access, or API returns thin data | MEDIUM - manual assembly from obituaries/people-search sites; slower; hallucination risk | $0-0.15 + your time |

Use the **Primary Path** below first. It replaces hours of manual obituary and genealogy hunting with a handful of API calls and produces a grounded heir set, a signing analysis, and scored phones. Drop to the browser waterfall only when the API can't resolve a record.

**Grounding rule (both paths):** every name, address, phone, and relationship must come from a source you actually retrieved (an API response or a page you read). Never infer or fabricate an heir. Surface conflicts; do not resolve them by guessing.

## When to Use This Skill

- Skip trace returned no usable phone numbers
- Called 3+ attempts with no contact
- Vacant mailing address discovered
- Return mail (bad address)
- Probate cases (often only docket number available)
- Entity/LLC ownership (need actual decision-maker)
- Conflicting owner/address information in public records

## Input Requirements

User provides any combination of:
- Property address
- Owner name (full or partial)
- Filing/docket number
- Probate case information
- Foreclosure notice details
- Any distress record data

## Workflow Overview

> For a **deceased owner with API access**, go straight to the **Primary Path: API-Based Heir Resolution** (Steps A-E) below, then produce the signing analysis - it covers identification, family tree, signers, and scored phones in a few API calls. The numbered flow below is the manual/fallback sequence and the deeper research levels.

```
1. Auto-select research level (L1/L2/L3) based on input
2. Execute mandatory source checks for selected level
3. Build ownership/title analysis
4. Resolve identity variants (if applicable)
5. Map family tree (if deceased owner)
6. VERIFY heir alive/dead status (recursive until living heirs found)
7. Identify decision-maker(s) from VERIFIED LIVING heirs only
8. Skip trace decision-maker at TruePeopleSearch, FastPeopleSearch, and CyberBackgroundChecks
9. Compile skip trace results (phone numbers, emails, associates)
10. Deliver formatted research pack with contact info included
```

## Primary Path: API-Based Heir Resolution (Recommended)

When you have API access to a people-data provider (Enformion / Endato) plus a phone-scoring API (Trestle), run this FIRST. It is far more reliable than manual research because the heir set comes straight from the provider's relationship graph - real records with birth years and addresses - instead of being assembled by hand from a survivors paragraph (which risks missed or fabricated heirs). Five steps, ~$2.30-3.00/record, billed per match (misses are free).

```
A. Person Search the DECEASED            -> relatives graph + date of death   (1 call)
B. Derive REQUIRED SIGNERS               -> living children only (the cost gate)
C. Person Search each SIGNER (only)      -> current address + phones + their kids
D. DEDUPE phones across signers          -> one unique set (second cost gate)
E. Trestle-score each unique phone       -> dial tiers + litigator risk
```

### Step A - Find the heir set (1 call on the deceased)

Person Search the deceased owner, anchored to the property address **with ZIP**:
- Endpoint: `POST https://devapi.enformion.com/PersonSearch`
- Auth headers: `galaxy-ap-name`, `galaxy-ap-password`, `galaxy-search-type: Person` (use YOUR access-profile credentials; never hardcode them in shared files)
- Body: `{"FirstName","LastName","Addresses":[{"AddressLine2":"City, ST ZIP"}]}`

The matched person object carries:
- `dod` / `datesOfDeath[].dod` - date of death. It may be masked/partial (`3/XX/2026`) or nested in a dict - **recover at least the YEAR** (the conflict check below is year-level). Compare to the obituary/filing date; see DOD conflict below.
- `relativesSummary[]` - **the heir graph.** Each entry: `firstName/middleName/lastName`, `relativeType` (the actual label: Son, Daughter, Brother, Spouse, ...), `relativeLevel` (**`ab` = closest kin**; `ac`/`ad`/`ae` = grandchildren, cousins, in-laws), `dob` (masked day, e.g. `9/XX/1955` - regex out the year), `isDeceased`, `score` (higher = closer)
- `addresses[].fullAddress`, `phoneNumbers[]` (`phoneNumber`, `phoneType`, `isConnected`)

> **Gotcha 1:** Enformion ALWAYS returns an `error` object (`{inputErrors:[], warnings:[]}`) even on a successful search. Detect failure by **HTTP status**, NOT by the presence of `error`.
> **Gotcha 2:** `relativeLevel "ab"` is CLOSEST KIN, not "children." It includes the surviving spouse, siblings, and parents too. Use `relativeType` to tell them apart - never assume a same-surname `ab` relative is a child (see Step B).

### Step B - Derive the REQUIRED SIGNERS (cost gate #1)

The people who must sign to sell are the **heirs at law** - for an intestate estate, the decedent's living children. Filter the relatives graph:

```
REQUIRED SIGNERS = relativesSummary where
    relativeLevel == "ab"                     (closest-kin tier)
    AND relativeType is a CHILD type          (Son / Daughter / Child) when present
        -- if relativeType is BLANK, fall back to: surname matches
           AND the age gap from the decedent looks parental (~20-45 yrs)
    AND surname matches as a WHOLE last-name token (not a substring:
        "Maxwell" does NOT match decedent surname "Well")
    AND isDeceased == false                   (living)
    AND birth year is present                 (needed for the Step C lookup)
```

**Do not guess the relationship.** A blank `relativeType` at level `ab` could be a surviving **spouse** (frequently shares the surname) or a **sibling** (shares the birth surname). Mislabeling either as a child puts the wrong party on the deed - a spouse outranks children, and a non-inheriting sibling has no signing authority while a living spouse/child exists. When you cannot confirm the relationship, mark the heir **UNVERIFIED** and confirm it via the obituary, the deed, or a quick Step-C lookup before treating them as a required signer. This is the same grounding discipline as the obituary path: surface what you cannot verify, do not assert it.

**Signer-gating is the optimization:** run the paid per-person search (Step C) ONLY for these signers. Everyone else costs $0 and is handled by flags, not searches:
- **Deceased child** → flag for manual review: their share passes to THEIR children (per stirpes) - a probate layer to verify, not auto-search.
- **Close kin, different surname** (e.g. a married-out daughter) → flag as possible heir to verify; do not assume a share.
- **Grandchildren / cousins / in-laws** (level `ac`+) → ignore unless a child predeceased.

On a messy estate (12 relatives, 3 living children) this turns a 13-search run into 4.

### Step C - Resolve each signer (signers only)

For each required signer, Person Search by **Name + DOB year** - the provider's minimum-criteria combo. Name + city alone is REJECTED as insufficient criteria.
- Body: `{"FirstName","LastName","Dob":"1961"}`
- Same name + same age can return multiple people. **Disambiguate** by preferring the candidate whose address history overlaps the family area (property ZIP / city / street tokens from Step A).

Capture per signer: current residential address (most-recent first), `phoneNumbers[]`, and their own children (level `ab`, born ~1975+) for the family tree. Enrich emails with a Contact Enrichment call ($0.25, includes emails+phones) or a skip-trace vendor (Tracerfy ~$0.10).

### Step D - Dedupe phones BEFORE scoring (cost gate #2)

Siblings share household landlines, so the same number appears across multiple signers. Pool every signer's phones into ONE unique set (cap each signer to their top ~6) **before** scoring. Scoring the same family line 5 times is pure waste - a typical estate collapses ~30 phone hits to ~15-19 unique numbers.

### Step E - Score phones (Trestle)

Score each UNIQUE number once for activity + line type + litigator risk, then map results back to the signer(s) who own it. Dial-priority tiers (consistent with the phone-validator skill):

| Activity score | Tier |
|---------------|------|
| 81-100 | Dial First |
| 61-80 | Dial Second |
| 41-60 | Dial Third |
| 21-40 | Dial Fourth |
| 0-20 | Drop |

Prefer high-score, **DNC-clear** mobiles. Drop any number flagged litigator-risk regardless of score.

### API-Path Cost

| Stage | Call | Unit | Typical (5 signers) |
|-------|------|------|---------------------|
| Find heirs | Person Search (deceased) | $0.35/match | $0.35 |
| Resolve signers | Person Search × signers | $0.35/match | $1.75 |
| Emails (optional) | Contact Enrichment / Tracerfy | $0.25 / ~$0.10 | $0.50-1.25 |
| Phone scoring | Trestle × unique phones | ~$0.015 | ~$0.28 |

**≈ $2.30-3.00 per record.** Billing is per MATCH - misses are free. Signer-gating (B) and phone-dedupe (D) are the two levers that keep cost down.

### Reference implementation (ships with this skill)

You do not have to wire the API from scratch:
- **`scripts/enformion_person_search.py`** - self-contained (only needs `requests`). Runs the whole A-E waterfall on one record and prints the heir graph, the required-signer table, and a deduped master dial sheet (Trestle-scored when `TRESTLE_API_KEY` is set). Fabricates nothing - a miss prints as a miss.
  ```bash
  export ENFORMION_AP_NAME=...   ENFORMION_AP_PASSWORD=...   TRESTLE_API_KEY=...   # optional
  python scripts/enformion_person_search.py --first Jane --last Doe \
      --street "123 Oak St" --city Knoxville --state TN --zip 37918
  ```
- **`references/enformion-person-search.md`** - endpoint, auth, full request/response schema, `relativeLevel` codes, the cost model, and every gotcha above in one place.

> **Running this in a bulk pipeline?** Gate it behind an opt-in flag and call Step A only (one search per deceased record) for the heir set; reserve the full A-E waterfall (per-signer searches + Trestle) for single high-value records. Bill per match, so signer-gating and phone-dedupe are what keep an automated run affordable.

## Who Must Sign to Sell (Signing Analysis)

This is the deliverable that makes heir research actionable. A deceased owner means the property belongs to the **heirs**, not one seller - and a deal only closes if everyone with an undivided interest signs.

**General rule (intestate / no will):** the property passes to the decedent's children as **tenants in common with equal undivided shares** (5 children → 1/5 each). To convey clear title, **every living child must sign the deed** - OR one is appointed estate administrator and sells through probate with court approval. A single holdout can block the sale.

Always produce a signer table:

| Heir (child) | Est. share | Lives at property? | Signature required? | Notes |
|--------------|-----------|--------------------|--------------------|-------|
| {name} | 1/N | yes/no | **Required** | on-site / out-of-state / etc. |

**Signing risk flags - verify via title search / probate attorney; never state as legal conclusions:**
- **Surviving spouse?** Takes a share under intestate law and must also sign.
- **A will?** Overrides the equal split and names an executor with authority.
- **Did a child predecease the parent?** Their share goes to their kids per stirpes - extra signers.
- **Recent second death in the household?** May add a probate layer (see DOD conflict).
- **Close kin with a different surname** (married-out daughter)? Confirm whether a child - it changes every share.

Out-of-state signers are the most common closing bottleneck - identify and engage them early.

## Level Selection Logic

> The L1-L4 levels below describe research DEPTH and apply to the fallback browser path (and to API records that need extra title/genealogy work). On the API path, Steps A-E above resolve most deceased-owner records directly; escalate to L4 only when signers are unresolved, all heirs are deceased through 3 generations, or title defects appear.

**L1 Enhanced Skip Trace** - Skip trace yielded no usable mobiles; verify with light public records
- Trigger: No phone numbers returned, but owner appears alive and reachable
- Focus: Cross-verification and simple public record lookups
- Time: 5-10 minutes per record
- Cost: $0.10-$0.15/record (skip trace vendor cost)

**L2 Ownership Verification** - Conflicting/incomplete owner/address/name; resolve via title/deed chain + history
- Trigger: Public records show conflicting information, multiple name variants, or incomplete data
- Focus: Title work (deeds), Google dorking, property history
- Time: 15-30 minutes per record
- Cost: $0/record (manual research, your time only)

**L3 Heir Research** - Owner likely deceased; identify living decision-maker via obits/genealogy
- Trigger: Owner appears deceased, heir/executor contact unknown
- Focus: Obituaries, Ancestry, newspapers, family tree mapping, **heir verification loop**
- Time: 30-60 minutes per record
- Cost: $25-$50/month (Ancestry.com subscription) + your time

**L4 Curative Title (Escalation)** - All identified heirs deceased through 3 generations, title clouds, or competing claims
- Trigger: Heir verification loop exhausted with no living heirs, or title defects found
- Who handles: Real estate attorney or title company
- Cost: $500-$2,000+
- Timeline: Days to weeks
- Deliverable: Title opinion letter or quiet title action filing

### When to Escalate to L4

- All identified heirs are deceased through 3 generations (heir verification loop exhausted)
- Title clouds: conflicting deeds, unrecorded conveyances, missing instruments
- Competing claims: multiple parties asserting ownership
- Tax sale redemption period disputes
- Heirship affidavit needed but no cooperating heir found

## Source Checklist by Level

### L1 Baseline Sources
- [ ] County Assessor/CAD (ownership & mailing)
- [ ] Recorder/Deed image (names, middle initials, instrument type)
- [ ] Google dorking on owner/address (site:, intitle:, filetype:)
- [ ] Tax payment history (or FOIA path if not public)
- [ ] Clerk civil/criminal/dockets (owner + co-owners)
- [ ] Skip trace completed at TruePeopleSearch, FastPeopleSearch, and CyberBackgroundChecks

### L2 Sources (add to L1)
- [ ] Deed chain (last 3-5 instruments) + instrument type
- [ ] Name-variant sweep (aliases, maiden/married, initials)
- [ ] Cross-county property/recorder/docket searches from address history

### L3 Sources (add to L1 as needed)
- [ ] Obituaries: Legacy.com, Newspapers.com, FindAGrave, Ancestry
- [ ] Minimal family tree (spouse/children/siblings + current cities)
- [ ] **Heir Verification Loop** (verify alive/dead status for each heir)
- [ ] Decision-maker identification (executor/surviving spouse/oldest child) - **from verified living heirs only**
- [ ] Skip trace completed for decision-maker at TruePeopleSearch, FastPeopleSearch, and CyberBackgroundChecks

## Research Execution

### Phase 1: Initial Verification and Title Review

| Step | Action | Purpose |
|------|--------|---------|
| 1.0 | Verify Current Ownership | Confirm seller still owns property; check for recent sales |
| 1.1 | Review the Deed (Critical) | Analyze for middle initials, relationships, transaction type (arms-length, quitclaim, inherited) |
| 1.2 | Identify Title Issues | Look for installment agreements, multiple owners/heirs |
| 1.3 | Initial Google Search | Use owner name + property address with dorking operators |

### Phase 1.5: Property Address Lookup (Probate - no address on file)

When the notice has a decedent name and PR/executor but NO property address (common for courthouse probate records), use this tiered lookup to find the decedent's property.

**Tier 1: County Tax API Name Search (try first)**
- Search county assessor by decedent name (format: "LAST FIRST MIDDLE")
- Score results by token overlap between search name and owner-on-file
- Accept matches scoring 0.4 or higher (40% token overlap)
- Try multiple name variations in order: full name, first+last only, without suffix (JR/SR/III)
- For female decedents with 4+ name parts (e.g., "LULA ELIZABETH MASSIE JONES"), also try penultimate name as surname (maiden name variant)

**Tier 2: Executor Family Search (if Tier 1 fails)**
- Search county assessor by the executor/PR name instead
- Look for properties where the decedent's last name appears in the owner field
- Logic: family property may have been transferred to the executor who is a relative
- Skip properties at the executor's own known address (that is their home, not the decedent's)

**Tier 3: People Search (if Tiers 1-2 fail)**
- Search TruePeopleSearch or FastPeopleSearch for the decedent's name
- Look for their last known address in the target county (e.g., Knoxville for Knox County)
- Use the address found as the property address

### Phase 2: Genealogy & Historical Research (L3)

| Step | Action | Purpose |
|------|--------|---------|
| 2.0 | Search for Obituaries | Find survivors, spouses, relationships |
| 2.1 | Newspapers.com Deep Dive | Historical mentions, city directories, marriage announcements |
| 2.2 | Map the Family Tree | Build tree from obituaries and deeds |
| 2.3 | "Go Back to Go Forward" | Use older records to find new leads |

#### DOD Sanity Check (Critical - prevents wrong-person matches)

When matching an obituary to a notice filing, verify the date of death is within range:

- **Anchor on the notice's PUBLICATION date** (when the legal notice ran / the filing date) - NOT the date you ingested the record into your system. If you stamp an "added on" date at import, do not use it here: every record imported today would share that date and the check would pass everything.
- **Rule:** Reject obituary matches where DOD is more than 3 years before the notice publication date
- **Rationale:** Probate is typically filed within 1-2 years of death; 3 years gives margin for delayed filings
- **Also reject:** DOD that is after the publication date (future death is impossible)

| Filing Date | Obituary DOD | Gap | Result |
|-------------|-------------|-----|--------|
| 2025-03-01 | 2024-06-15 | 0.7 years | ACCEPT |
| 2025-03-01 | 2022-01-10 | 3.1 years | BORDERLINE - accept with caution |
| 2025-03-01 | 2021-11-01 | 3.3 years | REJECT - likely wrong person |
| 2025-03-01 | 2014-05-20 | 10.8 years | REJECT - definitely wrong person |

Apply this check to both full obituary page matches and snippet/preview matches. When a match fails the DOD sanity check, continue searching for a different obituary - the name match was likely a different person with the same name.

#### Death-Index DOD vs Obituary Date Conflict (API path)

On the API path you get a `dod` straight from the provider's death index, which can disagree with the obituary/filing date by years. **Do not silently pick one.** A common real pattern: the original owner died long ago, a family member held and maintained the home, and *that* person's recent death triggered the new filing and the obituary. Implications:

- The **heir set (the children) usually holds either way** - your signer list stands.
- But the estate currently in probate may belong to the **recent decedent, not the named owner** - which changes your call opening ("sorry for your recent loss" vs. a decades-old estate) and the title path.
- Surface the conflict in the deliverable. If it matters for the deal, run one more Person Search to identify the recent household death (e.g., a surviving spouse who died recently).

### Phase 2.5: Heir Verification Loop (L3 - CRITICAL)

**Purpose:** Verify each identified heir is alive before adding as potential decision-maker. If deceased, find THEIR heirs and repeat until living heirs are confirmed.

#### Verification Process

```
Start with obituary survivors list.

FOR EACH heir identified in Phase 2:
    1. Search for heir's obituary/death record
    2. Check FindAGrave for burial record
    3. Search "[HEIR NAME] obituary [CITY/STATE]"
    4. Check Ancestry death records if available
    
    IF heir confirmed ALIVE:
        -> Mark as VERIFIED LIVING in heir map
        -> Add to potential decision-maker list
        -> Continue checking remaining heirs until 2-3 verified living found
        
    IF heir confirmed DECEASED:
        -> Mark as DECEASED in heir map
        -> Record DOD if found
        -> Search for THEIR obituary to find survivors
        -> Add their heirs to verification queue
        -> REPEAT verification process for new heirs (next generation)
        
    IF status UNCERTAIN:
        -> Mark as UNVERIFIED in heir map
        -> Note last known activity date
        -> Include in decision-maker list with LOW confidence

STOP WHEN:
    -> Found 2-3 verified living heirs with decision-making authority, OR
    -> All identified heirs verified as living, OR
    -> Reached 3rd generation with no living heirs found -> escalate to L4
```

#### Verification Sources (in order of reliability)

| Source | What to Look For | Reliability |
|--------|------------------|-------------|
| FindAGrave | Burial record, DOD, family links | HIGH |
| Legacy.com | Obituary with survivors listed | HIGH |
| Newspapers.com | Death notice, obituary | HIGH |
| Ancestry Death Records | SSN death index, state records | HIGH |
| Google "[Name] obituary [City]" | News articles, funeral home posts | MEDIUM |
| TruePeopleSearch/FastPeopleSearch | No record found or "Deceased" notation | MEDIUM |
| No recent activity (10+ years) | Indirect indicator only | LOW |

#### People Search Verification Order

For each heir, check these sites in order to confirm alive/dead status:
1. **TruePeopleSearch.com** - Primary. If a current record exists with recent address, likely alive.
2. **FastPeopleSearch.com** - Backup. Pulls from slightly different database; may show "Deceased" tag.
3. **CyberBackgroundChecks.com** - Deep data. Best for associates/relatives cross-reference.

If all three show no record and no recent activity for 10+ years, mark as "?" (unverified) and note the gap.

#### When to Stop the Loop

- Found at least 2-3 verified living heirs with decision-making authority OR
- All identified heirs verified as living OR
- Reached 3rd generation with no living heirs found (escalate to L4/attorney)

### Phase 3: Locating the Target (Verified Living Heir/Executor)

| Step | Action | Purpose |
|------|--------|---------|
| 3.0 | Identify the Target | Select from **verified living heirs only** using tiebreaker rules |
| 3.1 | Search for the Target | Use full name, city, estimated age |
| 3.2 | Cross-Reference and Validate | Use second source to validate |
| 3.3 | Document Findings | Record all valid contact numbers |

#### Decision-Maker Tiebreaker Rules

When multiple verified living heirs exist, select the decision-maker using this priority order:

| Priority | Who | Why |
|----------|-----|-----|
| 1 | Named executor/PR from court records (marked with ★) | Legal authority - they ARE the decision-maker by court appointment |
| 2 | Spouse of deceased | Closest family member, likely co-owner of property |
| 3 | Adult child living in same county as property | Geographic proximity = more engaged with the property |
| 4 | Adult child living in same state | Still accessible, likely involved in estate |
| 5 | Sibling (if no surviving spouse or children) | Next of kin |
| 6 | Grandchild (if children deceased) | Per stirpes inheritance |

**Tiebreaker within the same priority level:** The heir with the most recent, most complete contact information wins. A record with a current address + mobile phone from 2 sites beats a record with only a landline from 1 site.

**Multiple decision-makers:** When two heirs share equal authority (e.g., co-executors, or two children at same priority), trace BOTH. The first one to respond becomes your primary contact. List the second as backup in the Skip Trace Results Card.

### Name Disambiguation Guidance

When researching common names (John Smith, Mary Jones, etc.), use these techniques to confirm you have the right person:

1. **Middle name/initial:** Always capture from deed or court filing. "John R. Smith" vs "John T. Smith" eliminates most false matches.
2. **Age range cross-reference:** If obituary shows DOD and mentions age or birth year, verify it aligns with the deed history timeline. A 2020 obituary for a 45-year-old cannot be the same person who signed a deed in 1965.
3. **City/county match:** Confirm the person's last known location matches the property county. An obituary for "John Smith" in Portland, OR is unlikely to be your Knox County, TN property owner unless address history connects them.
4. **Associated names:** Cross-reference spouse/children names from obituary against names on deeds, tax records, or court filings.
5. **When ambiguous after all checks:** Mark confidence as LOW. Note both candidate identities in the research pack. Do NOT guess - present both options and let the caller verify during first contact.

## Fallback Path: Manual Skip Trace (3-Site Browser Waterfall)

> Use this only when you have no people-data API access, or the API (Primary Path Steps A-E) returned thin data for a person. It is slower and carries more hallucination risk because you assemble the heir set by hand - apply the grounding rule strictly: record only what a page actually shows.

After identifying the decision-maker(s), run them through all three sites in order. Each site pulls from slightly different databases, so hitting all three maximizes your chance of getting a working number. Browse each site directly using the browser tools.

### Getting past Cloudflare / JS walls (Scrapfly ASP fetcher)

The free people-search and county/genealogy sites increasingly sit behind Cloudflare or render their data with JavaScript, so a plain HTTP fetch (and the sandboxed browser your AI agent uses) gets a challenge page instead of data. Route those fetches through Scrapfly's Anti-Scraping Protection (residential proxy + headless render) with the bundled, self-contained `scripts/scrapfly_fetch.py` (needs only `requests` + a `SCRAPFLY_KEY`):

```bash
export SCRAPFLY_KEY=scp-live-...
python scripts/scrapfly_fetch.py "https://<assessor-or-deed-portal>/..."     # title vesting, grantee names
python scripts/scrapfly_fetch.py "https://www.findagrave.com/memorial/..."   # death confirmation / heirs
python scripts/scrapfly_fetch.py "<people-search detail url>" --out page.html
```

Validated sweet spot vs. limits (so you spend credits where they pay off):
- **County / records / genealogy portals** (assessor & deed datalets, FindAGrave, Legacy, court info pages): ASP clears them reliably. This is the payoff - e.g. pull a parcel's deed instrument + grantee names to settle how title is vested (entirety vs sole), or read an obituary the agent browser was blocked from.
- **Hardened people-search aggregators** (TruePeopleSearch, FastPeopleSearch): ASP is frequently IP-banned after the first hit, AND their results pages carry no phones (you must hop to each person's detail page). Best-effort only - lead with the API heir graph (Primary Path) and reach relatives through a known, verified contact rather than grinding these.
- **Records a county does not publish online** (some estate/probate cases, deed IMAGES behind a paid subscription): Scrapfly cannot fetch what is not online. That is a data-availability limit, not a Scrapfly failure - confirm via a phone/in-person request to the clerk instead.

In the SiftStack pipeline this is `src/scrapfly_browser.py` (importable `ScrapflyBrowserClient.fetch(url)`), and `run_deep_prospect.py --fallback-urls "<deed>,<obit>,<docket>"` pulls these pages inline during the same heir-waterfall run.

### Site 1: TruePeopleSearch.com (Primary)

The "King" of free skip tracing and the first stop for every manual trace. It often surfaces a high volume of wireless numbers and landlines, plus previous addresses that help confirm absentee owners who recently moved.

**How to search:**
1. Navigate to https://www.truepeoplesearch.com
2. Search by name: `{FULL NAME}` in `{CITY, STATE}`
3. If name search is too broad, use the address search: `{PROPERTY ADDRESS}` or `{TAX MAILING ADDRESS}`
4. **Scroll past the "Sponsored Results"** (they look like buttons) - the free data lives in the plain-text "Details" section below
5. Record: all phone numbers (mobile + landline), current/previous addresses, age, associated names

**What to capture:**
- Phone numbers (label as mobile/landline where shown)
- Current and previous addresses (confirm against property/tax records)
- Associated people / relatives (cross-reference with heir map if L3)
- Age (confirm against deed/obit timeline)

### Site 2: FastPeopleSearch.com (Backup / Cross-Reference)

The strongest alternative to TruePeopleSearch. Many investors report it pulls from a slightly different database, so it can unlock leads the first site misses. Clean layout makes it easy to copy/paste into a CRM.

**How to search:**
1. Navigate to https://www.fastpeoplesearch.com
2. Search by name: `{FULL NAME}` in `{CITY, STATE}`
3. If name returns too many results, try address search with `{KNOWN ADDRESS}`
4. Record any NEW phone numbers or addresses not already found on TruePeopleSearch
5. Pay attention to "Also Known As" names - useful for L2 name-variant cases

**What to capture:**
- Any phone numbers NOT already found on TruePeopleSearch
- Email addresses (sometimes shows these more reliably)
- "Also Known As" aliases (feed back into L2 name variant sweep)
- Current address (cross-reference for validation)

### Site 3: CyberBackgroundChecks.com (Deep Data / Associates)

A newer favorite in REI communities for its depth on email addresses and associates. The "Possible Associates" and "Relatives" lists are especially valuable for probate leads and elusive landlords - calling a relative is often the best route.

**How to search:**
1. Navigate to https://www.cyberbackgroundchecks.com
2. Search by name: `{FULL NAME}` in `{CITY, STATE}`
3. Focus on the "Possible Associates" and "Relatives" sections
4. Record any NEW contacts, email addresses, and associate names not found on the first two sites

**What to capture:**
- Email addresses (this site is often the best source for these)
- "Possible Associates" list (business partners, neighbors, co-signers)
- "Relatives" list (cross-reference with heir map; may reveal heirs you missed)
- Any additional phone numbers

### If All 3 Sites Return Nothing

When TruePeopleSearch, FastPeopleSearch, AND CyberBackgroundChecks all return no results for a person:

1. **Try Spokeo.com** (paid, ~$2/search) - deeper database, sometimes finds records the free sites miss
2. **Try Ancestry.com address search** - search by last known city/state, may find historical addresses
3. **Try Google**: `"{Full Name}" "{City}" phone` or `"{Full Name}" "{City}" address`
4. **Check obituary for address clues** - obituaries often mention city of residence, church, or employer
5. **If critical lead**: escalate to Deep Prospecting L4 (title attorney) for professional skip trace
6. **If not critical**: mark as "SKIP TRACE FAILED - direct mail only" and route to the Needs Mailed preset

> **Do not spend more than 15 minutes on a single person's skip trace.** If the free sites fail and Spokeo fails, the person is either not in public databases (common for younger people, recent immigrants, or privacy-conscious individuals) or the name is incorrect. Move to mail-only or L4 escalation.

### Skip Trace Validation

After running all three sites, cross-reference the results to build confidence:

**High-Confidence Match (ready to dial):**
- Same phone number appears on 2+ sites
- Associated addresses include subject property or tax mailing address
- Relatives/associates match names from deeds, obits, or heir map
- Age band fits deed history and obit dates

**Medium-Confidence Match (dial but verify):**
- Phone number appears on only 1 site
- Address matches but no relative/associate confirmation
- Name variant matches but slightly different city

**Low-Confidence Match (verify before investing time):**
- Only partial name match
- No address overlap with known records
- No relative/associate cross-reference

## Heir Map Template (L3)

When deceased owner identified, create visual heir map with **verification status**:

```
Decedent: † {DECEDENT FULL} (DOD {YYYY-MM-DD}) [{CITY, ST}]
|
+- Spouse/Partner:
|  +- {STATUS} {SPOUSE FULL} [{CITY, ST}] {DOD if deceased}
|
+- Children:
|  +- {STATUS} {CHILD 1} [{CITY, ST}] {DOD if deceased}
|  |   +- Grandchildren (if Child 1 deceased):
|  |       +- {STATUS} {GRANDCHILD 1A} [{CITY, ST}]
|  |       +- {STATUS} {GRANDCHILD 1B} [{CITY, ST}]
|  |
|  +- {STATUS} {CHILD 2} [{CITY, ST}] {DOD if deceased}
|  |   +- Grandchildren (if Child 2 deceased):
|  |       +- {STATUS} {GRANDCHILD 2A} [{CITY, ST}]
|  |
|  +- {STATUS} {CHILD 3} [{CITY, ST}]
|
+- Siblings:
   +- {STATUS} {SIBLING 1} [{CITY, ST}]
   +- {STATUS} {SIBLING 2} [{CITY, ST}]

STATUS MARKERS:
  †  = Verified DECEASED (with DOD if known)
  ✓  = Verified LIVING (confirmed no death record)
  ?  = UNVERIFIED (status unknown, needs confirmation)
  ★  = Executor (confirmed via probate filing)
  ▸  = Recommended decision-maker (verified living + authority)
  ●  = Current living owner (confirmed on title/deed)
```

### Heir Map Example (with verification)

```
Decedent: † John Robert Smith (DOD 2019-03-15) [Dallas, TX]
|
+- Spouse:
|  +- † Mary Jane Smith (DOD 2022-08-20) [Dallas, TX]
|
+- Children:
|  +- † Robert John Smith Jr. (DOD 2021-01-10) [Fort Worth, TX]
|  |   +- Grandchildren:
|  |       +- ✓ ▸ Michael Robert Smith [Austin, TX] <- DECISION-MAKER
|  |       +- ✓ Jennifer Smith-Lopez [Houston, TX]
|  |
|  +- ✓ Susan Smith-Williams [Plano, TX]
|  |
|  +- ? David Allen Smith [Last known: Arlington, TX, 2015]
|
+- Siblings:
   +- † William Smith (DOD 2018-05-22) [Oklahoma City, OK]

VERIFICATION SUMMARY:
- Verified Living: Michael R. Smith, Jennifer Smith-Lopez, Susan Smith-Williams
- Verified Deceased: Mary Jane Smith, Robert John Smith Jr., William Smith
- Unverified: David Allen Smith (no recent records, possible deceased)
- Recommended Decision-Maker: Michael Robert Smith (oldest grandchild, verified living)
```

## Deliverable Format

Deliver the finished pack as a **PDF** (it uploads cleanly into DataSift / Sift as a record attachment and reads better than a raw .md). In the SiftStack pipeline: `python src/deep_prospect_pdf.py <pack>.md` renders a branded, dash-clean PDF (heir map + master dial sheet stay monospaced). Keep the writing free of em/en dashes.

Output a research pack with these sections (headings + bullets only, no JSON):

```
## 1) Level Selected & Why
[State L1/L2/L3 and the specific reason]
[Include estimated time and cost for this level]

## 2) Source Checklist
[Mark [x]/[ ] with 1-line notes for each source checked]

## 3) Title & Ownership
- Current owner(s)
- Instrument type summary
- Red flags (QCD, installment/contract, etc.)

## 4) Property Address Lookup (if probate with no address)
- Tier attempted and result
- Name variations tried
- Match score (if Tax API used)

## 5) Identity Resolution (if variants exist)
- Which variant won & why (1-2 lines)
- Name disambiguation notes (middle initials, age range, city match)

## 6) Genealogy/Heir Findings (if family/estate elements)
- Obit links found
- DOD sanity check result (filing date vs DOD gap)
- Survivors identified
- Relationship notes

## 7) Heir Verification Summary (L3 required)
- Total heirs identified: [#]
- Verified living: [# and names]
- Verified deceased: [# and names with DODs]
- Unverified: [# and names with notes]
- Generations searched: [1st/2nd/3rd]

## 8) Heir Map (L3 required; L1/L2 if relationships relevant)
[ASCII tree per template above WITH verification status markers]

## 8b) Who Must Sign to Sell (REQUIRED for any deceased owner)
[Signer table: Heir | Est. share | Lives at property? | Signature required? | Notes]
[Signing risk flags to verify (spouse / will / predeceased child / recent death / married-out daughter)]
[Call out out-of-state signers explicitly - they are the usual closing bottleneck]

## 9) Decision-Maker Identified
- Name: {FULL NAME}
- Relationship: {owner/heir/executor/spouse}
- Verification Status: {✓ Verified Living / ? Unverified}
- Selection Reason: {e.g., "Priority 1: Named executor from court filing"}
- Current Address: {best known mailing address}
- Estimated Age: {age range based on records}
- Confidence: {HIGH/MEDIUM/LOW with reasoning}

## 10) Skip Trace Results
[Include the formatted results card - see template below]
```

## Skip Trace Results Output

At the end of research, after browsing all three skip trace sites, compile findings into a **Skip Trace Results Card**:

```
=====================================================================
                  SKIP TRACE RESULTS
=====================================================================

DECISION-MAKER: {FULL NAME}
  Relationship: {owner/heir/executor/spouse}
  Status:       {✓ Verified Living}
  Est. Age:     {AGE RANGE}
  Selected:     {Priority # - reason}

--- PHONE NUMBERS --------------------------------------------------
  #  | Number          | Type     | Source(s)       | Confidence
  1  | (xxx) xxx-xxxx  | Mobile   | TPS, FPS        | HIGH
  2  | (xxx) xxx-xxxx  | Landline | TPS             | MEDIUM
  3  | (xxx) xxx-xxxx  | Mobile   | CBC             | MEDIUM

--- EMAIL ADDRESSES ------------------------------------------------
  1  | xxxx@xxxxx.com  | CBC, FPS
  2  | xxxx@xxxxx.com  | CBC

--- ADDRESSES ------------------------------------------------------
  Current:  {ADDRESS} (confirmed on TPS + FPS)
  Previous: {ADDRESS} (matches tax mailing)

--- ASSOCIATES & RELATIVES -----------------------------------------
  * {NAME} - {RELATIONSHIP} - {CITY, ST} (from CBC)
  * {NAME} - {RELATIONSHIP} - {CITY, ST} (from CBC)

--- VALIDATION -----------------------------------------------------
  [x] Phone on 2+ sites    [x] Address matches records
  [x] Relatives match       [x] Age fits timeline

BACKUP DECISION-MAKERS (also traced):
  * {NAME 2} - {CITY, STATE} - {RELATIONSHIP} - {PHONE}
  * {NAME 3} - {CITY, STATE} - {RELATIONSHIP} - {PHONE}

SOURCE KEY: TPS = TruePeopleSearch | FPS = FastPeopleSearch | CBC = CyberBackgroundChecks
=====================================================================
```

The user can dial directly from these results - no additional lookup step needed.

### Master Dial Sheet (multi-signer / deceased-owner records)

When the property has multiple required signers (the usual deceased-owner case), one decision-maker card is not enough - you need a contact for EVERY signer plus a single deduped dial list. After Steps D-E (or the browser waterfall on each signer), output a master dial sheet, best number first, with the owning signer and DNC status:

```
=====================================================================
        MASTER DIAL SHEET - {PROPERTY} - {ESTATE} (deduped)
=====================================================================
PHONE            SCORE  TIER         TYPE      REACHES                 DNC
(xxx) xxx-xxxx   100    Dial First   Mobile    {shared household}      clear   <- start here
(xxx) xxx-xxxx   100    Dial First   Mobile    {Signer A}              clear
(xxx) xxx-xxxx    70    Dial Second  Landline  {Signer B}              clear
(xxx) xxx-xxxx   100    Dial First   Mobile    {out-of-state signer}   clear
(xxx) xxx-xxxx   100    Dial First   Mobile    {Signer C}              DNC ⚠
---------------------------------------------------------------------
Shared family landlines collapse to ONE entry. Lead with the highest-score,
DNC-clear mobile that reaches the on-site / most-engaged signer.
=====================================================================
```

Pair it with a one-line contact card per signer (name, age, current address, best number, email) so every required signature has a way to be reached.

## Cost Summary by Level

| Level | Per-Record Cost | Time per Record | What You Pay For |
|-------|----------------|-----------------|------------------|
| **Primary: API Heir Resolution** | **$2.30-$3.00** | **2-5 min** | Enformion/Endato Person Search ($0.35/match) + optional emails + Trestle (~$0.015/phone) |
| L1 Enhanced Skip Trace | $0.10-$0.15 | 5-10 min | Skip trace vendor API/credits |
| L2 Ownership Verification | $0 | 15-30 min | Your time only (free public records) |
| L3 Heir Research | $25-$50/mo shared | 30-60 min | Ancestry.com subscription + your time |
| L4 Curative Title | $500-$2,000+ | Days to weeks | Real estate attorney or title company |

Notes:
- The **API Primary Path is both faster and more reliable** than manual L3 research and should be the default for deceased-owner records. Billing is per match (misses free); signer-gating + phone-dedupe keep it at the low end. Many providers include a free monthly request allotment that covers the first few hundred records.
- L3 cost is a monthly subscription shared across all records researched that month, not per-record. At 20+ probate records/month, Ancestry cost is ~$1-2/record.

## Key Tools Reference

| Tool | Primary Use | Notes |
|------|-------------|-------|
| **Enformion / Endato Person Search** | **Primary heir discovery + per-signer resolution** | One call on the deceased returns the relatives graph + DOD; search signers by Name+DOB; `relativeLevel ab` = children. $0.35/match |
| **Enformion Contact Enrichment** | Emails + phones for a known signer | $0.25/match; one-vendor alternative to a separate skip-trace call |
| **Trestle** | Phone activity scoring + litigator check | ~$0.015/lookup; dedupe phones before scoring; tiers 81-100 Dial First … 0-20 Drop |
| **Scrapfly ASP fetch** (`scripts/scrapfly_fetch.py`) | Clear Cloudflare/JS walls on county-record + genealogy pages (L2/L3 fallback) | Residential proxy + headless render; sweet spot = assessor/deed datalets, FindAGrave, Legacy, court pages; hardened people-search aggregators often ban it |
| Tracerfy | Skip-trace enrichment (phones/emails) | ~$0.10/hit; optional email source on the API path |
| County Assessor/CAD | Ownership verification, property lookup | Token overlap scoring (0.4+ threshold for name match) |
| Knox Tax API | Tier 1 property lookup by decedent name | Search "LAST FIRST" format; multiple name variations |
| Recorder/Deed image | Title analysis, middle initials, transaction types | Look for QCD, installment, arms-length |
| Google Dorking | Narrowing search results | Use site:, intitle:, filetype: operators |
| Ancestry.com | Family trees, obituaries, death records | Essential for L3 cases ($25-50/mo) |
| Newspapers.com | Historical mentions, directories, obituaries | Useful for pre-2000 records |
| FindAGrave | **Heir verification**, burial records, family links | Primary source for death confirmation |
| Legacy.com | Obituaries with survivor lists | Key for heir identification AND verification |
| TruePeopleSearch.com | Phone numbers, addresses, associates | Primary skip trace site; scroll past sponsored results |
| FastPeopleSearch.com | Phone numbers, emails, aliases | Backup skip trace; pulls from different database |
| CyberBackgroundChecks.com | Emails, associates, relatives | Best for deep associate/relative data; great for probate |
| Social Media/LinkedIn | Professional/personal contact info | Last resort for living heirs |

## Error Handling

If information cannot be found:
- Mark as [MISSING] in deliverable
- State the next action to resolve
- Suggest alternative search strategies
- Note if title attorney consultation recommended (L4 scenario)

**Heir Verification Failures:**
- If heir status cannot be verified after checking all sources, mark as "?" (unverified)
- Include unverified heirs in decision-maker list with LOW confidence
- Note: "Status unverified - recommend confirming before extensive skip trace investment"
- If ALL heirs are deceased or unverified through 3 generations, escalate to L4 (title attorney)

**Name Disambiguation Failures:**
- If two candidate identities cannot be distinguished after all checks, mark confidence as LOW
- Document both candidates with supporting evidence for each
- Note: "Ambiguous identity - present both options; caller should verify during first contact"

**Property Lookup Failures (Probate):**
- If all 3 tiers fail to find a property address, note which variations were tried
- Check if decedent may have been a renter (no property to find)
- Consider: property may be in a different county, under a trust/LLC, or under a maiden/married name not yet tried
