# MLS offers

Two offers on every active single-family listing in a market: a cash offer priced off what investors
actually paid for similar houses nearby, and a creative offer where the seller's loan allows one
(subject-to on a low-rate loan, seller finance on a free and clear house). Each offer comes with the
listing agent, the price history and a drafted first text to the agent.

It runs on your own DataSift Open API key, stays inside DataSift's API terms, and sends nothing. A
person makes every call and sends every text.

This is the MLS offer play many wholesalers run by hand: pull every active listing, offer cash on all
of them, and offer subject-to wherever the seller's rate is low. The engine does the pulling, the
pricing and the loan math, so a person only works the agents.

## What you need

| Need | Notes |
|---|---|
| Python 3.10 or newer | `pip install requests openpyxl` (`python-dotenv` is optional) |
| `curl` on the PATH | The public HMDA host refuses Python's own HTTP client. Windows 10+, macOS and Linux ship it. |
| `REISIFT_API_KEY` | Your DataSift Open API key. Required. An account admin creates it; developers.datasift.ai explains how. It never expires. |
| `OPENWEBNINJA_API_KEY` | Optional. The Zillow check: still for sale, listing remarks, broker phone, rent estimate. Paid per call. |
| Record allowance | Only if you add the shortlist to your account. The run quotes it first. |

## Four ways to run it

**Claude Code or Cowork.** Install the skill once:

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install mls-offer-engine@siftstack
```

Then ask: "make offers on every listing in Knox County, TN". The skill runs the steps below and
explains the workbook.

**The command line, from a SiftStack checkout:**

```bash
# 1. Screen, price and shortlist. Free: search only, nothing added, nothing paid.
python src/mls_offers.py --county "Knox, TN" --phase all --shortlist 60

# 2. What adding the shortlisted listings that are not records yet would cost. A quote: nothing is added.
python src/mls_offers.py --county "Knox, TN" --phase add --add

# 3. Add them (list "MLS Offers", tag "MLS Listing"), read every shortlisted listing in full, offer again.
python src/mls_offers.py --county "Knox, TN" --phase all --shortlist 60 --add --commit
```

A market is `--county "Knox, TN"` (several separated by `;`), `--fips 47093,47009` or
`--zips 37914,37917`. `--sender "Your Name"` signs the drafted texts (or set `MLS_OFFERS_SENDER`);
unset, they name nobody. Every offer rule is a flag (`--help` lists them), or a JSON file through
`--policy`. After changing a rule, `--phase offers` and then `--phase export` rebuild the workbook
without calling the API again.

**Python:**

```python
import mls_offers_api as api

market = api.resolve_market(county="Knox, TN")
result = api.run(market, shortlist=60)            # add=True quotes; add=True, commit=True adds
print(result["workbook"], result["counts"], result["notes"])
```

**Slack, through the team** (the agents brain). Ask Roma: "MLS offers for Knox, TN". She answers
with the counts, the ten best offers and links to the workbook and the agent CSV. The reply carries
no phone number; the agent's line stays in the workbook. See "Running it for a team" below.

## What a run does

| Step | What happens | What it spends |
|---|---|---|
| screen | SiftMap search: every active single-family listing up to the buy box ceiling ($700,000 by default), with its days-on-market bucket, distress flags, value and equity | nothing |
| buyers | SiftMap search: investor purchases and flip resales from the last 24 months, by price band, so each sale's price is known within $5,000 under $300,000 | nothing |
| shortlist | the best N listings (60 by default) by motivation and offer room, plus up to N more that are already records in your account | nothing |
| add | quotes adding the shortlisted listings that are not records yet; with `--add --commit` it adds them | record allowance, only with `--commit` |
| detail | full details, only for listings that are records in your account: the agent (name, office, phone), the listing history (cuts, expiries, relists, true days on market), the seller's recorded loans, year built, the AI scores | nothing |
| rent | each listing's estimated rent, by $100 band | nothing |
| mortgage | each open loan's rate, balance and payment, modeled from HMDA, Freddie Mac and the Census geocoder | nothing (public data) |
| zillow | optional (`--zillow`): still for sale, remarks, broker phone | OpenWeb Ninja calls |
| offers, export | the offer rules, the workbook and the outreach CSV | nothing |

Measured on Knox County, TN, 2026-11-23, with nothing added: 1,058 listings screened, 2,175 investor
sales priced, 120 shortlisted, 101 read in full (the other 19 were not records yet), 47 loans modeled,
37 offers: 18 cash, 12 subject-to, 7 seller finance.

## Staying inside DataSift's acceptable use

- **Search in volume is allowed.** The screen, the investor sales and the rent are all search.
- **Full property details in volume are allowed only for records in your own account.** The engine
  reads details only for listings that are already records. The rest of the shortlist waits until
  you add it, and the workbook lists those listings under "More listings" with a preliminary cash
  number.
- **Adding is quoted first.** The quote route stores and charges nothing. Only `--add --commit` adds.
  `--expect-email you@example.com` refuses to add unless the key belongs to that user.
- **Paced at 80 calls a minute.** The account's limit (120 a minute, 5,000 an hour) is shared by every
  tool on the key; a 429 is retried after the wait the server asks for.
- **Never** autocomplete in volume, the property images route, or a partner namespace.

## Keep listings out of owner outreach

Added listings are real records, tagged "MLS Listing" and on the list "MLS Offers". The listing agent
is the contact, never the homeowner. Leave that tag and that list out of every preset that texts,
calls or mails owners. SiftStack's autopilot already holds any record whose MLS status reads Listed or
Pending from its texts, calls, mail and caller assignment.

## What you get

Output goes to `output/mls_offers/<market>/` in a checkout, `~/SiftStack/mls_offers/<market>/` for an
installed skill, or `--out` (or `MLS_OFFERS_OUT`). The public mortgage downloads are cached in
`output/mortgage_model/` or `~/SiftStack/cache/mortgage_model/` (`MORTGAGE_MODEL_CACHE` moves them).

The workbook:

- **Overview:** counts, the offer lanes, how much is owed against list, owner types, why listings got
  no offer.
- **Offers:** one row per listing with an offer, best first. The mortgage columns are modeled, and
  the rate source says whether the rate is HMDA's actual rate ("HMDA match") or an estimate ("Freddie
  Mac week + local spread"). "First text" is the drafted opener to the agent.
- **Subject-to:** the listings that owe closest to list first. That is where a seller cannot sell for
  cash and walk away with anything.
- **Buyers, Agents:** investor buyers near the offers where the data names them, and one row per
  listing agent (an agent with several stale listings is one relationship worth several calls).
- **Price watch:** cash offers that would be under half of list today, with the list price that makes
  each worth sending.
- **More listings, Not offered, Sources and caveats.**

`agent_outreach_<date>.csv` is the same call list for a dialer or a spreadsheet.

## The offer rules, short

- **Cash:** what investors paid for similar houses nearby, less a $15,000 fee, never above 70% of list
  (65% when the listing scores as motivated), on lists up to $300,000. With no investor sales nearby
  it falls back to value x 60% less repairs less the fee. Under half of list it goes to Price watch.
- **Subject-to:** on every term loan at or under 5.0%. Price is list, up to $12,000 to the seller at
  closing, the rest of the equity carried as a 0% note over 30 years whose payment counts in the
  monthly cost, and the buyer pays the listing commission. One that does not cash flow as a rental
  is kept and flagged (the exit may be a wrap or a resale). Credit lines and construction loans are
  payoffs; a blanket loan or debt over 125% of the price gets no subject-to.
- **Seller finance:** only on houses the data flags free and clear, 10% down, 4% over 30 years, and it
  has to return at least 10% cash on cash.
- The AI off-market score adds up to 15 points to the ranking.

The full method with every number: `skills/mls-offer-engine/references/method.md`.

## Before any number goes to an agent

- **The numbers are estimates.** Loan balances and rates are modeled, the rent is an estimate, repairs
  are a flat placeholder until someone reads the photos or walks the house. Confirm the payoff, the
  rate and the rent with the agent before anything is binding.
- **Check the listing is still for sale.** The data's listing status can lag by weeks. `--zillow`
  checks it.
- **Say what you are.** The drafts say "a local investor" and never claim a lender pre-approval. Do
  not add one, and never go around the agent.
- **Assignment and subject-to.** Write contracts "and/or assigns" and say up front that you may
  assign; wholesaling rules differ by state, so check yours with a closing attorney. On a subject-to
  the loan stays in the seller's name and the lender can call it due on a transfer: use an attorney
  who does these, and make sure the seller understands it.

## Running it for a team

On the agents brain the workflow is `mls_offers`, owned by Roma (`team/roster.yaml`). On every ask it
screens, prices and reads the listings already in the account. It adds records or reads Zillow only
when the box runs `AGENTS_MODE=live`, the ask says so, and the settings allow it. A box in `dry` or
`free` mode screens and quotes only (and `AGENTS_MODE=live` turns on every other paid workflow too):

| Setting | Default | What it does |
|---|---|---|
| `AGENTS_MLS_SHORTLIST` | 60 | listings read in full per ask (an ask may say up to 150) |
| `AGENTS_MLS_ADD_MAX` | 0 | the most records one ask may add; 0 means the team never adds |
| `AGENTS_MLS_ZILLOW_MAX` | 60 | Zillow calls per ask, inside `AGENTS_MLS_RUN_CAP_USD` (2) and the day cap |
| `AGENTS_MLS_MONTHS` | 24 | how far back investor purchases count |
| `AGENTS_MLS_LIST`, `AGENTS_MLS_TAG` | MLS Offers, MLS Listing | where added listings go |
| `MLS_OFFERS_SENDER` | unset | who the drafted texts are signed by |
| `MORTGAGE_MODEL_CACHE` | /data/mortgage_model | the public mortgage downloads, on the volume |

The brain's API calls share its request budget with every other stage (45 a minute by day), so a run
never crowds out the schedule, and it takes longer than on a workstation: one ZIP code with a short
shortlist took about 8 minutes the first time (the public mortgage data downloads once per county,
then stays on the volume), and a whole county takes a good deal longer. Ask for ZIP codes when speed
matters.

## When something looks wrong

- **Zero listings:** check the market spelling ("Knox, TN"), then run `--phase screen` alone.
- **401 on every call:** `REISIFT_API_KEY` is wrong or not exported into this shell.
- **The quote shows a shortfall:** the add needs more allowance than is left. Lower `--shortlist`, or
  work the listings already in your account.
- **Every rate reads "Freddie Mac week" and none "HMDA match":** the HMDA download failed. Check that
  `curl` runs from this shell.
- **The workbook did not overwrite:** it is open in Excel; the run saved a copy beside it.

Tests (zero network): `tests/test_mls_offers_api.py`, `tests/test_mls_offers.py`,
`tests/test_mortgage_model.py`, `tests/test_mls_offers_skill_sync.py`, and the agents selftest section
`selftest_mls_offers`.
