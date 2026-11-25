---
name: deal-desk-dispo-chain
description: >
  Take one hot lead to a signed contract and an assigned buyer. Comps inside a drawn boundary (SiftMap deeds on your DataSift Open API key, or the Zillow API), one rehab number from the walkthrough at the house's own layout, a one-tab wholesale offer (70% of ARV minus repairs and your fee), contractor bids that replace the placeholders, cash and creative offers on every active listing as a second source, then the buyers who buy houses like this one with a price guess each, their decision makers and checked numbers, a deal blast a person releases, and a buyer book for the next deal. Paid steps are quoted first, and nothing texts anyone without a person's go. Use when someone says "what should I offer on this lead", "run comps and rehab on this house", "we walked it, now what", "get contractor bids", "make offers on the listings in my county", "it's under contract, find me buyers", "blast this deal to my buyers", or "set up my deal desk".
---

# Deal desk and dispo

This chain takes a lead that raised a hand and walks it to a signed contract and an assigned buyer.
The result is one offer number you can say out loud, a rehab number the walk backs up, a short list
of buyers whose own deeds say they buy houses like this one (with the numbers that reach them), and
a blast that a person releases. It is for the operator who makes offers and the person who sells the
contract.

## Who does what

In Claude Code, one session runs every step, and you approve each spend and each send.

On the Slack team, Roma owns the chain and the work stays on one thread:

| Step | Agent | How it starts |
| --- | --- | --- |
| 1. Take the lead | the Prospector hands the raised hand to a person; Roma books the walk (`book_appointment`) | a hand raise in the text leads channel, a call-in, a web form |
| 2. Comps | Roma (`quick_screen`) | a DataSift sequence posts the record to the `quick-screen` webhook, or an ask with a pasted boundary |
| 3. Rehab | Roma (`walkthrough_video`, or `underwrite` with a walk) | a walkthrough video, photos or notes posted in the thread |
| 4. Offer | Roma (`offer_sheet`) | "offer sheet for task N" |
| 5. Bids | Diggs (`vendor_directory` is planned) | ask in the thread; the two vendor skills run in the conversation |
| 6. Listings | Roma (`mls_offers`) | "MLS offers for <County>, <ST>" |
| 7. Buyers | Roma (`deal_buyers`, `buyer_search`) | a DataSift sequence posts the record to the `under-contract` webhook, or an ask |
| 8. Blast | a person, on the texting side (`dispo_blast` is planned) | a person stages it and releases it |
| 9. Buyer book | a person, from Roma's workbooks | after each blast |

Roma never texts a seller or a buyer, never sends an address, and never negotiates. The release of a
blast, the address, the photos and any lockbox code stay with a person.

## Before you start

| Key or account | Used for | Cost |
| --- | --- | --- |
| `REISIFT_API_KEY`, your DataSift Open API key (an account admin creates it; it never expires) | the lead, SiftMap search and deeds, the listing screen, the buyer search | free to read; adding shortlisted listings uses record allowance, quoted first |
| `OPENWEBNINJA_API_KEY` (optional) | Zillow comps (comp-package, `post_walkthrough.py`), the "still for sale" check on listings | 100 free lookups a month, then metered; one ZIP's sold pull is about 50 to 80 calls |
| `OPENROUTER_API_KEY` (optional) | the walkthrough video's narration | about $0.002 an audio minute |
| `OPENROUTER_API_KEY` with `SERPER_API_KEY` or `SERPAPI_KEY` (optional) | the buyer research engine (or do that research yourself in Claude Code, step 7) | about $0.08 a buyer: searches about $0.06, the model about $0.02 |
| Enformion BusinessV2 (optional) | the person behind a company buyer | $0.10 a match, a miss is free |
| SmartSkip (optional) | a decision maker's numbers at a verified home street | $0.15 a hit |
| Trestle (optional, `TRESTLE_API_KEY`) | Real Contact: is this number the decision maker's | about $0.03 a name and number pair |
| A texting tool with numbers you own | the blast | your provider's rates |

SmartSkip and Enformion each take the account details they issue you, saved in `.env`; the
`deep-prospecting-v5` skill's setup names the SmartSkip lines. Measured on one deal with about 100
buyers to call, the whole paid contact run cost about $22.

Install the component skills:

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install siftstack-core@siftstack
/plugin install real-estate-comping@siftstack
/plugin install comp-package@siftstack
/plugin install rehab-estimator@siftstack
/plugin install deal-analyzer@siftstack
/plugin install vendor-directory-builder@siftstack
/plugin install contractor-call-sheet@siftstack
/plugin install mls-offer-engine@siftstack
/plugin install buyer-prospector@siftstack
/plugin install dispo-deal-blast@siftstack
```

Then ask for the SiftStack doctor (from `siftstack-core`): it says which keys are set and what each
missing one costs.

The `python src/...` commands run from your SiftStack folder (the starter folder or a clone of
github.com/DataSift-Ty-Personal/SiftStack) after `pip install -r requirements.txt`, with
`REISIFT_API_KEY` in that folder's `.env`. A component skill's own script is written as
`<skill>/scripts/<file>`: it sits in that skill's installed folder, or under `skills/` in a clone.
You also need `ffmpeg` (video frames) and `curl` (the listing screen's public mortgage data) on the
PATH.

## Parameters

- **address** (required): the house's street line with city, state and ZIP, like "123 Example St, <City>, <ST> <ZIP>".
- **record_uuid** (optional, default: found by the street line): the lead's DataSift record.
- **slug** (optional, default: the street line in lowercase with hyphens): the deal's folder under `output/deal-desk-dispo-chain/`.
- **county** (optional, default: the house's county): "County, ST" for the listing screen and the county buyer list.
- **boundary** (optional, default: none, which means a 1 mile radius on the Slack team and the whole ZIP on the command line): the drawn comp area, as `polygon.json`.
- **beds** (optional, default: the record): the county card's bedroom count.
- **baths** (optional, default: the record): the county card's bath count.
- **sqft** (optional, default: the record): the county card's living area.
- **year_built** (optional, default: the record): the county card's year.
- **months** (optional, default: 24): the comp pool; the engine prefers 12 months and widens only when it must.
- **rule_pct** (optional, default: 0.70): the share of ARV a flipper pays before repairs.
- **fee** (optional, default: 15000): your assignment fee.
- **ask** (required from step 7): the price you offer buyers.
- **contract_price** (optional): what you pay the seller. It never enters a deal sheet or a message.
- **buyer_radius_mi** (optional, default: 5, at most 10): how far around the house to look for buyers.
- **deed_reads** (optional, default: 150): the most SiftMap deeds one buyer search reads, nearest first.
- **mls_shortlist** (optional, default: 60): listings read in full on the listing screen.
- **disclose_address** (optional, default: false): name the exact address in the blast. A per-deal choice.
- **budget_usd** (optional, default: 25): the most the paid steps may spend on this deal.
- **dry_run** (optional, default: true): plan and quote only. Nothing is added, bought or staged.

## Steps

### 1. Take the lead

When a seller raises a hand (the Prospector's handoff, a call-in, a web form), read the record before
anything else: the owner, the status, the message board and what the seller already said. Then book
the walkthrough.

- Open the record at `https://app.reisift.io/records/properties/<record_uuid>`. The record can show
  that the owner died, that the house is vacant, or that the seller already named the defect, and
  each of those changes the walk.
- Book the walkthrough as an appointment task on the record (Slack: "book a walkthrough at 123
  Example St, Thursday 2pm" to Roma's `book_appointment`).
- Input: the hand raise. Output: `output/deal-desk-dispo-chain/<slug>/lead.md`, a few lines on the
  owner, motivation, condition, timeline and anything already said about price.

You MUST NOT name a price to the seller before step 4, because the offer comes from the walk and the
comps, and an early number becomes the ceiling. The Prospector never names a dollar figure by text
for the same reason.

**Check:** the person on the phone is the owner of record or the estate's decision maker, the record
carries no Do Not Market tag, and its status is a lead status.

**Then:** step 2 can start now; step 3 waits for the walk.

### 2. Comps and the ARV

Pull the sales the house really competes with, bucket them by condition, and set a conservative ARV
on same-bedroom comps.

- **Slack team:** ask Roma for a quick screen with the boundary pasted in (a box, a list of lat,lon
  points, a Google My Maps KML or GeoJSON, or "radius 0.75"). It reads SiftMap deeds on your key.
- **Claude Code with `OPENWEBNINJA_API_KEY`:** run the `comp-package` skill, or let step 4's
  `post_walkthrough.py` run do the pull inside your `polygon.json`. Then judge its ARV with the
  `real-estate-comping` skill's two-bucket method instead of accepting it.
- **Claude Code without that key:** run `real-estate-comping` by hand (Zillow, Redfin and the county
  assessor). It is the same method, with the comps gathered by you.
- **A deed read on your key, any county:** the buyer match script reads every arm's-length
  single-family sale inside a square around the house and prints a retail and an as-is median from
  SiftMap deeds:

```
python src/deal_buyer_match.py --slug <slug> --lat <lat> --lon <lon> --beds <beds> --sqft <sqft> \
    --ask <working ask> --comp-mi 0.75 --county none --detail-cap 60 --phase all
```

The house's latitude and longitude are on its DataSift record. `--ask` only scores buyers here; it
does not touch the comps. This script writes to `output/deal_buyers/<slug>/` (its fixed folder).

- Input: the address, `polygon.json` (see `references/deal-files.md`), the county card.
- Output: the comping skill's workbook, saved as `output/deal-desk-dispo-chain/<slug>/comps.xlsx`,
  and an ARV with its basis (comp count, beds, size window, dates, median $/sf).

You MUST build the base ARV from same-bedroom renovated comps, held to that band's median price,
because a house below the comp set's bedroom count lives in a lower value band than a per-bedroom
adjustment implies. You MUST NOT credit an added bedroom or bath in the ARV, because an addition
nobody committed to invents value and inflates what you can pay. You SHOULD test two or three
boundary sizes and look at which streets enter, because one expensive street can carry the whole
number. The county card MUST win over aggregators on beds, baths and size, because aggregators get
bedroom counts wrong.

**Check:** print the streets inside the boundary and compare them with the map; open the comps that
built the ARV and confirm they are same-bed, inside 0.70 to 1.35 times the size, and mostly from the
last 12 months; compare the ARV with the deed read's retail median.

**Then:** the renovated comps' finishes set the rehab scope in step 3.

### 3. Rehab: one number from the walk

Turn the walk into one repair number, at the finish the comps sold with, for the layout the house has
today.

- **Slack team:** post the walkthrough video (or a Drive or Dropbox link) to Roma's
  `walkthrough_video`, or photos and notes to `underwrite`. One repair number comes back with the
  photo findings.
- **Claude Code:** run the `rehab-estimator` skill on the photos. For a video, cut frames first:

```
ffmpeg -v error -i <video> -vf "fps=1/2,scale=900:-1" -q:v 4 output/deal-desk-dispo-chain/<slug>/frames/i_%03d.jpg
python playbook-creator/scripts/transcribe_video.py <video> --frames-dir output/deal-desk-dispo-chain/<slug>/frames
```

Transcribe only when there is speech worth having (about $0.002 an audio minute; the second line
needs the `playbook-creator` skill and `OPENROUTER_API_KEY`). The frames are the evidence.

- Write the walk file from the template and the frames (shape and rules in
  `references/deal-files.md`):

```
python src/post_walkthrough.py --walkthrough-template
```

- Input: photos, video frames, the comps' finishes. Output:
  `output/deal-desk-dispo-chain/<slug>/walk.json` and the skill's rehab workbook
  (`rehab-estimator/scripts/generate_rehab_excel.py`).

You MUST produce one number (`single_scenario`), never a menu of scenarios, because a menu is not an
answer. You MUST set `target_config` and `single_scenario` to the beds and baths the house has today,
because they drive the ARV and the rehab on separate paths and nothing cross-checks them. You MUST
set every flag's `scenarios` to `[]`, because a flag keyed to another scenario is dropped without a
word. You MUST NOT leave a pending quote blank or at $0, because the total has to stay a true
number: mark it `"placeholder": true` with a realistic cost. On Knox and Blount deals the locked
material list (`rehab-estimator/data/master_material_list_37914.csv`) is the material price, never
multiplied by a regional factor; elsewhere the cheat sheet and your local labor multiplier apply.

**Check:** the walk's flags are listed one by one (condition, not finish), placeholders show in red,
and the rehab per square foot sits inside the skill's sanity bands.

**Then:** `walk.json` feeds the offer in step 4.

### 4. The offer

Render the one-tab wholesale offer: the buyer's maximum is the rule times the ARV minus the rehab,
and the offer to the seller is that maximum minus your fee.

- **Slack team:** Roma's `underwrite` saves the pack; then "offer sheet for task N" (add "fee 12k" or
  an ARV to override).
- **Claude Code:**

```
python src/post_walkthrough.py --address "123 Example St" --city "<City>" --state <ST> --zip <ZIP> \
    --beds <beds> --baths <baths> --sqft <sqft> --year-built <year> --months 24 \
    --polygon output/deal-desk-dispo-chain/<slug>/polygon.json \
    --walkthrough output/deal-desk-dispo-chain/<slug>/walk.json \
    --save-pack output/deal-desk-dispo-chain/<slug>/pack.json \
    --out output/deal-desk-dispo-chain/<slug>/post_walkthrough.xlsx
python src/offer_sheet.py --pack output/deal-desk-dispo-chain/<slug>/pack.json \
    --walk output/deal-desk-dispo-chain/<slug>/walk.json --rule 0.70 --fee 15000 \
    --arv <the ARV you judged in step 2> --out output/deal-desk-dispo-chain/<slug>/offer.xlsx
```

Without `OPENWEBNINJA_API_KEY`, add `--no-api` to the first command (it then reads the lead and
builds the rehab, with no comps) and pass your ARV and as-is value on the second with `--arv` and
`--as-is`. An explicit `--arv` always beats the pack.

- For the full flip and wholetail view (financing, holding, both exits, the 75% and 70% maximums),
  run the `deal-analyzer` skill (`/deal-analyzer:analyze-deal 123 Example St, <City>, <ST>`).
- **The creative offer.** On a listed house, step 6 prices subject-to and seller finance for you. On
  an off-market lead, work it by hand with the same rules: subject-to on a term loan at or under
  5.0%, seller finance only on a house that is free and clear. Read the loan off the record's
  mortgage fields or the county register of deeds, and confirm the payoff with the seller.
- Output: `output/deal-desk-dispo-chain/<slug>/offer.xlsx` (Offer, The House, Repair Detail, Comps,
  Buyers).

You MUST state the math out loud: buyer's maximum, your fee, the offer, and the buy price that clears
your fee. You MUST say so when the numbers disagree (an as-is comp band above the flipper's maximum
on a heavy rehab), because the flattering number is the one that loses the deal later.

**Check:** open the workbook and change the rule cell from 0.70 to 0.75. The offer, the buyer's
profit and every return must move together; if they do not, the page is not live. The offer must
equal rule x ARV - rehab - fee to the dollar.

**Then:** the offer goes to the seller (a person makes that call). The scope goes to step 5 for bids.

### 5. Contractor bids

Build the market's contractor bench once, then get two or three bids on this scope and replace each
red placeholder with a signed number.

```
python vendor-directory-builder/scripts/build_directory.py <config.json> output/deal-desk-dispo-chain/vendors/directory.xlsx
python contractor-call-sheet/scripts/build_call_sheet.py output/deal-desk-dispo-chain/vendors/directory.xlsx output/deal-desk-dispo-chain/vendors/call_sheet.html
```

- `vendor-directory-builder` mines a community source (a local investor Facebook group, a forum)
  and checks every name against public records. A private group needs your own signed-in browser.
- `contractor-call-sheet` turns the top picks into a one-page call sheet and drafts a first text and
  voicemail per pick, plus the vetting-call questions. A person sends them.
- When a bid is signed, put the whole-scope figure in the walk (`"bids": {"reno": 52000}`) and run
  step 4 again.

You MUST NOT invent a phone number, license or rating; write "not found", because people dial these
numbers and sign contracts off the sheet.

**Check:** spot-check three phone numbers and any license claim against its source, and confirm each
pick serves the house's county.

**Then:** the bid-backed rehab re-renders the offer (step 4). The bench stays for the next deal.

### 6. Second source: offers on listed houses

Make a cash offer on every active single-family listing in the county and a creative offer wherever
the seller's loan allows one, then work the listing agents.

```
python src/mls_offers.py --county "<County>, <ST>" --phase all --shortlist 60 --sender "<your first name>" --out output/deal-desk-dispo-chain/mls
python src/mls_offers.py --county "<County>, <ST>" --phase add --add --out output/deal-desk-dispo-chain/mls
python src/mls_offers.py --county "<County>, <ST>" --phase all --shortlist 60 --add --commit --out output/deal-desk-dispo-chain/mls
```

- The first line is free (search only, nothing added). The second is a quote. The third adds the
  shortlist to the list "MLS Offers" with the tag "MLS Listing" (record allowance) and runs only
  after a person approves the quote.
- Before anything goes to an agent, check the top offers are still for sale (`--phase all --zillow
  --limit 50`, paid per call): the data's listing status can lag by weeks.
- Output: the workbook (Offers, Subject-to, Agents, Price watch, More listings, Not offered) and
  `agent_outreach_<date>.csv` in `output/deal-desk-dispo-chain/mls/`.
- Slack team: "MLS offers for <County>, <ST>" to Roma. Roma screens and quotes; adding records is
  off unless the box allows it.

You MUST NOT contact the homeowner of a listed house, because the listing agent is the contact. You
MUST keep the tag "MLS Listing" and the list "MLS Offers" out of every preset that texts, calls or
mails owners (see Built in the app). The drafts say "a local investor" and you MUST NOT add a lender
pre-approval claim, because agents file complaints over it. Write contracts "and/or assigns", and
check your state's wholesaling rules with a closing attorney.

**Check:** the screen count is not zero; read the quote's billable count and allowance before any
`--commit`; check one shortlisted listing's agent and loan by hand.

**Then:** an accepted offer comes back to step 2 with the agent's access for a walk. The Price watch
sheet is next week's list.

### 7. Under contract: find the buyers

The day the contract is signed, find who buys houses like this one nearby, with a guess at what each
would pay, then who decides at each buyer and the numbers that reach them.

The county list (free, any county in its data):

```
python buyer-prospector/scripts/analyze_buyers.py buyer-prospector/data/nationwide_buyers.csv \
    --county "<County>" --state <ST> --output output/deal-desk-dispo-chain/buyers/<County>_<ST>_buyers.xlsx
```

The buyers for this house (free, read only, on your key):

```
python src/buyer_search_cli.py --address "123 Example St, <City>, <ST> <ZIP>" --arv <ARV> \
    --radius 5 --reads 150 --show 0 \
    --work output/deal-desk-dispo-chain/<slug>/buyer_search \
    --json output/deal-desk-dispo-chain/<slug>/buyer_search.json \
    --csv output/deal-desk-dispo-chain/<slug>/buyers.csv
```

Who decides at each, and their numbers (dry until `--commit`; pass the same `--out` every time):

```
python src/buyer_contacts.py --slug <slug> --phase seed --work output/deal-desk-dispo-chain/<slug>/buyer_search \
    --record <record_uuid> --arv <ARV> --ask <ask> --out output/deal-desk-dispo-chain/<slug>/contacts
python src/buyer_contacts.py --slug <slug> --phase houses --out output/deal-desk-dispo-chain/<slug>/contacts
python src/buyer_contacts.py --slug <slug> --phase research --out output/deal-desk-dispo-chain/<slug>/contacts
python src/buyer_contacts.py --slug <slug> --phase officers --out output/deal-desk-dispo-chain/<slug>/contacts
python src/buyer_contacts.py --slug <slug> --phase contacts --out output/deal-desk-dispo-chain/<slug>/contacts
```

Each paid phase's dry run prints what it would buy: a cost for research, officers and the SmartSkip
trace, and a count of name and number pairs for the checks (about $0.03 each). Run it again with
`--commit` only after a person approves, and stop at `budget_usd`.

In Claude Code you MAY do the research yourself instead of buying it: for each buyer, fill the
`research/<buyer>.json` file the seed wrote with the people a page names together with the company
(`"from": "research"`, each with its URL and a quote), any number or email a page shows, and
`"researched": true`. The research phase then has nothing left to buy.

- Output: `buyer_search.json`, `buyers.csv`, and in `contacts/`: `ranked.json`, `dial_sheet.csv`,
  `Buyers_<slug>.xlsx`, `ledger.json` (what was spent).
- Slack team: the contract status posts the record to Roma's `deal_buyers`; "buyers for 123 Example
  St" runs `buyer_search` (add "buyers 11 to 30" for more).

You MUST keep each run inside the deed cap (150 by default, nearest first), because DataSift's
acceptable use forbids SiftMap detail reads in volume without adding the records. You MUST NOT
SmartSkip a decision maker by name and city, because a common name returns a stranger and a name
check then confirms the wrong person. You MUST hold back a registered agent who shares no name with
the company, because the registered agent is usually the company's lawyer. You MUST NOT count a
company officer from another state, because the officer search matches company names nationally and
an out-of-state officer is a different company. Builders, iBuyers, funds, lenders and government stay
on the list marked "not a target" with the reason.

**Check:** read the buyer search's meter line (investor sales seen, like this house, deeds read and
left unread, buyers to call); read each dry cost line before its `--commit`; open one buyer and
confirm the decision maker's page names both the person and the company, and that their number's
name check reads Yes.

**Then:** `contacts/ranked.json` and `contacts/dial_sheet.csv` build the blast cohort in step 8.

### 8. Blast the deal

Text the deal to the buyers whose purchase history fits the price. Stage it; a person releases it.

```
python dispo-deal-blast/scripts/cohort.py output/deal-desk-dispo-chain/<slug>/cohort_input.json --price <ask> --floor-mult 4 --months 12
```

- Build `cohort_input.json` from the contact run, and `deal_sheet.json` for the copy (both in
  `references/deal-files.md`).
- Write one message per buyer by the `dispo-deal-blast` copy rules
  (`dispo-deal-blast/references/copy-rules.md`) and audit every message, not a sample: signed by a
  real first name, no figure but the asking price, no link, no ZIP, no dash, at most one question,
  under 320 characters, and it names a city that buyer has bought in.
- Load the cohort into your texting tool as a held or scheduled batch. A person releases it.
- If you run SiftStack's own texting app with a dispo number pool, it stages from the same deal
  sheet: `python src/sms_agent/dispo_campaign.py --deal deals/<id>.json` (dry), add `--queue` to
  stage it held, and `python src/sms_agent/cli.py release --touch 1` sends it.
- Output: `output/deal-desk-dispo-chain/<slug>/messages.csv` (buyer, number, message).

You MUST NOT put the contract price or a lockbox code in the deal sheet, because what never enters
the process cannot leak. You MUST NOT state any figure but the approved asking price, because a
whitelist catches a stray number and a blocklist only catches the one you remembered. You MUST NOT
text the current owner of a house whose last sale was a flip, because that owner is a retail
homebuyer; the investor was the seller. You MUST stage a deal only once, because a second copy of a cold text
burns the number. Withhold the exact address unless `disclose_address` is set for this deal, and
keep each buyer on the number that texted them last.

**Check:** the cohort counts (in the file, dropped as not active, out of band, kept) and "band
covers" against "have never bought that cheap"; the audit passes on every message; the staged count
equals the cohort's textable count, with no number twice.

**Then:** replies go to step 9. A reply that asks for the address, photos, a walkthrough or a price
goes to a person.

### 9. Keep the buyer book

After each blast, sort every reply and keep the buyers for the next deal.

- Merge this deal's buyers and replies into `output/deal-desk-dispo-chain/buyer_book.csv` (columns
  and statuses in `references/deal-files.md`): verified buyers first next time, silent buyers
  texted again, and anyone who said stop, wrong number or no left out for good.
- Next deal, start step 7 from the book, then re-run the buyer search for that house and merge.

An opt-out MUST stay out across every future deal, because the person asked not to be texted again.
Sort only the replies to your dispo numbers, because sorting every reply in the account would
suppress seller numbers as a side effect.

**Check:** no buyer appears twice, and every opt-out and wrong number from this blast carries that
status in the book.

**Then:** the next contract starts at step 7 with a warm list.

## Stand it up

First run, on about 10 records:

1. One house you already walked or sold: run steps 2 to 4 and compare the offer with what really
   happened.
2. One ZIP on the listing screen: `python src/mls_offers.py --zips <ZIP> --phase all --shortlist 10
   --out output/deal-desk-dispo-chain/mls`.
3. One buyer search with `--reads 40`, then the contact run's research on 10 buyers (`--limit 10`),
   dry first.

It is working when the offer sheet recalculates (step 4's check), the listing screen returns offers
with an agent on each, the buyer search lists buyers with priced purchases near the house, and most
of the 10 researched buyers have a named person on a page that names the company too.

Then let it run:

- **Slack team:** deploy the agents brain once (`python deploy/agents_up.py`, dry by default, then
  `--commit`). The quick screen and the buyer run start themselves from your DataSift status
  changes (see Built in the app).
- **Claude Code:** run the free listing screen weekly from your operating system's scheduler (Task
  Scheduler or cron), or as a Claude Code routine (`/schedule`) set up where it can reach your
  SiftStack folder and its `.env`. Refresh the buyer search on every open contract the same day.
- **Weekly:** read the Price watch sheet, the offers waiting on agents, every contract without a
  buyer after 7 days, and the blast replies.

## What to watch

- **Offer to contract.** About 4 offers to 1 contract; below 20% usually means the comps are off.
- **Your fee per deal.** The spread between the buyer's maximum and your offer. The offer sheet
  defaults to $15,000; set your own with `--fee`.
- **Bids against placeholders.** The share of each rehab that rests on a signed bid.
- **Buyers to call with a checked number.** On one measured deal, about 4 in 10 buyers had a number
  matched to the decision maker (or registered to the company) at Dial First or Second.
- **Blast replies and showings.** The first measured blast drew replies from 16% of buyers. With the
  address withheld, replies ask for the address; with it public, watch lockbox and showing requests.

## Built in the app

What the API cannot do, as a click-through:

1. **A contract status.** If your account has no status for a signed contract, add one in the app's
   settings (or use a SiftLine column for it). The API cannot create a status.
2. **Webhooks to the Slack team (only if you run it).** Settings, Integrations, Webhooks (Super Admin
   or Sensei): add `https://<your brain>/hooks/datasift/<secret>/quick-screen` and
   `https://<your brain>/hooks/datasift/<secret>/under-contract`, and press Test. The brain records
   the test delivery.
3. **Sequences.** Sequence Builder: trigger "Property Status Change" to your screening status, action
   "Send Webhook" (quick-screen). A second one: "Property Status Change" to your contract status,
   "Send Webhook" (under-contract). Plans cap sequences (3 on Essentials, 8 on Professional).
4. **Listings out of owner outreach.** Filter Presets: open every preset that texts, calls or mails
   owners, set Tags "do not include" MLS Listing and Lists "do not include" MLS Offers, then Save.

## Where it works

- Comps, rehab and the offer: any market. The locked material list is Knox-local (ZIP 37914) for
  Knox and Blount deals; everywhere else the cheat sheet and your labor multiplier set prices. In a
  non-disclosure state (the comping skill lists them) recorded prices are scarce, so the comping
  skill routes to its triangulation method, and the deed read and buyer price guesses thin out.
- Buyer search, buyer contacts and the listing screen: any county with SiftMap data, on your own key.
  Two pieces still lean on East Tennessee: the buyer research prompt names Knoxville, and the dial
  sheet prefers 865 and 423 numbers when two are otherwise equal.
- The county buyer list: the counties in its bundled snapshot (about 1,471, buyers with 2 or more
  purchases in six months).
- The staged blast in SiftStack's texting app: only if you run that app with a dispo number pool.
  Everywhere else the blast is a file a person loads into their own texting tool.

## Gotchas

- **The stale pack.** A saved pack is a snapshot. Recompute the rehab from the walk, let an explicit
  `--arv` or `--as-is` beat the pack, and write corrections back.
- **The silent scenario drop.** With `single_scenario` set, a flag still keyed to other scenarios is
  deleted from the budget with no warning. Set every flag's `scenarios` to `[]`.
- **Two layout keys.** `target_config` drives the ARV and `single_scenario` drives the rehab. Set both
  to the layout that exists; the walk template ships a 3 bed / 2 bath target.
- **The bed clamp.** The ARV matches bed count exactly; in a thin pocket that can leave three comps on
  the two priciest streets. Look at which comps built the number.
- **Two comp files do not mix.** `comp-package`'s pull saves cleaned rows, and
  `post_walkthrough.py --sold-json` reads only raw API rows, so it reads zero comps from that file
  with no error. Let `post_walkthrough.py` do its own pull, or pass the judged ARV with `--arv`.
- **The flip trap.** A sale labeled "flip" means the current owner is a retail homebuyer; the
  investor you want sold it. The buyer search reads it right; your own lists must too.
- **Silent filters.** An unknown SiftMap filter key is ignored, not refused. Change one filter and
  compare the count before you trust it.
- **The county buyer list's swapped columns.** In the bundled buyer data the city and state columns
  hold each other's values, and the analysis copies them into the mailing columns as they are. Swap
  them back before anything is uploaded for a skip trace. Its purchase count is a six-month regional
  total, not that county's alone.
- **Listings lag.** A listing marked active can have gone pending weeks ago. Check before an agent
  hears a number.
- **Excel holds a lock.** With the workbook open, a run cannot save over it and writes a copy beside
  it. Close the file before you re-run.
- **Release is the only send.** Staging sends nothing. A person releases, and a person sends the
  address, the photos and the lockbox code.
