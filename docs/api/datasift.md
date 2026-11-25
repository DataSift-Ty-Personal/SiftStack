# DataSift (REISift)

The CRM everything lands in. DataSift publishes its API at
[developers.datasift.ai](https://developers.datasift.ai), and every skill here
reaches it the same way: your Open API key.

- **Domain:** `app.reisift.io`. API at `apiv2.reisift.io`. Not `app.datasift.ai`
- **Used by:** `kpi-engine`, `sequential-presets`, `sift-sequences`,
  `sift-market-research`, `account-blueprint`, `mls-offer-engine`,
  `phone-validator`, the upload stack

## The credential: your Open API key

Send `Authorization: Api-Key <key>` on every request, with the key read from
`REISIFT_API_KEY`. An account admin creates the key in DataSift; it never
expires and carries the permissions of the user it belongs to. It is the only
DataSift credential this library uses.

| | On the Open API key |
|---|---|
| Read records, lists, tags, statuses, presets | yes |
| The account activity log (calls, texts, statuses) | yes: `GET /api/internal/property/logs/`, filtered by `any_event_type` |
| Create and update records, tags, lists, owner phones | yes |
| Custom fields: values, fields, groups, options | yes |
| Task presets, preset folders and presets, SiftMap presets | yes |
| Sequences, custom statuses, boards and board columns | no published route: built in the app |
| Who the key belongs to | `GET /api/internal/user/` (the key's own user) |

**Limits, per account and shared by every key-based tool on it:** 120 requests
a minute and 5,000 an hour; stay near 80 a minute. A 429 says "available in N
seconds" or sends `Retry-After`: wait that long. Bulk jobs want 20 or more
records each.

## Upload contract, and four traps that fail quietly

`POST /property/` is **upsert by address**, so re-runs never duplicate. Lists
**accumulate** rather than replace: a record came back carrying both new lists
plus four it already had.

### 1. Tags must be an array

A comma string does not split. It creates one tag literally named
`"Courthouse Data, code_violation, Knox"`.

```json
"tags": ["Courthouse Data", "code_violation", "Knox"]
```

### 2. A select field's value is the option's UUID, not its label

```
"LEN"  ->  {"non_field_errors": ["'LEN' is not a valid UUID."]}
```

Resolve the label to its UUID from `custom-fields/` `options[]` first. Creating
a `select` custom field **requires its options in the same POST**; you cannot
add them afterwards.

### 3. An entity owner cannot have a blank first_name

The API rejects `""`. Send the business as `company` and **omit** the person
keys entirely. Omitting a key is not the same as sending an empty string, and
this is the difference between a 200 and a 400.

### 4. `notes` on the property payload returns 200 and is discarded

It looks like it worked. It did not. Post notes separately.

### Custom field writes

```
PATCH /api/internal/property/{uuid}/custom-field/update-values/
  [{"field_uuid": "...", "value": "..."}]
```

## The habit that catches all four

**Upload one record and read it back before releasing the file.**

That single habit caught the tag format, the entity rejection, the option-UUID
requirement, and a list-name mismatch that would have silently attached nothing
for 2,512 of 2,573 records. Every one of those returns a success status.

## Rate limiting on /api/internal

It throttles hard. Six threads at about 7 requests per second **429'd 529 of
740 records**. Single-threaded at about 2 per second, with backoff that reads
the server's "available in N seconds" hint, completed cleanly.

Make long pulls resumable and checkpoint every 25 records. You will hit this.

## Reading numbers off records

Field names on a record are **not** the CSV upload column names. Verified live:

```
estimate_value, equity_percent, last_sold, last_sale_price, rental_value,
sqft, bedrooms, bathrooms, year, lot_size, parcel_id, apn, investor_score,
structure_type, assigned_to, address{...}, owner{...}
```

`assigned_to` is a bare uuid, not an object. County and coordinates live on
`address`. Mailing address lives on `owner.address`.

**`Total Delinquency` is liens plus taxes.** One record reads 13,766.72 =
12,908.72 lien + 858.00 tax. Reading it as the tax figure double counts the
lien and inflates exactly the records a distress model is built to surface. Tax
amount comes from `Tax delinquency amount` or native `tax_delinquent_value`,
never from Total Delinquency.

## Status reads

`GET` property status needs `limit=1000` or you get a truncated page that looks
like a complete answer. The count index lags behind writes, so do not verify a
write by checking a count.

## Presets, sequences and SiftMap presets DO have an API

Earlier versions of this page said they did not. They live under `/api/internal/`
(`filter-preset-folder`, `filter-preset`, `task-group`, `custom-fields`) and
`map.reisift.io/filters/`, and they answer to your Open API key. Sequences, custom
statuses, boards and board columns have no published create route, so those are built in
the app. The `account-blueprint` skill (`python src/clone_account.py`) exports a whole
account to a title-keyed JSON and applies it to another account with read-back
verification on every object it creates, plus a click-through checklist for the rest.
Contracts that bite: the folder preset listing defaults to 10 rows (always `?limit=999`),
preset titles are unique per account, a saved preset's relative date windows are rejected
by the records search, and `/api/internal/account/user/` returns a flat array rather than
`{results}`.

If you would rather not automate, [build them by
hand](../setup/no-api-playbook.md#presets-by-hand): the skills carry all 21
preset and 26 sequence definitions as data, and the definition is the valuable
part.
