# Automated Caller-ID Reputation Workflow (target state)

The most-automated, most-robust process we can run to keep the outbound cold-call
numbers (SmrtPhone / Telnyx) out of "Spam Likely / Scam Likely / Spam Risk", detect
degradation early, and remediate hands-off. This is the design target the current
`monitor.py` grows into. ASCII hyphens only (house rule #7).

Anchor fact: our DIDs are **Telnyx** numbers (state file carrier string `TELNYX LLC`),
provisioned through SmrtPhone. So Telnyx-native APIs are the fit; Twilio Voice
Integrity is N/A unless we port. Everything below assumes the Telnyx account that
owns the DIDs is API-reachable (see "Blocker to confirm").

---

## 1. Design goals

1. **Automate everything that has an API; make the manual residue small, listed, and scheduled.** Honesty matters here: some signals (the literal on-screen label, the Free Caller Registry submit) have no API and never will. A "robust" process names those and puts them on a checklist, it does not pretend they are automated.
2. **Defense in depth.** Four independent signal layers so no single blind spot (like IPQS-only today) can hide a flag. Carrier-grade signals outrank proxies in the fused verdict.
3. **Lead, do not lag.** Our own call-outcome data (answer rate, talk time) degrades BEFORE a carrier label lands. That is the earliest warning and it is free. It is the tie-breaker the other layers defer to.
4. **Fail-safe and verifiable.** Verify the DATA landed, not the exit code (GR #17). Battery/path-safe scheduling (GR #18/#20). Incremental, idempotent, cost-capped.
5. **Scales as the pool grows** from a handful of numbers to whatever your calling team needs.

---

## 2. The signal stack (four layers)

| Layer | Source | Signal | Carrier-grade? | Automated? | Cost |
|---|---|---|---|---|---|
| **L1 Own traffic (LEAD)** | Telnyx Detail Records (CDR) API | Per-DID ASR, ALOC, short-call % vs SmrtPhone targets | Ground truth (behavioral) | Fully (daily API pull) | Free |
| **L2 Carrier reputation** | Telnyx Number Reputation API | `spam_risk` low/med/high, maturity/connection/engagement/sentiment, `spam_category`, across all 3 engines (Hiya-fed) | **Yes** | Fully (scheduled auto-recheck + on-demand) | Cached reads free; fresh checks/adds billable |
| **L3 Proxy (corroboration)** | IPQS (have) + optional Trestle | `fraud_score`, spammer/recent_abuse/DNC; line-type/litigator | No (proxy) | Fully (LRU quota, have) | Free tier |
| **L4 Literal label (spot)** | Caller ID Reputation Device Cloud (opt) OR manual sweep | The actual "Scam Likely" string per carrier, from real handsets | Yes | API+webhook if bought; else manual monthly | Paid / manual |

The current monitor is L3-only. That is the whole reason a registry flagged a number
while our dashboard read "Clean". The upgrade is adding L1 and L2 and letting them
outrank L3.

---

## 3. The daily automated pipeline (one cron run)

Extends the existing `monitor.py` run (Windows Task "Caller ID Reputation Monitor",
11:00 AM ET, after FTM 10:00 / Tier2 10:30, before Prospecting 11:30). One process,
one Slack digest, one dashboard write.

```
1. SYNC ROSTER      pull caller IDs from SmrtPhone (dialerConfigs, Playwright) ->
                    auto-enroll new DIDs into monitoring, mark added-date, put new
                    numbers in WARMING with a staggered ramp. Retire removed ones.

2. L1  CDR PULL     Telnyx Detail Record Search API, yesterday, direction=outbound,
                    group by originating number -> per-DID: dials, answered, ASR,
                    ALOC, short-call% (<6s). Compare to targets ASR>=30, ALOC>=30s,
                    short<=15%, and to each DID's own trailing baseline.

3. L2  REPUTATION   Telnyx Number Reputation: read CACHED (free) for every DID.
                    Force a FRESH (billable) check ONLY on DIDs that degraded in
                    step 2 or read non-clean on L3 -> cost tracks only degrading DIDs.

4. L3  PROXY        IPQS LRU batch (existing daily_quota). Optional Trestle line-type
                    on new DIDs. Corroboration only, never the sole voter now.

5. FUSE             Per DID, per carrier: status = severity-max across layers WITH a
                    carrier-grade override (see section 6). Write per-source, per-
                    carrier sub-record + the rolled-up status.

6. LIFECYCLE        Advance the state machine (section 5): warm-up ramp checks,
                    Watch/Rest/Retire, consecutive-flag escalation, rest-timer expiry.

7. AUTO-REMEDIATE   On any NEW Flagged: (a) fire Telnyx Number Reputation remediation
                    request (14-day per-number cooldown), (b) generate the manual
                    portal tickets for engines Telnyx does not cover, (c) move DID to
                    RESTING and drop it from the recommended active pool, (d) open a
                    row in the remediation ledger.

8. POOL PLAN        Compute the healthy ACTIVE pool and the bench list (DIDs resting/
                    warming/flagged), enforce the per-DID daily cap, write
                    out/active_pool.json for the dialer step to consume.

9. REPORT           Render dashboard HTML + one Slack digest (degrades, new flags,
                    remediation status, pool changes, recheck-due). Append ledger.

10. VERIFY          Assert the data landed (GR #17): row counts moved, state file
                    fresh, dashboard mtime current. Slack-alert on any silent no-op.
```

Cost control: L2 fresh checks and L4 are gated behind "a cheaper layer already
degraded", so steady-state spend tracks only the handful of DIDs actually going bad,
not the whole pool.

---

## 4. Signal cadence summary

| Cadence | What runs | Manual? |
|---|---|---|
| **Daily (cron)** | Steps 1-10 above: roster sync, CDR, reputation cached read, IPQS batch, fuse, lifecycle, auto-remediate, pool plan, report, verify | No |
| **Scheduled auto-recheck** | Telnyx Number Reputation re-scores DIDs (set `weekly` for ACTIVE, `daily`/`business_daily` for WATCH/RESTING) | No |
| **Weekly** | Rollup digest: answer-rate trend per DID, flags cleared/opened, spend | No |
| **Monthly** | L4 community/manual sweep of WATCH+RESTING DIDs only (real on-screen label); re-verify registration status | Yes (unless Device Cloud) |
| **On new DID** | Free Caller Registry submit + First Orion; auto: Telnyx reputation add + Trust Center STIR/SHAKEN + CNAM | FCR submit is manual |
| **On flag** | Auto-remediate (step 7) + rest + fix behavior | Behavior fix is human |
| **One-time** | Telnyx enterprise verify + LOA; confirm account API access | Yes |

---

## 5. Number lifecycle state machine

Each DID lives in exactly one state; the daily run advances it.

```
                 (new from roster sync)
                          |
                          v
   +------------------- WARMING -----------------------+
   |   ramp 20-30 -> 50-75 -> 100-125 over ~3 wks;     |
   |   staggered activation date; watched closely       |
   +----------------------+----------------------------+
                          | metrics healthy through ramp
                          v
                       ACTIVE  <-------------------------+
             (in the dialer pool, capped 50-75/day)      |
                 |                     ^                  |
   L1/L2/L3 degrade|                   | rest timer done  | reputation
                 v                     | + behavior fixed | recovers
               WATCH --------------> RESTING -------------+
        (pull from heavy         (out of pool; auto-
         rotation; fresh          remediation filed;
         L2 + L4 confirm)         30-day timer)
                 |                     |
                 |    3 consecutive    | flag survives
                 |    flags            v  rest + refile
                 +-------------------> RETIRED
                                    (release the number)
```

Transition rules (all in `thresholds.json`, already partly there):
- **WARMING -> ACTIVE:** completed ramp with ASR>=30, ALOC>=30s, short<=15%, no flag.
- **ACTIVE -> WATCH:** any one of L1 breach, IPQS Watch (fraud>=80 / spammer / recent_abuse / DNC), Telnyx `spam_risk=medium`, or L1 answer-rate down vs baseline.
- **WATCH -> RESTING (Flagged):** Telnyx `spam_risk=high` OR `spam_category` non-null OR L4 shows a real label OR 3-in-a-row non-clean (`trend_escalation_consecutive`).
- **RESTING -> ACTIVE:** `rest_days` (30) elapsed AND behavior root-cause fixed AND a fresh L2 check is clean.
- **-> RETIRED:** `retire_after_consecutive_flags` (3) flags survive resting. Release the DID; do not recycle it back in.

Rest is not enough on its own: the run also records the likely root cause (volume
spike on that DID, wrong-number rate from list quality) so the behavior gets fixed,
or the replacement burns the same way.

---

## 6. Fusion + decision logic (the core robustness rule)

The lesson from the missed flag, encoded: **a carrier-grade layer or our own call
data can flag a DID even when the proxy says clean; the proxy can never veto them.**

```
status(DID) = worst of:
    L1_status  (CDR behavior vs targets + baseline)
    L2_status  (Telnyx spam_risk: low->Clean, medium->Watch, high->Flagged;
                spam_category non-null -> Flagged)
    L3_status  (IPQS bands, existing)
    L4_status  (literal label if available: any label -> Flagged)

Overrides:
  - L2 high  OR L4 label  -> Flagged regardless of L1/L3.
  - L1 breach (ASR<30 sustained, ALOC<30s, short>15%) -> at least Watch, even if
    L2/L3 clean (early warning the proxies have not caught).
  - Store status PER CARRIER where the source gives it (a DID can be flagged on
    T-Mobile only). The dialer pool decision uses the worst carrier.
```

Do NOT flag on IPQS `fraud_score` alone for VOIP (GR #5 of this module: clean VOIP
sits 50-75). L3 stays a corroborating vote, demoted from "truth".

---

## 7. Registration + remediation automation (API vs manual, definitive)

| Action | Path | Automatable? |
|---|---|---|
| STIR/SHAKEN A-attestation | SmrtPhone Trust Center (Telnyx signs) | Auto once verified; verify state via monitor |
| Telnyx Number Reputation register | Enterprise verify + LOA once, then API add/query/recheck | Yes, after one-time LOA |
| First Orion (T-Mobile engine) | First Orion Business Number Registration API | Yes (T-Mobile only) |
| Free Caller Registry (fans to all 3) | Manual web portal, no API | **No** - the one irreducible per-new-number manual step |
| Hiya registration | Manual portal (reputation API is contract-gated) | No (portal) |
| CNAM business name | Telnyx/SmrtPhone | Auto once set |
| Dispute a flag | Telnyx Number Reputation remediation API (14-day cooldown) + manual portals (calltransparency.com T-Mobile, voicespamfeedback.com Verizon, Hiya ticket AT&T) | Telnyx path auto; portals manual, but the monitor pre-fills/queues them |

Registration state is tracked per DID per engine in the roster (the `registration`
block already exists in `numbers.json`), with the 90-day re-verify reminder (no real
renewal; re-submit only on re-label / new-or-recycled number / branding change).

---

## 8. The minimal manual touchpoints (the honest residue)

Everything else is automated. These four are not:

1. **Free Caller Registry submit for each NEW number** (~5 min). No API. The monitor tells you when a new DID needs it and tracks the state.
2. **Confirm/receive registration acknowledgement emails** and set each engine to `confirmed` (or automate by parsing the inbox later).
3. **Reading the literal on-screen label** for WATCH/RESTING DIDs - monthly, unless you buy Caller ID Reputation Device Cloud (then it is API + webhook).
4. **Applying pool changes in SmrtDialer** when a DID benches - IF the Telnyx account is owned by SmrtPhone and not API-writable by us (SmrtPhone's own API is narrow: recordings + DNC only). If we own the Telnyx account, more of this can be scripted. ~2 min per change either way.

---

## 9. Data model

Grow from the flat per-number JSON to a source-tagged, per-carrier, time-series store
(the schema was already sketched in METHODOLOGY; move to SQLite as the pool grows).

- **roster**: did, label, caller (which team member dials on it), added, state, warm-up day, registration{per engine}, active.
- **checks** (one row per DID per source per day): did, ts, source (cdr / telnyx_rep / ipqs / device_cloud), carrier, spam_risk, spam_category, fraud_score, spammer, recent_abuse, do_not_call, asr, aloc, short_call_pct, dials, answered, raw_json.
- **state** (rollup): did, status, per-carrier status, last_checked, rested_until, consecutive_flags, flag_count, retire_candidate.
- **remediation ledger**: did, opened, method (telnyx_api / fcr / carrier_portal), engine, status, cooldown_until, closed.
- **pool history**: date, active set, benched set, reason.

---

## 10. Robustness / ops checklist (hard-won rules)

- Schedule with `Register-ScheduledTask` (never `schtasks /TR` on a spaced path) - GR #18.
- `StopIfGoingOnBatteries=$false` + `DisallowStartIfOnBatteries=$false` (this is a laptop) - GR #20.
- Verify the DATA, not the exit code / "Ready" - GR #17. Step 10 does this.
- Incremental + idempotent: re-running the day is safe; only new/degrading DIDs cost money.
- Cost cap: L2 fresh + L4 gated behind a cheaper-layer degrade; L3 stays on the free LRU quota.
- Stdlib-first (the monitor is dependency-light today); add `requests` only if needed for Telnyx, else keep urllib.
- Secrets in `.env (next to the scripts)` (IPQS key already there; add `TELNYX_API_KEY`).
- Quiet on clean runs; Slack only on degrade / new flag / recheck-due / scan error.

---

## 11. Implementation roadmap (phased, each phase shippable)

**Phase 0 - today (no new spend):**
- Remediate the currently-flagged DID: identify engine -> carrier (Hiya=AT&T, First Orion=T-Mobile, TNS=Verizon), rest it, dispute via that portal + re-submit FCR, pull it from the pool.
- Confirm the Telnyx account that owns the DIDs is API-reachable and get/issue an API key (the blocker for Phases 1-2).

**Phase 1 - L1 CDR layer (free, biggest single win):**
- `telnyx_cdr.py`: pull yesterday's outbound CDRs, compute per-DID ASR/ALOC/short-call%.
- Wire into `monitor.py` as `source=cdr`; add the L1 thresholds to `thresholds.json`; add L1 to `classify()` and the dashboard.
- This alone would have caught the missed flag early.

**Phase 2 - L2 carrier reputation + auto-remediation:**
- One-time: Telnyx enterprise verify + LOA; enable Number Reputation; add the 13 DIDs; set auto-recheck schedule.
- `telnyx_reputation.py`: cached read for all, fresh check for degraded; parse `spam_risk`/`spam_category`/scores.
- Add the remediation call + ledger to step 7.

**Phase 3 - fusion, lifecycle, pool recommender:**
- Upgrade `classify()` to the severity-max + carrier-grade override; add the full state machine; emit `active_pool.json` + the bench list; add warm-up ramp tracking for new DIDs.

**Phase 4 - optional hardening:**
- Caller ID Reputation Device Cloud for API-based literal labels (drops the monthly manual sweep).
- Migrate JSON state -> SQLite.
- Auto-parse registration acknowledgement emails.

---

## 12. Blocker to confirm before Phase 1

Do we control the Telnyx account/API key that owns these DIDs, or does SmrtPhone own
that Telnyx account? Both Telnyx layers (CDR + Number Reputation) need API access to
that account.
- If we own it: build Phases 1-2 as written.
- If SmrtPhone owns it: request a subaccount or API key from them; if unavailable,
  substitute Caller ID Reputation (carrier-agnostic, API + webhook, supports SmrtPhone
  + real estate) as the L2/L4 layer, and use whatever call stats SmrtPhone exposes for
  L1. The rest of the workflow (fusion, lifecycle, pool, remediation tracking, cron)
  is unchanged.
