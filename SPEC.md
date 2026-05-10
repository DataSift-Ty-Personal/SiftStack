# Deep Prospecting — Build Spec

A drop-in, self-contained build spec for any agentic IDE (Antigravity, Claude Code, Cursor). Hand this file to the agent, point it at `~/Desktop/SiftStack/`, and let it work end-to-end.

---

## TL;DR

Build a Python module **inside the existing SiftStack repo** at `~/Desktop/SiftStack/deep_prospecting/` that automates the **deep prospecting** workflow — finding and verifying real-estate decision-makers (owner / heir / executor) when standard skip tracing fails. CLI takes a single property address, owner name, or probate docket and runs end-to-end **without intervention**, producing a verified decision-maker plus full skip-trace results in a structured markdown research pack.

**Why a sub-module of SiftStack:** reuses every pattern and API key SiftStack already has — Playwright async, `_safe()` wrappers, dataclass spine, Smarty / Anthropic / Firecrawl / Serper, `.env`. Zero duplication. Zero risk to the existing weekly cron because deep prospecting is a separate entry point — `python -m deep_prospecting`, never invoked by the Wednesday Modal job.

**Entry points:**

```bash
python -m deep_prospecting --address "123 Main St, Newark NJ"
python -m deep_prospecting --address "..." --owner "John Smith"
python -m deep_prospecting --probate --docket "PR-2025-1234" --county middlesex
python -m deep_prospecting --csv leads.csv [--parallel 3]
```

---

## Mission for the Agent

You are building this **without user babysitting**. Read this spec end-to-end, then read the existing SiftStack code at `~/Desktop/SiftStack/`, then build the module phase-by-phase per the build order.

You may make reasonable judgment calls without asking. **Only stop and ask the user when:**

1. An external credential is missing from `.env` and required to proceed.
2. A test run produces output that materially differs from the expected research-pack format.
3. A source has changed its DOM / anti-bot pattern in a way that requires a strategy change.
4. You hit an architectural fork that has long-term consequences (e.g., switching from Playwright to a different scraper).

Otherwise, keep moving. Implement → test against the included test cases → iterate.

---

## Default Behaviors (No Need to Ask)

- Browsers run **headless** (set `PROSPECT_HEADLESS=false` to watch).
- Logs to `outputs/{YYYY-MM-DD}/{slug}/run.log`.
- Output directory is auto-created per run.
- If a single source fails, it's marked `[MISSING]` in the deliverable; the run continues.
- If the property has no obit hits, mark owner as "presumed living" and proceed to skip trace (L1 path).
- Time budget per case: 5 minutes (configurable via `PROSPECT_TIME_BUDGET_SECONDS`).
- Cost target per run: $0.10. Hard ceiling: $0.20. If approaching ceiling, stop and surface findings so far.
- Default Anthropic models: `claude-haiku-4-5` for extraction, `claude-sonnet-4-6` only for the final decision-maker reasoning paragraph.

---

## Why This Exists

Skip tracing fails when:

- Owner is deceased (estate hasn't filed probate yet).
- Property is held by an LLC / Trust / Corp.
- Public records show conflicting owner data.
- Mailing address is vacant or returns mail.

Currently Rick handles these manually via the `deep-prospecting` skill (full skill content embedded below). It works but doesn't scale and requires Rick to drive every step. This module automates that workflow into a single command.

---

## Architecture

```
SiftStack/
├── ... (existing code untouched)
└── deep_prospecting/                    # NEW MODULE
    ├── __init__.py                      # exports run() for callable use
    ├── __main__.py                      # `python -m deep_prospecting` entry
    ├── README.md                        # quick reference
    ├── cli.py                           # argparse / click CLI
    ├── orchestrator.py                  # L1/L2/L3 auto-selection + phase coordination
    ├── models.py                        # Lead, Heir, HeirMap, DecisionMaker, SkipTraceResult, ResearchPack
    ├── safe.py                          # _safe wrapper (mirror SiftStack pattern)
    ├── llm.py                           # Anthropic client (Haiku for extraction)
    ├── output.py                        # Markdown research pack + skip trace card formatters
    ├── compile.py                       # Assemble ResearchPack from phase outputs
    ├── phases/
    │   ├── __init__.py
    │   ├── phase_1_title.py             # County records, deed, ownership, alive/dead signal
    │   ├── phase_2_genealogy.py         # Obit search → family tree extract (LLM)
    │   ├── phase_2_5_verification.py    # Heir alive/dead loop (recursive)
    │   ├── phase_3_target.py            # Decision-maker selection from verified living heirs
    │   └── phase_skiptrace.py           # TPS / FPS / CBC waterfall + cross-validation
    ├── sources/
    │   ├── __init__.py
    │   ├── tps.py                       # TruePeopleSearch (Playwright)
    │   ├── fps.py                       # FastPeopleSearch (Playwright)
    │   ├── cbc.py                       # CyberBackgroundChecks (Playwright)
    │   ├── findagrave.py                # FindAGrave (highest-reliability verification)
    │   ├── legacy.py                    # Legacy.com obituaries
    │   ├── obit_search.py               # Serper → Firecrawl → Haiku obit waterfall
    │   ├── google_dork.py               # Serper-powered Google dorking
    │   └── county_records.py            # NJ assessor + deed lookups (Essex/Middlesex/Somerset/Union)
    ├── outputs/                         # gitignored — per-run output
    └── tests/
        ├── __init__.py
        └── test_models.py               # validate dataclass shapes match research-pack contract
```

---

## File-by-File Spec

### `__main__.py`

```python
import asyncio, sys
from .cli import main
sys.exit(asyncio.run(main()))
```

### `cli.py`

- `argparse`-based CLI (or `click` — agent's choice).
- Flags: `--address`, `--owner`, `--docket`, `--county`, `--csv`, `--parallel N`, `--headless / --no-headless`, `--out DIR`, `--debug`.
- Validates input, loads `.env` (via `python-dotenv`), instantiates `Orchestrator`, calls `orchestrator.run()`.
- Prints summary table (`rich.table`) at end + path to research pack.
- Exit codes: 0 success, 1 partial (some sources missing), 2 hard failure.

### `orchestrator.py`

- Single class `Orchestrator` with `async def run(input: ProspectInput) -> ResearchPack`.
- Always runs Phase 1.
- Branch logic:
  - Phase 1 finds owner alive + reachable → **L1**: skip trace direct.
  - Phase 1 finds conflicting names / variants → **L2**: name-variant resolve → skip trace.
  - Phase 1 finds death signal → **L3**: Phase 2 → 2.5 → 3 → skip trace.
- Each phase wrapped in `_safe()`. Phase failures degrade gracefully and are surfaced in the deliverable.
- Tracks per-run cost via the LLM accumulator.

### `models.py`

Pydantic v2 (preferred) or vanilla `@dataclass` — be consistent. Required types:

```python
class ProspectInput(BaseModel):
    address: str | None = None
    owner: str | None = None
    docket: str | None = None
    county: str | None = None
    notice_type: str | None = None  # foreclosure / probate / tax / etc.
    raw_record: dict | None = None  # dump of distress-record fields if csv

class Deed(BaseModel):
    instrument_type: str  # WD, QCD, Sheriff Deed, etc.
    grantor: str
    grantee: str
    recorded_date: date | None
    consideration: float | None
    notes: str | None = None

class Lead(BaseModel):
    input: ProspectInput
    title_owner: str | None
    deed_history: list[Deed]
    death_signal: bool
    name_variants: list[str]
    red_flags: list[str]
    mailing_address: str | None
    parcel_id: str | None

class Heir(BaseModel):
    name: str
    relationship: str
    city: str | None = None
    state: str | None = None
    status: Literal["LIVING", "DECEASED", "UNVERIFIED"]
    dod: date | None = None
    sources: list[str]
    verification_notes: str | None = None

class HeirMap(BaseModel):
    decedent_name: str
    decedent_dod: date | None
    decedent_city: str | None
    heirs: list[Heir]
    generations_searched: int

class DecisionMaker(BaseModel):
    name: str
    relationship: str
    status: Literal["VERIFIED_LIVING", "UNVERIFIED"]
    current_address: str | None
    estimated_age: tuple[int, int] | None
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    reasoning: str

class Phone(BaseModel):
    number: str
    type: Literal["MOBILE", "LANDLINE", "UNKNOWN"]
    sources: list[str]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]

class SkipTraceResult(BaseModel):
    decision_maker: DecisionMaker
    phones: list[Phone]
    emails: list[dict]
    addresses_current: list[str]
    addresses_previous: list[str]
    associates: list[dict]

class ResearchPack(BaseModel):
    input: ProspectInput
    level_selected: Literal["L1", "L2", "L3"]
    level_reason: str
    source_checklist: list[dict]
    lead: Lead
    heir_map: HeirMap | None
    decision_maker: DecisionMaker | None
    skip_trace: SkipTraceResult | None
    cost_usd: float
    timestamp_utc: datetime
    duration_seconds: float
    research_pack_md: str
```

### `safe.py`

Mirror SiftStack's `_safe()` exactly — same retry semantics, same logging format. Every external call goes through this:

```python
async def _safe(coro, *, name: str, retries: int = 3, backoff: float = 2.0) -> T | None:
    """Catch + log + retry. Returns None on final failure."""
```

### `llm.py`

- Single `AsyncAnthropic` client.
- Helper: `extract_json(prompt: str, schema: type[BaseModel]) -> BaseModel`.
- Default model: `claude-haiku-4-5` for extraction; `claude-sonnet-4-6` only for the final decision-maker reasoning paragraph.
- Tracks token usage → returns to caller for per-run cost computation.

### `phases/phase_1_title.py`

- **Input:** `ProspectInput`.
- **Output:** `Lead`.
- **Sources:** `county_records.py`, `google_dork.py`, optionally `obit_search.py` for cross-check.
- **Detects death signal via:**
  - "estate of" in deed grantor/grantee.
  - Probate filing reference in property records.
  - Quick obit dork: `"{owner}" obituary "{city}"`.
- Always returns a `Lead`, even if sparse — never fails the run.

### `phases/phase_2_genealogy.py`

- **Input:** `Lead` (with `death_signal=True`).
- **Output:** `HeirMap` (unverified — verification happens in 2.5).
- **Pipeline:**
  1. `obit_search.py` → most recent obit for decedent.
  2. Firecrawl scrape obit page → text.
  3. Haiku extracts: spouse, children, siblings, grandchildren, executor (if mentioned), preceded_in_death.
  4. Build `HeirMap` with all heirs marked `status=UNVERIFIED` initially.
- May run multiple obit pages if the first is sparse (max 3).

### `phases/phase_2_5_verification.py`

**Most critical L3 module.** Recursive heir-by-heir verification:

```
FOR EACH heir in heir_map.heirs (status=UNVERIFIED):
    Source priority (HIGH → LOW reliability per skill):
      1. FindAGrave (search by name + state)
      2. Legacy.com obit search
      3. Newspapers.com (skip if paywalled — agent's call)
      4. Google dork: "{name} obituary {city}"
      5. TPS/FPS preview (no record found = supporting alive signal, MEDIUM only)

    IF death record found:
      heir.status = "DECEASED"
      heir.dod = found_dod
      Search for THEIR obit, extract THEIR heirs, queue for verification (recursive)

    ELIF all sources exhausted, no death found:
      heir.status = "LIVING"

    ELSE:
      heir.status = "UNVERIFIED"
```

**Stop conditions:**

- 2-3 verified-living heirs found with decision-making authority.
- 3rd generation reached with no living heirs (mark whole tree as L4 escalation, recommend title attorney).
- Time budget exceeded.

### `phases/phase_3_target.py`

- **Input:** `HeirMap` (post-verification).
- **Output:** `DecisionMaker`.
- **Selection priority** (verified-living heirs only):
  1. Named executor (if probate filed).
  2. Surviving spouse.
  3. Oldest living child.
  4. Sibling (if no spouse / children).
  5. Grandchild (if children deceased).
- **Confidence:**
  - HIGH: executor confirmed via probate, OR all 4 validation criteria from skill met.
  - MEDIUM: relationship clear, address matches partially.
  - LOW: relationship inferred, or last known activity 5+ years stale.
- Sonnet writes the 1-2 paragraph reasoning.

### `phases/phase_skiptrace.py`

- **Input:** `DecisionMaker`.
- **Output:** `SkipTraceResult`.
- Run TPS → FPS → CBC **sequentially** (NOT parallel — anti-bot reasons).
- Merge results by phone number; assign confidence per skill rules:
  - HIGH = phone on 2+ sites + relative match + age fits.
  - MEDIUM = phone on 1 site + address match.
  - LOW = phone on 1 site, no cross-reference.

### `sources/tps.py`, `fps.py`, `cbc.py`

Each exposes:

```python
async def search(name: str, city: str, state: str, address: str | None = None) -> SiteResult: ...
```

`SiteResult` = dict with `phones`, `emails`, `addresses`, `associates`, `aliases`.

**Anti-bot per site:**

- **TPS:** Scroll past Sponsored Results section. Look for "Details" link. Extract from plain-text section below.
- **FPS:** Capture "Also Known As" aliases. Often shows emails better than TPS.
- **CBC:** "Possible Associates" + "Relatives" sections are the high-value fields.

**Common defenses:** user-agent rotation, 2-5s random delays, proxy rotation if `PROSPECT_PROXY_POOL` env var set, Playwright stealth.

### `sources/findagrave.py`, `legacy.py`

- **FindAGrave:** search by name + state, return `dod` if burial record found.
- **Legacy.com:** search by name + city, return obit URL + DOD + survivors-list text.

### `sources/obit_search.py`

Mirror SiftStack's `obituary_enricher.py`. Same Serper → Firecrawl → Haiku waterfall.

### `sources/county_records.py`

NJ assessor + deed lookups for Essex, Middlesex, Somerset, Union. **Use SiftStack's existing scraping patterns** — copy or import directly. Don't rebuild.

### `output.py`

Renders the **9-section markdown research pack** per the skill (see Appendix). Format MUST match the skill's templates byte-for-byte where shown.

### `compile.py`

Stitches phase outputs into a `ResearchPack`. Computes total cost. Writes `research_pack.md` and `results.json` to `outputs/{date}/{slug}/`.

---

## SiftStack Patterns to Reuse

When in doubt, mirror SiftStack:

1. **Module structure** — see `~/Desktop/SiftStack/src/`.
2. **`_safe()` wrapper** — copy from SiftStack's existing implementation.
3. **Login retries with backoff** — `nj_scraper.py` aMember pattern is reusable.
4. **Dataclass spine** — `notice_parser.NoticeData` is the model.
5. **Slack reporter** — optional, off by default.
6. **Cost tracking** — mirror `cost_estimator.py`.
7. **Diagnostic logging** — capture every external response on failure.
8. **Modular sources** — each external site = its own file under `sources/`.

---

## .env Requirements

```bash
# Shared with SiftStack — read from ~/Desktop/SiftStack/.env if it exists
ANTHROPIC_API_KEY=sk-ant-...
SERPER_API_KEY=...
FIRECRAWL_API_KEY=...
SMARTY_AUTH_ID=...
SMARTY_AUTH_TOKEN=...

# Behavior
PROSPECT_HEADLESS=true
PROSPECT_LOG_LEVEL=INFO
PROSPECT_OUTPUT_DIR=outputs
PROSPECT_PARALLEL=1
PROSPECT_TIME_BUDGET_SECONDS=600

# Optional
PROSPECT_SLACK_NOTIFY=false
PROSPECT_PROXY_POOL=
```

---

## requirements.txt (additions to SiftStack's existing)

```
playwright>=1.45.0
playwright-stealth>=1.0.6
httpx>=0.27.0
beautifulsoup4>=4.12.0
lxml>=5.0.0
anthropic>=0.39.0
pydantic>=2.7.0
click>=8.1.0
rich>=13.7.0
python-dotenv>=1.0.0
pandas>=2.2.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

---

## Build Order

1. **Read this spec end-to-end.**
2. **Read SiftStack code** at `~/Desktop/SiftStack/` — at minimum: `notice_parser.py`, `obituary_enricher.py`, `nj_scraper.py`, `enrichment_pipeline.py`, the `_safe` implementation.
3. **Create the folder structure** under `~/Desktop/SiftStack/deep_prospecting/`.
4. **Build `models.py`** first. Show Rick the dataclass shapes before continuing.
5. **Build `safe.py` + `llm.py`**.
6. **Build a vertical slice end-to-end** on one test case (deceased owner, full L3 path):
   - `obit_search.py` → `phase_2_genealogy.py` → minimal `phase_2_5_verification.py` (FindAGrave only) → `phase_3_target.py` → `tps.py` only → `phase_skiptrace.py` (TPS only) → `output.py` → `cli.py` (`--address` only).
7. **Run end-to-end** on the test case. Show Rick the output.
8. **Expand horizontally** — add FPS, CBC, Legacy, full verification loop, name-variant resolution, CSV mode.

**Vertical slice first, then horizontal expansion.**

---

## Test Cases

Before any test runs, **ask Rick for 2-3 test cases:**

- A known deceased-owner property (with confirmed heir / executor).
- A known living-owner foreclosure.
- A known LLC / Trust-held property.

These validate output format without burning API credits on bad runs.

---

## Acceptance Criteria

A run is "complete" when:

1. `python -m deep_prospecting --address "123 Main St, Newark NJ"` produces:
   - `outputs/{date}/{slug}/research_pack.md` matching the skill's 9-section format.
   - `outputs/{date}/{slug}/results.json` with full `ResearchPack` serialized.
   - Console summary table with key fields + path to pack.
2. Total cost per run < $0.20 (target $0.10).
3. Total time per run < 5 minutes.
4. SiftStack's existing weekly cron has **zero regressions**.
5. All 9 sections of the research pack present (or marked `[N/A]` for skipped levels).
6. Skip Trace Results Card matches the skill's ASCII template.

---

## Out of Scope (Phase 1)

- Modal weekly cron integration.
- Slack reporter (optional flag, off by default).
- SQLite persistence / cross-run dedup.
- Per-record PDF reports.
- Knowledge graph / cross-record super-leads.
- Trestle phone scoring.
- Multi-state support (NJ-first; Essex, Middlesex, Somerset, Union).

---

## Related Context (Optional Reference)

The user has additional skills loaded that may inform decisions:

- `rick-profile` — communication preferences, business context.
- `probate-property-finder` — adjacent skill for filling property addresses on probate filings.
- `beenverified-csv-processor` — extracts heir contact data from BeenVerified screenshots.
- `sift-data-completion` — verifies LLC / Trust ownership via county records.

Not requirements for this build, but if the agent identifies a place where one of them would unlock cleaner architecture, **propose it before implementing**.

---
---

# Appendix: The Deep Prospecting Skill (Source of Truth)

The CLI must produce output that matches what this skill produces today (manually). This is the contract.

---

# Deep Prospecting

Deep prospecting is the manual research process for identifying and verifying decision-makers (owner / heir / executor) when standard skip tracing fails. Core philosophy: **"When everyone hits a wall, we bring a shovel."**

## When to Use This Skill

- Skip trace returned no usable phone numbers
- Called 3+ attempts with no contact
- Vacant mailing address discovered
- Return mail (bad address)
- Probate cases (often only docket number available)
- Entity / LLC ownership (need actual decision-maker)
- Conflicting owner / address information in public records

## Input Requirements

User provides any combination of:

- Property address
- Owner name (full or partial)
- Filing / docket number
- Probate case information
- Foreclosure notice details
- Any distress record data

## Workflow Overview

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

## Level Selection Logic

**L1 Initial Block** → Skip trace yielded no usable mobiles; verify with light public records
- Trigger: No phone numbers returned, but owner appears alive and reachable
- Focus: Cross-verification and simple public record lookups

**L2 Address/Name Variation** → Conflicting/incomplete owner/address/name; resolve via title/deed chain + history
- Trigger: Public records show conflicting information, multiple name variants, or incomplete data
- Focus: Title work (deeds), Google dorking, property history

**L3 Deceased Owner/Heirs** → Owner likely deceased; identify living decision-maker via obits/genealogy
- Trigger: Owner appears deceased, heir/executor contact unknown
- Focus: Obituaries, Ancestry, newspapers, family tree mapping, **heir verification loop**

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
| 1.1 | Review the Deed (Critical) | Analyze for middle initials, relationships, transaction type |
| 1.2 | Identify Title Issues | Look for installment agreements, multiple owners/heirs |
| 1.3 | Initial Google Search | Use owner name + property address with dorking operators |

### Phase 2: Genealogy & Historical Research (L3)

| Step | Action | Purpose |
|------|--------|---------|
| 2.0 | Search for Obituaries | Find survivors, spouses, relationships |
| 2.1 | Newspapers.com Deep Dive | Historical mentions, city directories, marriage announcements |
| 2.2 | Map the Family Tree | Build tree from obituaries and deeds |
| 2.3 | "Go Back to Go Forward" | Use older records to find new leads |

### Phase 2.5: Heir Verification Loop (L3 - CRITICAL)

**Purpose:** Verify each identified heir is alive before adding as potential decision-maker. If deceased, find THEIR heirs and repeat until living heirs are confirmed.

#### Verification Process

```
FOR EACH heir identified in Phase 2:
    1. Search for heir's obituary/death record
    2. Check FindAGrave for burial record
    3. Search "[HEIR NAME] obituary [CITY/STATE]"
    4. Check Ancestry death records if available

    IF heir confirmed ALIVE:
        → Mark as ✓ (verified living) in heir map
        → Add to potential decision-maker list

    IF heir confirmed DECEASED:
        → Mark as † (deceased) in heir map
        → Record DOD if found
        → Search for THEIR obituary to find survivors
        → Add their heirs to verification queue
        → REPEAT verification process for new heirs

    IF status UNCERTAIN:
        → Mark as ? (unverified) in heir map
        → Note last known activity date
        → Include in decision-maker list with LOW confidence
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

#### When to Stop the Loop

- All identified heirs verified as living OR
- Found at least 2-3 verified living heirs with decision-making authority OR
- Reached 3rd generation with no living heirs found (escalate to L4/attorney)

### Phase 3: Locating the Target (Verified Living Heir/Executor)

| Step | Action | Purpose |
|------|--------|---------|
| 3.0 | Identify the Target | Select from **verified living heirs only** |
| 3.1 | Search for the Target | Use full name, city, estimated age |
| 3.2 | Cross-Reference and Validate | Use second source to validate |
| 3.3 | Document Findings | Record all valid contact numbers |

**Decision-Maker Priority (from verified living heirs):**
1. Named executor (if probate filed)
2. Surviving spouse
3. Oldest living child
4. Sibling (if no spouse/children)
5. Grandchild (if children deceased)

## Manual Skip Trace Execution (3-Site Waterfall)

### Site 1: TruePeopleSearch.com (Primary)

The "King" of free skip tracing.

**How to search:**
1. Navigate to https://www.truepeoplesearch.com
2. Search by name: `{FULL NAME}` in `{CITY, STATE}`
3. If too broad, use address search: `{PROPERTY ADDRESS}` or `{TAX MAILING ADDRESS}`
4. **Scroll past the "Sponsored Results"** — the free data lives in the plain-text "Details" section below
5. Record: phone numbers (mobile + landline), current/previous addresses, age, associated names

### Site 2: FastPeopleSearch.com (Backup / Cross-Reference)

**How to search:**
1. Navigate to https://www.fastpeoplesearch.com
2. Search by name: `{FULL NAME}` in `{CITY, STATE}`
3. If name returns too many results, try address search
4. Record any NEW phone numbers or addresses not on TruePeopleSearch
5. Pay attention to "Also Known As" names — useful for L2 name-variant cases

### Site 3: CyberBackgroundChecks.com (Deep Data / Associates)

**How to search:**
1. Navigate to https://www.cyberbackgroundchecks.com
2. Search by name: `{FULL NAME}` in `{CITY, STATE}`
3. Focus on "Possible Associates" and "Relatives" sections
4. Record any NEW contacts, email addresses, and associate names

### Skip Trace Validation

**High-Confidence (ready to dial):**
- Same phone number on 2+ sites
- Associated addresses include subject property or tax mailing
- Relatives/associates match names from deeds, obits, or heir map
- Age band fits deed history and obit dates

**Medium-Confidence (dial but verify):**
- Phone on 1 site only
- Address matches but no relative/associate confirmation
- Name variant matches but slightly different city

**Low-Confidence:**
- Partial name match only
- No address overlap
- No relative/associate cross-reference

## Heir Map Template (L3)

```
Decedent: † {DECEDENT FULL} (DOD {YYYY-MM-DD}) [{CITY, ST}]
│
├─ Spouse/Partner:
│  └─ {STATUS} {SPOUSE FULL} [{CITY, ST}] {DOD if deceased}
│
├─ Children:
│  ├─ {STATUS} {CHILD 1} [{CITY, ST}] {DOD if deceased}
│  │   └─ Grandchildren (if Child 1 deceased):
│  │       ├─ {STATUS} {GRANDCHILD 1A} [{CITY, ST}]
│  │       └─ {STATUS} {GRANDCHILD 1B} [{CITY, ST}]
│  │
│  ├─ {STATUS} {CHILD 2} [{CITY, ST}] {DOD if deceased}
│  └─ {STATUS} {CHILD 3} [{CITY, ST}]
│
└─ Siblings:
   ├─ {STATUS} {SIBLING 1} [{CITY, ST}]
   └─ {STATUS} {SIBLING 2} [{CITY, ST}]

STATUS MARKERS:
  †  = Verified DECEASED (with DOD if known)
  ✓  = Verified LIVING (confirmed no death record)
  ?  = UNVERIFIED
  ★  = Executor (confirmed via probate filing)
  ▸  = Recommended decision-maker
  ●  = Current living owner
```

## Deliverable Format (9 Sections)

```
## 1) Level Selected & Why
[State L1/L2/L3 and the specific reason]

## 2) Source Checklist
[Mark [x]/[ ] with 1-line notes]

## 3) Title & Ownership
- Current owner(s)
- Instrument type summary
- Red flags

## 4) Identity Resolution (if variants exist)
- Which variant won & why

## 5) Genealogy/Heir Findings (if family/estate elements)
- Obit links found
- Survivors identified
- Relationship notes

## 6) Heir Verification Summary (L3 required)
- Total heirs identified: [#]
- Verified living: [# and names]
- Verified deceased: [# and names with DODs]
- Unverified: [# and names with notes]
- Generations searched: [1st/2nd/3rd]

## 7) Heir Map (L3 required)
[ASCII tree per template above WITH verification status markers]

## 8) Decision-Maker Identified
- Name: {FULL NAME}
- Relationship: {owner/heir/executor/spouse}
- Verification Status: {✓ Verified Living / ? Unverified}
- Current Address: {best known mailing address}
- Estimated Age: {age range}
- Confidence: {HIGH/MEDIUM/LOW with reasoning}

## 9) Skip Trace Results
[Skip Trace Results Card per template below]
```

## Skip Trace Results Card (Required Format)

```
═══════════════════════════════════════════════════════════
                  SKIP TRACE RESULTS
═══════════════════════════════════════════════════════════

DECISION-MAKER: {FULL NAME}
  Relationship: {owner/heir/executor/spouse}
  Status:       {✓ Verified Living}
  Est. Age:     {AGE RANGE}

─── PHONE NUMBERS ────────────────────────────────────────
  #  | Number          | Type     | Source(s)       | Confidence
  1  | (xxx) xxx-xxxx  | Mobile   | TPS, FPS        | HIGH
  2  | (xxx) xxx-xxxx  | Landline | TPS             | MEDIUM
  3  | (xxx) xxx-xxxx  | Mobile   | CBC             | MEDIUM

─── EMAIL ADDRESSES ──────────────────────────────────────
  1  | xxxx@xxxxx.com  | CBC, FPS
  2  | xxxx@xxxxx.com  | CBC

─── ADDRESSES ────────────────────────────────────────────
  Current:  {ADDRESS} (confirmed on TPS + FPS)
  Previous: {ADDRESS} (matches tax mailing)

─── ASSOCIATES & RELATIVES ──────────────────────────────
  • {NAME} - {RELATIONSHIP} - {CITY, ST} (from CBC)
  • {NAME} - {RELATIONSHIP} - {CITY, ST} (from CBC)

─── VALIDATION ───────────────────────────────────────────
  ☑ Phone on 2+ sites    ☑ Address matches records
  ☑ Relatives match       ☑ Age fits timeline

BACKUP DECISION-MAKERS (also traced):
  • {NAME 2} · {CITY, STATE} · {RELATIONSHIP} · {PHONE}
  • {NAME 3} · {CITY, STATE} · {RELATIONSHIP} · {PHONE}

SOURCE KEY: TPS = TruePeopleSearch | FPS = FastPeopleSearch | CBC = CyberBackgroundChecks
═══════════════════════════════════════════════════════════
```

## Key Tools Reference

| Tool | Primary Use | Notes |
|------|-------------|-------|
| County Deed Records | Title analysis, ownership verification | Look for middle initials, transaction types |
| Google Dorking | Narrowing search results | Use site:, intitle:, filetype: operators |
| Ancestry.com | Family trees, obituaries, death records | Essential for L3 cases |
| Newspapers.com | Historical mentions, directories, obituaries | Useful for pre-2000 records |
| FindAGrave | **Heir verification**, burial records, family links | Primary source for death confirmation |
| Legacy.com | Obituaries with survivor lists | Key for heir identification AND verification |
| TruePeopleSearch.com | Phone numbers, addresses, associates | Primary skip trace site |
| FastPeopleSearch.com | Phone numbers, emails, aliases | Backup skip trace |
| CyberBackgroundChecks.com | Emails, associates, relatives | Best for deep associate data |
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
- If ALL heirs are deceased or unverified through 3 generations, escalate to L4 (title attorney)

---

**End of build spec. Hand this entire document to the agent and let it run.**
