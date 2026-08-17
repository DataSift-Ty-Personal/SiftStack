---
name: caller-reputation-monitor
description: Keep outbound cold-calling numbers out of carrier "Spam Likely / Scam Likely" labels. Monitors every SmrtPhone caller ID daily using your own call outcomes (answer rate, call length, short calls), runs a warm-up / active / watch / rest / retire lifecycle with dial caps, writes an HTML health dashboard and a recommended dial pool, and walks you through free carrier registration and flag remediation. Use when a user asks about spam-flagged numbers, "Spam Likely", caller ID reputation, number health, number rotation or warm-up, answer rates dropping, or checking whether their dialing numbers are burned.
---

# Caller ID Reputation Monitor

Keep the user's outbound dialing numbers (DIDs) out of carrier spam labels: register them (prevention), monitor them daily (detection), rest and remediate the ones that degrade (recovery). Built for SmrtPhone users doing REI cold calling, but the concepts apply to any dialer.

**The core insight:** carriers never tell you a number is flagged. The truest signal you can get for free is your OWN answer rate cratering on a specific number. This skill reads that straight from the user's SmrtPhone call log. No Telnyx account, no port, no monthly fee for the default setup.

## What is in this skill

- `references/quick-start.md` - the 15-minute setup guide. START HERE for a new user.
- `references/registration-and-remediation.md` - why numbers get flagged, the free registration walkthrough (Free Caller Registry), and the runbook for clearing a number that is flagged right now.
- `references/reputation-workflow.md` - the full design: signal layers, fusion rules, the number lifecycle state machine, data model.
- `references/telnyx-api-contract.md` - verified Telnyx endpoints and fields (only needed for the optional carrier-grade upgrade).
- `references/methodology.md` - the deep research behind the whole system (carrier analytics engines, STIR/SHAKEN, healthy call-behavior targets, sources).
- `scripts/` - the complete working monitor. Stdlib-only Python except the two Playwright helpers.

## How the monitor works (know this before helping)

Two signal layers, fused as worst-of:

| Layer | Source | Signal | Cost |
|---|---|---|---|
| L1 own traffic (default) | SmrtPhone call log (`smrtphone_cdr.py`) | per-number ASR, ALOC, short-call % over a trailing 7-day window | free |
| L2 carrier reputation (optional, OFF by default) | Telnyx Number Reputation + Remediation API | carrier-grade spam_risk / spam_category + auto-remediation | $100/mo + porting numbers to Telnyx |

**L1 healthy targets (SmrtPhone's own published bands):** ASR >= 30%, ALOC >= 30 seconds, short calls (under 6s) <= 15% of outbound. A breach makes a number at least Watch, never Flagged on L1 alone. Below 15 dials in the window the sample is too small and L1 abstains (protects warm-up numbers).

**L2 mapping (if enabled):** spam_category non-null (a real label) = Flagged; spam_risk high = Flagged; medium = Watch; low = Clean.

**Trend escalation:** 3 non-Clean reads in a row = Flagged even if no single read is critical.

**Lifecycle per number:** NEW -> WARMING (ramp 30 -> 75 -> 125 dials/day over 3 weeks) -> ACTIVE (cap 75/day) -> WATCH (cap 37/day, stays dialable so it can clear itself) -> RESTING (benched 30 days) -> RECOVERED back to ACTIVE, or RETIRED after 3 consecutive surviving flags. All tunable in `config/thresholds.json` (inline notes explain every knob).

**Outputs per run:** `output/caller_reputation.html` (dashboard), `output/active_pool.json` (recommended dial pool with per-number caps, for loading the dialer), optional Slack digest via `SLACK_WEBHOOK_URL`.

## Helping a user set up (first session)

Follow `references/quick-start.md`. The sequence:

1. Copy the `scripts/` folder somewhere permanent on their machine (it writes state next to itself). Python 3.10+ required; Playwright only for the two helper scripts.
2. `python smrtphone_login.py` - headed browser, they log into SmrtPhone once, session saved locally.
3. `cp config/numbers.example.json config/numbers.json`, set `business_name`, then `python pull_smrtphone_numbers.py --commit` to auto-fill their real numbers.
4. **Backdate `added`** in numbers.json for numbers that have already been dialing for weeks, otherwise the monitor treats them as brand new and puts them in warm-up.
5. `python monitor.py` (dry run, no writes) to sanity check, then `python monitor.py --commit` for the first live scan.
6. Open `output/caller_reputation.html` with them and read the results.
7. Register every number at freecallerregistry.com (free, one form, biggest prevention lever). Walk them through `references/registration-and-remediation.md`.
8. Optional: schedule the daily run (Task Scheduler command in quick-start; use `Register-ScheduledTask`, never `schtasks /TR` on a path with spaces, and disable the battery-kill defaults on laptops).

## Interpreting results for a user

- **Clean + ACTIVE:** dial normally, respect the cap in active_pool.json.
- **Watch:** early warning. Keep dialing at the reduced cap. Check list quality (wrong-number rate) and daily volume on that number first; those are the usual root causes.
- **Flagged / RESTING:** pull it from the dialer pool now. Re-submit it at freecallerregistry.com (free, unlimited resubmits) and follow the remediation runbook. It rests 30 days; resting alone does not clear a carrier flag, the resubmission + fixed root cause does.
- **Low sample (abstain):** under 15 dials in the window, no judgment. Normal for new or lightly used numbers.
- **A number keeps re-flagging (3 strikes):** retire it. But remediation beats churn: a fresh number needs a 2-3 week warm-up and recycled numbers can inherit a prior owner's spam history, so do not replace numbers casually.

Key coaching points backed by `references/methodology.md`: three private analytics engines label calls (Hiya for AT&T, First Orion for T-Mobile, TNS for Verizon) and they do NOT share scores; STIR/SHAKEN attestation is authentication, not immunity; steady volume with no spikes and no dead days is what healthy traffic looks like; even 5-10 consumer complaints can tip a low-volume number.

## The optional Telnyx upgrade (L2)

Only mention if the user specifically wants the carrier's literal label or automated remediation. It requires porting their numbers into their own Telnyx account, Level 2 verification, and $100/month flat. Setup is `telnyx_setup.py` (register -> loa -> upload-loa -> enable -> status -> associate) with the enterprise identity from `config/enterprise.example.json`. Details in quick-start section 7 and `references/telnyx-api-contract.md`. For most users L1 is the product; skip this.

## Troubleshooting

- **"Session expired" or no L1 data:** re-run `python smrtphone_login.py`. The monitor idles cleanly (no crash) when the session is missing.
- **Short-call % reads ~0 on SmrtPhone:** known limitation, the SmrtPhone log's duration field does not capture sub-6s connects. ASR and ALOC carry the detection; this never false-flags.
- **Run "succeeded" but nothing changed:** the failure mode to check is a silent no-op. Verify `state/numbers_state.json` has today's `last_checked` and the dashboard file is fresh. The monitor self-checks this and alerts to Slack if configured.
- **Everything shows WARMING:** `added` dates in numbers.json are today. Backdate them for established numbers.

## Safety rails

- Never put API keys or webhooks in code or config that gets shared; they belong in a local `.env` next to the scripts (`SLACK_WEBHOOK_URL`, and for L2 only: `TELNYX_API_KEY`, `TELNYX_ENTERPRISE_ID`).
- `smrtphone_state.json` is a live login session. Treat it like a password: never commit, upload, or share it.
- The monitor is read-only against SmrtPhone. The only billable actions anywhere are the opt-in Telnyx L2 calls, and fresh L2 rechecks only fire on numbers a free layer already degraded, capped per run.
- This system exists to help legitimate businesses keep compliant outbound calling healthy. Pair it with real list hygiene and DNC compliance; it is not a way to evade labels earned by bad traffic, and remediation fails if the root cause is not fixed.
