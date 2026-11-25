---
name: estates-heirs-chain
description: >
  Estates and heirs, end to end: for every deceased owner (probate notices, the Obituary and Probate lists, skip trace deceased flags) find the house, confirm whose death it is, research the family tree and who must sign, get a number that provably belongs to each signer, rank the stuck titles worth curing and run the Curative Title board, put the decision maker on the record, and work the estate with calls and letters. Runs on your DataSift Open API key with SmartSkip, Trestle and research in Claude Code. Dry run, then a quote, then one record read back, then the rest; nothing reaches a family without a person's go. Use when someone says "work my probate list", "find the heirs", "who signs on this estate", "deep prospect this deceased owner", "set up curative title", "rank my obituary list", or "the owner died, now what".
---

# Estates and heirs chain

When an owner dies, the house sits until someone finds who can sell it. This chain takes every deceased-owner record
from a name on a list to a verified signer with a number that reaches them, then works the estate: an open probate
through the personal representative, a stuck title through the Curative Title board. It is for an operator working
probate, obituary and heir leads.

## Who does what

In Claude Code one session runs every step and does the research itself, on your Claude Code plan. The curative
research opens one more Claude Code session per house. The session stops at every quote, before every write to
DataSift, and before anyone is contacted.

On the Slack team (the agents on Fly) the work hands off on each estate's own thread: Diggs hands Roma a house once
a living heir has a confirmed number and the heir report is on the record, and Roma hands Quinn the letters.

| Step | On the Slack team |
|---|---|
| 1. Gather and sort | Diggs: `research_queue` picks which estates to research; the ranking runs by hand |
| 2. Find the house | Diggs: `probate_property` (Knox and Blount rolls) |
| 3. Who must sign | Diggs: `deep_prospect` (it can run its research on the low-cost OpenRouter models) |
| 4. Numbers | Diggs: `phone_score`; the name check runs inside `deep_prospect` |
| 5. Curative title | Diggs runs the research; Roma works the Curative Title board (`curative_pipeline`) and prices the house (`quick_screen`, `underwrite`) |
| 6. Owner swap | Diggs: `estate_owner_swap`, on estates whose research already wrote back |
| 7. Work the estate | Quinn: `heir_lane` (call tasks and heir letters) and `probate_mail` once switched on; Diggs: `obituary_lane` holds obituary records 30 days and stages family letters. Every letter batch waits for a person to release it |

## Before you start

- **The SiftStack folder** (the starter folder or a clone of the repo). Every script runs from it as
  `python src/<file>.py`, and every key goes in `.env` at its root. Install the packages once:
  `pip install -r requirements.txt`.
- **Your DataSift Open API key** as `REISIFT_API_KEY` in `.env`. An account admin makes it in DataSift
  (developers.datasift.ai explains it). Every DataSift read and write here uses it and nothing else.
- **Claude Code**, installed and signed in (run `claude` once). The research runs on your own plan.
- **Estates to work.** `first-to-market-chain` feeds probate notices and its death holds (status Deep Prospecting,
  tag Verify Deceased). DataSift's own Obituary and Probate lists feed the rest.

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install probate-property-finder@siftstack
/plugin install deep-prospecting-v5@siftstack
/plugin install phone-validator@siftstack
/plugin install siftstack-core@siftstack
```

| Key or account | What it does here | Cost |
|---|---|---|
| `REISIFT_API_KEY` | every DataSift read and write | comes with DataSift; a pulled-in record uses your record allowance |
| `SMARTSKIP_EMAIL`, `SMARTSKIP_PASSWORD` (your SmartSkip account) | relatives and their phones in one batch | $0.15 a hit; billed to the card on file, not the wallet |
| `TRESTLE_API_KEY` | dial tiers, and Real Contact (whose number it is) | about $0.015 a number; about $0.03 a Real Contact check |
| `TRACERFY_API_KEY` | phones for a named signer SmartSkip left without a live one | about $0.02 a record |
| `ENFORMION_AP_NAME`, `ENFORMION_AP_PASSWORD` | company owners (BusinessV2), and a people search the research may use | about $0.10 a search at the DataSift affiliate rate |
| `SCRAPFLY_KEY` | funeral home pages that refuse a plain read | Scrapfly's own price per page |
| Claude Code | the heir research | your plan's usage; the curative research caps its own estimate at $10 a house and $100 a day |

A full deep prospect on one record runs about $0.24 in vendor fees.

## Parameters

- `county` (required): the county name, for example `Polk`.
- `state` (required): the two-letter state, for example `FL`.
- `market`: the curative folder name. Default `<county>-<state>` in lowercase, for example `polk-fl`.
- `lists`: the lists the ranking reads. Default `Obituary,Probate,Foreclosure`.
- `tags`: tags added to the ranking's scope. Default `Verify Deceased`.
- `max_records`: records the ranking reads per run. Default 200 (it resumes where it stopped).
- `top_houses`: houses sent to curative research. Default 5 on the first run (the engine's own default is 25).
- `context`: the line at the top of each research pack, for example `Probate list record`. Default
  `Estate record`.
- `house_usd`, `daily_usd`: Claude Code usage caps for the curative research. Default 10 and 100.
- `budget_usd`: the most one run may spend on SmartSkip, Trestle and Tracerfy. Default 25.
- `owner_swap`: whether the write-back may change the owner of record. Default false.
- `dry_run`: default true. Nothing is written or bought until a person says go for that step.

## Steps

### 1. Gather and sort the estates

```bash
python src/curative_title.py rank --county "<County>, <ST>" --tags "Verify Deceased" --max-records 200
python src/curative_title.py next --market <market>
```

The ranking reads every record on the lists in scope, in full, plus SiftMap's detail for each house (deed owners,
the county assessment, death and lien flags, sales and loans), scores it out of 100, and writes
`output/curative_title/<market>/<Market>_Curative_Title_<date>.xlsx`. It is free and read only. Each record is two
requests paced inside DataSift's shared limit, about an hour for every 1,500 records; a record read in the last 7
days is not read again, so a big account goes a slice at a time.

Sort each estate into a lane with the table in `references/estate-files.md`: lane A is an open probate (the
personal representative decides), lane B is a stuck title (the owner died, no probate). Write
`output/estates-heirs-chain/sort_<date>.csv`.

You MUST NOT treat an obituary on a record as the owner's death, because it is often the spouse's: the survivor
usually holds the house and nothing is stuck. The ranking flags it; step 3 settles it.

**Check:** the workbook's count block adds up (houses read equals ruled out plus kept). Open three tier 1 houses and
three ruled-out spouse traps and agree with each call.
**Then:** lane A goes to step 2; lane B tier 2 goes to step 3 to settle its one fact; lane B tier 1 goes to step 5.

### 2. Find and confirm the house (lane A)

A probate notice prints only the court's, the lawyer's and the personal representative's addresses, so a probate
record can sit at the wrong house. For each probate whose house is missing or doubtful, run the
`probate-property-finder` skill (browser research on the county assessor and recorder; free). Then read the owner of
record at every lane A address and apply the house check in `references/estate-files.md`.

- Output: the finder's CSV as `output/estates-heirs-chain/probate_found_<date>.csv`, and
  `output/estates-heirs-chain/house_check_<date>.csv`.
- You MUST NOT accept a match on the surname alone, because it is usually a stranger or the executor's own house:
  the owner of record must carry the decedent's first and last name (a maiden name or an AKA counts).
- Only HIGH confidence finds go forward. A found house that is not yet a record goes up through
  `first-to-market-chain` steps 3 and 4 on the Probate list: the personal representative as owner at their own
  mailing address, with Decedent Name and Personal Representative filled.
- You SHOULD list a wrong house for a person instead of editing the record's address: a surviving spouse on the
  title or an estate that already sold both look like a mismatch.

**Check:** every lane A estate has a reading and a source URL; count kept, sold, wrong and held.
**Then:** step 3 for every kept estate.

### 3. Who must sign

One record at a time, with `deep-prospecting-v5`'s method, on the key. Set the record's street line and a run
folder once, then run the phases in order (bash):

```bash
A="123 Example St"; R=output/estates-heirs-chain/123-example-st
python src/deep_prospect_record.py --address "$A" --run "$R" --phase records --context "Probate list record"
python src/deep_prospect_record.py --address "$A" --run "$R" --phase smartskip            # free: uploads the batch, prints the billable count
python src/deep_prospect_record.py --address "$A" --run "$R" --phase smartskip --commit   # pays, waits, downloads ($0.15 a hit)
python src/deep_prospect_record.py --address "$A" --run "$R" --phase merge
```

Now the session researches and writes `research/<slug>.json` and `analysis/<slug>.json` in the run folder
(`references/estate-files.md` has the contract and an invented example to copy):

1. Find the obituary: the decedent's name, the city, "obituary", the year. Funeral home pages are best.
2. Confirm whose obituary it is against the owner of record (the spouse trap).
3. Copy the survivors paragraph word for word. Reconcile it with SmartSkip's relatives in both directions; married
   daughters carry another surname.
4. Replace SmartSkip's coarse labels ("Relative", "In-Law") with the obituary's real relationships.
5. Record the date of death. Reject an obituary whose death is more than 3 years before the probate notice's
   publication date: it is someone else with the same name.
6. Decide who must sign, from verified living people only: the personal representative on an open estate; the
   successor trustee when a trust holds the title; otherwise the heirs under the state's law.

```bash
python src/deep_prospect_record.py --address "$A" --run "$R" --phase heirs            # free: each signer as a subject at their own street
python src/deep_prospect_record.py --address "$A" --run "$R" --phase heirs --commit
python src/deep_prospect_record.py --address "$A" --run "$R" --phase merge
```

- A company owner cannot be name-traced: route it to Enformion BusinessV2 (the skill's
  `deep-prospecting-v5/references/entity-owners-enformion-business.md`). A family trust's successor trustee is
  found by the research and traced like any person.
- The records phase reads Knox County's tax roll by street. Outside Knox its "title owner" line is not your
  county's: confirm the title on your own assessor (step 2) and ignore that line.

**Check:** the smartskip dry run printed the billable count before you paid; the analysis names who must sign, each
with a source; whose obituary it is is answered in writing.
**Then:** step 4 scores and checks the numbers. A lane B tier 2 estate whose fact is now settled goes to step 5.

### 4. Numbers that reach the signers

`phone-validator`'s tier bands, plus a check of whose name is on each line.

```bash
python src/deep_prospect_record.py --address "$A" --run "$R" --phase score              # dry: the numbers it would score
python src/deep_prospect_record.py --address "$A" --run "$R" --phase score --commit     # Trestle, about $0.015 a number
python src/deep_prospect_record.py --address "$A" --run "$R" --phase gapfill --commit   # only when a signer has no live number: Tracerfy
python src/deep_prospect_record.py --address "$A" --run "$R" --phase verify             # dry: the name checks it would buy
python src/deep_prospect_record.py --address "$A" --run "$R" --phase verify --commit    # Trestle Real Contact, about $0.03 a pair
```

- For estate records outside a deep prospect run, tier their numbers with
  `python src/phone_tiers.py --uuids <uuid>,<uuid>` (dry), then the same with `--commit`.
- For curative houses (lane B), the heirs' numbers are checked by
  `python src/curative_title.py phones --market <market>` (dry: how many checks, about what it costs), then the same
  with `--commit` (cap `--max-pairs 250`; a pair checked once is never paid for twice).
- Tiers: Dial First 81-100, Dial Second 61-80, Dial Third 41-60, Dial Fourth 21-40, Drop 0-20; a litigator risk is
  tagged DNC instead. A number belongs to a signer only when Real Contact matches the signer's name with grade A or B
  and no litigator risk.
- You MUST NOT tag a number with a relationship when the owner also holds it, because the dial sheet would then
  call the owner's own line "Son". You MUST NOT put a person's name in a phone tag, because tags are shared across
  the account: names live in the note with the last four digits.

**Check:** each dry run's count before its commit (times the price above is the most it can cost); afterwards every
signer has a Dial First or Second number that matched their name, or the estate says "no confirmed number".
**Then:** lane A goes to step 6; lane B goes to step 5.

### 5. Curative title (lane B)

Family trees are optional; the research is not.

```bash
python src/curative_title.py family --market <market>            # builds the batch
python src/curative_title.py family --market <market> --quote    # SmartSkip's free estimate
python src/curative_title.py family --market <market> --commit   # pays, waits, downloads (cap --max-usd 60)

python src/curative_title.py stories select    --market <market> --top 5
python src/curative_title.py stories run       --market <market>
python src/curative_title.py stories status    --market <market>
python src/curative_title.py stories summarize --market <market>
```

`run` researches each house in its own headless Claude Code session, in a clean folder outside the repo, following
the heir method (`src/curative_engine/heir_method.md`): every death and its order, whose estate holds the house,
every marriage and child tied to a record that names the parent, children who died first, each heir's share under
the state's law, where each living heir lives, and what could stop a deal. Each house ends with `heirs.json` and a
one-page `story.md`. Caps: `--concurrency 3`, `--house-usd 10`, `--daily-usd 100`, 80 minutes a house. Create the
file `output/curative_title/<market>/stories/STOP` to stop after the houses running now; `run` again picks up where
it stopped.

Then step 4's `phones`, then re-rank with the research:

```bash
python src/curative_title.py score --market <market>
```

Set up the board and load the work-now houses. Every write needs `--commit` and `CURATIVE_ALLOW=1` together:

```bash
python src/agents/autopilot/curative_setup.py board                              # dry
CURATIVE_ALLOW=1 python src/agents/autopilot/curative_setup.py board --commit    # the tag Curative and the board Curative Title
# make the board's nine columns in the app (Built in the app), put one record on Research, then:
python src/agents/autopilot/curative_setup.py columns
python src/agents/autopilot/curative_setup.py load --results output/curative_title/<market>/stories --out output/estates-heirs-chain/curative
CURATIVE_ALLOW=1 python src/agents/autopilot/curative_setup.py load --results output/curative_title/<market>/stories --out output/estates-heirs-chain/curative --commit --limit 1
```

Read that first record back in the app: each living heir's confirmed numbers tagged with the heir's tie, the heir
story as a PDF in the record's documents, one pinned note with the heirs, their shares and who to call first (never
a phone number), and the tag Curative. Then load the rest without `--limit`. The loader finds each house by address,
never by guess, and leaves alone and lists any house held by Do Not Market, a litigator, a lead already being worked,
or a company or trust owner.

- Optional: `reset-statuses` (dry first) clears the old marketing statuses on the work-now houses and keeps sold,
  deal and do-not-contact statuses; `--undo <log>` puts them back.
- You MUST research every heir before any call, because a missed heir can block the title after money is spent.

**Check:** `stories status` lists no failed house (a failed house runs again with `run`); `stories summarize` gives
each house a verdict (`references/estate-files.md`); `next` says the market is ready to load; the first loaded
record reads back.
**Then:** step 7 works the board. A house with a single verified decision maker MAY also go through step 6.

### 6. Put the decision maker on the record

For lane A, and any estate whose research verified one decision maker: the owner of record becomes that living
person at their own mailing address, so calls, mail and DataSift's own sequences reach them instead of the person
who died.

```bash
python src/deep_prospect_record.py --address "$A" --run "$R" --phase build    # the research pack PDF and the board note
AGENTS_DP_OWNER_SWAP=1 python src/deep_prospect_record.py --address "$A" --run "$R" --phase writeback            # dry: what it would write
AGENTS_DP_OWNER_SWAP=1 python src/deep_prospect_record.py --address "$A" --run "$R" --phase writeback --commit
```

The write-back adds the Dial First and Second numbers (Third only as a reserve), one pinned research note, the
decision maker fields once verified, the death fields when the record's owner is the person who died, and, with the
switch, the owner change. Leave `AGENTS_DP_OWNER_SWAP` off to write everything else and change the owner by hand in
the app.

- The owner changes only to a verified living person at their own confirmed address. The write-back refuses a
  company or trust, two decision makers who tie, a person a skip trace flags deceased, and an owner who holds more
  than one house in the account. The dry run prints `owner -> <person>` or the reason it would not change; the
  house count is read only at `--commit`, so a dry "owner ->" line can still be refused there.
- You MUST read the dry output before `--commit`, because this is the first step in the chain that changes who the
  record belongs to.

**Check:** the dry output lists the owner change or the reason it was held; after `--commit` the record shows the
new owner at their own mailing address, the death tags and lists still on it, and a note saying who changed.
**Then:** step 7.

### 7. Work the estate

Nothing in this step reaches a family until a person says go for that batch. Write the week's list to
`output/estates-heirs-chain/outreach_<date>.csv` (address, lane, who first, channel, who approved).

- **Lane A, probate.** Call the personal representative: the estate call lane (Ready to Call, then Call Attempt 1
  to 4), Dial First and Second numbers first. Letters go to the representative's own address, counted from the
  filing: one at once, two follow-ups, then monthly. A letter may say where you found their name (the public probate
  record); a text never mentions the death, the estate or the court.
- **Lane B, the Curative Title board.** Research, Ready for Marketing, Day 1 to Day 5, then Heir Engaged or
  Exhausted. At most 10 houses start Day 1 a business day; each day, call every living heir with a confirmed number;
  an answered conversation moves the card to Heir Engaged; Day 5 with nobody reached moves it to Exhausted with a call
  back in 30 days. The board never texts. In Claude Code alone a person moves the cards in the app; on the Slack team
  Roma's `curative_pipeline` moves them and writes the call tasks.
- **Obituary records with no probate.** Wait at least 30 days after the death before the first call. A letter to
  the family goes to the family by surname or to a verified heir at their own address, never to the person who
  died.

The call order, for every lane:

1. Call 1 is discovery with no price: say plainly you are a local buyer, and ask who is who and who wants out. The
   offer comes on call 2.
2. Every share has to pay on its own: never pay more for a share than it is worth to you alone.
3. Walk away when an owner who will not sell lives in the house.
4. A title search comes before the first share is bought, and an attorney decides the paperwork (an affidavit of
   heirship, a deed of each share and a release, or opening the estate). Nobody in this chain is a lawyer.

You MUST NOT imply you represent the estate, the court, the lender or the family, because it misleads a grieving
family and invites a complaint. You MUST NOT promise to stop or delay a foreclosure, because state foreclosure
rescue laws regulate that promise. You MUST NOT mark a family member's number wrong, because the dialer would then
skip the owner's own children: tag it Relative instead.

**Check:** a person approved each batch; every call outcome is on the record; a stop request holds on every channel.
**Then:** back to step 1 each week: `rank` re-reads records older than 7 days, and new estates join the sort.

## Stand it up

**First run, about 10 estates.**

1. `rank` on the account (free). Pick 10 from the workbook: about 5 probate deals and 5 work-list houses.
2. Step 2 on the 5 probates.
3. Steps 3, 4 and 6 on two records first: records, the SmartSkip count, `--commit`, the research files, heirs,
   merge, score, verify, build, the write-back dry, then `--commit` on one record. Read it back. Then the other.
4. Step 5 on the 5 houses: `stories select --top 5`, run, status, summarize, phones, score, the board, the columns,
   `load --limit 1`, the read-back, then the rest.

**What working looks like:** every estate says whose death it is, who must sign, and a number that matched the
signer's name (or "none confirmed"); the spouse traps are caught and corrected; the first loaded house shows its heir
numbers, the PDF, the note and the tag; nobody has been called yet.

**On a schedule.**

- Weekly: `rank` and `next`, then the stories for new tier 1 houses inside the daily cap. A Claude Code routine
  (`/schedule`) or your computer's scheduler can start the run.
- Every business day: the board, worked by a person, or by Roma's `curative_pipeline` on the Slack team (it runs
  three times a weekday).
- On the Slack team, Diggs's `research_queue` picks the next estates to research every half hour in working hours.

**Weekly, about 15 minutes:** spend against `budget_usd`, any failed story, the Exhausted cards whose call back is
due, and the new Verify Deceased holds from `first-to-market-chain`.

## What to watch

1. **Estates sorted each week**, and how many the research changed (a spouse trap, a living owner, a wrong house).
2. **Signers reachable:** the share of signers with a number that matched their name (Real Contact grade A or B).
3. **Board flow:** houses starting Day 1, cards in Heir Engaged, cards in Exhausted.
4. **Owner changes** done against held, with the top reason held.
5. **Spend per estate** (SmartSkip hits, Trestle numbers, Real Contact pairs) against about $0.24 a record.

## Built in the app

1. **The Curative Title board's columns.** The published API has no route for board columns. On the board the
   `board` command made, add nine columns, left to right: Research, Ready for Marketing, Day 1, Day 2, Day 3, Day 4,
   Day 5, Heir Engaged, Exhausted.
2. **Custom fields.** Decedent Name, Date of Death, Decision Maker, DM Relationship, Personal Representative and
   Owner Deceased (a yes or no select). The Account Setup chain makes them.
3. **Tags.** On records: Curative, Verify Deceased, Heir Identified, Deep Prospected. On phone numbers: Relative.
4. **Estate lanes.** The estate text, call and mail presets and the curative folder (`docs/account-structure.md`,
   folders 04 to 07), built with `sequential-presets`' walkthrough.
5. **Sequences** (Sequence Builder; sequences are not in the published API). Optional: a record set to the status
   Deep Prospecting gets a card on the Deep Prospecting board (Call New Numbers, Call Relatives, Check Relatives,
   Research on Socials, Message on Socials, Doorknock). For the Slack team's research to start on that status, a
   Super Admin makes a webhook in Settings, Integrations, Webhooks, and the sequence gets a Send webhook action.

## Where it works

- **Any county:** the ranking from your own records and SiftMap, SmartSkip family trees, the research, Trestle, the
  board, the load and the owner change. Pulling in more death-flagged houses that are not records yet:
  `python src/curative_title.py widen --county "<County>, <ST>"` (the count and the allowance quote), then
  `--commit --approve <n>`.
- **Knox and Blount (Tennessee):** the ranking also reads the county tax roll. Knox adds the City of Knoxville's tax
  page, the library's obituary index and its probate index. A new county's own sources are one entry in
  `src/curative_engine/counties.py`.
- **Knox only:** the deep prospect's title lookup (step 3).
- **Heir shares:** Tennessee's law is written into the heir method; any other state's statute is looked up and
  cited, house by house.

## Gotchas

- **A skip trace deceased flag: trust a true, never a false.** SmartSkip misses deaths far more often than it
  invents them, and it has no date-of-death column: the date comes from an obituary. A survivors list in someone
  else's obituary only shows a person was alive the day it ran.
- **The spouse trap, and two names on a deed.** An obituary on the record may be the spouse's. A married couple's
  house passes to the survivor at the first death; it is stuck only after the second.
- **SmartSkip's relationships are guesses.** Most come back "Relative" or "In-Law". Stepchildren inherit nothing
  unless adopted; a child who died first leaves their share to their own children; children from an earlier marriage
  inherit like any other child.
- **SmartSkip billing.** A bulk run bills the card on file, not the wallet. An unpaid order is invisible in the order
  list, so the scripts save the order id before paying. Never pay before reading the billable count.
- **Trace a person at a street, never by name and city.** A name and city search returns whichever namesake ranks
  first. Drop the obituary's middle names when tracing heirs: in one run three of four children came back empty with
  them and all three matched without.
- **The probate notice's own details.** The representative's mailing address is often the house itself or the
  lawyer's office, because the parse takes the first address after the title. A phone number in a probate notice is
  the law firm's: never dial it as the family.
- **"Decedent not on the title" is often timing.** The title moved to the representative or the children after the
  record came in, or the estate sold. A $0 family deed does not show in DataSift's last sale field; the county's
  owner card shows it.
- **A "correct" phone can be the wrong person.** One record's number marked correct belonged to the
  representative's spouse and child. Real Contact settles whose line it is.
- **A family member is never a wrong number.** Tag their number Relative. A stop request holds on every channel.
- **The owner change renames.** DataSift renames the owner rather than attaching a new one, so the numbers stay on
  the renamed owner. That is why the write-back refuses an owner who holds more than one house: a rename would
  change every one of them.
- **Research proves nothing by its length.** Every claim needs a URL and a quote; a confident memo with no sources
  is a guess. Watch the usage caps, and create `stories/STOP` to stop a run.
- **Writes need both switches.** The curative loader writes only with `--commit` and `CURATIVE_ALLOW=1`; the board's
  columns are made in the app.
- **Every share has to pay.** The ranking wants equity for at least one $50,000 share; too many heirs for the
  equity is a pass, not a project.
