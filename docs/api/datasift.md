# DataSift (REISift)

The CRM everything lands in. It has **two API surfaces with different powers**,
and picking the wrong one is the single most common blocker here.

- **Domain:** `app.reisift.io`. API at `apiv2.reisift.io`. Not `app.datasift.ai`
- **Used by:** `kpi-engine`, `sequential-presets`, `sift-sequences`,
  `sift-market-research`, the upload stack

## Which credential can do what

| | Open API key | User JWT |
|---|---|---|
| Read properties, lists, tags | yes | yes |
| Write properties, upsert | yes | yes |
| **Custom fields** | **no, they do not exist in its 93 routes** | yes |
| Activity log for KPIs | limited | yes |
| Expiry | never | about 48 hours |

**If you need custom fields, you need the minted JWT.** The Open API key
returns 401 on every custom-field write, and the routes are not merely
forbidden, they are absent from its surface entirely. That distinction matters
because a 404 reads like a wrong URL rather than a permissions problem.

### Mint the JWT, never paste one

```
POST /api/token/
  {"email": "...", "password": "..."}
  -> {"access": "...", "refresh": "..."}
```

Mint from credentials at start of run and **re-mint every 30 minutes**. A long
upload that pastes a token dies partway through when it expires, usually after
it has already written half the records.

This is also why `kpi-engine` needs no special access: it mints from your own
login like any other user.

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

## No REST API for the UI work

Presets, sequences and SiftMap have no API. Those skills drive the web app with
Playwright, which is why they need your login rather than a key.

If you would rather not automate, [build them by
hand](../setup/no-api-playbook.md#presets-by-hand): the skills carry all 21
preset and 26 sequence definitions as data, and the definition is the valuable
part.
