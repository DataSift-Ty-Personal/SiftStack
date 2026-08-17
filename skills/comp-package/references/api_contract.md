# OpenWeb Ninja Real-Time Zillow Data: /search contract (verified live 2026-07-21)

Base: `https://api.openwebninja.com/realtime-zillow-data`
Auth: `x-api-key` header. Key from the `OPENWEBNINJA_API_KEY` environment variable.

## Endpoints that work

| Endpoint | Purpose | Notes |
|---|---|---|
| `/search` | Sold + active listings by location | The workhorse. See contract below |
| `/property-details-address` | Subject property lookup | Param `address` = full one-line address |

## Endpoints that are DEAD (retired, return 404)

`/similar-sale-homes`, `/similar-sold-homes`, `/similar-homes`, `/similar-properties`, `/comps`, `/sold-homes`. Do not waste calls rediscovering this.

## /search contract

- `location`: e.g. `"Knoxville, TN 37914"` (zip-level is the sweet spot)
- `home_status`: EXACT enum `RECENTLY_SOLD` or `FOR_SALE`. Any other casing (`SOLD`, `RecentlySold`) returns HTTP 400
- `min_price` / `max_price`: honored. `price_min` / `price_max`: SILENTLY IGNORED
- `listing_type`: defaults `BY_AGENT`. `BY_OWNER_AND_OTHER` returns 0 for sold history
- Every response echoes accepted params in `parameters`. ALWAYS check the echo: a param missing from the echo was ignored and your filter did not apply

## The 41-row cap (the big trap)

Every search returns at most 41 rows and `totalPages` is always 1. A bare RECENTLY_SOLD query = the ~41 most recent sales (about 5 weeks in an active zip). To reach back 12-24 months: partition by `min_price`/`max_price` bands and recursively split any band that returns >= 41 rows. Eight starting bands from $1K to $3M recovers 2-3 years of a typical county zip (roughly 50-80 API calls).

## Response field gotchas

- `dateSold` is epoch MILLISECONDS
- `soldPrice` is a display string ("$92,500"); use `unformattedPrice`
- `homeType` can be `LOT` for a house sold at land value, and new-construction sales may carry no sqft/beds. Verify surprises against the county card before using them as comps
- `zestimate` and `rentZestimate` ride along on most rows (fuel for condition bucketing and rental math)
- Occasional 500s under banding load: retry with backoff, and expect a few single-band misses to be harmless (the band re-splits catch most rows)

## What the API cannot see

MLS activity only. Tax sales, trustee auctions, wholesale assignments, and other off-market transfers never appear. County records (assessor card + register of deeds) are the source of truth for those, including the subject's own purchase history.
