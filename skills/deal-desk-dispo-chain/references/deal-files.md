# Deal files

The files the deal desk chain writes and reads, with the shapes the scripts expect. Every file
lives in the deal's folder, `output/deal-desk-dispo-chain/<slug>/`, unless a line says otherwise.

## The boundary (`polygon.json`)

The comp area the house really competes in, traced off a map as `[lat, lon]` pairs in order around
the edge. `post_walkthrough.py --polygon` reads this file (it also takes the same points as one
string, `"lat,lon lat,lon ..."`).

```json
[[35.9301, -83.9402], [35.9356, -83.9390], [35.9349, -83.9311], [35.9297, -83.9318]]
```

A pocket is bounded by roads, highways and rail, so a polygon beats a box. After the run, print
every street that landed inside and compare it with the map before you trust the ARV.

## The walk (`walk.json`)

Start from the template (`python src/post_walkthrough.py --walkthrough-template` writes
`walkthrough_template.json` in the folder you run it from), then change it to this shape. Anything
filled in here overrides the DataSift record, so leave a field empty unless the walk proved the
record wrong.

```json
{
  "walk_date": "2026-11-24",
  "media": "22 photos and 2 videos",
  "target_config": {"beds": 3, "baths": 1},
  "reconfig_verified": false,
  "single_scenario": {"key": "reno", "label": "Comp-match reno (3 bed / 1 bath)",
                      "tier": 2, "scope": "full", "gut": false,
                      "beds": 3, "baths": 1, "drop": []},
  "comp_finish_basis": "Renovated sales inside the boundary carry LVP, painted cabinets and a refreshed bath. None added a bath.",
  "work_done": [],
  "flags": [
    {"item": "Roof replacement", "cost": 9000, "placeholder": true, "scenarios": [],
     "note": "Placeholder until the roofer's bid"},
    {"item": "Crawl moisture barrier and sump", "cost": 4000, "scenarios": []}
  ],
  "gates": ["Plumber confirms the drain line before the contract goes hard"],
  "bids": {}
}
```

The rules this shape carries, each one a silent failure when broken:

- **The layout that exists.** `target_config` sets the ARV and `single_scenario.beds` / `.baths`
  set the rehab. They run on separate paths and nothing cross-checks them, so both carry the
  house's own beds and baths, and `reconfig_verified` stays `false`. The template ships a 3 bed /
  2 bath `target_config`: change it before the first run.
- **One number.** `single_scenario` collapses the rehab to one plan. Never ship the four-scenario
  menu.
- **Every flag's `scenarios` is `[]`.** A flag still keyed to another scenario (`["mid", "gut_t2"]`)
  is dropped without a word. An empty list keeps it.
- **Placeholders, never blanks.** A quote that has not come back gets a realistic cost and
  `"placeholder": true`. It prints red, and the total stays a true number.
- **A signed bid collapses the range.** When a contractor signs, put the whole-scope figure under
  the scenario key: `"bids": {"reno": 52000}`. Then run the offer again (step 4).
- **What the comps sold with is the spec.** Write the finish evidence into `comp_finish_basis`,
  naming the comps and their prices.

## The deal sheet (`deal_sheet.json`)

What the blast is allowed to say. It never holds the contract price or a lockbox code, so neither
can leak into a message.

```json
{"deal_id": "example-rd-1", "road": "Example Rd", "area": "the north side of town",
 "price": 135000, "beds": 3, "baths": 1, "sqft": 1200, "disclose_address": false}
```

- `road` is the road name only. It never starts with a house number and never holds a ZIP.
- `price` is the asking price to buyers, a whole number. It is the only figure a message may name.
- Disclosing the address is a per-deal choice: set `"disclose_address": true` and add
  `"address": "123 Example Rd, <City>, <ST> <ZIP>"` (a house number and a ZIP are then required).

SiftStack's own texting app reads this same shape (`src/sms_agent/dispo_campaign.py --deal`).

## The cohort input (`cohort_input.json`)

`dispo-deal-blast/scripts/cohort.py` wants one row per buyer:
`name, price_min, price_max, last_buy, n_buys, phone`. Build it from the contact run (step 7),
which holds each buyer's purchases near the house (`contacts/ranked.json`) and every number checked
against the decision maker (`contacts/dial_sheet.csv`). A number is textable only when it is a
mobile, scored Dial First or Dial Second, matched to the decision maker by name, and not flagged as
a litigator.

```python
import csv
import json
from pathlib import Path

deal = Path("output/deal-desk-dispo-chain/<slug>")
ranked = json.loads((deal / "contacts" / "ranked.json").read_text(encoding="utf-8"))["ranked"]

best = {}
with open(deal / "contacts" / "dial_sheet.csv", newline="", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        ok = (row["dm_match"] == "Yes" and row["litigator"] != "YES"
              and row["tier"] in ("Dial First", "Dial Second")
              and "mobile" in (row["line"] or "").lower())
        if ok and row["rank"] not in best:
            best[row["rank"]] = row["phone"]

rows = []
for r in ranked:
    prices = [h["price"] for h in r.get("local") or [] if h.get("price")]
    dates = [h["date"] for h in r.get("local") or [] if h.get("date")]
    rows.append({"name": r["buyer"],
                 "price_min": min(prices) if prices else None,
                 "price_max": max(prices) if prices else None,
                 "last_buy": max(dates) if dates else None,
                 "n_buys": r.get("lifetime_buys"),
                 "phone": best.get(str(r["top"]), "")})

(deal / "cohort_input.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
print(len(rows), "buyers,", sum(1 for x in rows if x["phone"]), "with a textable number")
```

A buyer with no textable number stays in the file: the cohort counts them, and they are a call,
not a text. A buyer with no priced purchase is kept as "band unknown", on purpose: dropping the
unknown is how a cohort quietly shrinks.

## The buyer book (`output/deal-desk-dispo-chain/buyer_book.csv`)

One row per buyer, kept across deals. Dedupe on the buyer's mailing address, but only merge two
names at an address that carries a suite or unit when the names match too: unrelated companies
share a registered agent's suite.

| Column | What goes in it |
| --- | --- |
| buyer | the buyer's name as the deed writes it |
| mailing_address | the deed's mailing address |
| decision_maker | the person named on a page that names both the person and the company |
| phone | the number they answered on, or the best checked number |
| status | verified, silent, no, wrong, optout |
| price_min, price_max | their real purchase range near your deals |
| last_buy | their latest purchase date |
| deals_offered | the deal ids they were texted |
| last_reply | the date and what they asked for (address, photos, walkthrough, a price) |

Status comes from what people typed, never from a hand-kept list:

| Status | Reply looks like | What happens |
| --- | --- | --- |
| optout | "Stop", "take me off" | never texted again, on any deal |
| wrong | "wrong number" | the number is marked wrong and dropped |
| no | "not interested", "only commercial" | not texted again |
| verified | asked for the address, the photos, the walkthrough | kept and texted first next time |
| silent | no reply | texted again on the next deal |
