# Registration and Remediation Guide

Prevention (register once, free), plus the runbook for a number that is flagged right now.

## Why numbers get flagged (60-second version)

Carriers do not label calls themselves. Three private analytics engines do, split by carrier:

| Engine | Carrier | On-screen label |
|---|---|---|
| Hiya | AT&T | "Spam Risk" |
| First Orion | T-Mobile | "Scam Likely" (hardest to clear) |
| TNS Call Guardian | Verizon | "Potential Spam" |

They do NOT share scores. A number can be clean on one carrier and flagged on another, and nobody notifies you either way.

What actually triggers a flag is behavior:

- **Volume spikes and dead days.** Healthy traffic is steady. Warm new numbers up gradually.
- **Short-call ratio:** calls under 6 seconds should stay at or under 15% of outbound.
- **Average length of call (ALOC):** keep at or above 30 seconds.
- **Answer rate (ASR):** keep at or above 30%.
- **Consumer complaints and blocks:** even 5-10 can tip a low-volume number.
- **Recycled history:** a number you just bought can arrive pre-flagged from its previous owner. This is why remediation usually beats replacing numbers.

STIR/SHAKEN is authentication, not a spam score. A-attestation (the network vouches that you own the number) is a positive input but does not make you immune. SmrtPhone handles STIR/SHAKEN and CNAM through its Trust Center; confirm yours is set up there.

## Prevention: Free Caller Registry (do once, free)

**https://www.freecallerregistry.com/fcr/** is a single form that fans your business identity and numbers out to all three engines. This is the single biggest free lever.

1. Complete the 3-step form: business info, your numbers (up to 20 typed in, or a file upload for more), and contact details. Email verification is required.
2. Service Provider: whoever provisions your numbers (for SmrtPhone users this is typically Telnyx). Category: pick the closest fit, "Other" is fine.
3. Save the acknowledgement emails. Each engine replies separately (First Orion, Hiya, TNS), on its own timeline.
4. Track it in `config/numbers.json` under `registration`: set `submitted` (YYYY-MM-DD; the monitor auto-fills `recheck_by` 90 days out as a re-verify reminder, this is not a renewal), and flip each engine under `carriers` to `confirmed` as its email arrives.

Re-submit only on real triggers: a number gets re-labeled, you add new or recycled numbers, or your business name / branding / category changes. Resubmits are free and unlimited.

## Remediation runbook: a number is flagged NOW

1. **Bench it immediately.** Pull it from the dialer pool. The monitor does this automatically (RESTING, 30 days) when it sees the flag; if you learned about the flag another way (a prospect told you, you saw it on a test call), rest it manually.
2. **Identify which engine flagged it** if you can. The engine tells you the carrier and the portal. If you only know "it shows Spam Likely on someone's phone," check what carrier that phone is on.
3. **Dispute it (all free):**
   - Re-submit at the Free Caller Registry (fastest single move, covers all three engines).
   - Or go direct to the carrier portal:
     - T-Mobile / First Orion: https://calltransparency.com/
     - Verizon / TNS: https://voicespamfeedback.com/vsf/
     - AT&T / Hiya: https://hiyahelp.zendesk.com/hc/en-us/requests/new
   - If you enabled the optional Telnyx layer, the monitor auto-files a remediation request through the API and polls the result.
4. **Fix the root cause or it re-flags.** Look at that number's history in the dashboard: was it over-volumed? Is the list quality bad (high wrong-number rate drives complaints)? Was it a recycled number that arrived dirty? Remediation without a root-cause fix is a revolving door.
5. **Expect:** roughly 1-2 weeks to clear on AT&T and T-Mobile, 2-4 weeks on Verizon, then a few days to propagate to handsets. Not guaranteed.
6. **Three strikes: retire it.** If a number survives remediation and re-flags 3 times (the monitor tracks this), retire it and warm up a replacement. But default to remediation over churn: a fresh number needs a 2-3 week warm-up ramp and may carry a recycled dark past, so replacing numbers is not the cheap escape it looks like.

## Warm-up ramp for new numbers

The monitor enforces this automatically via the `added` date, but for reference:

| Week | Max dials/day |
|---|---|
| 1 | 30 |
| 2 | 75 |
| 3 | 125 |
| After | 75 steady (active cap) |

Stagger activation when adding several numbers at once; do not light up five new numbers on the same morning.
