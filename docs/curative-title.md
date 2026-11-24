# Curative title: the whole workflow

A curative title deal is a house whose owner of record died, whose title is still in that name, and where nobody
opened a probate. The family usually does not know what to do, the taxes fall behind, and the house sits. Buying it
means finding every heir, buying each share, and having an attorney clear the title.

This workflow does every step except the attorney's:

| Step | Command | Costs | Writes to DataSift |
|---|---|---|---|
| 1. Rank every house tied to a death | `python src/curative_title.py rank` | free | no |
| 2. Family trees (optional) | `python src/curative_title.py family` | SmartSkip, about $0.15 a hit | no |
| 3. Research every heir, one house at a time | `python src/curative_title.py stories ...` | Claude Code usage | no |
| 4. Check whose phone each number is | `python src/curative_title.py phones` | Trestle, about $0.03 a check | no |
| 5. Re-rank with the research | `python src/curative_title.py score` | free | no |
| 6. Set up the board | `python src/agents/autopilot/curative_setup.py board` | free | the tag and the board |
| 7. Load the work-now houses | `python src/agents/autopilot/curative_setup.py load` | free | numbers, PDF, note, tag |
| 8. Work the board | the `curative_pipeline` stage (agents brain) | free | moves cards, writes call tasks |

`python src/curative_title.py next --market <market>` says where a market stands and what to run next.

Everything reads your own DataSift account over the Open API key. Nothing is ever sent, texted or mailed by steps 1
to 5; the paid steps run only with `--commit`, after a free dry run tells you the count.

## Before you start

- `REISIFT_API_KEY` in `.env` at the repo root: your DataSift Open API key (Settings, then API).
- Records on the lists that mark a death or an estate. By default the ranking reads the lists **Obituary**,
  **Probate** and **Foreclosure** (an estate in foreclosure is the hardest deadline). Add your own with
  `--lists` and `--tags`.
- For step 3, [Claude Code](https://claude.com/claude-code) installed and signed in (run `claude` once). Each house is
  researched in its own Claude Code session on your own plan.
- Optional keys, each used only with `--commit`: `SMARTSKIP_EMAIL` / `SMARTSKIP_PASSWORD` (family trees),
  `ENFORMION_AP_NAME` / `ENFORMION_AP_PASSWORD` (a people-search tool the research sessions can use),
  `SCRAPFLY_KEY` (reads funeral home pages that refuse a plain read), `TRESTLE_API_KEY` (phone checks).

## Step 1: rank

```bash
python src/curative_title.py rank --county "Knox, TN"
```

This reads every record on the lists in scope, in full, plus SiftMap's detail for each house (deed owners, the
county assessment, death and lien flags, sales and loans). Where the county has a built-in source it also reads:

| County | Source |
|---|---|
| Knox County, TN | the Trustee's live tax roll, the City of Knoxville's own tax page, the Knox County Public Library's obituary index and its probate index |
| Blount County, TN | the Trustee's live tax roll |
| any other county | the record and SiftMap only (the workbook says so) |

Any US county works by name and state (`--county "Polk, FL"`) or FIPS code. Giving a county its own sources (a tax
roll, an obituary or probate index) is one entry in `src/curative_engine/counties.py`.

It writes `output/curative_title/<market>/<Market>_Curative_Title_<date>.xlsx` (a market is the county, for
example `knox-tn`). The run is resumable: a record read in the last 7 days is not read again, and
`--max-records 200` reads a large account a little at a time. A big account takes a while: each record is two
requests, paced to stay well inside DataSift's shared limit (120 requests a minute for every tool on the account),
which is about an hour for every 1,500 records. If nothing else uses your key while it runs, `AUTOPILOT_API_GAP_S=0.75`
reads about 80 requests a minute.

### How a house is scored (105 points, shown out of 100)

| Part | Points | What earns them |
|---|---|---|
| Death and title | 35 | the owner died (by how sure we are), the title still in the decedent's name or an estate on the roll, one owner on the deed |
| No probate | 20 | no estate opened and the death at least six months back |
| Taxes and liens | 20 | county and city years behind, turned over or a tax suit, $2,000+ owed, liens, a foreclosure sale ahead |
| Money | 20 | the value band, how many heirs the equity carries at $50,000 each, free and clear |
| Disconnected | 10 | absentee or out-of-state mail, a vacant house, family other than the owner paying the bills |
| Penalties | | a trust, a life estate, a spouse trap, a surviving co-owner, an earlier not interested, research that found the owner living |

**Tier 1, work now:** 70+, the death confirmed, a ghost property, the magic three (one owner, an estate, taxes
behind) or an estate in foreclosure, equity for at least one $50,000 share, and no trust, spouse trap or surviving
co-owner. **Tier 2, research first:** one fact to settle (usually the death or a second owner). Probate deals sit
on their own tab: an open estate is organized and goes through the estate, not share by share.

Two traps the score is built around:

- **The spouse trap.** An obituary on the record is not proof the OWNER died. When the obituary found is the
  co-owner's, the survivor usually holds the house, and nothing is stuck.
- **Two names on a deed.** A married couple's house passes to the survivor at the first death. It is stuck only
  after the second death.

## Step 2: family trees (optional)

```bash
python src/curative_title.py family --market knox-tn            # builds the batch file
python src/curative_title.py family --market knox-tn --quote    # SmartSkip's free estimate
python src/curative_title.py family --market knox-tn --commit   # pays, waits, downloads (cap: --max-usd 60)
```

Each title holder is traced at the house, so SmartSkip returns the family: relatives, their possible relationship,
ages and phones. A deceased flag on a title holder counts as the death. SmartSkip's relationship labels are a guess
(most read Relative or In-Law): the research in step 3 settles who is family and who is a stepchild.

## Step 3: research every heir

```bash
python src/curative_title.py stories select    --market knox-tn --top 25
python src/curative_title.py stories run       --market knox-tn
python src/curative_title.py stories status    --market knox-tn
python src/curative_title.py stories summarize --market knox-tn
```

`select` takes the top of the ranking (likely vacant lots are left out) and writes one `input.json` per house in
`output/curative_title/<market>/stories/houses/<slug>/`. `run` researches each house in its own headless Claude Code
session, in a clean folder outside the repo, following `src/curative_engine/heir_method.md`:

1. every death and its order, from obituaries, Find a Grave, Social Security and the county's obituary index;
2. whose estate holds the house;
3. every marriage and every child, each tied to a record that names the parent (stepchildren inherit nothing);
4. children who died first, and the earlier marriages where heirs hide;
5. parents and siblings when there is no spouse or child;
6. each heir's share under the state's law (Tennessee is written out; any other state's statute is looked up and
   cited);
7. where each living heir lives, and any numbers found;
8. what could stop a deal (a will, an open probate, a non-seller living in the house).

Each house ends with `heirs.json` and a one-page `story.md`. Limits: `--concurrency 3` sessions at once,
`--house-usd 10` and `--daily-usd 100` of Claude Code usage (its own estimate), and 80 minutes a house. Create the
file `stories/STOP` to stop after the houses running now. A house that fails is listed by `status`; `run` again
picks up where it stopped. The research sessions get no keys: the tools they may run read `.env` themselves.

The research is research only: nobody is contacted, nothing is bought and nothing is written to DataSift.

**By hand instead:** in any Claude Code session, point it at a house folder's `input.json` and at
`heir_method.md`, and ask it to write `heirs.json` and `story.md` into that folder. `stories summarize` reads them
the same way. A person can also correct a house: put a `heirs_verified.json` beside `heirs.json` and it wins.

### The verdicts

| Verdict | Means |
|---|---|
| Work now | every heir tied with high or medium confidence, each share clears $50,000, and at least one heir has a confirmed number (or none were checked yet) |
| Work now, no confirmed number yet | the same, but the phone check found no number that belongs to an heir |
| Research more | a heir is unproven or the confidence is low |
| Will or probate on file | a will decides the house, or an estate is open: a probate deal |
| Under $50K a share | too many heirs for the equity |
| Not a house | the county appraisal is far under the value estimate (a vacant lot or a teardown) |
| Not curative | the owner is alive |

## Step 4: check whose phone each number is

```bash
python src/curative_title.py phones --market knox-tn            # how many checks, about what it costs
python src/curative_title.py phones --market knox-tn --commit   # cap: --max-pairs 250
```

Trestle Real Contact tests each heir's numbers against the heir's own name: does the line belong to them, how
reachable it is, and any litigator risk. Only the heir's own number with grade A or B and no litigator risk is ever
loaded onto a record. A pair checked once is never paid for twice.

## Step 5: re-rank with the research

```bash
python src/curative_title.py score --market knox-tn
```

A house with an heir story takes its tier from the story, and the ones ready to work sort first.

## Steps 6 and 7: the board and the load

Every write needs `--commit` **and** `CURATIVE_ALLOW=1`; without both, each command only says what it would do.

```bash
python src/agents/autopilot/curative_setup.py board                    # dry
CURATIVE_ALLOW=1 python src/agents/autopilot/curative_setup.py board --commit
```

This makes the tag `Curative` and the SiftLine board **Curative Title**. The published API has no route for a
board's columns, so with only the Open API key you make them once in the app, left to right, with these titles:
Research, Ready for Marketing, Day 1, Day 2, Day 3, Day 4, Day 5, Heir Engaged, Exhausted. Put any one record on
the Research column, then:

```bash
python src/agents/autopilot/curative_setup.py columns
```

It prints the column ids for the stage's `column_ids` in `team/autopilot.yaml` (the board stage can also learn them
by itself once a card is on the board).

Then load the work-now houses:

```bash
python src/agents/autopilot/curative_setup.py load --results output/curative_title/knox-tn/stories            # dry
CURATIVE_ALLOW=1 python src/agents/autopilot/curative_setup.py load --results output/curative_title/knox-tn/stories --commit
```

For each work-now house, the record is found by address (never by guess), and a house the gate holds (Do Not
Market, a litigator, a lead already being worked, a company owner) is left alone and listed. Then: each living
heir's confirmed numbers go on the record tagged with the heir's tie, the heir story is filed as a PDF in the
record's documents, one pinned note lists the heirs, their shares and who to call first (never a phone number), and
the `Curative` tag goes on last. The first record is read back before any other is touched.

## Step 8: work the board

The `curative_pipeline` stage in the agents brain works the board: a house moves from Research to Ready for
Marketing once a living heir has a usable number and the PDF is on the record; at most 10 houses a business day
start Day 1; each day every living heir with a number gets a call task; an answered conversation moves the card to
Heir Engaged; after Day 5 with nobody reached the card goes to Exhausted with a call back in 30 days. It never
texts. See the stage's block in `team/autopilot.yaml`.

## The call order

- Research every heir first. Call 1 is discovery with no price: say plainly that you are a local buyer, and ask who
  is who in the family and who wants out. The offer comes on call 2.
- Every share has to pay on its own: start offers at $500 to $1,000, and never pay more than the share is worth to
  you alone.
- Walk away when an owner who will not sell lives in the house.
- Never imply you represent the estate, the court, the lender or the family. Never promise to stop or delay a
  foreclosure.
- A family member is never a wrong number, and every stop request holds on every channel.
- Nobody here is a lawyer: an affidavit of heirship, a deed of each share and a release, or opening the estate, is
  your attorney's call. A title search comes before the first share is bought.

## Pulling in more houses

`rank` reads houses already in your account. To rank county houses SiftMap flags for a death or an estate that are
not records yet:

```bash
python src/curative_title.py widen --county "Knox, TN"                         # the count and the allowance quote
python src/curative_title.py widen --county "Knox, TN" --commit --approve 400  # pulls them onto Curative Candidates
```

A pull spends record allowance (the quote says how much first), and your account may skip trace new records on its
own. The next `rank` reads the Curative Candidates list with the others.

## Where things are

| What | Where |
|---|---|
| The engine | `src/curative_engine/` (one module a step) and `src/curative_title.py` |
| The research method | `src/curative_engine/heir_method.md` |
| The loader and the board | `src/agents/autopilot/curative_setup.py`, `src/agents/autopilot/curative.py` |
| A market's files | `output/curative_title/<market>/` (gitignored: it holds owner and family names) |
| Tests | `tests/test_curative_engine.py`, `src/agents/selftest_curative.py` |

The workbook and the research files carry owner and family names: keep them inside your team.
