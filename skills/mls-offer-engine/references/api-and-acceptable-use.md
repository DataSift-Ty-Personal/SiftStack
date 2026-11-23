# The API, and why the run adds a shortlist before it reads details

Everything here was verified against the live service on a DataSift Open API
key, the same access any account holder has.

## The rule that shapes the run

DataSift's API terms allow SiftMap **search** in volume. Pulling SiftMap
**property details** in volume without adding those records to your account
counts as scraping, even inside the rate limits.

So the run works in two layers:

1. **Search covers the whole market.** Every active listing, the investor
   sales around them, days on market and distress flags all come from search.
2. **Details only for records in your account.** The shortlist is added to
   your account first (quoted, then added with `--commit`), and only then are
   the details read. Listings that were already records in your account are
   read without adding anything.

Detail is where the listing agent, the listing history and the loans live, so
a listing that is neither shortlisted nor already in your account gets a
preliminary cash number only (the More listings sheet).

## Authentication and limits

- Header `authorization: Api-Key <REISIFT_API_KEY>`. The key does not expire
  and carries the permissions of the user who made it.
- About 120 requests a minute and 5,000 an hour per account, shared by every
  tool on the account, with a separate SiftMap counter of the same size. A 429
  answer says when to try again; the scripts wait and retry.

## Search: `POST https://map.reisift.io/properties/search/`

Body: `filters`, `addresses`, `polygon`, `size`, `result_index`.

- **`result_index` is a row offset, not a page number.** Pages of 250 are
  `result_index` 1, 251, 501, and so on. Passing 1, 2, 3 returns almost the
  same rows three times.
- **Keep any one query under about 9,000 rows.** Past row 10,000 the order
  changes and some rows never come back. Split a big market by value or by
  ZIP.
- **Unknown filter keys are silently ignored.** A misspelled key widens the
  search instead of failing. Prove every filter by the change it makes to
  `total_results`.
- **A county address uses the bare county name** ("Knox"). "Knox County, TN"
  returns zero rows, which looks exactly like an empty market.
- A ZIP address is `{"search": "37914", "type": "zip"}`.

Filters this run uses, each proven by a count change:

| Filter | What it does |
|---|---|
| `type_single_family: true` | single-family only |
| `extra_mls_status: ["listed"]` | active listings |
| `value_min`, `value_max` | value window |
| `extra_days_on_market: "30d"` / `"90d"` / `"365d"` | listed within that many days |
| `distressors_include` + `distressors_include_min_match` | one distress flag |
| `extra_last_sale_investor_transaction_type` | `pending`, `rental`, `wholetail`, `wholesale` (an investor bought and still holds) or `flip` (the renovated resale) |
| `extra_last_sale_date_min` | sales on or after a date |
| `extra_last_sale_value_min`, `extra_last_sale_value_max` | last sale price band |
| `extra_equity_percent_min`, `extra_equity_percent_max` | equity window |

`extra_last_sale_price_min` looks right and is silently ignored. The working
key is `extra_last_sale_value_min`.

A search row carries the property id, the address as one string, county, state,
FIPS, beds, baths, square feet, lot, use, estimated value, equity percent,
latitude and longitude, the MLS flags and list price, and whether the property
is already a record in your account (`saved`, `saved_uuid`). It carries no owner
name, no last sale price and no agent.

## Adding the shortlist

1. **Quote first:** `POST /properties/add-properties-by-query/quote/`. It
   returns how many of the listings are already in your account, how many are
   new, how many are billable, and what allowance you have left. Nothing is
   stored or charged.
2. **Add:** `POST /properties/add-properties-by-query/` with the same query, the
   listing ids as `{"search": "<id>", "type": "ids"}` addresses, and
   `"lists": ["MLS Offers"]`, `"tags": ["MLS Listing"]`,
   `"replace_owners": false`, `"auto_add_enabled": false`.
3. Adds are processed in the background. Records appear within minutes; the
   run waits and checks with a search by ids until each shows as saved.

Lists and tags are given by **name**. A uuid in their place creates a new list
literally named with that uuid.

What the added records do in your account: they are ordinary property records
on the list "MLS Offers" with the tag "MLS Listing". On plans that include it,
DataSift may skip trace them on its own. Their MLS status reads Listed. Keep the
tag out of every preset that texts, calls or mails owners: the listing agent is
the contact for these houses.

## Detail: `GET /properties/detail/{id}/`

For records in your account. The fields this run reads:

- `mls_agent`: `agent_name`, `agent_office`, `agent_phone`
- `mls_days_on_market`, `mls_listing_price`
- `mls_history`: one row per listing event, `status_date`, `status` (Active,
  Fail for an expiry or withdrawal, Pending, Sold), `price`, `sqft_price`,
  `days_listed`, `agent_name`
- `mortgage_history`: `record_date`, `loan_type`, `amount`, `due_date`,
  `lender_name`
- `sale_history` (newest first), `owner_info`, `years_built`,
  `investor_off_market_score`, `realtor_score`, `distressor_data`, `tax_amount`

`census_tract` is often empty, so tracts come from the Census geocoder.

## Public data the loan model uses (free, no key)

- **HMDA** (the Consumer Financial Protection Bureau's data browser), by county
  and year. The host refuses Python's HTTP client and serves `curl -L` a
  redirect to a prepared file, so the download goes through `curl`.
- **Freddie Mac's weekly mortgage rates** (30-year and 15-year), through FRED.
- **The Census geocoder**, for the 2020 and 2010 census tracts of each house.

Downloads are cached, so a second run does not fetch them again.

## Zillow check (optional, paid per call)

OpenWebNinja's `property-details-address`, with `OPENWEBNINJA_API_KEY`:
listing status, the agent and the broker with phones, the remarks, Zillow's
days on market (it restarts at a relist, so it reads lower than the listing
history), bank-owned and foreclosure flags, and a rent estimate. The agent's
email is usually empty.

Run it before anything goes out. The MLS status in property data lags: on one
test county, 26 of 164 listings the data called active were already pending or
sold on Zillow.
