# Entity owners: Enformion BusinessV2 (the one Enformion call v5 keeps)

v5 retired the Enformion **person** search. **BusinessV2 stays**, because nothing else
in the stack can do this job.

## Why it survives

SmartSkip requires a first and last name. An LLC, trust or estate returns nothing at all.
Tracerfy is consumer-only. So an entity-owned property is a hard stop for the primary path.

This is not a rare edge case. On a live vacant pull, **35 of 321 owners (11%) were
entities**: LLCs, family trusts and estates.

## Endpoint (verified live)

```
POST https://devapi.enformion.com/BusinessV2Search
header: galaxy-search-type: BusinessV2
```

Two hard-won facts about account access:
- The v1 `BusinessSearch` type is **access-denied** on this account.
- `AddressSearch` is **unlicensed** on this account.

Only BusinessV2 works. Do not spend a debugging cycle on the other two.

## What it returns

Human officers pulled from `usCorpFilings` and `newBusinessFilings`.

`find_principals(entity_name, city_state)` filters out:
- entity self-references (the LLC listing itself as its own officer)
- **commercial registered-agent fronts** (Northwest Registered Agent, US Corp Agents and
  similar), which otherwise present as the "principal" and are dead ends

## The reverse-address unmask (the Harper move)

When an LLC's mailing address is a **residence** rather than an office, reverse-look-up
that address through the SiftMap owner endpoint and take the human owner as the principal.
This resolved real principals on a live sweep where the corporate filing gave nothing usable.

Order of attack for an entity owner:
1. BusinessV2 officers.
2. If that yields only agents or nothing, reverse-look-up the mailing address.
3. If the mailing address is commercial, fall back to the Secretary of State filing.

## Cost

About **$0.10 per search**, billed per match; misses are free. At 11% of a batch this is a
small, bounded line item, unlike the per-relative bleed that got the person search retired.

## Once you have the human

Feed the resolved principal's name and address back into the **normal v5 flow**: SmartSkip
that person for relatives and phones, then Trestle-score. The entity lookup exists only to
turn an entity into a name; everything downstream is unchanged.
