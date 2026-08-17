# API contracts

What each API actually does, as opposed to what its documentation says.

Every file here records a contract that was verified against the live service,
plus the traps that cost real time to find. Most of these are not in any
vendor doc, and several are things the vendor doc states incorrectly.

Read the file before writing code against a service. It is faster than
rediscovering the same trap.

## Why this exists

Three failure patterns show up repeatedly across these providers, and they are
worth recognising as a class:

1. **The silent parameter.** You pass a filter, the API accepts the request,
   returns 200, and ignores the filter. Nothing tells you. The fix is always
   the same: read back the echoed parameters in the response and assert your
   filter actually applied.
2. **The empty success.** You are out of credit, or unlicensed for that search
   type, or the record does not exist, and the response is a 200 with an empty
   array. A run that succeeds with no data is worse than one that fails.
3. **The moved contract.** An endpoint is retired, or the gate changes from one
   provider to another, and the old call keeps returning something plausible.
   Every solve gets billed and discarded.

Where a file records a specific instance of one of these, it says so.

## The files

| File | Covers | Used by |
|---|---|---|
| [openwebninja-zillow.md](openwebninja-zillow.md) | Property search, sold comps, listing data | `comp-package`, `deal-analyzer` |
| [trestle.md](trestle.md) | Phone intelligence and scoring | `phone-validator` |
| [skip-trace.md](skip-trace.md) | SmartSkip, Tracerfy, Enformion | `deep-prospecting-v5` |
| [datasift.md](datasift.md) | DataSift / REISift, both API surfaces | `kpi-engine`, uploads, the CRM stack |
| [smrtphone.md](smrtphone.md) | SMS send, call logs, recordings, webhooks | the coach skills, the SMS agent |
| [county-data.md](county-data.md) | Assessor, recorder and court portals | `first-market-county-data`, `probate-property-finder` |
| [scraping-infra.md](scraping-infra.md) | Scrapfly, Apify proxy, 2Captcha, Turnstile | the acquisition pipeline |

## Conventions

- **No credentials anywhere.** Every example uses a placeholder. If you find a
  real key in this directory, that is a bug, and CI is supposed to catch it.
- **Verified means verified.** A line that says "verified" was run against the
  live service on the stated date. Anything inferred says so.
- **Costs are what we were actually billed**, not list price. Where a rate is
  an affiliate or negotiated rate, it says which.

## If a contract has moved

These services change. When a call in here stops matching reality, fix the file
in the same commit as the code, and record what the old behaviour was. The
history of a contract is often more useful than its current state, because it
tells you what kind of change this vendor makes.
