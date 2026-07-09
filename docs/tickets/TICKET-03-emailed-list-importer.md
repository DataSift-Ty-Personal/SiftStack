# TICKET-03 — Emailed distress-list importer (foreclosure Notice-of-Default lists)

> **Supersedes** the original "DOR delinquent Excel ingester" ticket. Per the user, distress lists
> arrive by email and are saved manually to a folder. **Sample analysis (2026-07-01):** three real
> samples in hand — Sarpy foreclosure NOD (PDF), Douglas foreclosure NOD (spreadsheet,
> `RPTTYPE=NDEF`), and Douglas tax sale (spreadsheet, `RPTTYPE=DSL`). **Key insight:** the two
> Douglas files share ONE 28-column Register-of-Deeds export template — a single parser handles
> both, keyed by `RPTTYPE` (`NDEF`=foreclosure, `DSL`=tax sale). Formats are a **digital PDF
> (Sarpy)** and **spreadsheets (Douglas)**, not uniform CSV.

**Priority:** High — NOD is the earliest foreclosure signal (§ 76-1006 recording, ~30-day cure
before the trustee sale ever hits the newspaper). Best first-to-market data in the whole project.

## Intake model (user-confirmed)
- **Manual file drop** into a watched folder (local or Dropbox). No Gmail/IMAP automation.
- Formats to support: **digital text PDF** (Sarpy) + **spreadsheet** (Douglas `.xlsx`/CSV export).
  Sarpy PDF is text-based → extract with `pypdfium2`/`pdfplumber`, **no OCR/LLM** (unlike scanned
  tax-sale PDFs). Douglas spreadsheet → `openpyxl`/`pandas`.
- Both → `notice_type="foreclosure"`, `state="NE"`.

## Source A — Sarpy County "Notice of Default" report (PDF)
Header report: Doc Type / Filed Date / Instrument # / Grantor / Grantee / Legal. Example row:
`NOTICE OF DEFAULT | 04/21/2026 09:46:21 AM | 202608908 | LINDQUIST ERIC H TR | WHOM IT MAY CONCERN | GRANITE LAKE Lot: 204`

**Column → NoticeData mapping:**
| PDF column | NoticeData field | Notes |
|---|---|---|
| Grantor | `owner_name` | The distressed owner/trustor (unique per row). ⚠️ see party-role caveat. |
| Grantee | — (ignore) | Generic "WHOM IT MAY CONCERN". |
| Filed Date | `date_added` | Parse `MM/DD/YYYY hh:mm:ss AM` → `YYYY-MM-DD`. |
| Instrument # | `source_url` ref | e.g. `email-list://sarpy_nod_2026-04.pdf#instr=202608908`. No parcel col. |
| Legal | `raw_text` (+ address-lookup input) | Subdivision + lot only (`GRANITE LAKE Lot: 204`) — **NO street address**. |
| — | `county="Sarpy"`, `notice_type="foreclosure"`, `state="NE"` | constants |

**⚠️ Address gap:** Sarpy rows have NO street address — only a legal description. Like probate,
these need address resolution downstream: Sarpy County Assessor/CAMA (`apps.sarpy.gov`,
`property_lookup.py`) by owner name and/or subdivision+lot, or by Instrument # at the Register of
Deeds. Enrichment cannot proceed on address until this resolves.

## Source B — Douglas County NOD export (spreadsheet, `RPTTYPE=NDEF`)
27 columns; the ones that matter. Example row:
`NDEF | FRSTPARTY="RASMUSSEN,DAVID W TR" | SNDPARTY="INCEPTION REALTY LLC" | ... | ADDNNAME=BONITA | LOT=24 | BLOCK=15 | INSTR NUMB=2026050870 | FILEDATE TIME=06/15/2026 14:57:47 | ADDRESS="2339 N 70 AV, OMAHA" | ZIP=68104 | PARCEL=649510000`

**Column → NoticeData mapping:**
| Sheet column | NoticeData field | Notes |
|---|---|---|
| `SNDPARTY` | `owner_name` | **The borrower/owner (target).** Format `LAST,FIRST` or LLC name. |
| `OTHRSND` | co-owner → append to `owner_name` or DM2 | e.g. `RYDER,KELSEY T`. |
| `FRSTPARTY` | — (store as trustee, NOT owner) | ⚠️ **Repeats** across rows = foreclosure trustee (`RASMUSSEN,DAVID W TR`). Do NOT map to owner. |
| `ADDRESS` | `address` + `city` | Split `"2339 N 70 AV, OMAHA"` → street=`2339 N 70 AV`, city=`OMAHA`. Present ~90% of rows. |
| `ZIP` | `zip` | e.g. `68104`. |
| `PARCEL` | `parcel_id` | e.g. `649510000`. |
| `INSTR NUMB` | `source_url` ref | NOD instrument number. |
| `FILEDATE TIME` | `date_added` | `MM/DD/YYYY hh:mm:ss` → `YYYY-MM-DD`. |
| `REFNUMBER` | `raw_text` | Underlying deed-of-trust reference. |
| `ADDNNAME`+`LOT`+`BLOCK` | `raw_text` (address-lookup fallback) | Legal desc; use when `ADDRESS` blank (few rows). |
| `RPTTYPE`/`CHARACTER` | filter | Keep only `NDEF` / `N DEF - N/DEF`. |
| `AMOUNT` | — (ignore) | Always `0.00` in sample. |
| — | `county="Douglas"`, `notice_type="foreclosure"`, `state="NE"` | constants |

Douglas NOD is the easy one — street address + parcel + zip already present, so most rows enrich
with no address-lookup step.

## Source C — Douglas County tax-sale export (spreadsheet, `RPTTYPE=DSL`)
**Same 28-column template as Source B**, so the same Douglas parser handles it; branch on
`RPTTYPE`. Example row:
`DSL | FRSTPARTY="STATE" | SNDPARTY="BERRY-FISHER,BEAUFIELD B" | OTHRSND="FISHER,ROBERT D" | REFNUMBER=2023025542 | INSTR NUMB=2026049213 | FILEDATE TIME=06/10/2026 10:47:42 | CHARACTER="D SL - D/SL" | UCCNUMB="2606098858-6" | ADDRESS=(blank) | PARCEL=(blank)`

**Column → NoticeData mapping:**
| Sheet column | NoticeData field | Notes |
|---|---|---|
| `SNDPARTY` | `owner_name` | **The delinquent owner (target).** |
| `OTHRSND` | co-owner | e.g. `FISHER,ROBERT D`. |
| `FRSTPARTY` | — (ignore) | Always `STATE` (the lien side), not a person. |
| `UCCNUMB` | `raw_text` / cert ref | Tax-sale certificate number, e.g. `2606098858-6`. |
| `REFNUMBER` / `INSTR NUMB` | `source_url` ref | e.g. `email-list://douglas_dsl_2026-06.xlsx#instr=2026049213`. |
| `FILEDATE TIME` | `date_added` | `MM/DD/YYYY hh:mm:ss` → `YYYY-MM-DD`. |
| `RPTTYPE`/`CHARACTER` | filter + type | `DSL`/`D SL - D/SL` → `notice_type="tax_sale"`. |
| — | `county="Douglas"`, `state="NE"` | constants |

**⚠️ Address gap (worse than Sarpy):** DSL rows have **NO address, parcel, OR legal description** —
only owner name + certificate/instrument numbers. Address resolution must run entirely on **owner
name** via the Douglas Assessor/Beacon name search (Tier 1 of the probate lookup in
`property_lookup.py`), or by resolving `REFNUMBER`/`INSTR NUMB` at the Register of Deeds. Expect a
lower address-match rate; common names (e.g. `JAMES,JOSEPH`) will be ambiguous — carry a confidence
flag.

## ⚠️ Party-role caveat (verify with more rows — biggest correctness risk)
The two files use **opposite conventions**. Douglas `FRSTPARTY` is the trustee (proven by names
repeating across many properties); owner = `SNDPARTY`. Sarpy `Grantor` appears to be the owner
(unique per row, generic grantee), but the sample is only 2 rows. **Rule of thumb:** if a
first-party name repeats across many rows, it's the trustee, not the owner. Confirm the Sarpy
mapping against a fuller sample before trusting it.

## Integration points (existing code)
- **Output type:** `NoticeData` (`src/notice_parser.py:29`).
- **NOT the existing `csv-import`:** `_run_csv_import` (`main.py:740`) / `data_formatter.read_csv`
  (`data_formatter.py:360`) expect SiftStack's own SIFT_COLUMNS. These county files are in their
  own layouts → need per-source mapping profiles first.
- **Name normalization:** Douglas uses `LAST,FIRST`; Sarpy uses `LAST FIRST` with `TR` suffixes.
  Reuse/extend `data_formatter._split_name` (`data_formatter.py:113`); strip `TR`/`TRUSTEE`.
- **Address resolution (Sarpy NOD + Douglas DSL):** ✅ IMPLEMENTED in `src/ne_property_lookup.py`,
  wired as pipeline Step 3d (`enrichment_pipeline.py`). Owner-name search against public county
  ArcGIS parcel layers (Douglas `dcgis.org`, Sarpy AGOL) → token-overlap scoring → best match,
  with a confidence flag (`addr_from_owner_lookup:high|medium|low`) and an `ambiguous_owner_match:N`
  flag when a common name / investor LLC ties across many parcels. Smarty then standardizes.
- **Dedup:** `data_formatter.deduplicate()` — key by **address/parcel**, NOT owner (one borrower
  can have many properties; one trustee covers many). Also collapses Sarpy rows that also arrive
  via the Omaha Daily Record foreclosure scraper.
- **Downstream:** enrichment → dedup → export/upload unchanged; `datasift_formatter.py` already
  maps `foreclosure`→"Foreclosure".
- **State:** `emailed_list_state.json` (processed files by name/hash) via `save_state`/`load_state`.

## New files / changes
- `src/list_importer.py` — `import_list(path, source_profile) -> list[NoticeData]`. Profiles:
  `sarpy_nod` (PDF text parse) + a single `douglas_export` spreadsheet parser that branches on
  `RPTTYPE` (`NDEF`→foreclosure, `DSL`→tax_sale). Extensible for future RPTTYPEs.
- `requirements.txt` — `openpyxl` (+ `pandas`); `pypdfium2` already present.
- `src/config.py` — watched-folder path, per-source profiles, `NE` handling, state path; add
  `tax_sale` to `NOTICE_TYPES` (currently `["foreclosure","probate"]`; `foreclosure` present).
- `src/main.py` — mode `list-import` (`--file --source ...` or `--folder` auto-detect); wire into
  `enrichment_modes` (main.py:63).

## CLI (proposed)
```bash
python src/main.py list-import --file ./drop/sarpy_nod_2026-04.pdf --source sarpy_nod
python src/main.py list-import --file ./drop/douglas_nod_2026-06.xlsx --source douglas_export   # RPTTYPE=NDEF → foreclosure
python src/main.py list-import --file ./drop/douglas_taxsale_2026-06.xlsx --source douglas_export  # RPTTYPE=DSL → tax_sale
python src/main.py list-import --folder ./drop            # auto-detect source per file
```

## Acceptance criteria
- Sarpy PDF → `NoticeData` (`county="Sarpy", notice_type="foreclosure"`, owner from Grantor,
  legal desc retained; address filled by downstream lookup).
- Douglas NOD sheet → `NoticeData` (`county="Douglas", notice_type="foreclosure"`, owner from
  **SNDPARTY**, address/parcel/zip populated, trustee NOT mistaken for owner).
- Douglas DSL sheet → `NoticeData` (`county="Douglas", notice_type="tax_sale"`, owner from
  **SNDPARTY** (`FRSTPARTY=STATE` ignored), address filled by name-based assessor lookup, cert #
  retained).
- Re-dropping the same file creates no duplicates; dedup keys on address/parcel.
- Sarpy rows dedup cleanly against Daily-Record foreclosures.
- Records enrich + upload through the unchanged pipeline.

## Open questions
1. **Sarpy party-role** (Grantor = owner?) — verify with a fuller multi-row sample (tell: a
   first-party name repeating across rows = trustee, not owner).
2. Address-match rate for Douglas DSL (name-only) — measure; low match on common names is expected.
   Decide whether to hold unmatched tax-sale rows for manual review vs. ship with a low-confidence flag.
3. Whether the emailed files keep these exact layouts each period (header/RPTTYPE stability).

## Optional backstop (deferred)
Nebraska DOR/PAD Delinquent Real Property Excel (Douglas=28, Sarpy=77) remains a free annual
backstop for **tax delinquency** — covers Sarpy tax delinquency (no email exists for it). Add a
`dor_delinquent` profile to `list_importer.py` later if wanted. Not the same as the DOR
income/sales-tax "Delinquent Taxpayers" lists.

## Effort estimate
Low–Med (1–3 days). Douglas sheet is trivial (rich structured data). Sarpy PDF parse is simple;
the real work is the Sarpy address-resolution join (reuses probate lookup).
