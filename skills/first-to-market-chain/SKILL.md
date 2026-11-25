---
name: first-to-market-chain
description: >
  First to market, end to end, on your DataSift Open API key. Maps where your county keeps each distress record (foreclosure, probate, tax sale and tax delinquent notices, evictions, divorces, code violations, condemnations, liens), brings each source in by the path that fits it (Tennessee's notice pull, courthouse photos, scanned PDFs, county exports), cleans and screens the rows, uploads them one list per record type, traces and tiers every number, sends deceased owners to the estates chain, and works the rest in text, call and mail lanes. Dry run, then a quote, then one record read back, then the rest; nothing reaches a homeowner without a person's go. Use when someone says "set up first to market for my county", "pull the courthouse data", "import these courthouse photos", "upload my foreclosure notices to DataSift", "skip trace my foreclosure list", or "work my first-to-market leads".
---

# First to market chain

County distress records reach the owner before the nationwide lists do. This chain takes one county from "where
is the data" to records in DataSift on their own lists, traced, tiered and worked in lanes, every week. It is for
an operator who wants first-to-market data running by hand in Claude Code or on the Slack team.

## Who does what

In Claude Code one session runs every step. It stops for a person at every quote and before anything texts, calls
or mails a homeowner.

On the Slack team (the agents on Fly) each step belongs to an employee, and the work hands off on one thread:
Atlas posts what landed, Diggs posts the trace and the death holds, Quinn posts the texts and mail staged for a
person to release.

| Step | On the Slack team |
|---|---|
| 1. Map the sources | Atlas (hand it the source sheet this step makes) |
| 2. Bring the sources in | Atlas: `ftm_pull` and `county_pull` run on their own box every day; `courthouse_import` is planned, so photos and PDFs run by hand |
| 3. Clean and screen | Atlas; Diggs finds a probate's house (`probate_property`, Knox and Blount rolls) |
| 4. Upload | Atlas |
| 5. Trace and tiers | Diggs: `ftm_native_trace`, `ftm_retrace`, `ftm_score`, or `skip_trace` and `phone_score` on request |
| 6. Death check | Diggs: `ftm_death_screen` |
| 7. Outreach | Atlas: `ftm_assign`, `ftm_auction` (the hot auction tag). Quinn: `ftm_texts`, `ftm_attempts`, `ftm_pipeline`, mail once switched on. The Prospector answers every text reply and hands a raised hand to a person |
| 8. Weekly | Quinn: `kpi_report`. Atlas: `sold_cleanup` |

## Before you start

- **The SiftStack folder** (the starter folder or a clone of the repo). Every script runs from it as
  `python src/<file>.py`, and every key goes in `.env` at its root. Install the packages once:
  `pip install -r requirements.txt`.
- **Your DataSift Open API key** as `REISIFT_API_KEY` in `.env`. An account admin makes it in DataSift
  (developers.datasift.ai explains it). Every DataSift read and write in this chain uses it and nothing else.
- **The account structure.** Run the Account Setup chain first (`account-setup-chain`): the lists, the custom
  fields and the text, call and mail lane presets this chain fills.
- **The doctor.** Install `siftstack-core` and ask for "siftstack doctor" to see which keys are set.

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install first-market-county-data@siftstack
/plugin install probate-property-finder@siftstack
/plugin install phone-validator@siftstack
/plugin install sequential-presets@siftstack
/plugin install text-touch-builder@siftstack
/plugin install kpi-engine@siftstack
/plugin install siftstack-core@siftstack
```

| Key or account | What it does here | Cost |
|---|---|---|
| `REISIFT_API_KEY` | every DataSift read and write | comes with DataSift; each new record uses your record allowance |
| DataSift's own skip trace | the first trace of new records | part of your DataSift plan |
| `TRESTLE_API_KEY` | dial tiers on every number | about $0.015 a number |
| `TRACERFY_API_KEY` | a second phone source | about $0.02 a record |
| `ENFORMION_AP_NAME`, `ENFORMION_AP_PASSWORD` | a third phone source | about $0.10 a match at the DataSift affiliate rate, $0.35 at rack rate; a miss is free |
| `ANTHROPIC_API_KEY` | the photo and PDF parse | billed by Anthropic per use; an OpenRouter key alone does not parse photos |
| Tesseract OCR (a free program) | reading photos and scanned PDFs | free |
| `SMARTY_AUTH_ID`, `SMARTY_AUTH_TOKEN` | city and ZIP on rows that lack them | 250 free lookups a month |
| `OPENWEBNINJA_API_KEY` | beds, baths and value on imported rows | 100 lookups a month free |
| `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`, `DROPBOX_ROOT_FOLDER` | the watched photo folder (optional) | free |
| Tennessee notice pull: the tnpublicnotice.com Smart Search subscription (`TNPN_EMAIL`, `TNPN_PASSWORD`), `CAPTCHA_API_KEY` (2Captcha), a proxy (`SIFTSTACK_PROXY_URL`, or `APIFY_TOKEN` with `APIFY_PROXY_GROUPS`) | the daily notice pull | the subscription's and the proxy's own prices; about $0.003 a CAPTCHA solve, and one solve covers a run |
| Knox only: the Register of Deeds subscription (`PAXSUB_USERNAME`, `PAXSUB_PASSWORD`) and Playwright (`playwright install chromium`) | Knox liens | the subscription's own price |
| `SCRAPFLY_KEY` | Knox condemnation agendas from a cloud machine | Scrapfly's own price per fetch |

## Parameters

- `county` (required): the county name, for example `Polk`.
- `state` (required): the two-letter state, for example `FL`.
- `record_types`: default `foreclosure,probate`. Any of foreclosure, probate, tax_sale, tax_delinquent, eviction,
  code_violation and divorce, plus lien and condemnation where a source like Knox's exists.
- `sources`: per record type, the intake path from step 1 (`notice_site`, `photos`, `pdf`, `csv`,
  `knox_reference`). Default: what the source sheet says.
- `photo_folder`, `pdf_path`, `csv_path`: the input for each path. No default.
- `filing_date`: the date stamped on photo and PDF rows. Default today.
- `list_names`: default one list per record type: Foreclosure, Probate, Tax Sale, Tax Delinquent, Eviction, Code
  Violation, Divorce, Liens, Condemned.
- `value_min`, `value_max`: the buy box. Default 1 and 700000.
- `single_family_only`: default true.
- `max_records`: rows per run. Default 10 on the first run.
- `budget_usd`: the most one run may spend on paid lookups. Default 25, the re-skip scripts' own ceiling.
- `dry_run`: default true. Nothing is written to DataSift or bought until a person says go for that step.

## Steps

### 1. Map the county's sources

Run the `first-market-county-data` skill for the county and each record type. For each type find the office, the
site or portal, the access (open, account, paid subscription, in person, records request), how often it changes,
the cost, and the intake path step 2 will use. Verify every URL live.

- Output: `output/first-to-market-chain/sources.csv` with the columns record_type, office, url, access, cadence,
  cost, intake_path, last_checked.
- You SHOULD start with the Priority A types (foreclosure, tax sale, tax delinquent, probate) in one county, then
  add types, then counties.
- You MUST compare each source's newest record with real filing dates. A portal can keep serving an old file that
  looks current.

**Check:** every record type has an intake path, or a written reason it has none (in person only, a records
request pending).
**Then:** step 2 takes the sheet row by row.

### 2. Bring each source in

Every import writes a CSV. Keep them in `output/first-to-market-chain/intake/` by setting the output folder for
the run: in bash put `SIFTSTACK_OUTPUT_DIR=output/first-to-market-chain/intake` in front of the command; in
PowerShell run `$env:SIFTSTACK_OUTPUT_DIR = "output/first-to-market-chain/intake"` first.

**a. Notice website, Tennessee.** `ftm_runner.py` pulls the tnpublicnotice.com saved searches (foreclosure and
probate), parses each notice, enriches it and writes the upload file.

```bash
python src/ftm_runner.py --doctor                            # keys, proxy, state folder, configured searches
python src/main.py list-searches                             # the live saved-search names on the site
python src/ftm_runner.py --stages notices --max-notices 2    # dry: scrapes two, writes ftm_notices_<stamp>.csv, uploads nothing
```

- The saved searches live in `src/config.py` (`SAVED_SEARCHES`) and are set for Knox and Blount. Another Tennessee
  county means adding its exact dropdown names there. A name that does not match scrapes nothing.
- `--doctor` can report a blocking line about DataSift credentials even with `REISIFT_API_KEY` set. The upload
  runs on the key, so that one line is not a blocker.
- A dry run still scrapes (proxy traffic and a CAPTCHA solve). It only skips the upload.
- Exit code 3 means the site refused the machine's IP. Retrying does not help; fix the proxy.

**a, other states.** Save your state's public notice site results as a CSV (the state aggregators are listed in
`first-market-county-data/references/common-offices.md`), rename the headers to the intake columns in
`references/upload-layout.md`, then:

```bash
python src/main.py csv-import --csv-path <file.csv> --csv-county <County> --skip-obituary --include-entities
```

**b. Courthouse terminal photos.** One folder per county and record type, JPG or PNG only.

```bash
python src/main.py photo-import --folder <photos> --photo-county <County> --photo-type eviction --photo-date 2026-11-20 --skip-obituary --include-entities
```

- `--photo-type` is one of foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce.
  Needs Tesseract and `ANTHROPIC_API_KEY`. Writes `<county>_<type>_<timestamp>.csv`.
- It is built for Tennessee terminal screens: the parse writes state TN on every row and only Tennessee rows get a
  missing ZIP filled without Smarty. Outside Tennessee, read the photos in the session instead (Claude reads JPG
  and PNG files) and write the rows straight into the upload layout.
- It keeps the address, the owner (the landlord on an eviction, the petitioner on a divorce) and a probate's
  decedent. It drops the case number, the filing date, the amount owed, a foreclosure's sale date, a divorce's
  respondent and a code violation's deadline: add the ones you need in step 3.
- Probate photos: the import drops a probate whose house it cannot find, and its house lookup is Knox only. Read
  probate photos in the session into step 3's probate list instead.
- Optional Dropbox folder: `python src/main.py dropbox-watch --max-polls 1 --no-delete`, with the folders laid out
  as `<root>/<County>/<type>/photo.jpg`. You MUST NOT start the watcher before the same county and type went
  through `photo-import` cleanly, because it uploads every batch straight to DataSift with no dry run, buys a
  Tracerfy trace for every record when `TRACERFY_API_KEY` is set, runs the import's obituary step whenever an
  Anthropic key is set, and deletes the photos unless `--no-delete` is passed.

**c. Scanned PDFs.** `python src/main.py pdf-import --pdf-path <file.pdf> --pdf-county <County> --pdf-date <date>`
(without an Anthropic key it reads the rows by pattern only). It reads Knox and Blount tax sale lists only: every
row needs a dashed parcel id, every row comes out as a Tennessee tax sale, and the city defaults to Knoxville or
Maryville. Any other PDF: read the pages in the session into the upload layout.

**d. County portal exports.** Rename the headers to the intake columns and run `csv-import` as in path a.

**e. Knox County reference automations (a pattern to copy elsewhere):**

```bash
python src/knox_condemnations.py --scratch output/knox     # the city's condemnation agendas, parsed
python src/knox_liens_daily.py --scratch output/knox       # new liens from the Register of Deeds, matched to parcels
python src/knox_ftm_pull.py --scratch output/knox --out output/first-to-market-chain/intake/knox_county_pull.csv
```

- `knox_ftm_pull.py` writes the upload layout and applies the buy box (single family, $1 to $700,000 by default;
  `--value-min`, `--value-max`).
- After step 4 uploads that file: `python src/knox_liens_daily.py --scratch output/knox --mark-uploaded`. You MUST
  NOT mark liens before the upload succeeds, because a failed upload would leave them recorded as sent and they
  would never go out.

**Check:** each import's log ends with the count it exported. Compare it with what you fed in. A zero is a failure
to explain, never a quiet day.
**Then:** step 3 reads every CSV in `output/first-to-market-chain/intake/`.

### 3. Clean and screen

1. Turn each import into the upload layout: `python src/import_to_upload.py convert <import.csv> --out
   upload_<type>_<date>.csv --out-dir output/first-to-market-chain` (it writes nothing to DataSift). The
   Tennessee pull's file and the Knox pull's file already are in that layout.
2. Probates with no house: run `probate-property-finder` on them (browser research on the county assessor and
   recorder). Only HIGH confidence finds go forward; MEDIUM and LOW go to the held file. Map each find into the
   upload layout: Property Address, City, State and Zip into the four Property columns; Executor Name into Owner
   First Name and Owner Last Name; the executor's address into the Mailing columns; Decedent Name and Personal
   Representative filled; Lists `Probate`. The personal representative is the owner, at their own mailing address.
3. Screen every row: a house number, single family, inside the value band, not sold. Where a row carries Structure
   Type and Estimated Value (filled when `OPENWEBNINJA_API_KEY` is set), hold the ones outside the buy box. A blank
   is not a reason to drop: DataSift fills the property facts after the upload, and the lane presets can screen on
   structure there.
4. One row per address across files: keep the newest notice and put the other notice's facts in the Notes.
5. Hold any row with no Owner First Name and no Owner Last Name (a company the import could not name a person
   for, or a name it could not read). A company owner needs entity research, not a person's skip trace.
6. Outside Tennessee, set Property State and Mailing State to your state on every row.
7. Add what the photo import dropped: Foreclosure Date on foreclosures, the filing date and case number in Notes.
8. Read five split names by eye. County records often write LAST FIRST.

- Output: `output/first-to-market-chain/upload_<type>_<date>.csv` (one per record type) and
  `output/first-to-market-chain/held_<date>.csv` with a reason on every row.

**Check:** rows in equals rows to upload plus rows held, and every held row says why.
**Then:** step 4 uploads each file.

### 4. Upload on the key

```bash
python src/datasift_api_upload.py --csv output/first-to-market-chain/upload_eviction_2026-11-24.csv --limit 2            # dry: prints two payloads, writes nothing
python src/datasift_api_upload.py --csv output/first-to-market-chain/upload_eviction_2026-11-24.csv --limit 1 --commit   # one record
python src/datasift_api_upload.py --csv output/first-to-market-chain/upload_eviction_2026-11-24.csv --start 1 --commit   # the rest
```

- Between the second and third command, read the first record back with `python src/import_to_upload.py
  readback --street "<street as DataSift stores it>" --zip <zip>` and open it in the app. You MUST NOT send the rest before that read matches, because a wrong tag
  format, a missing select option or a wrong list fails quietly on every row.
- Each row lands on the list in its Lists column, with its tags, its custom fields and its note on the message
  board. A field or select option the account lacks is skipped and listed under WARNINGS.
- A row whose address is already a record attaches its list and tags to that record. It also replaces the
  record's owner when the file names someone else, unless the record is tagged Deep Prospected or Heir Identified
  or its owner is marked deceased (a probate notice's representative replaces even those, with a note).
- Errors land in `output/upload_errors.txt`.

**Check:** the dry run's payloads look right; the one record reads back with its list, tags, fields and note; the
final line's created count equals the file's row count minus failures.
**Then:** step 5 traces the records on each list. Probate records stop here and go to `estates-heirs-chain`.

### 5. Trace and tier

1. **DataSift's own skip trace.** In the app: Records, filter to the list, select the records, Send To, Skip
   Trace. Your account may also trace new records on its own. Wait for it to finish before step 5.3.
2. **Tiers.** `phone-validator`'s tier bands, written on the key:

   ```bash
   python src/phone_tiers.py --list Eviction            # dry: how many numbers, what scoring would cost
   python src/phone_tiers.py --list Eviction --commit   # Trestle score, one tier on each phone, a sample read back
   ```

   Dial First 81-100, Dial Second 61-80, Dial Third 41-60, Dial Fourth 21-40, Drop 0-20; a litigator risk is tagged
   DNC instead. Only numbers with no tier are scored, never one marked wrong, dead or do-not-call, and a number is
   bought once (`output/phone_tiers/scores.json`).
3. **Re-skip the owners still without a number:**

   ```bash
   python src/tracerfy_ftm.py --list Eviction                     # dry: who would be traced, the most it can cost
   python src/tracerfy_ftm.py --list Eviction --commit --score    # trace, add the numbers, tier them
   python src/enformion_ftm.py --list Eviction --limit 2 --commit # a two-record test of the third source
   python src/enformion_ftm.py --list Eviction --commit --score
   ```

   Each run refuses to spend over `--max-usd` (default 25), leaves out company, trust and estate owners by name,
   adds only numbers the owner does not already have, and reads the first record back before the rest. You MAY
   skip the Enformion pass when Tracerfy left only a few owners without a number; those few are the deep
   prospecting queue.

You MUST NOT re-skip before DataSift's own trace has landed, because the re-skip picks the owners with no phone and
would buy numbers DataSift gives you on your plan. You MUST NOT re-skip the Probate list here, because a probate's
signer is traced in the estates chain.

**Check:** each dry run's count and most-it-can-cost; after: the tier counts printed by `phone_tiers.py` and the
read-back line.
**Then:** step 6 screens the owners before any text or call.

### 6. Death check

Before anyone texts or calls, look for owners who have died. A deceased owner goes to `estates-heirs-chain`, never
to a call lane.

1. In the session, search each new living-owner record: the owner's full name plus the city plus "obituary". Use
   the county's obituary index where one exists.
2. Also take every record whose owner DataSift marks deceased or puts on its own deceased or pre-probate list, and
   every owner a skip trace flagged deceased.
3. Write `output/first-to-market-chain/death_check_<date>.csv`: address, owner, what was found (the URL, the name,
   the age, the date), and a verdict: clear, or hold.
4. For each hold, in the app: set the status Deep Prospecting and add the tag Verify Deceased. The lane presets
   leave the status Deep Prospecting out, so the record stops getting texts and calls.

You MUST NOT mark an owner dead from a same-name obituary, because common names collide; a death needs the name and
the age or a named relative to match, and a possible match is a hold for research, not a death. A skip trace
deceased flag that reads true counts as a death; one that reads false proves nothing.

**Check:** holds plus clears equals the records screened, and every hold has a URL.
**Then:** holds go to `estates-heirs-chain` step 1; clear records go to step 7.

### 7. Outreach

Nothing in this step reaches a homeowner until a person says go for that batch.

- **The lanes.** Foreclosures and probates carry the tag FTM and land in the foreclosure and estate lane presets
  (Ready to Text, Texted Not Yet Called, Needs Skipped, Skipped No Numbers, Ready to Call, Call Attempt 1 to 4,
  Mail 01 to 06, and weekly letters counting down to a sale). The other record types carry Courthouse Data without
  FTM: give each list its own text, call and mail presets (copy the foreclosure lane and anchor it on the list) or
  work them in your Best Lists lanes. Build presets with `sequential-presets` (its step-by-step walkthrough in the
  app) or from `docs/account-structure.md`, Step 4.
- **Texts.** `text-touch-builder` writes four texts per record into the custom fields Text Touch 1 to 4: export the
  Ready to Text preset, then

  ```bash
  python skills/text-touch-builder/scripts/build_text_touches.py <export.csv> --out output/first-to-market-chain/text_touches_<list>.csv
  ```

  and import that file back in the app (Upload File, Add Data, the existing list, map Text Touch 1 to 4). Or load
  a campaign in your texting platform; before it starts, check the contacts actually arrived there, because an
  integration push can report a count and deliver nothing. On the Slack team the Prospector answers every reply.
- **Calls.** Dial First and Dial Second numbers first, in the call lane order: Ready to Call, then Call Attempt 1
  to 4 on the dialer's own attempt counter.
- **Mail.** The mail lane presets, sent through your mail tool after a person approves the batch.
- **Hot auction list.** Each Monday the session lists the foreclosures whose Foreclosure Date falls in the next 45
  days (from the upload files) in `output/first-to-market-chain/hot_auction_<date>.csv`. Add the tag Auction Soon
  to them in the app; the hot auction preset reads that tag. When a sale date passes, set the status Auction Date
  Passed, which every lane leaves out.

You MUST NOT name the list in any message (foreclosure, probate, tax, lien, eviction, code violation), because the
owner should feel found, not targeted. You MUST NOT contact a number again after a stop request, on any channel,
because a stop is a request about the person, not about one channel.

**Check:** before each batch, the preset's count against the number you meant to send, and a person's go.
**Then:** step 8 reads the results every week.

### 8. Weekly

1. **Calling numbers.** Run the `kpi-engine` skill on the key (from a clone of the repo:
   `python skills/kpi-engine/scripts/pull_kpis.py --days 7 --xlsx`). It reports per caller and account-wide; for
   each list, read the lane presets' counts in the app.
2. **Sold check.** A record is Sold only when the house sold after it became a lead. Compare the last sale date
   with the day the record came in; an earlier sale is the owner's own purchase.
3. **Source check.** Any source with nothing new for 7 days: find out why (a lapsed subscription, a renamed saved
   search, a dead proxy, a moved website). Note each paid subscription's end date.
4. **Sale dates.** For each foreclosure past its sale date, check the county roll first: a new owner means set the
   status Auction Date Passed; the borrower still on the title means it moves to a long-term lane.
5. Write `output/first-to-market-chain/weekly_<date>.md` with the numbers in "What to watch".

**Check:** the week's file exists and every number has last week's beside it.
**Then:** back to step 2 for the new week's records; step 1 again when a source changes.

## Stand it up

**First run, about 10 records.** One county, one record type, one source: ten photos, ten rows of an export, or the
Tennessee pull with `--max-notices 10`.

1. Steps 2 and 3 into one upload file. Read every row once.
2. Step 4: the dry run, one record, the read-back, then the other nine.
3. Step 5: DataSift's trace on the list, then `phone_tiers.py` dry and `--commit`.
4. Step 6 on the ten.
5. Open the lane presets.

**What working looks like:** ten rows in; ten records on the right list (or a held file that says why), each with
its tags, Notice Type, County and note; most with a Dial First or Second number; the Ready to Text or Ready to Call
preset counting them; probates waiting for the estates chain; nothing texted or called yet.

**On a schedule.** Once two runs come out clean:

- The Tennessee notice pull: `python src/ftm_runner.py --stages notices --commit` once a day, early, from your
  computer's scheduler (Task Scheduler on Windows, cron on Mac or Linux) or a Claude Code routine (`/schedule`).
- Photos, PDFs and exports: as they come in, by hand in a session.
- Knox reference pulls: daily, so no condemnation agenda is missed.
- On the Slack team, the agents run their stages on their own clock.

**Weekly, about 15 minutes:** step 8, plus the federal Do Not Call scrub when it is due (at least every 31 days).

## What to watch

1. **Records in against records landed**, per source per run. Zero is a failure until explained.
2. **Reach:** the share of new records with a Dial First or Dial Second number after step 5.
3. **Speed:** days from the filing or publication date to the upload. Daily sources should stay under two.
4. **Hot auction count** against foreclosures with a sale still ahead. Zero while foreclosures arrive means the sale
   date is not landing in Foreclosure Date.
5. **Death holds** sent to the estates chain each week.

## Built in the app

The published API has no route for these, or they are one-time setup:

1. **Custom fields and options.** Settings, Custom Fields. Notice Type (a select) needs an option for every record
   type you upload; County (a select) needs your county; Date Added, Foreclosure Date and Tax Auction Date are date
   fields; Source URL, Personal Representative and Decedent Name hold text. The Account Setup chain makes the
   fields; add your county's option by hand.
2. **Lane presets.** Filter Records, then Filter Presets, one folder per lane, with `sequential-presets` walking
   each filter. Every preset leaves out Sold, Do Not Market and the dead statuses.
3. **DataSift's skip trace.** Records, filter to the list, select, Send To, Skip Trace.
4. **Text Touch 1 to 4.** Four text custom fields, then the re-import of `text-touch-builder`'s file through Upload
   File, Add Data, the existing list.
5. **Statuses and tags.** Deep Prospecting, Auction Date Passed, Exhausted and Return Mail statuses; Auction Soon
   and Verify Deceased tags.
6. **Sequences** (Sequence Builder; sequences are not in the published API): a new lead makes a task for the lead
   manager; a record set to Deep Prospecting gets a card on the Deep Prospecting board.

## Where it works

- **Any county:** the source sheet, county exports and saved notice CSVs through `csv-import`, photos and PDFs read
  in the session, the upload, the trace and tiers, the death check, the lanes.
- **Tennessee:** the notice pull (tnpublicnotice.com; Knox and Blount set up, other Tennessee counties by adding
  their saved searches), and the photo and PDF scripts, whose parse writes TN.
- **Knox:** liens and condemnations, the probate house lookup inside the imports, and the PDF tax sale list (with
  Blount). Knox and Blount rows also get the county tax balance.

## Gotchas

- **Zero is a failure.** A mistyped saved search, a lapsed subscription, a dead proxy and a quiet day all read as
  zero rows. When a paid notice site or recorder subscription ends, the login still works and every search comes
  back empty.
- **Notice sites gate by IP.** Home, office and cloud IPs alike can get "not permitted to view public notices"; the
  Tennessee pull needs a proxy. The site also blocks an IP after roughly 200 notice views, so a 12-month backfill
  runs one saved search and one month per process (`ftm_runner.py` with `--backfill-months 1 --backfill-offset <M>`),
  and a run of blocked jobs waits for the IP pool to cool.
- **Probate notices hide the people.** The notice names the court in prose before it names the personal
  representative under a heading, so a careless read stores the court as the owner. Its only street addresses
  belong to the court, the lawyer or the representative, never the house. Trustee sales leak into probate searches.
  Narrow columns break names across lines with a hyphen. A phone number in the notice is the law firm's. The house
  always comes from a separate lookup.
- **Foreclosure filtering.** Keep only real trustee sale language and drop the exclusions listed in
  `first-market-county-data`. The owner is the borrower who executed the deed of trust; cut the name before
  "payable to", or the lender rides along in the owner name.
- **The sale date must land.** It goes in Foreclosure Date. Without it the hot auction list reads zero while
  foreclosures pile up. The photo import drops it, so add it by hand.
- **Filters that delete a whole type.** The vacant land filter drops rows with no house number, which would drop
  every probate; probate and divorce are exempt on purpose. A row with no ZIP is dropped at validation; outside
  Tennessee only Smarty fills one.
- **Terminal photos.** Screen moire is the OCR killer; the import's bilateral filter, Otsu threshold and
  single-column mode handle it. Let the photo's own orientation rotate it. Blurry photos are rejected and named in
  the log: retake them. iPhones save HEIC by default, which the import skips; set the camera to Most Compatible.
- **Upload contract.** Tags go as separate tags, a select value needs its option to exist, notes post to the
  message board, and a company owner goes in the last name with the first name blank. Always one record first.
- **The shared limit.** DataSift allows 120 requests a minute per account, shared by every tool on the key. The
  scripts pace themselves; do not run two big jobs at once.
- **Date Added means two things.** Notice uploads write the day you added the record; the Knox county pull writes
  the county filing date. The notice's own date rides in the month tag.
- **After the sale.** More than half of foreclosure borrowers in one county's year of notices still owned the
  house after the sale date. Check the county roll before closing a record out; one still owned 45 days later
  belongs in a long-term lane, not the trash.
- **Sold means sold after the lead.** Never auto-tag recently sold houses from a fixed date floor: one account's
  "recently" grew to 46 months and marked fresh leads Sold the night they arrived. DataSift's last sale date lags a
  closing by about two months.
- **Liens are filed against a person.** No parcel comes with them, so a name-to-parcel match lands about 40% of the
  time. Count active liens only (recorded minus released); in one pull 8% of lien debtors had every lien released.
  Keep one row per property.
- **Condemnation agendas are overwritten.** A cycle not captured while it is posted is gone. A moved city website
  can leave the old link serving a frozen file, so check the newest date inside it.
- **A tax "amount due" is not a delinquency.** It includes the current year's bill, which is not late yet. Count
  only bills marked delinquent and unpaid.
