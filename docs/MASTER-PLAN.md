# Wright Home Offer — Master Plan (Q2 2026)

**Audience:** Aaron + Travis. Operating plan for the next 60–120 days.
**Purpose:** Lay out where we are, what we're adding (and what we're explicitly NOT changing), how we'll prove it works, and the phase gates that trigger reinvestment vs pause.
**Companion docs:** [AARON-PATH-OFF-W2.md](AARON-PATH-OFF-W2.md) (private, partner-only), [SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md), [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md), [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md), [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md), [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md), [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md).

> **Update (2026-04-26):** The door-knocker network described in Section 4c is **deferred pending Aaron + Travis alignment**. References to door knockers throughout this doc (cadence diagrams, phase gates, risk register, decision points) are preserved as the original proposal for that partner conversation, but **no recruiting, onboarding, or build work happens against them** until the partners agree on the model. Mail vendor has been chosen: **OpenLetter** (DataSift's native integrated mail house) — see [SOP-MAIL-TEMPLATES.md](SOP-MAIL-TEMPLATES.md) for the cadences and templates per distress type.

---

## Executive summary

Wright Home Offer is currently producing **~10 closed contracts per month** from bulk DialForce cold calling against 60,000 skip-traced records — at **$0 incremental marketing cost**. That is an unusually strong starting position; most operators at this stage are still trying to land their first stable lead engine.

The proposed strategic shift is **not to replace bulk** but to **layer a niched-sequential operation on top of it** for the records most likely to convert. Three Ohio counties (Franklin, Montgomery, Greene), ~750 active records at any time (250 per county), multi-channel cadence (cold call + SMS + niched mail + redemption-window door knocking + ISA appointment setting). Hypothesis: niche converts the BEST records at 15–25% vs bulk's ~4% on the average record.

**Total incremental cost to test:** $3,000/month (niched direct mail). Everything else — DialForce direct-placement ISA, DataSift, dialer infrastructure, SiftStack automation — is free or already paid.

**Go/no-go decision in 60 days** based on mail response rate + first niche-attributable contracts.

If the niche layer adds **3 contracts/month**, that's **$51,000/month additional gross margin** at the $17,000 average margin per deal — a return that justifies the spend on the first month alone and creates the runway to fund Phase 2 expansion without new partner capital.

---

## 1. Where we are today

### 1a. The lead engine (the part that's already working)

- **DialForce partnership:** 5 dedicated callers, ~2.5 leads/day per caller = ~250 leads/month. Cost: $0 (monetized via referral exchange + dialer cannibalization).
- **DataSift partnership:** highest-tier package, $0 (partnership). Provides 60,000 skip-traced records monthly that feed DialForce.
- **Current production:** ~10 contracts/month at ~4% lead-to-contract conversion.
- **Master Planner goal:** 5 contracts/month closed at 65% success rate (= 15 contracts under-contract). Current run rate is exceeding this.

### 1b. SiftStack — what's been built (technical inventory)

The automation backbone is more complete than commonly understood. Detailed map:

| Component | Status | Files |
|---|---|---|
| OH scrapers — probate (Franklin, Montgomery, Greene) | Live | `src/scrapers/oh_*_probate.py` |
| OH scrapers — foreclosure auction (3 counties) | Live | `src/scrapers/oh_*_foreclosure.py` |
| OH scrapers — lis pendens / Common Pleas (3 counties) | Live | `src/scrapers/oh_*_lis_pendens.py` |
| 10-step enrichment pipeline (Smarty + Zillow + obituary + skip + phone) | Live | `src/enrichment_pipeline.py` |
| County Auditor property lookup (3-county) | Live | `src/*_auditor.py` |
| DataSift CRM upload + enrich + skip trace automation | Live | `src/datasift_uploader.py` |
| Niche sequential preset taxonomy (FTM_*) | Live (Mike's presets ready) | `docs/SOP-TAG-FLOW.md` |
| 8 operational SOPs | Live | `docs/SOP-*.md` |
| Apify cloud deployment (daily 6am autonomous run) | Live | `.actor/` |
| Tax sale routing | Live (recent) | committed `efe5518` |
| Tier 0 phone band + multi-day trend tracking | Live (recent) | committed `9aa4e82` |
| OH 20-county investor-density scoring | Live (this conversation) | `src/score_oh_counties.py` |

### 1c. SiftStack — what's queued for build (the niche play)

| Component | Status | Estimated build |
|---|---|---|
| Redemption-window niche (court docket post-sale watcher + tag + preset + sequence + scripts) | Queued | 1–2 days |
| Lead scoring / "best 250 per county" capacity narrowing module | Queued | 0.5 day |
| Niched FTM mail vendor integration (YellowLetters.com → API later) | Queued | 0.5 day |
| Stage-progression handler (lis pendens → auction listing → sale → confirmation re-tagging) | Queued | 1 day |

**Total queued build effort: ~4 days.** Aaron handles. No external dev cost.

---

## 2. The strategic shift — niche on top of bulk

### 2a. The diagnosis

Bulk converts at ~4% lead-to-contract because the 60k record list has variable signal quality. We dial everything; some records are gold (probate executor with vacant property, motivated, decision-maker on the call) and some are noise (stale skip data, dead numbers, owner-occupant who isn't selling). Bulk works because volume papers over signal-to-noise — but it doesn't extract maximum value from the gold records.

### 2b. The hypothesis

If we can **identify the gold records before dialing** and run them through a multi-channel sequential cadence (5+ touches across SMS, mail, cold call, door knock), we can lift conversion on those specific records from 4% to 15–25%. That's published industry benchmark for properly-segmented niche-sequential.

### 2c. The Data Pyramid — how records get filtered to the top

The narrowing logic isn't a single "pick top 250" filter — it's a **multi-layer pyramid** where each layer adds signal and reduces volume. Each layer below feeds the layer above via lead scoring + Trestle narrowing.

```
              Tier A — top 50/county = 150 total
              ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
              HYPER mail + SMS + ISA call + DialForce + door knock
              The "gold" records. Maximum-spend treatment.

           Tier B — next 200/county = 600 total
           ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
           SMS + DialForce + ISA call (Trestle-validated phones only)
           Active sequential cadence. No mail.

       Active monthly nurture pool — ~500/county
       ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
       SMS only. Drops to monthly cadence between rotations.

   Bulk DialForce — 60,000 records statewide
   ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Cold dial only. No SMS, no mail, no nurture.
   The wide base — produces 10 contracts/mo today.
```

### 2d. The data inputs that build the pyramid

The ranking that decides who lands in Tier A vs B vs nurture is built from a **stacked data pipeline** — each layer narrows volume but adds signal:

1. **Layer 1 — F2M (First-to-Market) scrape data.** Notice info pulled directly from county records: probate court filings, foreclosure auction listings, lis pendens / Common Pleas case filings. We have this data BEFORE third-party brokers do. Volume: ~250–500 new records per week per county across all notice types. **This is our structural advantage** — first-to-market timing means we hit motivated sellers before the competitive noise floor rises.
2. **Layer 2 — SiftStack 10-step enrichment.** Smarty USPS validation + ZIP+4 geocode + Zillow Zestimate/MLS/equity + obituary heir research + Tracerfy skip trace + Trestle phone scoring + entity research + County Auditor parcel lookup + DOD sanity check + data validation. Each step adds signal and narrows mailability/dialability.
3. **Layer 3 — DataSift AI overlay.** Once records upload to DataSift, the AI stacking adds equity calculations, MLS status, structure type/year, owner mailing reconciliation, and AI-assisted decision-maker confidence scoring. This layer compounds on top of our enrichment — it's signal we don't have to build.
4. **Layer 4 — Trestle phone narrowing.** 5-tier phone scoring (Tier 0 priority through Tier 4 drop). Only Tier 0–3 phones get dialed; Tier 4–5 are dropped from the active call pool. **This is the gate that separates "callable" from "mailable-only" records.**
5. **Layer 5 — Lead scoring.** Multi-factor composite score from Layers 1–4: phone tier × equity % × DM verified-living × DPV match × stage urgency × distress signal stack × notice freshness. Output: `lead_score` 0–100. **Top 250/county per active rotation = active rotation. Within active rotation, top 50/county = Tier A "hyper" treatment.**
6. **Layer 6 — Channel routing.** Mail goes ONLY to Tier A (the apex — ~150 records). SMS goes to Tier A + B (~750 records). ISA dials Trestle-validated phones in Tier A + B. Door knockers deploy on redemption-window properties only (subset of Tier A).

**The economic logic:** mail is the most expensive channel ($1.75/piece × 1,700 pieces = $3k/mo). It only earns its keep on records where conversion probability justifies the spend. By restricting mail to the top 50 per county (Tier A), we hit only records that have cleared every filter — F2M-scraped, fully enriched, AI-overlaid, Trestle-validated, lead-score-ranked. **Mail spend stays controlled while cadence intensity stays maximum on the records that matter most.**

### 2e. Capacity sanity-check

- Tier A active = 150 records (50/county × 3 counties)
- Tier B active = 600 records (200/county × 3 counties)
- Total active rotation = 750 records
- Touch-to-process rate at 5% across all touches = ~35 processes/month
- **Master Planner goal = exactly 35 processes/month.** The pyramid is right-sized for current capacity, not aspirational.

### 2f. Mike's SMS cadence — chain-position triggers

Mike works from chain-position triggers, not records-by-day. The SMS he sends depends on where each record sits in the funnel — not which day of the calendar it is:

| Position in chain | Mike's action |
|---|---|
| New record (Day 0, just landed in active rotation) | Day 1 SMS per [SOP-CALL-SCRIPTS](SOP-CALL-SCRIPTS.md) |
| No reply by Day 2 | Day 2 SMS |
| No reply by Day 3 | Day 3 SMS final |
| Engaged reply (any day) | Tag `interested`, escalate to ISA call within 24 hrs |
| ISA call → qualified | Tag `hot`, escalate to Danielle |
| ISA call → not qualified | Tag based on objection, drop to Tier B or monthly nurture |
| Tier A → Tier B (lead score drops below top-50 threshold) | SMS continues, mail stops |
| Falls off active rotation (drops below top 250) | Drop to monthly mail nurture, no SMS |
| Stage progression event (e.g., redemption window opens) | Re-tag, move back to Tier A immediately, restart cadence |

### 2g. What Tier A actually gets — the apex treatment

The 50 best records per county per cycle (~150 total) get EVERY available touch:

| Day | Channel | Touch |
|---|---|---|
| 0 | DialForce cold call | First dial attempt; lead routed to ISA if connected |
| 1 | Mike SMS | Day 1 SMS per [SOP-CALL-SCRIPTS](SOP-CALL-SCRIPTS.md) |
| 1 | ISA call (Trestle-validated phones only) | Live dial attempt; VM if no answer |
| 2–3 | Mike SMS | Day 2/3 follow-up |
| 5 | HYPER mail | Handwritten yellow letter, segment-specific (probate / foreclosure / redemption-window) |
| 5 | ISA call | Second live attempt |
| 10 | ISA call | Third live attempt |
| 14 | Mike SMS final + ISA escalation | Last SMS + final qualification call attempt |
| 21–30 | Status decision | Hot → Danielle; warm → drop to Tier B; cold → monthly nurture |
| Day 30 | Re-mail (HYPER segment) | Second mail piece if still in Tier A and no engagement |

This cadence is **expensive per record** but only fires on ~150 records out of the 60,000-record bulk pool, so total monthly mail spend stays at $3k–5k regardless of cadence intensity.

### 2h. The flywheel — why this compounds

Once the pyramid is running, three feedback loops engage:

1. **Lead score learns from outcomes.** Records that converted get studied; the lead-scoring model adjusts factor weights. Tier A composition improves over time.
2. **F2M timing shrinks the funnel.** As we tune scrapers to hit county records faster (target: <24hrs from filing), Tier A gets fresher records and conversion lifts because we're contacting BEFORE the competitive noise.
3. **DataSift AI overlay improves with volume.** More records = better DataSift AI training = better DM confidence + equity scoring on the next cohort.

The flywheel matters because it means **the niche play gets MORE effective per dollar over time**, not less. The first $3k/mo of mail produces some result; the third month's $3k/mo produces a better result on the same spend, because the pyramid is sharper.

---

## 3. Marketing allocation — phased

### Phase 1: Validation (Days 1–60)

| Channel | Spend/mo | Volume | Source |
|---|---|---|---|
| Bulk cold call (DialForce) | $0 | ~250 leads/mo | Free partnership |
| Niched FTM mail | $3,000 | ~1,700 pieces/mo | NEW — soft launch |
| SMS (existing tools) | ~included | ~200/day | Mike via Launch Control |
| DialForce direct-placement ISA | $0 | 30+ hrs/wk | Free placement (DialForce owner managed) |
| **Total incremental spend** | **$3,000** | | |

**Mail segments in Phase 1 (conservative — start with highest-converting niche):**
- Franklin probate: ~$1,000/mo (~570 pieces)
- Montgomery probate: ~$1,000/mo (~570 pieces)
- Greene probate: ~$1,000/mo (~570 pieces)

(Probate first because it's the highest-motivation segment per [SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md). Foreclosure mail added in Phase 2 if Phase 1 validates.)

### Phase 2: Optimization (Days 61–120)

Triggered by phase gate from Section 5. If green:

| Channel | Spend/mo | Change |
|---|---|---|
| Bulk cold call | $0 | unchanged |
| Niched FTM mail | $5,000 | scale up + add foreclosure + redemption-window segments |
| Door-knocker network (commission-only) | variable | NEW: 1099 contractors, $1,500–3,000 per closed deal commission, redemption-window properties only |
| **Total incremental spend** | **$5,000 + commissions** | |

### Phase 3: Expansion (Days 121+)

Triggered by Phase 2 gate. If green: begin OH statewide expansion per `output/oh_county_scores_20260425_112126.csv`. First adds: Cuyahoga (highest investor density in state, 2,653 transactions/6mo) and Hamilton (96% A/B class concentration). Mail scales linearly with new counties.

---

## 4. Staffing

### 4a. Current team — what does what

(Roles only; compensation specifics in [Master Planner.xlsx](https://drive.google.com/file/d/1QqSV1Q9lD50RFlOvuA_r2yoN42ZyT4ZC/view).)

- **Aaron** — Marketing/sales lead. Builds and operates SiftStack. Trains the team.
- **Travis** — Operations/dispositions/construction lead. Brings organic deals through builder network.
- **Danielle** — Closer. Takes hot escalations from Mike/ISA, makes offers, signs contracts.
- **Mike** — Data manager. Opens DataSift at 9am daily, works leads via SMS + initial calls, escalates `hot` to Danielle. Primary audience for the 8 SOPs.
- **Dylan** — Project manager.
- **Dawn, Luke, Joy** — Supporting roles per Master Planner.
- **Acel** — Data/list puller. SiftStack is automating most of this role; recommend pivot to data QA / lead validation post-launch.

### 4b. Phase 1 add: DialForce direct-placement ISA

The DialForce owner has offered to place a high-skill caller as a dedicated VA inside Wright Home Offer's systems, managed by DialForce, working our SOPs and DataSift workflow. **Cost: $0** (continuation of the existing referral-monetized relationship).

**Why this solves the niche-sequential bottleneck:**
- DialForce bulk callers do cold dial only. They do not do SMS, mail follow-up, or appointment booking. The niche-sequential cadence requires all of those touches happen on the same record across 14 days.
- An ISA inside our systems can run the full sequence: SMS Day 1, dial Day 1, SMS Day 2/3, dial Day 7, qualification call Day 14, escalate Day 21.
- The 8 existing SOPs serve as training material. Aaron handles 60–90 minutes of recorded onboarding plus a first-week shadow.

**Risk: VA is split across other DialForce clients.** Mitigations:
- Request fully-dedicated placement OR a 30-hr/week minimum commitment in writing
- Plan a backup capacity (offshore $10–12/hr ISA) ready to onboard if reliability issues emerge in Days 1–30
- Track redemption-window cadence adherence as the leading reliability indicator (those windows are time-bound; a flaky ISA will miss them visibly)

### 4c. Phase 2 add: Door-knocker network for redemption-window niche

> **DEFERRED — pending Aaron + Travis alignment (2026-04-26).** This section preserves the original proposal as input to the partner discussion. No recruiting, onboarding, or 1099 setup happens until the partners agree on the model.

(If approved, would be triggered only if Phase 1 produces ≥1 redemption-window contract — see Section 5.)

**Model:**
- 1099 contractors, localized to county courthouse areas (Columbus, Dayton, Xenia)
- Commission-only: **$2,000 flat per closed deal** OR 12% of margin (whichever is higher to the knocker, cap at $5,000/deal)
- Records assigned: redemption-window only (7–30 day window, geographically clustered, urgent)
- Liability protection: background check, W-9, training on consent law (Aaron-led 60-min onboarding before first deployment)

**Why commission-only:**
- Aligns incentives: knockers only earn on closes, so they self-select for hustle
- Zero fixed cost: doesn't add to monthly burn until deals close
- Matches the niche economics: redemption-window deals are urgent, time-bound, high-margin (distressed seller + active redemption = motivated)

**Capacity:** 2–3 knockers in Franklin, 1–2 in Montgomery, 1 in Greene. Total network: 4–6 contractors, deployed only as redemption-window inventory permits.

### 4d. Phase 3 add (Days 121+): Second ISA

If contract volume reaches 15+/mo and current ISA is at capacity, hire a second ISA. Recommended source: offshore (Filipino REI-experienced, $10–12/hr × 30hrs/wk = ~$1,200–1,400/mo). Onboarded against the same SOPs as Phase 1 ISA.

---

## 5. Phase gates — what triggers reinvestment vs pause

| Trigger | Threshold | Action |
|---|---|---|
| **Phase 1 → Phase 2 (mail proven)** | Mail response rate ≥ 0.5% over 60 days OR ≥ 1 niche-attributable closed contract | Scale mail to $5k/mo, add foreclosure + redemption segments, kick off door-knocker network |
| **Phase 1 → Pause** | Mail response rate < 0.25% over 200 pieces | Pause mail. Redesign piece + targeting before resuming. |
| **Phase 1 → Hold** | Mail response rate 0.25–0.5% | Hold $3k/mo spend. Refine message. Re-evaluate at Day 90. |
| **Phase 2 → Phase 3 (3-county model proven)** | ≥ 2 niche-attributable contracts/mo for 60 consecutive days | Begin OH statewide expansion: add Cuyahoga as 4th county |
| **Phase 3 expansion → 5-county** | ≥ 5 niche contracts/mo for 60 days | Add Hamilton + Summit. Hire 2nd ISA. |
| **DialForce reliability failure** | ISA coverage drops below 30 hrs/wk for 2 consecutive weeks OR redemption-window cadence misses ≥ 25% | Activate offshore ISA backup |
| **Construction capacity bind** | Travis flags inability to handle deal volume | Pause mail expansion until capacity addressed |

**Hard pause trigger (any phase):** Total monthly burn exceeds $50k OR active deal count drops below 8/mo for 2 consecutive months. Either signals a structural problem requiring strategy review before further reinvestment.

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| DialForce ISA reliability (split-attention) | Medium | High | Request dedicated placement; offshore ISA backup ready |
| Niched mail piece doesn't convert | Medium | Medium | $3k/mo for 60 days is the test; pause + redesign trigger built in |
| Niche records overlap with bulk = double-touch | High | Low | Dedup logic in SiftStack flags overlap; coordinate cadence so DialForce doesn't dial same number ISA called yesterday |
| Travis bandwidth on construction | Medium | High | Surface in partner sync at Day 30; Phase 3 expansion only if capacity confirmed |
| DataSift partnership change (free tier disappears) | Low | High | Contingency: Tracerfy direct skip at $0.02/record; cost adds ~$100–200/mo at current volume |
| Compliance issue (distressed-seller mail/calls) | Low | Medium | SOP-CALL-SCRIPTS already addresses tone; legal review of redemption-window mail templates before deployment |
| Door-knocker liability incident | Low | High | Background checks, W-9, consent-law training, zero-tolerance disciplinary policy in 1099 agreement |
| OH state law change on redemption procedure | Very low | Medium | Monitor; redemption window is statutory (ORC §2329.33), would require legislation to change |

---

## 7. The 90-day sequence — what actually happens, week by week

| Week | Milestone |
|---|---|
| 1 | Aaron builds redemption-window infrastructure (court docket watcher + tags + preset). Aaron + DialForce owner finalize ISA placement details. |
| 2 | ISA placed; Aaron runs 60–90 min recorded onboarding. ISA shadows Mike for first week. Lead scoring module deployed; first "best 250 per county" rotation activated. |
| 3 | YellowLetters.com account opened. First 50-piece niched probate test mail (Montgomery). Track responses. |
| 4 | Soft-launch full Phase 1 mail spend ($3k/mo at ~1,700 pieces). Mail vendor template finalized for probate, foreclosure, redemption-window segments. |
| 5–8 | Steady-state Phase 1 operation. Track: mail response rate, niche-attributable contracts, ISA performance, DialForce reliability. |
| 9 | Day 60 review. Phase gate evaluation. Decision: Phase 2 / Hold / Pause. |
| 10–12 | If Phase 2: scale mail, recruit door-knockers, deploy redemption-window cadence. If Hold: refine, continue Phase 1. |
| 13–16 | Phase 2 steady state. Track 60-day rolling niche contract count. |
| 17 | Day 120 review. Phase gate evaluation. Decision: Phase 3 statewide expansion / Hold / Pause. |

---

## 8. Decision points for partner conversation

These need explicit yes/no/modify from Travis before Day 1 execution:

1. **Approve $3,000/month niched FTM mail spend for 60-day validation test?** Hard cap = $6,000 total at-risk capital before any deal closes.
2. **Approve acceptance of DialForce direct-placement ISA?** Implications: no incremental cost; commits Aaron's training time; requires backup-capacity contingency planning.
3. **Approve 1099 door-knocker model for Phase 2?** (Triggered only if Phase 1 validates.) Compensation structure ($2,000 flat OR 12% margin, capped at $5k) needs sign-off.
4. **Construction capacity check:** can the construction side absorb 5+ additional deals/month if niche converts? If not, what's the timeline to add capacity?
5. **Phase gate sign-off:** do we both agree on the triggers in Section 5? These are the rules that automate "scale vs pause" decisions and remove emotion from reinvestment.
6. **Acel transition:** SiftStack automates most of her current role. Pivot to data QA / lead validation, or wind down? Cost is small ($417/mo) but principle matters.

---

## 9. What this plan explicitly does NOT do

To be clear about what's out of scope so we don't accidentally drift:

- **Does not replace bulk DialForce.** Bulk continues unchanged as the primary lead engine.
- **Does not require new partner capital.** $3k/mo Phase 1 spend comes from existing operating cash flow.
- **Does not expand outside Ohio.** OH 20-county scoring CSV is in the back pocket; statewide expansion is Phase 3+ only.
- **Does not require new closer hiring.** Danielle handles current and Phase 1–2 closing volume. Phase 3 may require capacity add — flagged as risk.
- **Does not commit to direct mail beyond 60 days unless validated.** Phase gates govern reinvestment.

---

## Appendix A — File inventory for Travis review

If Travis wants to drill into any layer:

- **Operational SOPs:** [docs/SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md), [docs/SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md), [docs/SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md), [docs/SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md), [docs/SOP-TAG-FLOW.md](SOP-TAG-FLOW.md), [docs/SOP-RED-FLAGS.md](SOP-RED-FLAGS.md), [docs/SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md), [docs/SOP-DATASIFT-NAVIGATION.md](SOP-DATASIFT-NAVIGATION.md)
- **Scoring data:** `output/oh_county_scores_20260425_112126.csv` — OH 20-county investor-density ranking
- **Master Planner:** Drive id `1QqSV1Q9lD50RFlOvuA_r2yoN42ZyT4ZC` — payroll, marketing, goals, channel benchmarks
- **Technical architecture:** [CLAUDE.md](../CLAUDE.md) — full SiftStack architecture reference
