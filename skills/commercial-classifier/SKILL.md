---
name: commercial-classifier
description: >-
  Classify commercial real estate in any parcel or property file into 13 asset classes and 76 sub-types
  (Office, Industrial, Retail, Restaurant, Shopping Center, Multifamily, Specialty, Health Care,
  Hospitality, Sports & Entertainment, Lab, Land, Residential Income), with confidence, provenance,
  business-point evidence, value bands, true-owner grouping and a review queue. Works on any data: a
  county assessor export, a DataSift export, Regrid, PropStream, ATTOM, LoopNet or Crexi, First
  American, or a county's free ArcGIS parcel layer it downloads for you. Use for "classify these
  commercial properties", "what type of property is this", "asset class", "sub-type", "find the
  self-storage / gas stations / medical offices in this county", "who owns the most commercial
  property here", "build a commercial list", or any spreadsheet tagged only "Commercial".
---

# Commercial Classifier

Turn a property file that says "Commercial" (or a bare county land-use code) into a real asset
class and sub-type, then check it against the businesses actually operating on each parcel,
put a value band on it, roll every parcel up to its true owner, and hand back a short list of the
parcels a person should look at.

The engine is Python in `scripts/` and runs on the standard library. It does the per-parcel work.
**Your job is the part a script cannot do: label a county's land-use codes once, read the review
queue, and explain the result.** A county has 50 to 400 distinct use codes and 100,000 parcels.
Label the codes, never the parcels.

## Setup

Python 3.9+. Nothing else is required. Optional, each used only when present and skipped with a
stated reason when not:

```bash
pip install openpyxl duckdb shapely pyarrow
```

openpyxl writes the Excel workbook and reads .xlsx input. duckdb pulls free Overture business
points (without it the engine falls back to OpenStreetMap). shapely speeds up polygon matching.
pyarrow reads .parquet files. No API keys, no paid services, nothing to sign up for.

All commands below are `python scripts/cc.py <command>` from this skill's folder.

## Step 1: get the data

**They have a file.** Any CSV, XLSX, parquet or JSON of parcels or properties. Go to step 2.

**They have nothing.** Most US counties publish parcels as a free public ArcGIS layer. Find the
layer URL (see `references/data-sources.md`: search the county GIS or open data site for
"parcels", or an ArcGIS REST directory), then:

```bash
python scripts/cc.py probe-layer "<layer url ending in /FeatureServer/0 or /MapServer/0>"
python scripts/cc.py fetch-parcels "<layer url>" --out parcels.csv
```

`probe-layer` shows the fields, the row count and which ones the classifier will use. If it
finds no land-use or class field, tell the person this layer cannot be classified and look for
another (many counties keep land use in a separate assessor layer). `fetch-parcels` pages through
the whole layer, keeps the polygons, and resumes if interrupted.

## Step 2: inspect before running

```bash
python scripts/cc.py inspect <file>
```

It prints the detected source (`fa`, `datasift`, `regrid`, `loopnet_crexi`, `reonomy`, `arcgis`,
`generic`), which header feeds which field, and sample values of the classification signals.
Check the mapping. If a column was missed or mis-read, point it at the right header:

```bash
python scripts/cc.py inspect <file> --map use_desc="Property Use" --map building_sqft="Bldg SF"
```

Field names are in `references/profiles.md`. A DataSift export needs no mapping at all: its
"Structure type" text resolves straight to the national land-use table.

## Step 3: run

```bash
python scripts/cc.py run <file> --state OH
```

`--state` matters only when the file has no situs state column (it drives out-of-state owner
flags). Add `--county-key "OH:LICKING"` when the file has no county or FIPS column. The work
directory is `cc_<file name>/` next to the file; every phase is saved there and a re-run resumes.

**Exit code 3 means codes need labels.** That is expected on any source without a built-in table.

## Step 4: label the codes (this is your job)

Open `<work>/distinct_codes.csv`. Each row is one distinct use value with evidence about the
parcels behind it: count, median building size, share improved, median units, median value, the
words that recur in owner and business names, sample addresses, and a keyword guess. Fill
`asset_class` and `sub_type` on every row marked `needs_label = Y`, then:

```bash
python scripts/cc.py label <work>/distinct_codes.csv --work <work>
python scripts/cc.py run <file> --work <work>
```

How to label well (`references/signals.md` has the vocabulary):

- Use only classes and sub-types from `references/taxonomy.md`. `cc label` rejects anything else
  and says which row.
- The description says what the county meant; the evidence says what is actually there. Code
  "305 Research/Chemical Labs" whose owner words are QTS, Amazon and EdgeConneX is a data center.
  `pct_improved` near 0 is vacant land, whatever the description says.
- Leave the sub-type blank rather than guess one. A blank sub-type is honest; a wrong one repeats
  on every parcel with that code.
- For a catch-all ("Other Commercial Structures"), label the class you believe and add a
  `confidence` column with 0.5 so those parcels land in the review queue.
- Claude labels are capped at 0.75 confidence; a person's (`--source human`) at 0.9. The three
  most valuable parcels behind each of your labels are queued as a spot check.

If the person wants speed over accuracy, `cc run <file> --accept-keywords` keeps the keyword
guesses (capped at 0.6). Say that this was done.

## Step 5: read the results back

Outputs are in `<work>/out/`:

| File | What it is |
|---|---|
| `commercial_classified.xlsx` | Summary, Parcels, Review, Owners, Groups, Crosswalk, QA tabs |
| `classified.csv` | every input column, then `cc_*` result columns |
| `review_queue.csv` | the parcels to check, most expensive mistakes first |
| `entities.csv`, `ownership_groups.csv`, `related_parties.csv` | true-owner roll-up |
| `qa_report.json` | coverage, confidence, value accuracy, and every skipped phase with its reason |

Report to the person, in this order: how many parcels by class (lead with the commercial ones they
asked about), how many need review and the top three reasons, the value-band holdout accuracy, the
largest owners, and anything skipped. Read `skipped` in `qa_report.json` out loud: a phase that
did not run is never a green light.

## Step 6: close the loop

When the person reviews parcels, they fill `verdict`, `correct_class`, `correct_sub` in
`review_queue.csv` (a verdict of `ok` confirms the current class). Then:

```bash
python scripts/cc.py apply-review reviewed.csv --work <work>
python scripts/cc.py run <file> --work <work>
```

Those become `verified` overrides and survive every re-run.

## Rules

- **Never classify parcel by parcel at scale.** Label distinct codes and let the engine apply them.
- **Missing is not zero.** A file without a building-size or improvement-value column says nothing
  about vacancy; the engine knows this, so do not "correct" it by hand.
- **Value bands are bands.** Quote the holdout accuracy from `qa_report.json` whenever you quote a
  band. A file with only assessed values and no sales gets no value bands, by design.
- **Owner groups are leads, not proven corporate parents.** Say so when you present them.
- **Nothing here walked the property.** Classes come from codes, rules and business listings.
- Public and free data only. The skill never asks for a key and never spends money.

## References

- `references/taxonomy.md`: the 13 classes and 76 sub-types, and how to settle ambiguous cases
- `references/signals.md`: what land-use words, zoning, business names and building specs mean
- `references/data-sources.md`: where to get parcel data, free and paid, and how to find a county layer
- `references/profiles.md`: every canonical field, the supported sources, and `--map`
- `references/qa.md`: confidence, provenance, the review queue, ownership tiers, and the QA report
