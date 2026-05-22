# Probate Deep-Prospecting — Lessons From 128 Completed Cases

**Source:** Synthesis of all 128 completed deep-prospect case files (Jefferson County KY probate + foreclosure leads worked manually with Claude, ~Apr–May 2026). Counts are approximate (aggregated from 4 parallel reviews of 32 cases each) but the *rank order* is robust.

**Purpose:** Ground the automated probate-enrichment pipeline ([phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md)) in what the manual process actually had to do. The headline: a naive **"find PVA property → find CourtNet executor → skip trace"** pipeline (which is roughly Phase 2 as first scoped) would **mishandle ~60–70% of these cases**. The clean happy path fit only ~10–14 of every 32.

---

## The seven recurring patterns, ranked by frequency

### 1. Skip trace is the bottleneck, not the finish line — ~66% (≈85/128) ended at "NEEDS TRACERFY"
The DM's phone was **not** available from free sources in two-thirds of cases. Free people-search sites (TruePeopleSearch / FastPeopleSearch / Radaris) were frequently **403/Cloudflare-blocked** (Forshee, Blair, Bell, Buschman, McDaniel, Nagel, Preston), and the Trestle key returned **dead 403s** (O'Connor, Herflicker). Out-of-state "ghost" DMs (Morrison-CO, O'Connor, Poe, Perrin-TN/FL, Roberts-IN, White-John-NKY) don't surface in KY-centric search.
> **Implication:** The current plan says *"skip trace is NOT auto-invoked by deep-prospect."* The data says skip trace is the **single most common gap** — it must be a first-class auto-step, not deferred. Without it, the pipeline ships near-empty contact records two times out of three.

### 2. Name-matching is the silent killer — ~73% (≈94/128) had at least one name quirk
Exact-string matching on the filed name breaks constantly:
- **Maiden / prior-married surname** — CourtNet sometimes indexes the decedent *only* under the maiden name (canonical: Jackson-Greathouse — a `LAST=Jackson` search returned **nothing**; the case lived under GREATHOUSE). Also Ayers/Weartz, Faulkner/Higgins, Foreman/Crowdus, Frindel/Fallmann, Slone/Blocker, Wheatley/Prince, Krebs/Webb, Kirchner/Geftos.
- **Married-name heirs ≠ decedent surname** — the DM rarely shares the decedent's last name (Hawkins, Donlon, Grassi, Chesser, Kirchner-from-Marx, Mikelson).
- **Legal name changes** — Underwood's heir changed names 3× (Underwood→Koenig→Price); Lewis dropped "III" in 2021; Baker (Marshall Carlisle→Carlisle); Meehan (Marksberry→Meehan). Liens filed under the old name are missed.
- **Non-Anglo naming** — Cuban/Hispanic paternal+maternal (Farinas: García/Fariñas; Gonzalez-Gonzalez = a *sibling cohort*, not a typo; Pena≠Martinez spouse), Slavic gender feminization (Lozinskaya↔Lozinskiy, Grabova↔Graboviy), Chinese (Zacharias stepson Chen), Croatian (Herflicker).
- **Same name, different person (false positives)** — 3 Thomas Shavers, 2 Richard A Wagners, 4 James A Browns, 2 Donald R Riggs, a different Rachel Williams with USAA mortgages, a different David Skaggs with tax liens. Naive lien/deed joins attach the **wrong** encumbrance.
- **Clerk typos** — "JACSON MONROE", "MILLER" for MEIER, "TOMPSON", middle-initial drift (Wagner L vs A).
- **Co-borrower-only indexing** — Burkhart's mortgage surfaced only under the wife's (MARY) surname.
> **Implication:** Need name-variant fan-out (maiden / prior / changed / suffix / co-borrower / non-Anglo) **and** a same-name disambiguation guard (by age, address history, DOD, obit cross-ref) across every lookup (PVA, CourtNet, deeds, skip trace).

### 3. The CourtNet executor is often NOT the person who can sell the house — ~26% (≈33/128)
The deal-defining question — *does this even need probate to sell, and who actually controls the deed?* — is invisible to "PVA owner + CourtNet executor":
- **Revocable/living trust (~16 cases)** — Sauer, Schrenger, Williams, Zacharias, Smith-Charles, Atlas (QPRT), Bryan, Byrd, Guss, Long, Morrison, Mudd-Keysferry, Palmer-Ball, Plymale. The PVA owner reads "X REVOCABLE TRUST", the probate is **personal-estate-only**, and the real DM is the **successor trustee** named in a private (often unrecorded) trust agreement — who can sell **without closing probate**. Worst case Smith-Charles: trust-titled + DM unidentifiable from public records.
- **Joint title / survivorship (~12 cases)** — Hale, Karem, Kirzinger, Koch (TBE), Layton, Martin-Bluffview, Pfeifer, Wagner, Tedder, Woods, Stratman. Real estate passes to the surviving owner outside probate; the court administrator governs only personal property.
- **Already deeded pre-death (~4)** — Caffee, Robbins, Harper, Hodges (often Medicaid asset-protection 3–4 yrs pre-death). Property is **out of the estate**.
- **Already sold (1)** — Bell (to a flipper LLC before the lead was worked).
> **Implication:** Need a **title-path classifier** that runs *before* DM assignment: parse the PVA owner string + latest deed (vs DOD) to route each lead to standard-probate / successor-trustee / surviving-owner / out-of-estate. Picking the CourtNet executor blindly names the wrong DM a quarter of the time.

### 4. "Free-and-clear" ≠ full equity, and assessed value ≠ sellable equity — ~36% (≈46/128) carried distress liens
- **Medicaid / MERP / TEFRA estate recovery** — Duckworth (DMS noticed as a party), Harper (a nursing home *force-filed* the probate), and an **elder-law/Medicaid-specialist attorney** is itself a signal (Jenkins-Ruth, Underwood, Duckworth-Bullock). MERP can wipe equity on an otherwise mortgage-free home.
- **HECM reverse mortgages** — Wheatley, Herflicker: due-and-payable on death, negative-amortizing, *invisible* to a "standard open mortgage" check, and a straight-line balance estimate is wrong.
- **Judgment / credit-card / tax / code liens + lis pendens** — Presley (junior VA + CC liens eat the equity), Logsdon (4 unreleased state liens), Buschman, McMenamin, Walker/Thompson-Hale (code+tax liens near or above value → negative equity on a "free-and-clear" home).
- **Capped result sets hide releases** — Murphy, Mudd-Francis, Perrin: a mortgage with **no release in the first page of deed results** was only *estimated* open. JCD pagination (1 record/key) and 50-record windows hide the release.
> **Implication:** Replace binary free-and-clear with a **lien/encumbrance sweep** (full history, not first page): mortgages+releases, state/judgment/tax/code liens, lis pendens, HECM/HUD instruments, plus a Medicaid-risk proxy (DMS party or elder-law attorney). Net junior liens before reporting equity.

### 5. The freshest, most valuable leads aren't indexed yet — ~16% (≈21/128) hit CourtNet/obit latency
"Filed today/this week" probates returned **0 rows** at recon time (Barnett, Davis, Blevins, Angelini, Parrino, McCoomer, O'Connor, Smith-Charles still missing at ~25 days). Obits weren't indexed yet for ~15 cases. And ~9% (≈11/128) had **no probate at all** — the death surfaced as a Lis Pendens / tax foreclosure with "UNKNOWN HEIRS" defendants and a court-appointed Warning Order Attorney (Combs, Cooper, Dorsey, Blair, Gonzalez, Herflicker, McGarvey, Rutter, Spencer, Thompson-Hale, Walker). McGarvey: dead 4.5 years, no probate ever, looming tax foreclosure = *most* motivated.
> **Implication:** A one-shot synchronous lookup drops live leads. Need a **re-poll queue** (re-search after 3–5 business days) for fresh CourtNet cases + obits, and a **no-probate branch** (lis-pendens / affidavit-of-descent / heir identification) — which directly overlaps Phase 3 lis pendens.

### 6. ~25% (≈32/128) were weak fits the naive pipeline would still spend credits on
Recurring weak-fit archetypes, none detectable from "PVA hit + executor + phone":
- **Decedent owns no real estate / renter** (Humphrey, Fernandez, Maupin, Peter, Skaggs, Schubert-wrong-property) — ~5%.
- **No / thin / negative equity** (Jackson-Lorene 100% LTV, Spencer, Schubert-condo, Cooper/Dorsey vacant teardowns).
- **Property already out of estate** (Caffee, Robbins, Bell, Smith-Algonquin).
- **Luxury / high value tier → lists with a broker, not a wholesaler** (Atlas $1.7M, Frindel $1M, Smith-Charles $1.14M, Jewell, Karem, Huntington, Hale $750K).
- **Sophisticated / investor DM who resists wholesale** (Williams RIA principal, Zacharias neurosurgeon, Moriarty flipper, Barton/Crain savvy operators).
> **Implication:** A **wholesale-fit score/gate** (ownership confirmed? value band? equity after liens? DM sophistication proxy?) should drop or down-rank weak leads **before** spending Tracerfy/Trestle credits — exactly the spirit of the plan's "PVA hard filter," extended.

### 7. DM identification is messier than "the executor" — co-signers, non-family PRs, inversions, attorney fallback
- **Non-family executors — ~13% (≈17/128)** — friends (Roberts/Zipperle, Gross/Johnson, Funk/Langness), in-laws (Sauer/Patterson sister-in-law, Bryan/Stultz), professional/public administrators (McMenamin/Furnish), creditors (Harper/Regency nursing home). The PR's surname won't match the decedent and may not be in any people-search under a family link.
- **Co-signers / fractured heirship (~30 cases)** — co-executors (Atlas, Ford, Frindel, Palmer-Ball, Mudd-Francis 3 sisters), intestate heir splits (Walker ~10, Rutter 11, Rogers 4 sons, Lynch 6 siblings), per-stirpes claims from predeceased children (Tedder, Tindall, Woods, Smith-Algonquin). Single-DM output understates closing friction; **count "N signatures required."** Flag non-heir co-PRs (Ford: partner Pam has no legal inheritance under KY law).
- **Decedent/DM inversion** — the user themselves inverted two: Rutter (Rose alive = administrator; mother Judy Pryor = decedent) and Spencer (father Stephenson = decedent; son Spencer = DM, different surname). Always reconcile DEC vs P/AA from the party graph before trusting the input framing.
- **Placeholder / typo EE fields** — Preston's EE = estate-name placeholder "PRESTON, DEAN"; Meier's petitioner = clerk typo "MILLER". Validate that EE/P resolves to a real person.
- **Attorney (AP) as fallback channel** — when skip trace fails and CourtNet guest tier hides party addresses (Gross, McCawley, O'Connor), the **estate attorney** is the reliable route, and the petitioner's address/phone is in the **AOC-805 petition image** the tool doesn't fetch.

---

## Prioritized improvements for the automated pipeline

In rough ROI order (frequency × correctness impact):

| # | Improvement | Fixes pattern | ~Cases |
|---|---|---|---|
| P1 | **Auto skip trace** (Tracerfy first-class) **+ staleness/death-index guard** (reject dead-person phones like Davis/Armstrong; age sanity) **+ out-of-state pull + attorney/AOC-805 fallback** | #1, #7 | ~85 |
| P2 | **Name-variant fan-out + same-name disambiguation** (maiden/prior/changed/suffix/co-borrower/non-Anglo; guard by age/DOD/address) across PVA, CourtNet, deeds, skip trace | #2 | ~94 |
| P3 | **Title-path classifier** before DM assignment (PVA owner string + latest-deed-vs-DOD → standard-probate / successor-trustee / surviving-owner / out-of-estate) | #3 | ~33 |
| P4 | **Lien/encumbrance sweep** replacing binary free-and-clear (full-history mortgages+releases, state/judgment/tax/code liens, lis pendens, HECM; Medicaid proxy via DMS party or elder-law attorney) | #4 | ~46 |
| P5 | **Wholesale-fit score/gate** before spending credits (ownership confirmed, value band, equity-after-liens, DM-sophistication proxy) | #6 | ~32 |
| P6 | **Re-poll queue** for fresh CourtNet/obit + **no-probate branch** (lis pendens / affidavit of descent) | #5 | ~32 |
| P7 | **Full party-graph capture + DEC/P reconciliation + co-signer count + placeholder/typo validation** | #7 | ~47 |

### What stays right in the current plan
- The **PVA-as-hard-filter** instinct is correct — just split "no property = drop (renter)" from "trust-titled = keep, research trustee" (don't drop a trust just because the owner string isn't a person's name).
- **DOD sanity check (MAX_DOD_GAP_YEARS=3)** is sound but needs nuance: a *late* filing on a long-held free-and-clear home is **sell-intent**, not noise (Layton 2.5yr, Harper 16mo). Surface DOD-uncertainty rather than silently rejecting (Logsdon sits right at the 3yr edge).
- **Caching by name/case** is correct, but the re-poll queue (P6) is the missing half for fresh filings.

---

## References
- Pipeline plan being improved: [phase_2_ky_probate_enrichment.md](phase_2_ky_probate_enrichment.md)
- Lis pendens / no-probate overlap: [phase_3_ky_lis_pendens_apify.md](phase_3_ky_lis_pendens_apify.md)
- Case files: `~/.claude/projects/.../memory/project_*.md` (128 completed deep-prospect records)
- Existing tool surfaces: `src/property_lookup.py`, `src/kcoj_case_detail.py`, `src/jefferson_deeds_scraper.py`, `src/obituary_enricher.py`, `src/deep_prospector.py`, `src/report_generator.py`
