# OpenWeb Ninja, Zillow property data

Sold comps, active listings and property detail. The comping stack runs on it.

- **Key:** `OPENWEBNINJA_API_KEY`
- **Cost:** 100 lookups per month free, metered after
- **Used by:** `comp-package`, `deal-analyzer`, `src/zillow_market_api.py`
- **Verified:** 2026-07-21, re-checked 2026-08

## The endpoint moved, and the old one still looks alive

`similar-sale-homes`, and every other comps-shaped endpoint, is **retired and
404s**. If you are reading an older integration that calls it, that code has
been dead since the change.

**`/search` is the workhorse.** Everything comping-related goes through it.

`property-details-address` is still live and is a different thing: single
property detail by address, not comps. `src/property_enricher.py` uses it.

## The contract

```
GET /search
  location      "Knoxville, TN" or a ZIP
  home_status   RECENTLY_SOLD | FOR_SALE      <- exact enum, see below
  min_price     integer
  max_price     integer
  page          integer
```

### home_status is an exact enum

`RECENTLY_SOLD` and `FOR_SALE`, spelled exactly. Anything else returns 400.
Not `recently_sold`, not `SOLD`, not `Recently Sold`.

### Every search caps at 41 rows

`totalPages` comes back as 1 no matter how many results exist. In an active ZIP
that is roughly five weeks of sales, which is nowhere near enough for a comp
set and is very easy to mistake for "this market is quiet".

**The workaround is price-band partitioning.** Split the search by
`min_price`/`max_price` into bands, and recursively split any band that comes
back saturated at 41. `pull_sold()` in `src/zillow_market_api.py` implements
it. Expect 50 to 80 calls to recover 2 to 3 years for one ZIP.

### price_min and price_max are silently ignored

This is the classic silent parameter. The names are `min_price` and
`max_price`. Pass `price_min` and the request succeeds, returns 200, and the
filter does nothing, so you get an unfiltered 41-row window and no indication
anything is wrong.

**Always read back the echoed `parameters` object** in the response and assert
your filter actually applied. Do this for every filter, not just price.

### Field traps

| Field | Trap |
|---|---|
| `dateSold` | Epoch milliseconds, not seconds and not a date string |
| `soldPrice` | A display string like `"$132,000"`. Use `unformattedPrice` for math |
| `homeType: LOT` | Ambiguous. Either a house that sold at land value, or a new build with sqft missing. Check the county card before bucketing it |
| `livingArea` | Frequently absent on older records. Missing, not zero |
| `bedrooms` | Aggregators get this wrong often enough that a county card always wins |

## What it will never show you

MLS-only data. Auction sales, wholesale assignments and off-market transfers do
not appear at all.

For those, county records are the only truth, and that is not a limitation you
can work around inside this API. If a property traded and this API has no
record, that absence is itself a signal worth noticing.

## Cost control

- One property comped properly is several calls, not one. The 100 free lookups
  go faster than the number suggests.
- Cache the band pull. `comp_package.py --sold-json` re-runs off a saved pull
  and costs nothing, which matters because you will re-run.
- Partition by price, not by date. Date filtering is not reliable enough to
  bound the set.

## Without the key

Use the `real-estate-comping` skill: the same method with comps gathered by
browser. See [the no-API playbook](../setup/no-api-playbook.md#comping-without-an-api).
