# Confidence, provenance and the review queue

## Every class carries where it came from

| Provenance | Meaning | Confidence |
|---|---|---|
| `sourced` | a built-in table (FA national codes, the FA description index, LBCS) | the table's value, up to 0.95 |
| `learned-human` | a person labelled this code | up to 0.9 |
| `learned-claude` | Claude labelled this code | up to 0.75 |
| `learned-keyword` | the keyword classifier guessed from the description | up to 0.6 |
| `derived` | a rule (size ladder, vacant, units, fuel brand, business name) | 0.5 to 0.85 |
| `imputed` | a business inside the parcel polygon corrected a weak label | 0.8 |
| `verified` | a review verdict (overrides.csv) | 0.9 |

Two independent signals that agree lift a non-sourced label by 0.1. A coarse signal (property
class, zoning) never counts for more than 0.5, so a specific use code always wins over it.

## The method column

`tier0-xw:<system>` table, `tier0-learned:<system>` learned label, `tier1-keyword:<system>`
keyword, `tier1-sc-size` shopping-center ladder, `tier1-mall-outparcel`, `tier1-fuel-keyword`,
`tier1-vacant` no building and no improvement value, `tier1-res-units` units decided the class,
`tier1-stories-correction` an industrial label on a 3+ story building became Office,
`tier1-condo-*` the FA commercial-condo rule, `+poi-confirm`, `tier3c-<source>-correct`,
`override`.

## Business points

A property-level business (gas station, hotel, self-storage, church, hospital, car dealer and
similar) inside a parcel:
- agrees with the class: confidence rises to 0.85, and a blank sub-type is filled in
- disagrees, the label is weak (under 0.70) and not from a table or a person: the parcel is
  relabelled (`imputed`)
- disagrees otherwise: it goes to the review queue with the business named

Tenant-level businesses (a restaurant inside a mall) are listed in `cc_businesses` and never
decide anything. Without polygons, a point within 40 m of the parcel can confirm or queue, never
correct.

## Review queue reasons, by weight

| Weight | Reason |
|---|---|
| 4 | a business inside the parcel disagrees |
| 3 | confidence under 0.55, or two confident signals disagree |
| 2 | value implausible for the building size, industrial code on a 3+ story building, a nearby business disagrees |
| 1 | spot check: the three most valuable parcels behind each Claude-learned label |

Sorted by weight times the log of the value, so the expensive mistakes come first. Parcels with
no use signal at all are not queued (there is nothing to review); `qa_report.json` counts them.

## Value bands

Recent arm's-length sale, else market or assessed value times that county and class's median
sale-to-value ratio, else the source's own estimate, else a class median per acre or per square
foot. Half the sales calibrate and the other half are scored; `qa_report.json` gives exact and
within-one-band accuracy. Measured: Knox County TN 46% exact / 91% within one band; Licking County
OH 72% / 97%.

## Owner match tiers

| Tier | Meaning |
|---|---|
| `single` | one parcel; nothing was matched, so nothing is scored |
| `green` | two signals agree across 2+ parcels: one name at one mailing address (95), or name variants merged by a shared address or a co-owner (90) |
| `grey` | one name with scattered mailing addresses (80 business, 65 person), or no mailing data |

An address behind many unrelated names (a bank, a servicer, a property manager) never links
them. Groups join owners that share a mailing address; they are leads, not proven corporate
parents. Out of state compares each parcel's mailing state to its own situs state: Y, N, or mixed.

## The QA report

`out/qa_report.json` has, per county: parcels, share at 0.7+ and 0.55+ confidence, the review
band, class and method counts, value and grade coverage, and the owner green rate among
multi-parcel owners. It also lists the profile and mapping, the class signals the file did not
have, every phase's stats, and `skipped`: every phase that did not run, with the reason.
