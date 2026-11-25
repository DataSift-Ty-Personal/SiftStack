# Estate tables and the research files

Load this at step 1 (sorting), step 2 (the house check), step 3 (writing the research files) and step 5 (reading
the curative verdicts). Every command runs from the SiftStack folder.

## Step 1: from the ranking workbook to a lane

`python src/curative_title.py rank` writes `output/curative_title/<market>/<Market>_Curative_Title_<date>.xlsx`.
Its tabs sort the estates:

| Workbook tab | What it holds | Lane | Next |
|---|---|---|---|
| Probate deals | an estate is already open; the family is organized around a personal representative | A | steps 2, 3, 4, 6, 7 |
| Work list, tier 1 (work now) | the owner died, no probate, the title still in the dead owner's name, equity for at least one $50,000 share | B | step 5, then 4 and 7 |
| Work list, tier 2 (research first) | the same, with one fact to settle first (usually the death or a second owner on the deed) | B | step 3 settles the fact, then step 5 |
| All ranked, tier 3 (watch) | weaker signals | none yet | re-ranked every week |
| Foreclosures, no death | no sign the owner died | a living owner | back to `first-to-market-chain` step 7 |
| Ruled out | each house with its reason: a trust, a life estate, a surviving co-owner, the spouse trap, an earlier not interested, a living owner | none | correct the record; a living owner goes back to `first-to-market-chain` |
| Taxes behind | houses owing delinquent county or city tax on the offices' own bills | any | a deadline to note on the estate |

Write `output/estates-heirs-chain/sort_<date>.csv` with the columns address, lane, tab, why, next_step.

## Step 2: does the record's address belong to the estate?

Read the owner of record at the address on the county assessor (not an aggregator), then:

| What the county shows at the address | Reading | Action |
|---|---|---|
| the decedent by first and last name (a maiden name or an AKA counts), or "Estate of" the decedent | the estate's house | keep |
| the personal representative, and the representative's mailing street is this street | the representative's own home | find the real house |
| the personal representative, living somewhere else | most likely inherited | keep: this is the lead to work |
| someone unrelated, with a priced sale after the death | the estate sold it | close it out |
| someone unrelated, with no transfer since the death | a wrong house | find the real house |
| only the surname matches | not proof | hold for a person |
| a family member on a $0 deed after the death | the title moved inside the family | keep, and say so in the note |

DataSift's last sale field skips a $0 family transfer, so "no recent sale" never rules one out: the county's owner
card shows it. Write `output/estates-heirs-chain/house_check_<date>.csv` with the columns address, decedent,
owner_on_title, reading, action, source_url.

## Step 3: the two research files

`src/deep_prospect_record.py` renders and writes only what two files in the run folder say. The session writes
them, following `deep-prospecting-v5`:

- `research/<slug>.json`: sourced facts. Who owns the house, whether they are living, every death found (name,
  relationship to the owner, date of birth and death, the obituary URL, the survivors paragraph word for word), the
  people the sources name, and anything unsettled in `unresolved`.
- `analysis/<slug>.json`: the judgment. The headline, the owner's status, who must sign and why, every person with
  their relationship, whether they are living (Living, Deceased or Unknown), their role (owner, signer, decision
  maker, channel or none), where to trace them (`trace_address`: street, city, state, zip), the call order, the
  death block (decedent, date of death, obituary URL, whether the owner of record died, whether it is confirmed),
  and the record fields to fill (Owner Deceased, Date of Death, Decedent Name, Obituary URL, Decision Maker, DM
  Relationship).

The exact keys are `RESEARCH_SCHEMA` and `ANALYSIS_SCHEMA` in `src/agents/research/researcher.py`. A complete,
invented example of each is built by the functions `analysis` and `research_file` in
`src/agents/selftest_writeback.py`: copy their shape. The slug is the record's street line in lowercase, with every
run of other characters turned into one hyphen (`123 Example St` becomes `123-example-st`), the same slug the run
folder's other files use.

Rules for both files:

- Every claim carries a URL you opened and a short quote from it. Anything you cannot source goes in `unresolved`.
- A person is Living only when something recent names them as living and no death matches their name and age.
  Unknown is for a specific possible death you could not rule out; name it. A skip trace deceased flag that reads
  true makes a person Deceased.
- Never mark the owner of record dead on someone else's obituary.
- A heir to trace needs a street. Never trace by name and city.

## Step 5: the curative verdicts

`python src/curative_title.py stories summarize` gives each researched house one verdict:

| Verdict | Means |
|---|---|
| Work now | every heir tied with high or medium confidence, each share clears $50,000, and at least one heir has a confirmed number (or none were checked yet) |
| Work now, no confirmed number yet | the same, but the phone check found no number that belongs to an heir |
| Research more | an heir is unproven or the confidence is low |
| Will or probate on file | a will decides the house, or an estate is open: a probate deal, lane A |
| Under $50K a share | too many heirs for the equity |
| Not a house | the county appraisal is far under the value estimate (a vacant lot or a teardown) |
| Not curative | the owner is alive |

Only the two Work now verdicts are loaded onto the board. A person can correct a house by putting a
`heirs_verified.json` beside its `heirs.json`; the corrected file wins.
