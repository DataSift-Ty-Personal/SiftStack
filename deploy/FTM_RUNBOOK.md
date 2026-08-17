# First-to-market pull: runbook

The scheduled job that pulls foreclosure and probate notices for Knox and
Blount from tnpublicnotice.com, enriches them, and pushes them into DataSift.

Local entry point: `python src/ftm_runner.py`
Cloud entry point: `python src/ftm_schedule.py` (the container's CMD)

---

## The one thing to understand first

**The site decides per-IP whether it will serve notices at all.** Verified live
2026-08-14 on the same logged-in account:

| egress | what the detail page shows |
|---|---|
| office / home IP | no CAPTCHA at all, just "You are not permitted to view public notices from this computer at this time" |
| Apify datacenter proxy | normal Cloudflare Turnstile gate, solvable, notice served |
| Scrapfly residential | notice served with no gate at all |

So a Fly machine, whose IP is a datacenter range by definition, **will not work
without a proxy configured**. This is not something code can route around. If a
run exits with code **3**, that is what happened.

**A hyphen in the Apify session id returns 407.** Apify accepts only letters,
digits, `_` and `.` in a session id. `session-tnpn-fly` fails with
"407 Proxy Authentication Required", which reads exactly like a bad password and
cost an hour of hunting through credentials on the first deploy. Verified live
from the Fly machine on one token and password: `tnpn-fly` 407s while `tnpn_fly`,
`tnpnfly` and `tnpn.fly` all return 200. `proxy_resolver._safe_session` now
rewrites illegal characters and logs it, so this cannot recur.

`proxy_resolver.py` resolves egress in this order:

1. `SIFTSTACK_PROXY_URL`: any `http://user:pass@host:port`
2. `APIFY_PROXY_GROUPS` + `APIFY_TOKEN`: looks the proxy password up through
   the Apify API (the API token is *not* the proxy password)
3. direct: works only from an allowed IP

**The site blocks an egress IP by VOLUME, and this is the single biggest
operational constraint on a backfill.** Measured 2026-08-14: one backfill month
running all four searches through a single sticky Apify session viewed about
**204 notices** before that IP started returning "not permitted to view", and
every subsequent month then failed in about 60 seconds against the same dead IP.
Two things follow, both now built in:

- `ftm_runner` rotates to a fresh Apify session id on every process
  (`proxy_session.json` on the volume), so consecutive jobs land on different
  upstream addresses instead of grinding one into the ground.
- The backfill runs **one process per (saved search x month)**, 48 jobs, which
  keeps the worst case (Knox probate, ~150 a month) under the threshold.

If a whole run still exits 3, wait for the IP pool to cool off rather than
retrying immediately; `BUYPROXIES94952` has 27 addresses to cycle through.

Apify's `RESIDENTIAL` group is not on the current plan (`availableCount: 0`).
The `BUYPROXIES94952` group (27 US datacenter IPs) clears the block today. If it
stops clearing it, the fix is a residential proxy, not a code change.

---

## Deploy

Prerequisites: `flyctl` installed and `fly auth login` done.

```bash
# 1. create the app and its volume
fly launch --no-deploy --copy-config --config fly.ftm.toml --name siftstack-ftm
fly volumes create ftm_data --size 3 --region iad -a siftstack-ftm

# 2. push credentials from .env (masked plan first, nothing is sent)
python deploy/sync_ftm_secrets.py
python deploy/sync_ftm_secrets.py --commit

# 3. ship it
fly deploy --config fly.ftm.toml
```

### Verify before trusting it

Run these in order. Do not skip to a scheduled `--commit` run.

```bash
# credentials, egress, state dir, configured searches
fly ssh console -a siftstack-ftm -C "python /app/src/ftm_runner.py --doctor"

# the saved-search names still match the live site exactly
fly ssh console -a siftstack-ftm -C "python /app/src/main.py list-searches"

# a real 2-notice dry run: scrapes, enriches, writes a CSV, uploads nothing
fly ssh console -a siftstack-ftm -C "python /app/src/ftm_runner.py --max-notices 2"

# when the next fires land where you expect
fly ssh console -a siftstack-ftm -C "python /app/src/ftm_schedule.py --next"
```

### Go live

`FTM_ARGS` in `fly.ftm.toml` is the safety switch. It ships as
`--stages notices` (no `--commit`), so the first scheduled run does everything
except write to DataSift. Read that Slack summary, then:

```toml
FTM_ARGS = "--stages notices --commit"
```

```bash
fly deploy --config fly.ftm.toml
```

---

## What a run does

1. **scrape**: logs into tnpublicnotice through the proxy, runs each saved
   search, walks the results grid, clears the Turnstile gate on the first
   notice (the gate is session-level, so one solve covers the rest), and parses
   each notice. Screenshots were retired 2026-08-14
   (see `archive/notice_screenshots/`).
2. **probate address lookup**: probate notices carry no property address, so
   `property_lookup` resolves it: Knox Tax API by decedent name, then executor
   family search, then people search.
3. **enrich**: Smarty address standardization, Zillow, tax, filters.
4. **CSV**: DataSift-shaped, written to `/data/output/`.
5. **upload**: `datasift_api_upload` over the API. It mints its own JWT from
   `DATASIFT_EMAIL` / `DATASIFT_PASSWORD` every 30 minutes, so a long run cannot
   die on token expiry. `POST /property/` is upsert by address, so re-runs never
   duplicate.
6. **report**: Slack summary, plus a line appended to `/data/ftm_runs.jsonl`.

### Exit codes

| code | meaning |
|---|---|
| 0 | everything that ran, worked |
| 1 | a stage failed, details in the summary and in Slack |
| 2 | misconfiguration, caught before any work started |
| 3 | egress blocked: the site refuses this IP. Retrying will not help. |

---

## Zero notices is a failure, not a quiet success

The scrape sat dead for 19 days in July 2026 while every run reported success,
because the code inferred "the CAPTCHA text is gone, so we must have passed",
and after the reCAPTCHA-to-Turnstile migration that string was *always* gone.

Two rules now hold the line, and neither should be softened:

- Clearing the gate requires **positively seeing the notice body**. There is no
  "the challenge markup disappeared" fallback.
- A run that scrapes **0 notices reports EMPTY and exits non-zero**. Foreclosure
  and probate notices publish continuously in Knox and Blount; a silent day
  means the gate, the login, the egress IP, or a saved search changed.

## Cost ceilings

- `--max-notices N` bounds a run. It stops **within** a results page, not just
  between pages, a cap of 1 used to still grind all 50 results on page one.
- The seen-ID cache on the volume is what stops a re-scrape. If `/data` is lost,
  the next run re-scrapes and re-pays for months of notices. Do not delete the
  volume to "start clean".
- **seen_ids is persisted only after a SUCCESSFUL upload.** The scrape no longer
  writes it incrementally. That ordering matters: the upload happens after the
  whole scrape, so persisting mid-scrape meant an aborted run left notices
  flagged as handled that were never sent. The egress block did exactly this to
  204 notices, and a retry would have skipped every one of them.
- `--deep-heirs` bills per record (obituary and skip-trace). It is off unless
  you ask for it.

---

## When something breaks

| symptom | cause | fix |
|---|---|---|
| exit 3, "not permitted to view" | egress IP is blocked | check `APIFY_TOKEN` / `SIFTSTACK_PROXY_URL`; `python src/proxy_resolver.py` prints the live egress IP |
| exit 1, stage EMPTY | gate, login, or a renamed saved search | `main.py list-searches`, then `ftm_runner.py --doctor` |
| "Could not select ... from dropdown" | the saved search was renamed or deleted | `main.py list-searches` shows configured-but-missing entries |
| gate never clears, 2Captcha billed | sitekey rotated | the solver reads the live sitekey and logs `SITEKEY ROTATED`; update `TURNSTILE_SITEKEY` |
| DataSift created 0 from a non-empty file | credentials or field mapping | run the uploader with `--limit 1` and read the record back |
| 407 Proxy Authentication Required | illegal Apify session id (usually a hyphen) | sanitized automatically now; check the `APIFY_PROXY_SESSION` value if it reappears |
| browser takes ~60s to launch | machine too small for Chromium | the VM is shared-cpu-2x / 2GB for this reason; do not drop it to 1x |
| probate records vanish | the vacant-land filter | probate is exempt via `NO_ADDRESS_TYPES`; covered by `tests/test_vacant_filter_probate.py` |

Logs: `fly logs -a siftstack-ftm`. Run history: `/data/ftm_runs.jsonl`.

---

## Backfilling 12 months

### The cap that makes this necessary

The site truncates **every** result set at 20 pages / 1000 rows, newest first,
so a plain 12-month search silently loses its tail. Measured 2026-08-14: Knox
foreclosure and Knox probate both hit that ceiling and show only their most
recent few weeks. The site retains **12 months and no more** ("Notices for the
past 12 months are available in the current search"), so 12 is both the target
and the maximum.

`--backfill-months N` therefore runs each saved search once per calendar month.
Every slice lands well under the cap: Knox probate is about 150 notices a month,
Knox foreclosure about 100.

| Saved search | per month | 12 months |
|---|---|---|
| Probate Knox | ~150 | ~1,800 |
| Foreclosure Knox | ~100 | ~1,200 |
| Probate Blount | ~80 | ~950 |
| Foreclosure Blount | ~33 | ~400 |
| | | **~4,350 raw notices, roughly 19 hours** |

Foreclosure counts are RAW grid rows. The saved search matches broadly
("judgment", "forfeiture", "notice of sale") and `foreclosure_filter` then keeps
only real trustee sales, so the number of records that reach DataSift is far
lower than the number of notices opened.

### Run it one month at a time

`--backfill-offset M` shifts the window back M months, so the job becomes twelve
resumable ~90-minute runs instead of one 19-hour process. Re-running a month is
cheap: the seen-ID cache is checked against the results grid, so an
already-captured notice is skipped without opening its page.

```bash
# 1. stage the oldest month only, upload nothing, and read the CSV
fly ssh console -a siftstack-ftm -C \
  "python /app/src/ftm_runner.py --backfill-months 1 --backfill-offset 11 --no-upload"
fly ssh sftp get /data/output/ftm_notices_<stamp>.csv   # eyeball it

# 2. once the CSV looks right, walk the remaining months with --commit
for M in 11 10 9 8 7 6 5 4 3 2 1 0; do
  fly ssh console -a siftstack-ftm -C \
    "python /app/src/ftm_runner.py --backfill-months 1 --backfill-offset $M --commit"
done
```

A single month takes longer than an `fly ssh console` session is comfortable
with, so for unattended chunks run it detached on the machine and watch the log:

```bash
fly ssh console -a siftstack-ftm -C \
  "sh -c 'nohup python /app/src/ftm_runner.py --backfill-months 12 --commit \
   > /data/backfill.log 2>&1 &'"
fly ssh console -a siftstack-ftm -C "tail -40 /data/backfill.log"
```

### What to check in the staged CSV

- `Owner First Name` / `Owner Last Name` split correctly, and the probate owner
  is the **personal representative**, never a court or an attorney
- probate rows carry a real **property** address, not the courthouse
- no foreclosure/trustee-sale notices are sitting in the Probate list
- `Lists` is Foreclosure / Probate, and `Tags` carries Courthouse Data + county + month
- spot-check two or three against `Source URL`

### Two classes of record the backfill drops on purpose

**Trustee sales that surfaced in the probate search.** The probate saved search
matches the word "probate", which also appears in foreclosure notices, so a
successor-trustee sale lands in the probate results. Unguarded it uploads to the
Probate list with the trustee's law firm parsed as the personal representative
(seen live: a Marinosci Law Group notice produced "PR = From Felicia F.
Coalson"). `foreclosure_filter.looks_like_trustee_sale` now drops those, and
requires the ABSENCE of a genuine probate anchor so a real estate filing that
merely mentions a trustee still passes.

**Probate records whose property never resolved.** No address means no DataSift
record, so validation drops them. Expect this on a minority of rows; it is the
lookup missing, not the notice being bad.

### Probate has no phone in it

Verified on a 10-notice Knox sample: 8 carried no phone at all, and the 2 that
did carried the **law firm's** number ("The Ebbert Law Firm ... Telephone (865)
234-2488"). The PR is published with a mailing address only. Every probate phone
number therefore has to come from skip trace against the PR name and address;
a number lifted from the notice body would dial the estate's attorney.

## What is NOT in the cloud job yet

`knox_ftm_pull.py` (the Knox county multi-source pull: liens, condemnations,
trustee deeds, evictions) runs as the `county` stage but **skips in the
container**, with the reason stated in the run summary. It needs the SiftMap
client from the Deal Room `_api` checkout for buy-box enrichment, and that is
not on the machine. Until that client is vendored the way
`sms_agent/crm_standalone.py` vendored the reisift client, run that pull from
the workstation:

```bash
python src/knox_lien_resolve.py --all --workers 6
python src/knox_ftm_pull.py --out output/knox_ftm_pull.csv
python src/datasift_api_upload.py --limit 1 --commit   # always verify one first
python src/datasift_api_upload.py --commit
```
