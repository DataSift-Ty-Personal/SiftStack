<p align="center">
  <img src="team/assets/datasift.png" width="96" alt="DataSift">
</p>

# SiftStack

SiftStack is a free, open-source kit for running a real estate investing business on [DataSift.ai](https://datasift.ai) with Claude. It comes in three layers. Install only as far as you need:

1. **Skills for Claude.** 27 current packages that each teach Claude one job: comp a house, price a rehab, find the heirs behind a probate, score phone numbers, grade cold calls. Two commands and no server.
2. **The AI team.** One small always-on app on Fly.io. Four AI employees work in your Slack, take requests, and pick up work your DataSift account sends them. One command sets it up.
3. **Two more always-on apps.** A texting agent that reads and sorts replies to your seller texts, and a first-to-market runner that pulls new foreclosure and probate notices every morning and loads them into DataSift.

Most people only need the first layer.

---

## 1. The skills

### Claude Code

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install siftstack-all@siftstack
```

That installs all 27 current packages. Then turn on updates once: run `/plugin`, open **Marketplaces**, select **siftstack**, choose **Enable auto-update**. Claude Code leaves auto-update off for any marketplace that is not Anthropic's, which is why this step exists. From then on every improvement reaches you on its own.

Want one division instead of everything? Install a bundle: `siftstack-deal-analysis`, `siftstack-market-intel`, `siftstack-crm`, `siftstack-coaching` or `siftstack-operations`. Or one skill by name: `/plugin install rehab-estimator@siftstack`.

Then ask Claude to **run the SiftStack doctor**. It tells you what works right now, what needs a key, what each key costs, and the free route without it.

If the install fails with `Unrecognized key: "dependencies"`, your Claude Code predates bundles. Run `claude update` and try again.

### Claude Cowork

**Customize**, then **Plugins**, then **Add marketplace**, and enter `DataSift-Ty-Personal/SiftStack`. Install `siftstack-all` or any single plugin. Use **Update** on the marketplace to pull the latest. Cowork runs skills in a sandbox with limited network access, so a skill that calls a paid API may be blocked there; run that one from Claude Code instead.

### Without plugins

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/DataSift-Ty-Personal/SiftStack/main/install.py | python3 -

# Windows PowerShell
irm https://raw.githubusercontent.com/DataSift-Ty-Personal/SiftStack/main/install.py | python -
```

That copies the skills into `~/.claude/skills/`. Python 3.9 or newer, standard library only. It does not update itself, so re-run it to pull changes. Flags go after the dash, for example `... | python3 - --plugins`, which registers the marketplace and installs every plugin instead and works even on a Claude Code too old for bundles. `--list`, `--only <name>`, `--category "<name>"`, `--dest <dir>` and `--dry-run` let you pick and preview.

### claude.ai chat and Projects

Chat and Projects take the packaged file. Download the `.skill` you want from [`dist/`](dist/) and upload it, for example `https://raw.githubusercontent.com/DataSift-Ty-Personal/SiftStack/main/dist/rehab-estimator.skill`.

### What a skill needs from you

**14 work on install, out of 27 current packages.** No key, no login, no card.

| Tier | Count | What it means |
|---|---|---|
| No credentials | 14 | Works on install |
| A login you already have | 8 | DataSift, SmrtPhone, Google. Browser-driven, no API access needed |
| A metered API key | 5 | Faster and deeper, and every one has a free route |

From a terminal, `... | python3 - --doctor` (the installer line above) prints the same readout. It sends no requests and never prints a credential. Every paid step has a no-API route in the [no-API playbook](docs/setup/no-api-playbook.md), and a missing key skips that step and says so rather than failing.

More: [Getting started](docs/setup/GETTING-STARTED.md), [API contracts](docs/api/), and [`.env.skills.example`](.env.skills.example) (every variable, what it unlocks, what it costs).

### The catalog

| Package | What it does |
|---|---|
| **Deal analysis** | |
| `comp-package` | Boundary-filtered comps for one property and a dual-track ARV, delivered as an Excel workbook |
| `rehab-estimator` | Room-by-room rehab costs across 4 tiers, priced from a locked SKU material list |
| `deep-prospecting-v5` | For a deceased owner: the heirs, who must sign, and the numbers that reach them |
| `probate-property-finder` | The real property behind a probate filing when all you have is a decedent name |
| `real-estate-comping` | The valuation method on its own: Two-Bucket ARV, adjustments, disclosure state routing |
| `deal-analyzer` (plugin) | Comps, rehab, MAO and financing in one pass, with exit strategies compared |
| **Market intelligence** | |
| `sift-market-research` | ZIP code scoring off DataSift Market Finder, as a 7-sheet Excel report |
| `first-market-county-data` | Where to pull distress lists in any US county, across 7 notice types, with FOIA templates |
| `buyer-prospector` | A cash buyer list for any county, with the people behind the LLCs and trusts |
| **Operations** | |
| `phone-validator` | Trestle scoring into 5 dial tiers, with a litigator risk check |
| `text-touch-builder` | A four-touch pre-call text sequence per record that never reads machine-written |
| `sequential-presets` | The sequential marketing filter presets, designed and built in DataSift |
| `caller-reputation-monitor` | Keeps your outbound numbers out of carrier "Spam Likely" labels |
| `playbook-creator` | Turns a transcript or a recording into an SOP, a playbook or a step guide |
| `candidate-intake` | Applicants from Indeed, Gmail and Facebook in one scored list |
| `team-hiring` | Who to hire next, what they own, pay bands, job posts, interviews, the first two weeks |
| `vendor-directory-builder` | A vetted contractor directory for any market, every name checked against public records |
| `contractor-call-sheet` | A one-page call sheet and first-contact scripts from a finished directory |
| `dispo-deal-blast` | Text a deal to a vetted buyer list without a wrong price ever going out |
| **Coaching and performance** | |
| `cold-call-coach` | Transcribes your call recordings and grades every conversation against the cold-calling rubric |
| `lead-manager-coach` | The same engine, graded on qualification and next-action discipline |
| `closer-coach` | The same engine, graded on the money conversation and commitment |
| `kpi-engine` | Dials, connects, correct numbers, leads and pacing from your own DataSift account |
| **CRM** | |
| `sift-sequences` | 26 sequence templates and how to build them |
| `account-blueprint` | Builds a DataSift account in a reference structure over the API, every object read back |
| `sift-operations` (plugin) | The CRM operations encyclopedia: boards, drips, tasks, filters, tags, skip trace |
| **Setup** | |
| `siftstack-core` (plugin) | The doctor and the guided setup. Every bundle installs it |

Two superseded packages (`deep-prospecting`, `deep-prospecting-v4`) stay in the repo so old runs still resolve. The installer skips them unless you name one.

---

## 2. The AI team

<p>
  <img src="team/assets/atlas.png" width="72" alt="Atlas">
  <img src="team/assets/diggs.png" width="72" alt="Diggs">
  <img src="team/assets/roma.png" width="72" alt="Roma">
  <img src="team/assets/quinn.png" width="72" alt="Quinn">
</p>

One always-on app on Fly.io, called the brain. Four AI employees live in it: Atlas (analysis), Diggs (research), Roma (sales) and Quinn (operations). They take requests in Slack, run the workflows each one owns, and react to your DataSift sequences through a webhook, for example researching the heirs when you move a record to Deep Prospecting.

It ships safe. `AGENTS_MODE` starts as `dry`: no vendor spend and no CRM writes (the research model still runs). Every scheduled stage in `team/autopilot.yaml` ships in `plan` mode or `off`: it reports what it would do and changes nothing until you switch it on.

**What you need:**

- A [Fly.io](https://fly.io) account and `flyctl`, signed in with `fly auth login`
- Your DataSift Open API key as `REISIFT_API_KEY`, and an Anthropic key as `ANTHROPIC_API_KEY`, in `.env`. No DataSift password is ever read or sent
- A Slack workspace where you can install an app, and a DataSift user with the Super Admin or Sensei role (creating a webhook needs one)
- Python 3.12 with the brain's requirements installed

**The one command:**

```bash
git clone https://github.com/DataSift-Ty-Personal/SiftStack.git
cd SiftStack
pip install -r deploy/requirements-agents.txt
cp .env.example .env            # add REISIFT_API_KEY and ANTHROPIC_API_KEY

python deploy/agents_up.py --app your-team-name             # the plan: checks everything, changes nothing
python deploy/agents_up.py --app your-team-name --commit    # does it
```

Fly app names are global, so pick your own with `--app`. `--region` defaults to `iad`. Every step checks first and skips itself when it is already done, so a rerun picks up where the last one stopped. With `--commit` it:

1. **Preflights:** the team roster is valid and `team/TEAM.md` is current, the selftest passes (no network), both keys are present, and `flyctl` is signed in. If anything fails, it stops before touching anything.
2. **Creates the Fly app.**
3. **Creates the volume** `agents_data` (1 GB) for the queue, the spend ledger and the work folders.
4. **Pushes your secrets** from `.env` with `deploy/sync_agents_secrets.py`. The first time, it generates `AGENTS_WEBHOOK_SECRET` and adds it to `.env`.
5. **Deploys** `fly.agents.toml`.
6. **Waits for `/health`** to report the worker alive.
7. **Slack, your turn:** it prints a one-click link that creates the Slack app with every scope and event filled in. Install it, add `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` to `.env`, and rerun. Create `#ai-team` and one channel per employee and invite the app, or set `AGENTS_SLACK_CHANNEL` to one channel's ID and everyone posts there.
8. **DataSift, your turn:** webhooks are made in the DataSift app, not over the API, so it prints the webhook URL and the steps: add the webhook, press **Test** on it, and point a Sequence Builder sequence at it. Rerun, and it confirms DataSift's Test delivery reached the brain.
9. **Says hello:** the team introduces itself in Slack.

Then ask in Slack. `https://your-team-name.fly.dev/health` shows what the brain is doing, and `fly ssh console -a your-team-name -C "python /app/src/agents/cli.py status"` lists the mode, today's spend and the last ten tasks.

---

## 3. The texting agent and the first-to-market runner

Each is its own Fly app with its own volume, separate from the brain, so a long run never sits in front of a webhook. Both start from the clone above with `pip install -r requirements.txt` and `cp .env.example .env`. Fly secrets override the `[env]` block in a Fly config, so when you need to know what an app is really doing, ask it (`/health`, or `fly ssh console -a your-app -C printenv`) rather than the file.

### The texting agent (`fly.toml`)

Receives smrtPhone webhooks, classifies every reply, writes the result back to DataSift (phone status, opt-outs, lead status) and hands interested sellers to a person in Slack. It ships with dry run on at phase 1: it classifies replies and writes and sends nothing. Turning `SMS_AGENT_DRY_RUN` off lets it write to DataSift, and raising `SMS_AGENT_PHASE` adds escalation (2), drafted replies held for approval (3) and auto-sent replies (4). It needs a smrtPhone account and API token, your DataSift Open API key, an Anthropic key, and a Slack webhook.

```bash
python src/sms_agent/cli.py selftest      # the whole pipeline, no network
python src/sms_agent/cli.py doctor        # what is wired, and the webhook URLs to paste into smrtPhone

fly launch --no-deploy --copy-config --name your-sms-app
fly volumes create sms_agent_data --size 1 --region iad -a your-sms-app
fly secrets set -a your-sms-app SMS_AGENT_WEBHOOK_SECRET=... SMRTPHONE_API_KEY=... \
    REISIFT_API_KEY=... ANTHROPIC_API_KEY=... SMS_AGENT_SLACK_WEBHOOK=...
fly deploy -a your-sms-app
```

Set `SMS_AGENT_LOCALITY` in `fly.toml` to your county first. The full guide is [`src/sms_agent/README.md`](src/sms_agent/README.md).

### The first-to-market runner (`fly.ftm.toml`)

A scheduled job that pulls new foreclosure and probate notices from tnpublicnotice.com (Tennessee's statewide notice site) every morning, enriches them and uploads them to DataSift. It needs a paid Smart Search subscription with your own saved searches, a 2Captcha key, your DataSift login for the uploader, and a proxy the site will serve: it refuses datacenter IPs, so set `APIFY_TOKEN` with your own `APIFY_PROXY_GROUPS`, or `SIFTSTACK_PROXY_URL`. Another state needs its own scraper target.

```bash
playwright install chromium                      # the site needs a real browser
python src/main.py list-searches                 # your saved searches, as the site names them
python src/ftm_runner.py --doctor                # credentials, egress, state, configured searches
python src/ftm_runner.py --max-notices 2         # a bounded dry run that uploads nothing

fly launch --no-deploy --copy-config --config fly.ftm.toml --name your-ftm-app
fly volumes create ftm_data --size 3 --region iad -a your-ftm-app
python deploy/sync_ftm_secrets.py --app your-ftm-app            # masked plan, sends nothing
python deploy/sync_ftm_secrets.py --app your-ftm-app --commit
fly deploy --config fly.ftm.toml -a your-ftm-app
```

Before the first deploy, put your saved searches in `SAVED_SEARCHES` in `src/config.py`, and set `FTM_ARGS` in `fly.ftm.toml` to `--stages notices` with no `--commit`, so the first scheduled run (06:30 business time) does everything except write to DataSift. Read that run's summary, then add `--commit`. The runbook is [`deploy/FTM_RUNBOOK.md`](deploy/FTM_RUNBOOK.md).

---

## How it fits together

Every agent in the system is described once, in [`docs/agents.json`](docs/agents.json), and rendered two ways: [the interactive map](https://learn.datasift.ai/agent-org-chart) and [`docs/AGENT-MAP.md`](docs/AGENT-MAP.md). Each entry gives the trigger, the steps, where a person signs off, what comes out, and the trap it exists to avoid. Everything else in `src/` (comps, offer sheets, lender packages, the buyer registry) runs from the clone, and the map says what each piece does.

## Working on a skill

The source of truth is the folder, not the ZIP: edit `skills/<name>/`, then rebuild.

```bash
python tools/build_skills.py --marketplace
python tools/build_skills.py --build --manifest
python tools/build_skills.py --verify
```

Found a problem or have an idea? Open an issue.

## License

MIT. See [LICENSE](LICENSE).

Built by [DataSift.ai](https://datasift.ai) for the REI community.
