# Deep Prospecting Research Pack

**Property:** {PROPERTY ADDRESS}
**Research Date:** {DATE}
**Researcher:** {RESEARCHER}
**Path used:** {API (Enformion/Endato + Trestle) | Manual 3-site waterfall}

---

## 1) Level Selected & Why

**Level:** L{1/2/3}

**Reason:** {Brief explanation of why this level was selected based on initial findings}

---

## 2) Source Checklist

### L1 Baseline
- [ ] County Assessor/CAD  -  {1-line note}
- [ ] Recorder/Deed image  -  {1-line note}
- [ ] Google dorking  -  {1-line note}
- [ ] Tax payment history  -  {1-line note}
- [ ] Clerk civil/criminal/dockets  -  {1-line note}

### L2 (if applicable)
- [ ] Deed chain (last 3-5 instruments)  -  {1-line note}
- [ ] Name-variant sweep  -  {1-line note}
- [ ] Cross-county searches  -  {1-line note}

### L3 (if applicable)
- [ ] Obituaries  -  {1-line note}
- [ ] Family tree  -  {1-line note}
- [ ] Decision-maker ID  -  {1-line note}

---

## 3) Title & Ownership

**Current Owner(s):** {Name(s) as shown on deed}

**Instrument Type:** {Warranty Deed / QCD / Executor's Deed / etc.}

**Vesting:** {Sole / Joint Tenants / Tenants in Common / Trust / etc.}

**Red Flags:**
- {List any concerning findings: QCD, installment contract, multiple heirs, etc.}
- {Or "None identified" if clean}

---

## 4) Identity Resolution

*(Include only if name/address variants exist)*

**Variants Found:**
- {Variant 1}
- {Variant 2}

**Winning Variant:** {Name that matched across sources}

**Why:** {1-2 line explanation of how identity was confirmed}

---

## 5) Genealogy/Heir Findings

*(Include only if family/estate elements appear)*

**Obituary Links:**
- {URL 1}
- {URL 2}

**Survivors Identified:**
- {Name}  -  {Relationship}
- {Name}  -  {Relationship}

**Relationship Notes:** {Any relevant family dynamics, executor status, etc.}

---

## 6) Heir Map

*(Required for L3; include for L1/L2 if relationships are relevant)*

```
Decedent: † {DECEDENT FULL} (DOD {YYYY-MM-DD}) [{CITY, ST}]
Spouse/Partner:
└─ {SPOUSE FULL} [{CITY, ST}]
Children:
├─ {CHILD 1} [{CITY, ST}]
│   └─ Notes: {maiden/married name, match cues}
├─ {CHILD 2} [{CITY, ST}]
└─ {CHILD 3} [{CITY, ST}]
Siblings:
├─ {SIBLING 1} [{CITY, ST}]
└─ {SIBLING 2} [{CITY, ST}]

Markers: † = deceased, ★ = executor (confirmed), ▸ = likely decision-maker
```

---

## 7) Decision-Maker Identified

**Name:** {FULL NAME}

**Relationship:** {Owner / Heir / Executor / Surviving Spouse / etc.}

**Current Address:** {Best known mailing address}

**Estimated Age:** {Age range based on records, e.g., 55-65}

**Confidence:** {HIGH / MEDIUM / LOW}

**Confidence Reasoning:**
- {Bullet point explaining why confidence level}
- {E.g., "Name matches deed with middle initial"}
- {E.g., "Address confirmed via tax records"}

---

## 8) Who Must Sign to Sell (required for any deceased owner)

| Heir (child) | Est. share | Lives at property? | Signature required? | Notes |
|--------------|-----------|--------------------|--------------------|-------|
| {name} | 1/N | yes/no | **Required** | on-site / out-of-state / unverified relationship / etc. |

**Signing risk flags to verify (title search / probate attorney  -  never state as legal conclusions):**
- Surviving spouse? (takes a share, must also sign)
- A will? (overrides equal split, names an executor)
- Did a child predecease the parent? (their share → their kids, per stirpes  -  extra signers)
- Recent second household death? (may add a probate layer  -  see DOD conflict)
- Close kin with a different surname (married-out daughter)? (confirm whether a child)

> Out-of-state signers are the usual closing bottleneck  -  identify and engage them early.

---

## 9) Master Dial Sheet (deduped, best number first)

```
═══════════════════════════════════════════════════════════════════════
        MASTER DIAL SHEET  -  {PROPERTY}  -  {ESTATE} (deduped)
═══════════════════════════════════════════════════════════════════════
PHONE            SCORE  TIER         TYPE      REACHES               DNC
(xxx) xxx-xxxx   100    Dial First   Mobile    {shared household}    clear   <- start here
(xxx) xxx-xxxx    70    Dial Second  Landline  {Signer B}            clear
(xxx) xxx-xxxx   100    Dial First   Mobile    {out-of-state signer} clear
(xxx) xxx-xxxx    95    Dial First   Mobile    {Signer C}            DNC ⚠
-----------------------------------------------------------------------
Shared family landlines collapse to ONE entry. Lead with the highest-score,
DNC-clear mobile reaching the on-site / most-engaged signer. Drop any number
flagged litigator-risk regardless of score.
═══════════════════════════════════════════════════════════════════════
```

**How these were produced:**
- **API path (preferred):** Enformion/Endato Person Search on the decedent (heir set) → per-signer search → phone dedupe → Trestle activity scoring. Run `scripts/enformion_person_search.py` to generate this sheet directly.
- **Manual fallback:** trace each signer at TruePeopleSearch, FastPeopleSearch, and CyberBackgroundChecks; cross-reference numbers appearing on 2+ sites.

One contact card per signer (name, age, current address, best number, email) so every required signature has a way to be reached.

---

## 10) Next Steps / Missing Information

*(Include if any information could not be found)*

**Missing:**
- {Item 1}  -  [MISSING]
- {Item 2}  -  [MISSING]

**Recommended Actions:**
- {Action to resolve missing item 1}
- {Action to resolve missing item 2}

**Title Attorney Consult Needed:** {Yes/No  -  explain if yes}

---

## Research Notes

{Any additional observations, alternative contacts, or context that may be useful for outreach}
