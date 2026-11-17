---
name: deep-prospecting-v5
description: Deep prospect real estate leads to identify the heirs/decision-makers and exactly who must sign to sell when an owner is deceased or skip tracing fails. Use when the user provides a property address, filing, probate docket, foreclosure notice, or any distress record and needs the correct owner/heir/executor plus their contact info. PRIMARY path is SmartSkip bulk skip trace (grounded relatives WITH their phone numbers in one batch call) plus a mandatory obituary/web research layer for date of death and true relationships, then Tracerfy gap-fill and TrestleIQ dial scoring. Delivers a family tree, a who-must-sign table, scored phone numbers, and emails.
---

# Deep Prospecting

Deep prospecting is the process for identifying and verifying the heirs/decision-makers behind a distress lead, and determining **who must actually sign for the property to sell**. Core philosophy: **"When everyone hits a wall, we bring a shovel."**

> **Version 5 (2026-07-29). The heir engine changed.** v4 resolved relatives through the Enformion/Endato Person Search. v5 replaces that with **SmartSkip**, measured head to head on live Knox and Blount County records. Enformion's *person* search is retired from this skill. Enformion **BusinessV2 is retained**, for entity owners only, where nothing else works.
>
> Why the change, with the numbers that drove it:
> - **Coverage.** Enformion returned **zero relatives on 6 of 12** owners tested. SmartSkip returned relatives on 12 of 12.
> - **Phones.** Enformion's `relativesSummary` carries names but **no phone numbers**. Every relative you actually want to call costs another billed search. SmartSkip returns the relatives *and their phones* in one batch row.
> - **Cost.** For a 100-owner batch with 682 relatives: SmartSkip plus Tracerfy = **$15.90**. The Enformion equivalent = **$78.20**. That is **4.9x**.
> - **Precision.** On the validation record, SmartSkip returned exactly 3 relatives and **all 3 appeared in the published obituary**. Enformion returned 50 (a capped blob) plus out-of-state numbers that looked like wrong-person bleed.
>
> What did NOT change: the research layer. SmartSkip is **wrong about death** (Gotcha 1). The obituary and web pass is now mandatory, not optional.

## The v5 stack

| Stage | Tool | Cost | What it gives you |
|---|---|---|---|
| A. Relatives plus phones | **SmartSkip** bulk skip | $0.15/hit | The grounded people graph, with numbers |
| B. Gap-fill | **Tracerfy** | $0.02/record | Phones for relatives SmartSkip named but left phoneless (about 7%) |
| C. Death and relationship truth | **Obituary / web research** | free | Date of death, who actually died, true relationships |
| D. Dial scoring | **TrestleIQ** | $0.015/number | Dial tiers, line type, litigator risk |
| E. Entity owners only | **Enformion BusinessV2** | ~$0.10 | LLC and trust principals (nothing else can do this) |

Typical single record: **about $0.24 all in.**

**Grounding rule (unchanged, and it is the whole point):** every name, address, phone, and relationship must come from a source you actually retrieved. Never infer or fabricate an heir. Surface conflicts; do not resolve them by guessing.

---

## THE GOTCHAS. Read these before running anything.

### 1. SmartSkip's `Deceased` flag is unreliable, and there is NO date-of-death column
Live proof: SmartSkip returned `Deceased = false` for a man who died 12/06/2025 and whose obituary was published by a funeral home. SmartSkip carries a `Deceased` boolean and **no DOD field at all**. Never let SmartSkip decide who is alive. **Date of death comes from the obituary and web pass, always.**

### 2. THE SPOUSE-OBITUARY TRAP. An obituary on the record does NOT mean the owner died.
This is the highest-value check in the skill and it is easy to miss.

A live CRM record sat on the Obituary list with a recent obituary date and a status of Deep Prospecting, which reads as "owner is dead, go find the heirs." The obituary was for **the owner's husband**, not the owner. She was alive and she owned the property. The correct action was not heir research at all; it was to call the owner, gently, eight months after she was widowed. A caller working the un-researched record would have opened by asking for a man who had died.

**Always confirm WHOSE obituary it is before treating a record as an heir case.** Match the decedent's name against the owner of record. If they differ, the record is a *living owner with a recent death in the household*, which is a different and often better lead. Correct the record, do not just work it.

### 3. SmartSkip's relationship labels are coarse. Verify them.
The column is literally called "Possible Type." In a 100-record batch, **63% of relationships came back generic** ("Relative" or "In-Law"). On the validation record it labeled the *husband of 62 years* as a plain "Relative." The obituary fixed it. Treat SmartSkip relationships as a starting hypothesis and let the research layer overwrite them.

### 4. The wallet does not pay for bulk skip
Topping up the SmartSkip wallet does **not** fund a bulk run. Verified live: with $25.00 in the wallet, a paid batch charged the saved Stripe card and left the wallet untouched. Bulk skip always bills the card through `payment-intent`. Budget on the card, not the wallet.

### 5. Unpaid orders are invisible. Keep the bulkSkipId.
An order that has not been paid does **not** appear in `GET /bulk-skip`. If you lose the `bulkSkipId` returned at upload, you cannot find the order again. Persist it before paying.

### 6. Entity owners cannot be name-traced at all
SmartSkip requires a first and last name. LLCs, trusts and estates return nothing, and Tracerfy is consumer-only. In a real vacant pull, **35 of 321 owners (11%) were entities.** Those route to Enformion BusinessV2 (see `references/entity-owners-enformion-business.md`). Filter entities out of the batch up front rather than discovering this per record.

### 7. The owner rule wins on a shared household line
When the owner and a relative share a number, that number carries **source and tier only, no relationship tag**. Otherwise the dial sheet labels the owner's own landline "Husband" and the caller misreads who is picking up.

### 8. The DOD sanity check still applies, and still anchors on the publication date
Reject an obituary match whose date of death is more than **3 years** before the notice's **publication** date (`date_published`, not the date you ingested the record). This still matters in v5: a stale-index date of 2004 surfaced on a live record during validation.

### 9. Never put a person's name in a phone tag
Names live on the message board using the last-4 method. Phone tags carry source, dial tier and relationship only.

---

## Workflow

### Step A. SmartSkip the owner (the heir engine)

Build a CSV with the required columns (First Name, Last Name, Mailing Address) plus the property address, then run `scripts/smartskip_trace.py`. Full API contract in `references/smartskip-api.md`.

```bash
export SMARTSKIP_EMAIL="..." SMARTSKIP_PASSWORD="..."
export SKIPTRACE_RUN_DIR="$HOME/dp_run"

# FREE: upload + auto-map columns + calculate. Prints the billable row count.
python3 scripts/smartskip_trace.py submit --csv owners.csv

# CONFIRM THE COST WITH THE OPERATOR, then bill the card:
python3 scripts/smartskip_trace.py pay --id <bulkSkipId>

# poll, then pull the export
python3 scripts/smartskip_trace.py status   --id <bulkSkipId>
python3 scripts/smartskip_trace.py download --id <bulkSkipId> --out export.csv
```

`submit`, `status` and `download` are free. **Only `pay` charges.** Never chain straight to payment on someone else's account without confirming the row count first.

Parse with `scripts/parse_smartskip.py`, which handles both export layouts (vertical "Campaign" and horizontal "CRM"), keeps Subject plus Relatives, drops Associates, drops absurd ages, and infers a canonical relationship tag.

**Expected yield** (Knox and Blount, vacant and distressed owners): about **89% hit rate**, roughly 6.8 relatives per record, about 16.6 unique numbers per record, and **93% of relatives arrive with at least one phone**.

### Step B. Tracerfy gap-fill (small, cheap, often unnecessary)

Only for relatives SmartSkip **named but returned no phone for**. That is roughly 7% of relatives, and in practice all of them carry a mailing address for Tracerfy to anchor on. On a 100-record batch this was 45 people and **90 cents**. Skip the step entirely when the gap is zero, which is common on single records.

### Step C. Obituary and web research (MANDATORY for any suspected death)

This is what replaced Enformion, and it does the job Enformion was originally brought in to do: grounding. The difference from the v3 hallucination problem matters. Back then an LLM was asked to *invent the heir set* from obituary prose. Now SmartSkip hands over a grounded relative list and the research layer only has to **confirm relationships and supply the date of death**. That is a far safer job.

Do all of these:
1. Search the decedent's name plus city plus "obituary" plus the year. Funeral-home pages are the best source.
2. **Confirm whose obituary it is** against the owner of record (Gotcha 2).
3. Extract the verbatim survivors paragraph. Reconcile it against the SmartSkip relative list in both directions: relatives SmartSkip found that the obituary does not name, and survivors the obituary names that SmartSkip missed (married-out daughters are the classic miss).
4. Overwrite SmartSkip's coarse relationship labels with the obituary's real ones.
5. Record the date of death and run the 3-year sanity check.
6. If pages sit behind Cloudflare or a JS wall, use the Scrapfly fetcher. County, records and genealogy portals are the sweet spot; hardened people-search aggregators frequently IP-ban it.

`references/google-dorking.md` has the query patterns. `references/deed-analysis.md` covers title.

### Step D. TrestleIQ scoring

Score the deduped union of every number: existing CRM numbers, SmartSkip numbers, Tracerfy numbers. **Dedupe globally first**, since a number's tier is the same wherever it appears. Skip numbers that already carry a recent tier.

Dial tiers (identical across every skill in this library): **81-100 Dial First, 61-80 Dial Second, 41-60 Dial Third, 21-40 Dial Fourth, 0-20 Drop.**

### Step E. Signing analysis

Determine who must sign, ranking decision makers by signing authority under state intestacy rules, from **verified living** heirs only. Never assert an heir interpretation; flag it. For probate, label relationships relative to whoever was skip traced ("Sons of [PR]").

Reconcile the signer set against the published obituary's survivor list, not the relative graph alone.

### Step F. Deliverable

Produce the research pack (`templates/research-pack-template.md`) and render it to PDF for upload as a record attachment. Lead the pack with the **headline correction** whenever research changed what the record means, as in the validation example, because that is what actually stops a bad call.

Write back to the CRM: phones with source, tier and relationship tags, one combined message-board post, and property tags.

---

## When to Use This Skill

- Skip trace returned no usable phone numbers
- Called 3 or more attempts with no contact
- Vacant mailing address discovered
- Return mail (bad address)
- Probate cases (often only a docket number)
- Entity or LLC ownership (needs the decision maker, see Gotcha 6)
- Conflicting owner or address information in public records

## Cost model

| Scenario | Cost |
|---|---|
| One record, full flow | about $0.24 |
| 100 records, 682 relatives | about $15.90 (SmartSkip $15.00 plus Tracerfy $0.90) |
| Same via the retired Enformion person path | about $78.20 |
| Entity owner (BusinessV2) | about $0.10 |

Trestle is usually the largest line item on a big batch because it prices per unique number. Dedupe globally before scoring and skip already-tiered numbers.

## What v5 removed

The Enformion **person** search. `scripts/enformion_person_search.py` is retained only as a deprecated reference for anyone still running v4. Its failure modes, for the record: zero relatives half the time, no phones on the relatives graph, a roughly 50-relative cap that silently truncates, a surname gate that drops married-out daughters, `isDeceased` flags that lag reality, and wrong-person matches when anchored on city and zip alone rather than the full street line.
