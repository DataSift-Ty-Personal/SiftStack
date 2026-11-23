---
name: mls-offer-engine
description: >
  Make a cash offer and a creative offer on every active MLS listing in a county or a set of ZIP codes, on your own DataSift Open API key. Screens every active single-family listing through SiftMap search, prices each cash offer off what investors actually paid for similar houses nearby, adds only the shortlist to your DataSift account (quoted first) so it can read the listing agent, the price history and the seller's loans, models each loan's real rate and payment from public HMDA and Freddie Mac data, and offers subject-to on every low-rate loan. Writes an Excel workbook and an agent call list with a drafted first text for each listing. Sends nothing. Trigger for: make offers on MLS listings, offer on every listing in my county, subject-to offers on listed houses, build an MLS offer list, which listings would take a subject-to, or call listing agents with cash offers.
---

# MLS Offer Engine

Two offers on every active listing in a market: a cash offer priced off what
investors actually pay nearby, and a creative offer where the seller's loan
allows one (subject-to on a low-rate loan, seller finance on a free and clear
house). Then a call list of the listing agents, with a drafted first text for
each listing.

It runs on your own DataSift Open API key and stays inside DataSift's API
terms. It sends nothing to anyone. Every text it writes is a draft for a person
to send.

Use it when someone says "make offers on every listing in my county", "which
listed houses would take a subject-to", "build me an MLS offer list", or "who
are the listing agents I should call with offers".

Do not use it to reach homeowners. A listed house has an agent, and the agent
is the contact.

## What you need

- Python 3.10 or newer with `requests` and `openpyxl`
  (`pip install requests openpyxl`). `python-dotenv` is optional.
- `curl` on the PATH. The mortgage model downloads the public HMDA files with
  it, because that host refuses Python's own HTTP client. Windows 10 and newer,
  macOS and Linux ship it.
- `REISIFT_API_KEY`: your DataSift Open API key. Required. An account admin
  creates it in DataSift (developers.datasift.ai explains it). It never
  expires.
- `OPENWEBNINJA_API_KEY`: optional. Turns on the Zillow check (still for sale,
  listing remarks, broker phone, rent estimate). Metered per call, see
  openwebninja.com.
- Record allowance, only if you add the shortlist to your account (step 3
  below). The quote shows the cost before anything is spent.

Keys are read from environment variables. If the person keeps them in a `.env`
file, export them for the command rather than assuming the scripts find the
file:

```bash
# bash or zsh, from the folder that holds the .env
set -a; . ./.env; set +a
```

```powershell
# PowerShell, from the folder that holds the .env
Get-Content .env | Where-Object { $_ -match '^[A-Z0-9_]+=.+' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
```

Replace `SKILL_DIR` below with this skill's folder. In Claude Code and Cowork
Claude resolves it; on a terminal it is wherever the skill is installed (for
example `~/.claude/skills/mls-offer-engine`). Always pass `--out` with a folder
in the person's working directory, so the workbook lands where they can find
it. Without `--out` a run writes to `~/SiftStack/mls_offers/<market>`, and the
public mortgage data it downloads is kept in `~/SiftStack/cache` (never inside
the skill's own folder, which a plugin update replaces). `MLS_OFFERS_OUT` and
`MORTGAGE_MODEL_CACHE` move them.

## Run it

Work in three steps. Only step 3 changes anything in the account, and only with
`--commit`.

```bash
# 1. Screen, price and shortlist. Free: search only, nothing added, nothing paid.
#    Shortlisted listings already in your account get their full detail read.
python SKILL_DIR/scripts/mls_offers.py --county "Knox, TN" \
    --phase all --shortlist 60 --out mls-offers-knox

# 2. See what adding the shortlist would cost. Quote only, nothing is added.
python SKILL_DIR/scripts/mls_offers.py --county "Knox, TN" \
    --phase add --add --out mls-offers-knox

# 3. Add the shortlist to the list "MLS Offers" (tag "MLS Listing"), read every
#    shortlisted listing's detail, and rebuild the offers.
python SKILL_DIR/scripts/mls_offers.py --county "Knox, TN" \
    --phase all --shortlist 60 --add --commit --out mls-offers-knox
```

Optional Zillow check on the top offers (paid per call, needs
`OPENWEBNINJA_API_KEY`). Run it before anything goes to an agent: the listing
status in the data can lag by weeks.

```bash
python SKILL_DIR/scripts/mls_offers.py --county "Knox, TN" \
    --phase all --zillow --limit 50 --out mls-offers-knox
```

Pass `--sender "Your Name"` on every run so the drafted texts carry the
caller's own first name.

Choosing the market:

- `--county "Knox, TN"` (one county; the word County is optional)
- `--fips 47093,47009` (one or more county FIPS codes)
- `--zips 37914,37917` (ZIP codes instead of whole counties)
- `--name knox-east` (the folder and file name to use)

The phases run in this order and each can be run alone: `screen`, `buyers`,
`shortlist`, `add`, `detail`, `mortgage`, `zillow`, `offers`, `export`. `all`
runs every phase except the paid Zillow check unless `--zillow` is given. Every
offer rule is a flag too (for example `--subto-max-rate 5.5`, `--fee 12000`,
`--cash-max-list 250000`); `--help` lists them. After changing a rule, run
`--phase offers` and then `--phase export` to rebuild without pulling again.

From Python, the same run:

```python
import sys
sys.path.insert(0, "SKILL_DIR/scripts")
import mls_offers_api as api

market = api.resolve_market(county="Knox, TN")
result = api.run(market, out="mls-offers-knox", shortlist=60)
print(result["workbook"])
print(result["counts"])   # listings, shortlisted, in_account, detailed, offers, cash, subject_to, ...
print(result["notes"])    # anything the run could not do, in plain words
```

`run(..., add=True)` quotes the add; `run(..., add=True, commit=True)` adds the
shortlist. `result["quote"]` carries the quote (new, billable, allowance left).

## What happens in a run

1. **Screen.** Every active single-family listing in the market comes back from
   SiftMap search: list price, value, equity percent, size, location, a
   days-on-market bucket and any distress flags. Search is the part DataSift
   allows in volume, so this covers the whole market at no cost.
2. **What investors pay.** Investor purchases and flip resales from the last 24
   months come from the same search, split into price bands so each sale's
   price is known to within half a band ($5,000 under $300,000).
3. **Shortlist.** The best N listings (default 60) by motivation and offer
   room.
4. **Add (optional).** DataSift's acceptable use allows full property details
   in volume only for records in your own account. So the shortlisted listings
   that are not already records get added to the list "MLS Offers" with the tag
   "MLS Listing", after a quote. That uses your record allowance.
5. **Detail.** For each shortlisted listing in your account: the listing agent
   (name, office, phone), the listing history (price cuts, expiries and
   relists, true days on market), the original loans (amount, record date, due
   date, type), year built and the AI scores.
6. **Loans.** Each open loan's rate, balance and payment, modeled from public
   data: the actual rate from HMDA where one loan matches, otherwise Freddie
   Mac's weekly rate in the lock week plus a calibrated local spread, plus FHA,
   USDA or private mortgage insurance. Free, no key.
7. **Zillow (optional).** Still for sale, remarks, broker phone, rent estimate.
8. **Offers.** The rules below.
9. **Export.** The workbook and the agent outreach CSV.

Full method and every number: `references/method.md`. The routes, the filters
that silently do nothing, and why step 4 exists:
`references/api-and-acceptable-use.md`. Working the call list:
`references/outreach.md`.

## The offer rules, short

- **Cash:** what investors paid for similar houses nearby, less a $15,000 fee,
  never above 70% of list (65% when the listing scores as motivated). Cash
  offers only on lists up to $300,000. With no investor sales nearby it falls
  back to value x 60% minus repairs minus the fee.
- **Lowballs wait.** A cash offer under half of list is the kind that burns an
  agent, so it goes to the Price watch sheet with the list price that would
  make it worth sending.
- **Subject-to:** on every term loan at or under 5.0%. The price is the list
  price, the seller gets up to $12,000 at closing, the rest of their equity is
  carried as a 0% note over 30 years (that payment counts in the monthly cost),
  and the buyer pays the listing commission on top. A deal that does not cash
  flow as a rental is kept and flagged, because the exit may be a wrap or a
  resale of the loan instead. Credit lines and construction loans are payoffs,
  never taken over. A loan shared with other listed houses (a blanket loan), or
  debt over 125% of the price, gets no subject-to.
- **Seller finance:** only on houses the data flags free and clear (no loan on
  record is often just missing data), 10% down, 4% over 30 years, and it has to
  return at least 10% cash on cash.

## Reading the workbook

- **Overview:** counts, the offer lanes, how much is owed against list, owner
  types, and why listings got no offer.
- **Offers:** one row per listing with an offer, best first. The mortgage
  columns are modeled: rate source says whether the rate is HMDA's actual rate
  or an estimate. "First text" is the drafted opener to the agent.
- **Subject-to:** closest to owing the list price first. That is where a seller
  cannot sell for cash and walk away with anything.
- **Buyers:** investor buyers near the offers, where the data names them.
- **Agents:** one row per listing agent. An agent with several stale listings
  is one relationship worth several calls.
- **Price watch:** cash offers that would be under half of list today, with
  the list price that makes each sendable.
- **More listings:** screened but not shortlisted, with a preliminary cash
  number. Raise `--shortlist` to read them in full.
- **Not offered:** every listing left out and why.
- **Sources and caveats:** what every number rests on.

The CSV (`agent_outreach_<date>.csv`) is the same list ready for a dialer or a
spreadsheet.

## Guardrails

- **It sends nothing.** Every text is a draft. A person calls or texts the
  listing agent.
- **The listing agent is the contact, never the homeowner.** Listings added to
  your account are real records with the tag "MLS Listing". Keep that tag (or
  the list "MLS Offers") out of every preset that texts, calls or mails owners.
- **Say what you are.** The drafts say "a local investor" and never claim a
  lender pre-approval. Do not add one.
- **Assignment.** Write contracts "and/or assigns" and tell the agent up front
  that you may assign. Wholesaling rules differ by state: check yours with a
  closing attorney before you market any contract.
- **Subject-to.** The loan stays in the seller's name and the lender can call
  it due on a transfer. Use a closing attorney who does these, and make sure
  the seller understands it.
- **The numbers are estimates.** Loan balances and rates are modeled, rent
  comes from an estimate, and repairs are a flat placeholder until someone
  reads the photos or walks the house. Confirm the payoff, the rate and the
  rent with the agent before any number is binding.

## When something looks wrong

- **Zero listings.** Check the market spelling, then run `--phase screen`
  alone. The county search wants the county name, not "County of".
- **401 on every call.** `REISIFT_API_KEY` is wrong or not exported into this
  shell.
- **The quote shows a shortfall.** The add needs more allowance than the
  account has left. Lower `--shortlist`, or skip step 3 and work the listings
  already in your account.
- **Every rate says "Freddie Mac week" and none say HMDA.** The HMDA download
  failed. Check that `curl` runs from this shell.
- **The workbook did not overwrite.** It is open in Excel. The run saves a
  copy beside it with a number in the name.
