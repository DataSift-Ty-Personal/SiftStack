# Getting started

Install the library, find out what you can already run, then add credentials
only for the things you actually want.

14 work on install, out of 27 current packages. You do not need an API key,
a developer account, or a credit card to get value out of this on day one.

## 1. Install

In Claude Code:

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install siftstack-all@siftstack
```

Then turn on updates once: `/plugin`, **Marketplaces**, **siftstack**, **Enable
auto-update**. Claude Code leaves that off for any marketplace that is not
Anthropic's, so without it you stay on the version you installed.

In Claude Cowork: **Customize**, **Plugins**, **Add marketplace**, enter
`DataSift-Ty-Personal/SiftStack`, and install `siftstack-all`.

You do not invoke skills by name: describe the task and the right one triggers.

If the install fails with `Unrecognized key: "dependencies"`, run `claude update`
first. No plugins at all? The one-line installer still works and needs only
Python 3.9 or newer:

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/DataSift-Ty-Personal/SiftStack/main/install.py | python3 -

# Windows PowerShell
irm https://raw.githubusercontent.com/DataSift-Ty-Personal/SiftStack/main/install.py | python -
```

For a claude.ai chat or Project, download the package you want from
[`dist/`](../../dist/) and upload it.

## 2. Find out where you stand

Ask Claude to **run the SiftStack doctor**. From a terminal the same check is:

```bash
python3 install.py --doctor
```

It reads your environment and a `.env` if you have one, then prints three
groups: what works right now, what needs you signed in somewhere, and what
needs a credential. Every blocked skill prints its no-API alternative on the
same line, so you never get told no without being told what to do instead.

It sends no requests and spends nothing. It never prints a credential value,
only whether a name is set.

## 3. Choose your path

There are three tiers, and most people should stop after tier 1 for a while.

### Tier 1: no credentials

14 work on install.

| Skill | What you get |
|---|---|
| `rehab-estimator` | Room-by-room rehab costs across 4 tiers, with a real material list |
| `real-estate-comping` | Full comping method by browser. Zillow, Redfin, Realtor, by hand |
| `first-market-county-data` | Where to pull county distress lists for any US county |
| `probate-property-finder` | Find the property behind a probate filing, using free portals |
| `buyer-prospector` | Cash buyer list for any county, ships its own data |
| `team-hiring` | Who to hire, what to pay, where to post, how to interview |
| `playbook-creator` | Turn a recording or transcript into a real SOP |
| `text-touch-builder` | Four-touch pre-call SMS sequence per record |
| `sift-operations` | The CRM operations encyclopedia |
| `candidate-intake` | One scored master list of every job applicant, from any channel |
| `vendor-directory-builder` | A vetted contractor directory for any market |
| `contractor-call-sheet` | A printable call sheet and first-contact drafts from that directory |
| `dispo-deal-blast` | The method and guards for texting a deal to a vetted buyer list |
| `siftstack-core` | The doctor and the guided setup |

### Tier 2: a login you already have

No API keys. These drive a browser, or mint their own token from your own
account credentials.

| Skill | Needs | Set |
|---|---|---|
| `sift-market-research` | DataSift login | `DATASIFT_EMAIL`, `DATASIFT_PASSWORD` |
| `sequential-presets` | DataSift login | `DATASIFT_EMAIL`, `DATASIFT_PASSWORD` |
| `sift-sequences` | DataSift login | `DATASIFT_EMAIL`, `DATASIFT_PASSWORD` |
| `kpi-engine` | DataSift login | `REISIFT_TOKEN`, or it mints one from your login |
| `cold-call-coach` | SmrtPhone session, a transcription model | Browser session, `OPENROUTER_API_KEY` |
| `lead-manager-coach` | same | same |
| `closer-coach` | same | same |
| `candidate-intake` | Google account, Claude in Chrome | Nothing to set, it runs in the browser |

The three DataSift skills use Playwright to drive the web app the same way you
would by hand. That is deliberate: it means you need no API access at all, and
it works on any DataSift plan.

### Tier 3: a metered API key

Faster and more thorough, and every one of them has a no-API alternative.

| Skill | Key | Cost | Without it |
|---|---|---|---|
| `comp-package` | `OPENWEBNINJA_API_KEY` | 100 free/month | Use `real-estate-comping` |
| `deal-analyzer` | `OPENWEBNINJA_API_KEY` | 100 free/month | Use `real-estate-comping` |
| `phone-validator` | `TRESTLE_API_KEY` | $0.015 per number | [Phone scoring by hand](no-api-playbook.md#phone-scoring-without-trestle) |
| `deep-prospecting-v5` | SmartSkip, Trestle | about $0.24 per record | [Heir research by hand](no-api-playbook.md#heir-research-by-hand) |
| `caller-reputation-monitor` | `TELNYX_API_KEY` | included with Telnyx | [Spam checks by hand](no-api-playbook.md#spam-flag-checks-by-hand) |
| `playbook-creator` (optional) | `OPENROUTER_API_KEY` | ~$0.002 per audio minute | Paste your recorder's free transcript (Loom, Zoom, Fireflies) |

## 4. Add credentials

Copy the example and fill in only what you want. Everything is optional.

```bash
curl -fsSL -o .env.skills.example https://raw.githubusercontent.com/DataSift-Ty-Personal/SiftStack/main/.env.skills.example
cp .env.skills.example .env
```

Skills read from your environment. Either export the variables in your shell
profile, or keep a `.env` in the folder you work from.

**Every skill degrades rather than failing.** A missing key means that step is
skipped and the run says so. Nothing hard-fails because you did not sign up
for something.

Re-run `python3 install.py --doctor` and the skill moves out of the blocked
list.

## 5. Use them

Describe the task. Do not name the skill.

- "Run comps on 123 Main St, Knoxville TN 37914" triggers the comping stack.
- "Estimate the rehab on this, three bed one bath, 1,100 square feet, full gut"
  triggers `rehab-estimator`.
- "Who should I hire next and what do I pay them" triggers `team-hiring`.
- "Grade yesterday's cold calls" triggers `cold-call-coach`.

If nothing triggers, the skill either is not installed or its description does
not match how you phrased it. `python3 install.py --list` shows every
description, and those are what Claude matches against.

## Keeping up to date

```bash
python3 install.py            # re-run any time, skips what is current
python3 install.py --force    # reinstall everything
```

Each installed package carries a `.siftstack-version` file holding the SHA-256
of the archive it came from. That is how the installer knows what is current,
and it is why re-running is cheap and safe.

## Where things are

| | |
|---|---|
| No-API routes for every paid step | [`no-api-playbook.md`](no-api-playbook.md) |
| API contracts and their gotchas | [`../api/`](../api/) |
| The agent system these skills drive | [`../AGENT-MAP.md`](../AGENT-MAP.md) |
| Interactive map | [learn.datasift.ai/agent-org-chart](https://learn.datasift.ai/agent-org-chart) |

## When something does not work

Run the doctor first. It answers most of it.

**A skill never triggers.** Check it is installed and read its description with
`--list`. Describe your task in those words.

**TLS or certificate errors during install.** Usually a corporate proxy
intercepting HTTPS. Clone the repo and run `install.py` from inside it.

**GitHub rate limits partway through.** Installing everything is 24 requests.
The installer backs off and retries, and if it still gives up, wait a few
minutes and re-run: finished packages are skipped and it resumes.

**A paid step returns nothing.** Check the balance on that account before
assuming the skill is broken. Most of these APIs return an empty result rather
than an error when you are out of credit.
