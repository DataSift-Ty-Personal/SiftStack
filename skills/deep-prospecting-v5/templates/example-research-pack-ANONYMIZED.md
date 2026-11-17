# Deep Prospecting Research Pack (worked example, anonymized)

> Teaching example. Every name, address and phone below is fabricated. The *shape*
> is real: this is the v5 flow run end to end on a live record where the research
> layer changed what the record meant. Never ship a pack containing a real
> homeowner's details anywhere public.

## [Street Address], [City], [State] [ZIP] ([County] County)

Prepared [DATE]. Flow: SmartSkip + TrestleIQ + published obituary. No Enformion person search.

## Headline

This record is NOT a deceased-owner heir case, and the CRM currently implies it is.
The obituary attached to it belongs to the owner's HUSBAND. The owner is alive and
is the decision maker.

- [Spouse Name], born [DOB], died [DOD] at home, age 84.
- [Owner Name], the owner of record, is his surviving wife and is LIVING.
- Married 62 years. Widowed roughly 8 months.

Call the owner. Do not ask for the deceased spouse.

> **This is the single highest-value check in the skill.** An obituary on a record
> does not mean the OWNER died. Match the decedent's name against the owner of
> record before doing any heir work.

## Family (obituary-confirmed)

| Person | Relationship | Age | Location |
|---|---|---|---|
| [Owner Name] | Owner, living, surviving spouse | n/a | subject property |
| [Spouse Name] | Husband, DECEASED [DOD] | 84 | subject property |
| [Daughter Name] | Daughter | 61 | nearby town |
| [Son-in-law Name] | Son-in-law | 66 | nearby town |
| [Grandchild names] | Grandchildren | n/a | not traced |

Every person SmartSkip returned appeared in the published obituary. Precision 3 of 3.

Note what SmartSkip got wrong on its own: it flagged the deceased spouse
`Deceased = false` and labeled a husband of 62 years a generic "Relative."
The obituary corrected both. That is why Step C is mandatory.

## Master dial sheet

```
TIER          NUMBER          TYPE       WHO                     NOTE
Dial Second   [xxx-xxx-xxxx]  Landline   [Owner] (OWNER)         household line, start here
Dial First    [xxx-xxx-xxxx]  Mobile     [Daughter]              score 100
Dial First    [xxx-xxx-xxxx]  Mobile     [Son-in-law]            score 100
Dial Fourth   [xxx-xxx-xxxx]  Landline   [Daughter household]    score 30
Drop          [xxx-xxx-xxxx]  Mobile     [Deceased spouse]       DECEASED, do not dial
Suspect       [xxx-xxx-xxxx]  Landline   unverified              out-of-state, bulk-data
                                                                 carryover, not corroborated
```

Two rules visible here:
- The owner's own household line carries **source and tier only, no relationship tag**,
  even though the deceased spouse shared it.
- A pre-existing bulk number that the skip trace did not corroborate is flagged
  **suspect**, not promoted.

## Situation

- Owned since 1987, purchased for $42,000. Estimated value $283,000. Equity 64.9%.
- 988 sqft, 1 bath, built 1952.
- Lists: Tax Delinquent, Senior Homeowners, Owner Occupied, High Equity.

Senior widow, 38 years of tenure, tax delinquency, very high equity. Motivation is
plausible but this is a sensitive call. Do not open with the house.

## Title

Verify at the county Register of Deeds whether the deceased spouse was on the deed.
Joint with right of survivorship passes to the survivor automatically. Sole ownership
or tenancy in common requires an estate step before closing. Confirm before writing
a contract.

## Cost and sources

| Step | Source | Cost |
|---|---|---|
| Relatives and phones | SmartSkip bulk skip | $0.15 |
| Gap-fill for phoneless relatives | Tracerfy | $0.00 (no gaps on this record) |
| Dial tiers and line types | TrestleIQ, 6 numbers | $0.09 |
| Date of death, relationship truth | Published obituary, web research | $0.00 |
| **Total** | | **$0.24** |

Enformion equivalent on this record: 1 owner search plus 50 relative searches to reach
the same phone coverage, about $5.10, and it returned no date of death.
