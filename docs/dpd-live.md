# Live Doors per Deal

The County List Playbook ranks a county's lists by doors per deal: how many houses on a list you
market to for each one an investor buys. The published numbers are rebuilt every few months. This tool
keeps them current from your own DataSift Open API key, every night if you want, and says which lists to
work first right now.

```bash
python src/dpd_live.py --fips 47093 --table              # one county, every list, best first
python src/dpd_live.py --fips 47093 --fips 47009         # two counties
python src/dpd_live.py --fips 47093 --phase all          # also save this month's list members (see below)
```

It needs only `REISIFT_API_KEY` (the account's Open API key, in the environment or in `.env`). Every
request is a SiftMap search, paced under DataSift's per-account limit; the tool cannot add a record or
spend anything. Results land in `output/dpd_live/<fips>/` (`ladder.json`, `ladder.csv`), or wherever
`DPD_LIVE_DIR` points.

## What is live, what is measured, what is borrowed

Every number in the output says which.

| | Where it comes from |
|---|---|
| **Doors** (list size) | Live. Each list is counted with its own filter, the one the published ladder was built with. |
| **Market speed** | Live. Investor purchases a month from the sold-property search, compared with the same search over the months the published ladder used. The newest month is left out: deeds reach the data four to eight weeks after a closing. |
| **Deals per list** | Measured where possible, borrowed where not. A list starts on the published deals per door, adjusted for today's market speed, and moves to its own measured rate as it earns sales. |
| **AI score lists** | Live only in counties where your account has the off-market AI add-on. Elsewhere the published size. |

## Why deals are measured forward

You cannot ask "which houses on this list sold to an investor" after the fact. Once a house sells, its
flags describe the new owner: a tax-delinquent seller's flag clears, and an investor buyer makes the
house absentee-owned. Measured on one county over six months, the published back-test counted 50 deals on
the tax delinquent list and a live query found 0; it counted 345 on absentee owners and the live query
found 467.

So with `--phase all` (and on the agents brain, automatically) the tool saves every list's members once a
month, by their SiftMap ids. Each later month's investor purchases are matched to the save taken before
that month began. That is the back-test, run forward, on your own key. A list needs a few months of
sales before its measured rate outweighs the published one; until then the published number carries it.

## Using it with the agents brain

The `dpd_live` stage (in `team/autopilot.yaml`) runs this every night for the counties you name and posts
the best lists to the marketing board. With `live_lists: true` on the Top 1,000 stage, each house's deal
odds are adjusted by how much better or worse its list is selling now than the published ladder says.
That adjustment is 1.0 for every house until a list has measured sales of its own.
