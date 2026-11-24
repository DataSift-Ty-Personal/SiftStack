# Calling agency lists: pick the houses, pull them in, hand over the file

Three commands take a county from nothing to the file a calling agency dials: one owner a row, only the numbers
your CRM has not already ruled out, best houses first.

| Step | Command | Costs | Writes to DataSift |
|---|---|---|---|
| 1. Pick the houses | `python src/ai_pools.py` | free (SiftMap searches) | no |
| 2. Pull them in | `python src/pull_by_ids.py` | record allowance, quoted first | the records, a list and a tag |
| 3. Build the agency file | `python src/agency_call_list.py` | free (record reads) | no |

All three run on your own DataSift Open API key: `REISIFT_API_KEY` in `.env` at the repo root (Settings, then API).

## Step 1: pick the houses

```bash
python src/ai_pools.py --county "Knox, TN" --pool investor:20000 --name knox
python src/ai_pools.py --county "Franklin, OH" --pool realtor:20000 --pool investor:20000 --rank ladder
```

Any US county works, by name and state or by FIPS code (`--county 47093`); repeat `--county` for more than one.

**What ranks a house:**

- `investor` (the default `--rank ai`) ranks by SiftMap's investor AI score, 100 first, one point at a time.
- `investor --rank ladder` walks your county's Doors per Deal ladder (the lists that turned into deals most often,
  in order), then fills by the investor score.
- `realtor` ranks by the realtor AI score.
- Inside one score or one list, more equity goes first.

**The buy box:** single family, a value band (`--value 100000-700000`, default $1 to $700,000), not listed for sale,
no sale since a date (`--since 2023-01-01`; `--since ""` keeps them), no company owner. `--exclude-zips` leaves out
ZIP codes, and `--exclude` takes a file of ids never to pick (say, owners already marked do not market).

**Two pools never share a house.** A house that ranks in both goes to the pool where it ranks higher, and the other
pool takes its next-best house instead.

The AI scores are a per-county add-on to your DataSift account. If your account cannot see them in a county, the run
says so at the start: an investor pool there falls back to the ladder's lists, and a realtor pool cannot be ranked.

It writes `output/ai_pools/<name>/<pool>.json` (the houses in rank order, with the score or list that picked each one)
and `summary.json` (how many houses the buy box held, how many each cut dropped, how many searches it took). Nothing is
added to your account in this step. A trust or an owner with no name cannot be seen before a pull, so step 3 holds
those out of the call file.

## Step 2: pull them in

```bash
python src/pull_by_ids.py --ids output/ai_pools/knox/investor.json --list "Knox - Investor AI 20K"
python src/pull_by_ids.py --ids output/ai_pools/knox/investor.json --list "Knox - Investor AI 20K" --commit
```

The first command asks DataSift's own quote how many of these houses are new to your account, how much record
allowance they would use and how much you have left, and stops. `--commit` checks that quote again and refuses when
the pull would go past your allowance; then it pulls one house first and reads it back, and only then sends the rest.
Houses already in your account just get the list and the tag. `--approve 5000` caps the new records a run may add.

On DataSift's unlimited plan, new records are skip traced by DataSift on their own. `--phase qa` says how many records
are on the list and how many the skip trace has not reached yet; `--phase skiptrace` (dry, then `--commit`) traces
the ones it missed, only after DataSift's free estimate confirms the scope.

## Step 3: build the agency file

```bash
python src/agency_call_list.py --pool output/ai_pools/knox/investor.json --list "Knox - Investor AI 20K" \
    --phase all --about "Knox single family, investor AI order, no sale since 2023"
```

It reads every record once (about three a second, resumable) and writes, in `output/agency_call_lists/`:

- `<list>_Call_List_<date>.csv` and `.xlsx`: one row per owner, Phone 1 to 10 with type (mobiles first), emails,
  value and equity, the owner's other houses on the list, and List Rank.
- `<list>_Call_List_<date>_no_phones.csv`: owners with no callable number.
- A Held out sheet with the reason on each row: the owner asked not to be called, a company or trust or nameless
  owner, a live lead, upside down, or a lot with no house.

A number comes off only for what your CRM already knows: invalid, dispositioned wrong, dead or do-not-call, a prior
litigator flag, disconnected, or a repeat. There is no Trestle scoring and no do-not-call registry scrub on purpose:
a calling agency runs its own scrub before dialing. `--not-on "<other list>"` holds owners already on another list's
call file, so nobody is called twice.

## Another account

Put that account's Open API key in `.env` under any name and pass `--key-env NAME` to steps 2 and 3, with
`--expect-email <the email of the user the key belongs to>` on step 2, which refuses to write otherwise.
