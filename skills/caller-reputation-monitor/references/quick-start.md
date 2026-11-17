# Quick Start: Caller ID Reputation Monitor

Get from zero to a daily number-health scan in about 15 minutes. Everything in the default setup is free and read-only against SmrtPhone.

**What you get:** every day the monitor reads your own SmrtPhone call log, scores each of your dialing numbers (answer rate, average call length, short-call rate), moves each number through a health lifecycle (warm-up, active, watch, resting, retired), and writes:

- `output/caller_reputation.html` - a dashboard you can open in any browser
- `output/active_pool.json` - which numbers to load in the dialer today and their dial caps
- an optional Slack message when anything degrades

## Requirements

- SmrtPhone account (any plan; you just need login access to phone.smrt.studio)
- Python 3.10 or newer (`python --version`)
- Playwright, only for the two one-time helper scripts:
  ```
  pip install playwright
  python -m playwright install chromium
  ```
  The daily monitor itself uses only the Python standard library.

## Step 1: put the scripts somewhere permanent

Copy the `scripts/` folder from this skill to a permanent spot, for example `C:\tools\caller-reputation\`. The monitor writes its config, state, logs, and output next to itself, so do not run it from a temp folder.

## Step 2: log into SmrtPhone once

```
python smrtphone_login.py
```

A browser window opens at the SmrtPhone login. Log in normally. The session is saved to `smrtphone_state.json` next to the scripts and the window can be closed. Re-run this whenever the monitor says the session expired (every few weeks).

Treat `smrtphone_state.json` like a password. Never share or upload it.

## Step 3: build your number roster

```
copy config\numbers.example.json config\numbers.json
```

Edit `config/numbers.json` and set `business_name`. Then pull your real numbers straight from SmrtPhone:

```
python pull_smrtphone_numbers.py            # preview what it found
python pull_smrtphone_numbers.py --commit   # write them into config/numbers.json
```

**Important:** open `config/numbers.json` and fix the `added` date on each number. `added` drives the 3-week warm-up ramp. A number that has already been dialing for months should be backdated (for example `"added": "2025-06-01"`) so it scores as ACTIVE instead of being treated as brand new.

## Step 4: first scan

```
python monitor.py            # dry run: no writes, shows what would happen
python monitor.py --commit   # live: pulls the last 7 days of calls and scores every number
```

Open `output/caller_reputation.html`. Each number shows Clean, Watch, or Flagged plus its lifecycle state and the metrics behind it.

Reading the numbers (SmrtPhone's own healthy targets):

| Metric | Healthy | Meaning |
|---|---|---|
| ASR (answer rate) | 30% or higher | answered / total outbound |
| ALOC (avg call length) | 30 seconds or higher | average duration of answered calls |
| Short calls (under 6s) | 15% or lower | hang-up-fast connects |

A number under 15 dials in the window is not judged (sample too small). A number that breaches a target goes to Watch: keep dialing it at the reduced cap, it can clear itself. Three bad reads in a row escalates to Flagged.

## Step 5: register your numbers (free, biggest prevention lever)

Do this once for every number, in parallel with everything above. Go to **https://www.freecallerregistry.com/fcr/** and complete the 3-step form. This fans your registration out to all three analytics engines (Hiya, First Orion, TNS) that create the spam labels on AT&T, T-Mobile, and Verizon.

Full walkthrough, plus what to do when a number is already flagged: `registration-and-remediation.md`.

## Step 6: schedule it daily (optional but recommended)

Windows Task Scheduler, from PowerShell (adjust the folder). Note: use `Register-ScheduledTask`, not `schtasks /TR`, which breaks on paths with spaces. The two battery settings matter on laptops; the default kills the task on battery power.

```powershell
$dir = 'C:\tools\caller-reputation'
$action  = New-ScheduledTaskAction -Execute "$dir\monitor_run.cmd" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 11:00am
Register-ScheduledTask -TaskName 'Caller ID Reputation Monitor' -Action $action -Trigger $trigger -Force
$t = Get-ScheduledTask -TaskName 'Caller ID Reputation Monitor'
$t.Settings.StopIfGoingOnBatteries     = $false
$t.Settings.DisallowStartIfOnBatteries = $false
Set-ScheduledTask -TaskName 'Caller ID Reputation Monitor' -Settings $t.Settings
```

For Slack alerts, create a file named `.env` next to the scripts with:

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

## Step 7 (optional, most users skip): Telnyx carrier-grade layer

The default setup detects flags from your own traffic. If you also want the carrier's literal spam label and an automated remediation API, Telnyx offers Number Reputation at $100/month flat, but it requires porting your numbers into your own Telnyx account and passing Level 2 account verification. That is heavy; do it only if you run a large pool and want the extra certainty.

If you do: fill `config/enterprise.json` from `enterprise.example.json`, then run `telnyx_setup.py` in order: `register`, `loa`, `upload-loa`, `enable`, `status` (wait for approved), `associate`. Put `TELNYX_API_KEY` and `TELNYX_ENTERPRISE_ID` in `.env`. The daily monitor picks up the extra layer automatically. API details: `telnyx-api-contract.md`.

## Daily commands reference

| Command | What it does |
|---|---|
| `python monitor.py` | dry run: rebuild dashboard, no API calls or writes |
| `python monitor.py --commit` | the daily live scan |
| `python monitor.py --dashboard-only` | just re-render the HTML dashboard |
| `python monitor.py --commit --always-notify` | Slack heartbeat even when nothing changed |
| `python pull_smrtphone_numbers.py --commit` | re-sync the roster after adding/removing numbers in SmrtPhone |
| `python smrtphone_login.py` | refresh the SmrtPhone session when it expires |

## Troubleshooting

- **"No session" / "session expired":** re-run `python smrtphone_login.py`.
- **Every number shows WARMING:** backdate the `added` dates in `config/numbers.json` (Step 3).
- **Short-call % always 0:** known SmrtPhone log limitation (sub-6-second connects are not captured in the duration field). ASR and ALOC do the detecting; nothing is wrong.
- **Run exits 0 but nothing updated:** check `state/numbers_state.json` has today's `last_checked`. The monitor also self-checks for this and alerts to Slack if configured.
