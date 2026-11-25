---
name: best-lists-chain
description: Finds the lists that produce deals in a county right now and works them inside a budget, on the DataSift Open API key. Reads Market Finder by ZIP and neighborhood, writes a buy box, ranks the county's lists by live Doors per Deal, builds a ranked pool of owners (AI scores where the account has the county's AI add-on, the Doors per Deal ladder where it does not), pulls it after DataSift's own quote, adds skip trace and dial tiers, puts it in text, call and mail lanes, then sends it to Smarter Contact, a calling agency file, cold email or mail, with a weekly check. Use when someone says "pull the best lists", "what lists should I market in my county", "find my best lists", "build my top 1,000", "pull an agency list", "run doors per deal", "set up my marketing for my county", "what should I pull this month", or "send my list to Smarter Contact".
---

# Best lists chain

Finds the lists that turn into deals in your county right now, pulls the best owners inside your budget, gets them working phone numbers, and works them in lanes out to texts, a calling agency, email or mail. It is the bootcamp's list-building SOP (step 9): market analysis, a SiftMap search, the pull, skip trace, a filter preset, then send to an integration. It is for an operator marketing one county a month, once the account setup chain has built the lanes step 7 uses.

## Who does what

**In Claude Code:** one session runs every step in order. It stops at each quote and before each send for your yes.

**On the Slack team:** Atlas picks and pulls, Diggs traces and tiers, Quinn runs the campaigns, and the Prospector answers sellers.

- **Atlas:** `market_report` (step 1), the nightly `dpd_live` stage (step 3), and `list_pull` (step 5), which quotes first and pulls only the count you approve. In Knox and Blount counties, Atlas also runs the monthly Top 1,000 ranking (`top1000_refresh`, step 4).
- **Diggs:** `skip_trace` then `phone_score` (step 6). Both start on their own after a `list_pull`, 25 records an ask. The lanes' own trace and score stages handle the rest.
- **Quinn:** the agency file (`agency_export`), the cold email stages, `qa_batch` on every staged batch before a person releases it, and `kpi_report` every Monday at 08:00 (step 9).
- **The Prospector:** answers the replies to your texts, writes the result to DataSift, and hands a raised hand to a person.
- **Handoffs** ride one Slack thread: Atlas posts the quote, you approve the count, Atlas pulls and hands the list to Diggs, Diggs hands the traced list to Quinn.
- Atlas pulls a pool it is given but cannot build one today. Build the pool (step 4) in Claude Code, then give Atlas the ids: pasted for a small pull, or the pool file copied onto the box for a big one.

## Before you start

| What | Used for | Cost |
| --- | --- | --- |
| Your DataSift Open API key, as `REISIFT_API_KEY` in `.env` | every step | included with DataSift; Market Finder, Doors per Deal and the SiftMap searches spend nothing |
| Record allowance | step 5, new records only | set by your plan; DataSift's own quote shows the count and what is left before anything is pulled |
| DataSift skip trace | step 6 | on DataSift's unlimited plan, new records are traced on their own; on a metered plan, step 6's trace phase refuses because its estimate carries a cost |
| The county's off-market AI add-on (optional) | step 4, ranking by AI score | a per-county purchase in DataSift |
| Trestle, `TRESTLE_API_KEY` | step 6, dial tiers | about $0.015 a number |
| Tracerfy, `TRACERFY_API_KEY` | step 6, a second skip trace source | about $0.02 a record |
| Enformion (optional; key names in `docs/phone-reskip.md`) | step 6, a third source | about $0.10 to $0.35 a match, misses free |
| Smarter Contact, connected to DataSift | step 8, texts | your Smarter Contact plan |
| A calling agency | step 8, the call file | the agency's fee |
| Instantly and ZeroBounce, `INSTANTLY_API_KEY` and `ZEROBOUNCE_API_KEY` | step 8, cold email | your plans with each |
| DataSift direct mail | step 8, mail | about $3 a record for the first four touches on a bulk list (DataSift Business tier, November 2026; check your own price table) |

Install the component skills once:

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install sift-market-research@siftstack
/plugin install phone-validator@siftstack
/plugin install sequential-presets@siftstack
/plugin install text-touch-builder@siftstack
/plugin install kpi-engine@siftstack
```

The scripts run from a SiftStack folder: the bootcamp starter folder, or a clone.

```
git clone https://github.com/DataSift-Ty-Personal/SiftStack
cd SiftStack
pip install -r requirements.txt
```

Put the line `REISIFT_API_KEY=<your key>` (and any vendor keys you use) in a `.env` file in that folder and run every command from there. Keys go in the file, never into the chat.

## Parameters

| Parameter | Meaning | Default |
| --- | --- | --- |
| `county` | the county's name, without the word County (`Knox`) | none, required |
| `state` | the state's two letters (`TN`); the AI pools take both as `"Knox, TN"` | none, required |
| `fips` | the county's 5-digit FIPS code, from `data/us_counties.csv` | looked up from `county` and `state` |
| `value_band` | the buy box's value band | `1-700000` |
| `since` | leave out houses that sold on or after this day | `2023-01-01` |
| `exclude_zips` | ZIP codes to leave out (from step 1) | none |
| `year_built` | year built range, set on the lane presets | none |
| `suppress_neighborhoods` | neighborhoods to leave out, set on the lane presets | none |
| `pool` | `investor:<count>` or `realtor:<count>`, one or two | `investor:1000` |
| `rank` | `ai` (the AI score) or `ladder` (the Doors per Deal ladder) | `ai`; without the AI add-on the run falls back to the ladder on its own |
| `list_name` | the DataSift list the pull goes on | `<County> Best Lists <YYYY-MM>` |
| `tag` | the tag the pull adds; `Top 1000` puts the list in the Top 1,000 lanes | the list name |
| `max_records` | the most new records the pull may add | the quote's new count, after your yes |
| `budget_usd` | the most step 6 may spend on scoring and re-skips | 25 |
| `channels` | any of texts, agency, email, mail | texts |
| `fred_dom` | today's national median days on market from FRED | read the day of the run |
| `dry_run` | quote and plan only; nothing is pulled, bought or sent | true |

The chain's own files go in `output/best-lists-chain/<county>_<st>/` (for example `output/best-lists-chain/knox_tn/`). Each script's own output folder is named in its step.

## Steps

Every command that writes or spends carries `--commit` (or `--yes` for the email launch). With `dry_run` true, the chain runs only the commands without it and shows you what the rest would do.

### 1. Read the market (sift-market-research)

Pull every ZIP row, every neighborhood row and the county summary from Market Finder, on the key, then build the 7-sheet workbook.

```
python src/extract_market_finder.py --state TN --county Knox --source api --output-dir output/best-lists-chain/knox_tn
```

The sift-market-research skill's own `skills/sift-market-research/scripts/extract_market_finder.py` makes the same pull without `--source` once its key-only release is in your copy (its `--help` then says it runs on your Open API key).

Read the national median days on market from FRED (series MEDDAYONMARUS) the same day, then:

```
python skills/sift-market-research/scripts/generate_market_excel.py --input output/best-lists-chain/knox_tn/market_finder_TN_Knox_<stamp>.json --fred-dom <fred_dom> --output output/best-lists-chain/knox_tn/Knox_County_TN_Market_Research.xlsx
```

- **In:** `county`, `state`, `fred_dom`. **Out:** the Market Finder JSON and the workbook in the chain folder.
- The pull proves its own filters: a ZIP row outside the county, or fewer pages than the server reports, makes it refuse and write nothing.
- You MUST NOT switch to a browser pull when the API pull is refused, because this chain runs on the key alone. Report the refusal instead.
- **Check:** the run prints the ZIP and neighborhood counts, and the summary's monthly investor transactions roughly match the ZIP rows' six-month totals divided by six.
- **Then:** the Tier 1 ZIPs (10 or more monthly investor transactions, 3 to 6 months of supply, days on market under the FRED baseline) and the ZIPs and neighborhoods to drop go to the buy box.

### 2. Write the buy box

Claude writes `output/best-lists-chain/<county>_<st>/buy_box.json` from step 1 and your answers: single family, the value band, no sale since a date, the ZIPs to drop, a year built range, and the neighborhoods to suppress.

- The AI pools (step 4) apply single family, the value band, no sale since, the ZIPs, and no company owner. A SiftMap search row carries no year built and no neighborhood, so those two go on the lane presets in step 7.
- **In:** the step 1 workbook. **Out:** `buy_box.json`.
- **Check:** you read the buy box back and say yes. Step 4's `summary.json` then says how many houses it holds.
- **Then:** steps 3 and 4 read it.

### 3. Find the lists that work now (live Doors per Deal)

Doors per deal is how many houses on a list you market for each one an investor buys. Fewer is better. This keeps the County List Playbook's ladder current from your own key.

```
python src/dpd_live.py --fips 47093 --table
python src/dpd_live.py --fips 47093 --phase all
```

Run the second form on the first run of each month: it also saves every list's members, so later sales can be matched to the lists that held them before they sold.

- Doors are live counts. Market speed is the county's live investor purchases a month. Deals per list start on the published ladder, adjusted for today's speed, and move to the list's own measured rate as it earns sales. Every number says which it is.
- It cannot add a record or spend anything.
- **In:** `fips`. **Out:** `output/dpd_live/<fips>/ladder.json` and `ladder.csv`; Claude copies the best lists, best first, to `best_lists.md` in the chain folder.
- **Check:** each list's doors against the published number (a list that moved more than 25% is named). "The published ladder has no county" means no ladder for your county: go to step 4 with `--rank ai` if your account holds the county's AI add-on; with neither, build the list by hand in SiftMap (see Built in the app).
- **Then:** the best lists decide step 4's ranking: the AI score, or the ladder's lists in work order.

### 4. Rank the owners (AI pools, or the Top 1,000)

```
python src/ai_pools.py --county "Knox, TN" --pool investor:1000 --name knox_tn_2026-12 --value 100000-700000 --since 2023-01-01 --exclude-zips <zips from step 1>
```

- `--rank ladder` walks the county's Doors per Deal ladder in work order, then fills by the investor score. Use it when step 3 shows the ladder's lists beating the AI score.
- `--pool investor:1000 --pool realtor:1000` builds two pools that never share a house.
- Next month, add `--exclude output/ai_pools/<last month's name>/investor.json` so no house repeats.
- The AI scores are a per-county add-on. When the account cannot see them, the run says so at the start: an investor pool falls back to the ladder's lists, and a realtor pool cannot be ranked.
- **The Top 1,000** is the Slack team's monthly ranking of the houses already in the account, on deal odds learned from Knox and Blount sales (`top1000_refresh`, Atlas). It tags the top 1,000 owners `Top 1000`. Outside those two counties, use the AI pools.
- **In:** `county`, `state`, `pool`, `rank`, the buy box. **Out:** `output/ai_pools/<name>/investor.json` (houses in rank order, with the score or list that picked each) and `summary.json`.
- **Check:** `summary.json` shows how many houses the buy box held, how many each cut dropped, and how many searches ran; the top rows' score or list matches step 3. Nothing is added to your account.
- **Then:** the pool file goes to the pull.

### 5. Pull, quoted first (pull_by_ids)

```
python src/pull_by_ids.py --ids output/ai_pools/knox_tn_2026-12/investor.json --list "Knox Best Lists 2026-12"
python src/pull_by_ids.py --ids output/ai_pools/knox_tn_2026-12/investor.json --list "Knox Best Lists 2026-12" --commit --probe-only
python src/pull_by_ids.py --ids output/ai_pools/knox_tn_2026-12/investor.json --list "Knox Best Lists 2026-12" --commit --approve <max_records>
```

`max_records` is the new count from the quote, once the person says yes to it.

- The first command asks DataSift's own quote: how many houses are new to your account, how much allowance they use, how much is left. It writes `quote.json` and stops.
- `--probe-only` lands one new house, reads it back (same id, on the list) and stops.
- The third checks the quote again and refuses past your allowance or past `--approve`, then sends the rest in chunks. Houses already in your account just get the list and the tag.
- Add `--tag "Top 1000"` to work the list in-house through the Top 1,000 lanes (step 7). Left off, the tag is the list name.
- **In:** the pool file, `list_name`, `tag`, `max_records`. **Out:** the records on the list; the run's state in `output/pull_by_ids/`.
- You MUST NOT run `--commit` without the person's yes on the quoted new count, because a pull spends record allowance and cannot be taken back.
- **Check:** the quote, the probe's read back, then `--phase qa` (step 6) for how many records the list holds.
- **Then:** DataSift's own skip trace reaches the new records; step 6 picks up an hour later.

### 6. Trace and tier the numbers (phone-validator)

Wait about an hour after the pull: the add's own skip trace lags, and an untraced record reads like one with no number.

```
python src/pull_by_ids.py --ids <pool file> --list "<list_name>" --phase qa
python src/pull_by_ids.py --ids <pool file> --list "<list_name>" --phase skiptrace
python src/pull_by_ids.py --ids <pool file> --list "<list_name>" --phase skiptrace --commit
python src/phone_tiers.py --list "<list_name>"
python src/phone_tiers.py --list "<list_name>" --commit
python src/tracerfy_ftm.py --list "<list_name>"
python src/tracerfy_ftm.py --list "<list_name>" --commit --score --max-usd <budget_usd>
python src/enformion_ftm.py --list "<list_name>" --limit 2 --commit --score
```

In order: `qa` counts the list, the records with a phone and the records the trace has not reached. `skiptrace` traces only those, after DataSift's free estimate confirms the scope (dry, then `--commit`; skip it when `qa` shows none, and on a metered plan it refuses because the estimate carries a cost). `phone_tiers` scores every untiered number with Trestle and writes one tier per phone (dry shows the count and cost). `tracerfy_ftm` re-skips the owners who still have no phone, then scores the new numbers. `enformion_ftm` tests a third source on two records before you run more.

- Tiers: Dial First 81 to 100, Dial Second 61 to 80, Dial Third 41 to 60, Dial Fourth 21 to 40, Drop 0 to 20. A litigator risk is tagged DNC instead of a tier.
- A number is scored once (kept in `output/phone_tiers/scores.json`). A number a caller marked wrong, dead or do not call is never touched.
- The phone-validator skill is the scoring method and the tiers, and its own script scores a CSV export. Its DataSift upload step drives a browser, so on the key the tiers go on through `phone_tiers.py`.
- You MUST NOT run a `--commit` whose dry run costs more than `budget_usd`, because Trestle, Tracerfy and Enformion bill every number and match they return.
- **In:** the list. **Out:** numbers and tiers on the records.
- **Check:** each dry run's count and cost against `budget_usd`; the sample `phone_tiers` reads back; the share of owners with a Dial First or Second number.
- **Then:** the list goes into lanes.

### 7. Put the list in lanes (sequential-presets)

- **In-house** (you text and call it): if the pull carried `--tag "Top 1000"`, the Top 1,000 lanes (folders 08 to 10 from the account setup chain) already show it. Or give the list its own lane family with the sequential-presets skill (account setup chain, step 3).
- **Agency or email:** leave the `Top 1000` tag off. The agency file (step 8) is built from the pool and the list, not from a lane.
- Add the buy box's year built range and neighborhood cuts to the list's lane presets now. A record with no year built drops out of a year filter, so check the count before and after.
- Lanes move on DataSift's own counters (texts sent, the dialer's call attempts, mail attempts). Check one record after its first text and first dial: a tool that does not raise the counter leaves the record in its lane.
- **In:** the traced list. **Out:** the list's records in Ready to Text and Ready to Call.
- **Check:** the Ready to Text and Ready to Call counts for the list, and one record you open sits in exactly one lane preset.
- **Then:** step 8 sends it.

### 8. Send to an integration

Every channel reaches a homeowner. Each one's first send waits for a person's yes, goes to about 10 owners, and is checked before the rest.

**Texts (Smarter Contact).** In the app, open the list's Ready to Text preset, select the records, then Send To, Smarter Contact. In Smarter Contact, check the group holds the contacts before you build anything (a push has come back empty before). Write the templates to the text-touch-builder recipe: one question, under 160 characters, no links, never the list's name, signed with the caller's first name. `python skills/text-touch-builder/scripts/build_text_touches.py --check-pools` audits that skill's own variants. Replies go to a person in the Smarter Contact inbox, or to the Prospector where your team runs one.

**Calling agency.**

```
python src/agency_call_list.py --pool output/ai_pools/knox_tn_2026-12/investor.json --list "Knox Best Lists 2026-12" --phase all --about "Knox single family, investor AI order, no sale since 2023"
```

It reads every record once and writes `output/agency_call_lists/<list>_Call_List_<date>.csv` and `.xlsx` (one row per owner, mobiles first), a `_no_phones.csv`, and a Held out sheet with the reason on each row. Read the Held out sheet, then a person sends the file. `--not-on "<other list>"` holds owners already on another list's call file.

**Cold email (Instantly).** The copy in `src/cold_email/emailcopy.py` is written in one operator's voice and names them: put in your own first name and voice, then run `python src/cold_email/emailcopy.py`, which checks every combination. `python src/cold_email/setup_accounts.py` (dry, then `--commit`) puts every sending inbox on the sending settings, with your sender name from `COLD_EMAIL_SENDER_FIRST` and `COLD_EMAIL_SENDER_LAST` in `.env`. Then:

```
python src/cold_email/build_list.py --preset "<a preset that holds the list>" --target 50 --states TN
python src/cold_email/build_list.py --preset "<a preset that holds the list>" --target 50 --states TN --verify
python src/cold_email/campaign.py create --name "<campaign>"
python src/cold_email/campaign.py load --name "<campaign>" --leads <the leads.json build_list printed>
python src/cold_email/campaign.py preview --name "<campaign>" --leads <the same file>
python src/cold_email/campaign.py launch --name "<campaign>" --yes
```

The first build reads only; `--verify` spends ZeroBounce credits and keeps "valid" addresses only. `--states` is where your copy's "local buyer" line is true. The reply watcher and the rest are in `src/cold_email/README.md`.

**Mail.** Build the piece in DataSift Mail Design and download its proof, then send the campaign from the list's Mail 01 preset. `docs/direct-mail-swipe-file.md` has the copy formulas, the per-piece prices and the merge fields Mail Design can and cannot fill.

- You MUST NOT put an owner in both the in-house lanes and the agency file, because the owner would get two sets of calls from two teams.
- You MUST NOT launch a campaign, send a file or release a batch without a person's yes, because each one reaches homeowners and cannot be called back.
- **In:** the lanes and the pool. **Out:** campaigns and files; Claude logs what went where and when in `sends.md` in the chain folder.
- **Check:** the first 10 went to exactly the owners you approved: read the Smarter Contact group, the agency file's row count, the Instantly preview, or the mail proof.
- **Then:** step 9 measures it.

### 9. Check it weekly

- **KPIs:** `python skills/kpi-engine/scripts/pull_kpis.py --days 7 --xlsx` (on the key once the kpi-engine key-only release is in your copy; its `--help` then says so). It reports per caller and for the whole account.
- **Leads by list:** kpi-engine does not split by list, so filter the list's records by the lead statuses in the app and count them.
- **Doors per Deal:** `python src/dpd_live.py --fips <fips> --table`, and `--phase all` on the first run of the month.
- **The sold check:** in the app, filter the list's records by last sale date on or after the day you pulled it. Those are real sales: tag them `recently sold`, and the Recently Sold to Sold Status sequence sets them Sold. A sale dated before the pull is the owner's own purchase; never mark it sold.
- **In:** the list and the week. **Out:** `weekly_<date>.md` in the chain folder.
- **Check:** this week's numbers against last week's.
- **Then:** next month, step 3 with `--phase all` and step 4 with `--exclude` last month's pool.

**Optional branches.** Vacant land (buildable lots) runs in Union County, OH today: `python src/vacant_land.py --phase probe` first, then `--phase all`, or Atlas's `buildable_lots` on the Slack team. Active MLS listings go to the Deal Desk & Dispo chain (mls-offer-engine).

## Stand it up

**First run, about 10 records.**

1. Steps 1 to 4 as written, with `--pool investor:10`. They spend nothing.
2. Step 5: the quote, then `--commit --probe-only`, then `--commit --approve` the quoted new count.
3. Step 6 an hour later: `--phase qa`, then `phone_tiers.py` dry and `--commit` (10 owners cost well under $1 at $0.015 a number).
4. Step 7: the 10 records sit in Ready to Text or Ready to Call.
5. Step 8: one channel, those 10 owners, with your yes.

Working looks like this: the quote matched what landed, 10 of 10 are on the list, most carry a Dial First or Second number, each sits in one lane, and the send reached exactly those 10. Then run the full pool the same way.

**On a schedule.**

- Monthly (the 1st): `dpd_live.py --phase all`, `ai_pools.py` with `--exclude` last month's pool, and the pull's dry run and quote, which then waits for a person.
- Weekly (Monday): the three checks in step 9.
- In Claude Code: a routine (`/schedule`) runs the monthly and weekly commands and stops at the quote. The key stays in the routine's environment, never in its prompt.
- On the Slack team (the Fly agents): set your county in the `dpd_live` stage of `team/autopilot.yaml` (it runs nightly at 00:05), Quinn's `kpi_report` runs Mondays at 08:00, and you ask Atlas for the month's pull.

Check weekly: the doors per deal on the lists you work, the allowance left after the month's pull, the leads the list produced, and the real sales after the pull date.

## What to watch

- **Doors per deal on the lists you work** (step 3), and the county's market speed. A list that gets more doors per deal month over month is a list to drop.
- **New records the quote counts against allowance left.** The pull should never come close to the allowance.
- **Owners with a Dial First or Second number** after step 6, and how many still have no phone after the re-skips.
- **Leads:** hand raises per 100 owners texted and leads per 1,000 dials (kpi-engine), next to the list's own lead count from the app.
- **Real sales after the pull date** against leads: a list that keeps selling to other investors while you work it is reaching the right houses too late.

## Built in the app

- **Smarter Contact and DataSift:** connect the integration. In Smarter Contact, under Workflows, Integrations, DataSift, map the Lead label to New Lead, Not interested to Not Interested, and Wrong Number to the phone status Wrong, so a reply lands on the record.
- **The send:** from the list's Ready to Text preset, select the records, then Send To, Smarter Contact. Build the campaign in Smarter Contact.
- **Direct mail:** build the piece in Mail Design, check it with Download proof, then create the campaign from the Mail 01 preset (DataSift Help: "Creating and sending direct mail campaigns").
- **SiftMap by hand,** when no script fits (no ladder and no AI add-on in your county): build the best list's filter in SiftMap with the buy box, then Add Records to Account, which shows the allowance before it adds. Leave auto-add off.
- **The AI add-on:** bought per county in your DataSift account.
- **Instantly:** the sending inboxes and their profile photos (Instantly support sets the photos).
- **The calling agency:** send the file, and ask for results back with one row per number dialed, so a "remove me" reaches the right record.

## Where it works

- **Any US county:** Market Finder (any county it lists), the AI pools, the pull, skip trace and tiers, the lanes, and the agency file.
- **Doors per Deal** (step 3 and `--rank ladder`): only where the County List Playbook publishes a ladder for the county.
- **Ranking by AI score:** only where your account holds that county's off-market AI add-on.
- **The Top 1,000 on deal odds:** Knox and Blount counties, on the Slack team. The model was trained on their sales.
- **Cold email:** `--states` sets where the copy's "local buyer" line is true (TN by default), and the copy needs your own name first.
- **Buildable lots:** Union County, OH only today.
- **The sold check by script** (`src/sold_guard.py`) is set up for Knox and Blount; everywhere else, use the filter in step 9.

## Gotchas

- **Market Finder from the SiftStack folder needs `--source api`.** Without it, the script tries a browser.
- **SiftMap ignores a filter it does not know** and returns the unfiltered count. The scripts prove each filter by count; a hand-edited filter key proves nothing.
- **The 10,000-row wall.** SiftMap refuses a page past 10,000 rows (the AI pools split by value to stay under it), and `phone_tiers.py` and the re-skips read only the first 10,000 records of a list. Keep a list under 10,000 records, or split it.
- **A pull's state follows the pool file, not the list.** Running the same pool file again with a different list name sends nothing. A new list needs its own pool file.
- **A scoped skip trace once ignored its scope and traced a whole account.** Only a list scope that DataSift's free estimate confirms is safe, which is what `--phase skiptrace` does.
- **A flag describes whoever owns the house now.** After a sale, the old owner's tax delinquent flag clears, so "which listed houses sold" cannot be asked after the fact. Deals per list are measured forward from the monthly member save.
- **Deeds reach the data four to eight weeks after a closing,** and DataSift's last sale date lags about two months. This week's sold check misses last month's closings.
- **SiftMap search rows stopped carrying the distress flags and AI scores in November 2026.** Filters still work, so counts by filter are right, but anything that reads a flag off a row reads none.
- **Trusts and owners with no name only show after the pull.** The agency file holds them out with the reason.
- **Smarter Contact sends a contact at most 3 texts in 7 days, then waits 14 days.** A fourth day in a row never sends. A template cannot fall back when the first name is empty, so give no-name records their own group.
- **Owner emails are a loose list:** about 5 an owner, many belonging to other people. Send one address whose name part carries the owner's name, checked valid. Instantly's schedule takes America/Detroit for Eastern time.
- **The agency file has no registry scrub and no Trestle scoring, on purpose.** The agency scrubs before it dials.
- **`--since 2023-01-01` leaves out owners who bought recently.** A house with no sale on record stays in.
- **Never mark a sale that happened before the record became a lead.** That sale is the owner's own purchase, and calling it sold kills a live lead.
