# Enformion / Endato Person Search — API Reference

The grounded heir-finder behind the Primary Path. One Person Search on a deceased
owner returns their relatives graph (the heir set), date of death, addresses, and
phones in a single call — no obituary parsing, no guessing. Schema below verified
against live responses (June 2026).

> **Grounding rule:** every name, date, address, and phone in your deliverable
> must come from a response you actually retrieved. Enformion is the source of
> truth for the relationship graph; do not infer relatives it did not return, and
> do not assert a relationship the record did not label.

## Endpoint & Auth

- **Endpoint:** `POST https://devapi.enformion.com/PersonSearch`
- **Headers:**
  - `galaxy-ap-name: <your access-profile name>`
  - `galaxy-ap-password: <your access-profile password>`
  - `galaxy-search-type: Person`
  - `Content-Type: application/json`
  - `Accept: application/json`
- Credentials are the **API access profile** (Enformion Console → API), NOT your website login. Keep them in environment variables / a secrets store — never hardcode them in a shared skill or repo.

## Minimum search criteria

Enformion rejects under-specified searches. Use ONE of these combos:

| Goal | Provide | Notes |
|------|---------|-------|
| Find the deceased + heir graph | Name **+ address (with ZIP)** | Anchor to the property or last-known address |
| Resolve a named signer | Name **+ DOB year** | Name + city alone is REJECTED as insufficient |

### Request — deceased (Step A)
```json
{
  "FirstName": "Jane",
  "LastName": "Doe",
  "Addresses": [{ "AddressLine2": "Knoxville, TN 37918" }],
  "Page": 1,
  "ResultsPerPage": 5
}
```
`AddressLine2` follows Enformion's "City, ST ZIP" convention.

### Request — signer (Step C)
```json
{ "FirstName": "John", "LastName": "Doe", "Dob": "1961" }
```
Same name + same age can return multiple people. Disambiguate by preferring the
candidate whose address history overlaps the family area (property ZIP / city /
street tokens from Step A).

## Response schema

Top level: **`persons[]`** (also seen as `people` / `results` defensively). Each person:

| Field | Meaning |
|-------|---------|
| `name` | `{ firstName, middleName, lastName, rawNames }` |
| `age`, `dob` | Age and date of birth (day is often masked, e.g. `9/XX/1955`) |
| `dod`, `datesOfDeath[].dod` | Date of death (may be masked/partial, or nested in a dict) |
| `addresses[]` | Address history; use `fullAddress`, most-recent first |
| `phoneNumbers[]` | `{ phoneNumber, phoneType, isConnected, lastReportedDate }` |
| `relativesSummary[]` | **The heir graph** (see below), up to ~50 entries |
| `associatesSummary[]` | Non-family associates (neighbors, co-owners) |
| `isCurrentPropertyOwner` | Whether this person currently owns property |

### `relativesSummary[]` — the heir graph

Each entry:

| Field | Meaning |
|-------|---------|
| `firstName` / `middleName` / `lastName` | The relative's name |
| `relativeType` | The actual label: `Son`, `Daughter`, `Brother`, `Sister`, `Spouse`, `Mother`, `Father`, ... **Use this to classify.** May be blank. |
| `relativeLevel` | Kinship tier: **`ab` = closest kin**, `ac` = grandchildren, `ad` = cousins, `ae` = in-laws / distant. (Sort `ab` first.) |
| `dob` | Birth date, day usually masked (`9/XX/1955`) — regex out the **year** |
| `isDeceased` | Boolean (or `"true"`/`"yes"` string) |
| `score` | Match strength; higher = closer/more confident |

**Ranking:** sort by `relativeLevel` (ab → ae), then `score` descending, living before deceased.

## Critical gotchas

1. **Failure detection by HTTP status, not `error`.** Enformion returns an `error`
   object `{ inputErrors: [], warnings: [] }` on EVERY response, including
   successes. A 200 with a populated `error` is still a success. Treat non-200 as
   failure; do not branch on the presence of `error`.

2. **`relativeLevel "ab"` is CLOSEST KIN, not "children."** It includes the
   surviving spouse, siblings, and parents. Always read `relativeType` to tell
   them apart. If `relativeType` is blank, do NOT assume "child" from a surname
   match — a widow usually shares the surname, and so does a sibling (birth
   surname). Mark such an entry UNVERIFIED and confirm before treating it as a
   required signer.

3. **Surname match = whole last-name token.** When you must fall back to a surname
   check, compare the relative's final name token to the decedent surname — not a
   raw `endswith` substring. ("Maxwell" must not match surname "Well".)

4. **Masked / partial / dict dates.** `dob` and `dod` may arrive as `9/XX/1955`,
   `3/XX/2026`, or `{ "year": "2019" }`. Recover at least the **year** — the DOD
   conflict check is year-level, so a recovered year is enough.

5. **DOD conflict (death-index vs obituary).** Enformion's `dod` can disagree with
   the obituary/filing date by years. Common cause: the original owner died long
   ago, a family member maintained the home, and *that* person's recent death
   triggered the new filing. The heir set (the children) usually stands either way,
   but the estate currently in probate may belong to the recent decedent. **Surface
   the conflict in the deliverable; never silently pick one.**

## Cost model

| Stage | Call | Unit | Typical (5 signers) |
|-------|------|------|---------------------|
| Find heirs | Person Search (deceased) | ~$0.35/match | $0.35 |
| Resolve signers | Person Search × signers | ~$0.35/match | $1.75 |
| Emails (optional) | Contact Enrichment / Tracerfy | $0.25 / ~$0.10 | $0.50-1.25 |
| Phone scoring | Trestle × unique phones | ~$0.015 | ~$0.28 |

**≈ $2.30-3.00 per record.** Billing is **per match — misses are free.** The two
cost levers: **signer-gating** (only Person-Search the living closest-kin children)
and **phone dedupe** (score each unique number once). A free trial is typically
~100 searches; make exactly one search per person and never loop.

## Minimal client (Python, `requests` only)

```python
import os, requests

def person_search(first, last, *, city="", state="", zip_code="", dob_year=""):
    body = {"FirstName": first, "LastName": last, "Page": 1, "ResultsPerPage": 5}
    addr2 = " ".join(p for p in [f"{city}," if city else "", state, zip_code] if p).strip()
    if addr2:
        body["Addresses"] = [{"AddressLine2": addr2}]
    if dob_year:
        body["Dob"] = str(dob_year)
    r = requests.post(
        "https://devapi.enformion.com/PersonSearch",
        headers={
            "galaxy-ap-name": os.environ["ENFORMION_AP_NAME"],
            "galaxy-ap-password": os.environ["ENFORMION_AP_PASSWORD"],
            "galaxy-search-type": "Person",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body, timeout=45,
    )
    if r.status_code != 200:        # failure = HTTP status, NOT the `error` object
        return {}
    return r.json()
```

See `scripts/enformion_person_search.py` in this skill for the full A-E waterfall
(decedent → required signers → per-signer → dedupe → Trestle scoring).
