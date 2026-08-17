# Skip trace: SmartSkip, Tracerfy, Enformion

Three providers with different jobs. Using the wrong one for a job is the most
common and most expensive mistake here.

| Provider | Good at | Cost | Do not use for |
|---|---|---|---|
| SmartSkip | Relatives **with their phone numbers**, in one batch | $0.15 per hit | Death data. It is wrong about death |
| Tracerfy | Cheap gap fill on a known person | $0.02 per record | Entities. Consumer only |
| Enformion | LLC and trust principals | about $0.10 per match | The relatives graph. It whiffs half the time |

## The measured comparison that set this order

Twelve owners, same records, both providers, 2026-07-29:

- **Coverage.** Enformion returned zero relatives on **6 of 12**. SmartSkip
  returned relatives on 12 of 12.
- **Phones.** Enformion's `relativesSummary` carries names but **no phone
  numbers**, so every relative you actually want to call is another billed
  search. SmartSkip returns relatives and their phones in the same row.
- **Cost.** 100 owners, 682 relatives: **$15.90** the SmartSkip way,
  **$78.20** the Enformion way. About 4.9x.
- **Precision.** On the validation record SmartSkip returned exactly three
  relatives and all three appeared in the published obituary. Enformion
  returned a capped 50-name blob with out-of-state numbers that looked like
  wrong-person bleed.

That is why the heir engine is SmartSkip and Enformion is kept only for
entities.

---

## SmartSkip

Bulk skip trace. Fully drivable over the API.

### The flow, and where the money is

```
upload      free
map columns free
calculate   free      <- returns the price, still costs nothing
payment-intent        <- THIS is the only billing call
poll + download
```

Everything up to `payment-intent` is free, so you can price a batch exactly
before committing to it.

### Three traps

**The wallet does not pay for bulk skip.** It bills the saved card via
`payment-intent`. We had $25 sitting untouched in the wallet while a batch
charged the card. Funding the wallet does not prepay a batch.

**Unpaid orders are invisible.** `GET /bulk-skip` does not list an order that
has not been paid for. Persist the `bulkSkipId` the moment you get it, before
paying, or you will lose track of a batch that exists but cannot be found.

**Entities cannot be name-traced.** It needs a First and a Last. On one vacant
owner list, **35 of 321** were LLCs or trusts. Filter them out before the batch
and route them to Enformion, or you pay for guaranteed misses.

### It is wrong about death

`Deceased` came back `false` for a man who died 12/06/2025 with a published
funeral home obituary. There is **no date-of-death column at all**.

Death data comes from the obituary and web research pass. Always. Do not let a
`Deceased` flag from here decide whether a record is an heir case.

### Relationship labels are coarse

The column is literally "Possible Type". On a 100-record batch, **63 percent**
came back generic: "Relative" or "In-Law". It labelled a husband of 62 years a
plain "Relative".

Treat the label as a hint and let the obituary overwrite it. The obituary was
written by someone who knew the family.

---

## Tracerfy

Cheap batch gap fill. Use it for relatives SmartSkip named but left phoneless,
which is roughly 7 percent.

### Contract

Multipart CSV upload with a Bearer token.

**The `mail_*` columns are required or the request 400s.** Even when you have
no mailing address, the columns have to be present. Send them empty rather than
omitting them.

### Phones come back flat, not as an array

This surprises everyone once. There is no `phones` list. The fields are flat:

```
primary_phone
mobile_1, mobile_2, ...
landline_1, landline_2, ...
```

Parse the flat names. Code that looks for `phones[]` finds nothing and reports
zero results on a batch that worked.

### The deceased flag lives on the other endpoint

The `deceased` boolean is on the **instant lookup** `persons[]` response, not
on the $0.02 batch path. Batch gives you no death signal. There is no date of
death on either.

---

## Enformion / Endato

Kept for one job: resolving the humans behind an LLC or a trust.

### Business search

```
POST devapi.enformion.com/BusinessV2Search
  header galaxy-search-type: BusinessV2
```

**The v1 `BusinessSearch` type is access-denied** on this account tier, and
`AddressSearch` is unlicensed. Both return failures that read like bugs rather
than entitlement problems, which is a trap worth knowing before you spend an
afternoon on it.

Principals come from `usCorpFilings` and `newBusinessFilings`. Filter out
entity self-references and commercial registered agent fronts, or you get
"Northwest Registered Agent" as your decision maker.

### Person search, if you use it at all

**Anchor on the full street line for common names.** A name plus city and ZIP
returned the wrong person as `persons[0]`: an Alabama "James B Key" for a
Knoxville "James G Key". Only

```json
"Addresses": [{"AddressLine1": "7619 Trey Oaks Ln",
               "AddressLine2": "Knoxville, TN 37918"}]
```

pinned the right record. `enformion_heir.person_search()` sends only
`AddressLine2` by default, so pass the street line yourself on common names.

**Detect failure by HTTP status, not by the `error` object.** That object is
always present, including on success.

### Its relatives graph is not trustworthy

- `relativesSummary[].isDeceased` **lags and is wrong**. It showed a decedent,
  his late wife and two long-deceased sons as living.
- The graph is **capped at about 50** and truncates silently.
- It **misses married-out daughters**, because they carry a different surname,
  and they are frequently required signers.

If you must use it, reconcile against the published obituary rather than
trusting the graph.

---

## Cost per record, end to end

The v5 chain: SmartSkip $0.15, Tracerfy gap fill about $0.02, obituary and web
research free, Trestle scoring about $0.015 per number.

**About $0.24 per record.** The retired Enformion-first path was about $1.18.

## Without any of them

[Heir research by hand](../setup/no-api-playbook.md#heir-research-by-hand). It
takes 20 to 40 minutes a record and the obituary step is better than anything
you can buy.
