<!-- Generated 2026-06-25 from a verified multi-source research pass (7 research dimensions + 12 adversarially fact-checked claims + synthesis). Inline (URLs) are the cited sources. Companion: README.md (runbook) and the monitor.py code. -->

# Caller ID Reputation Monitor: Definitive Methodology and Implementation Guide

For a real-estate investment operation cold-calling foreclosure and distressed homeowners from approximately 42 Twilio VOIP numbers via SmrtPhone / ReadyMode, with a pre-call MMS (auction-notice screenshot), running on Windows 11, Python 3.12, Slack, and Windows Task Scheduler.

This guide is built to replicate AND improve on a colleague's free, 4-layer "Caller ID Reputation Monitor." Every load-bearing fact is sourced. Where a verification verdict corrected a claim, the corrected version is used and the correction is noted.

---

## 1. Executive summary

- Spam labeling is NOT one system. Three private analytics engines decide the label on the three major carriers: First Orion powers T-Mobile (and Metro, Boost; the source of "Scam Likely"), Hiya powers AT&T (and Cricket, plus the Samsung native dialer), and TNS Call Guardian powers Verizon (and US Cellular) (https://blog.hiya.com/dont-pay-a-vendor-to-register-your-phone-numbers-across-carriers-use-free-caller-registry). A number can be clean on one and flagged on another, and a flag propagates within hours.

- Labeling is mostly behavior-driven and complaint-driven, not registration-driven. The FCC's own "reasonable analytics" indicia name large bursts of calls in a short window, low average call duration, and a high volume of complaints as primary signals (https://www.dwt.com/insights/2020/08/fcc-robocall-blocking-safe-harbor-rules). A high-volume cold foreclosure dial pattern is exactly the profile these engines flag, so registration alone will not protect a number that dials like a robocaller.

- Two free wins come first, both genuinely free: (a) full STIR/SHAKEN A-attestation through Twilio Trust Hub for the Twilio-hosted numbers, and (b) registering all 42 numbers in one submission at FreeCallerRegistry.com, which fans out to all three engines (https://freecallerregistry.com/fcr/public/html/home.html). A-attestation authenticates identity; it does NOT stop spam labeling (https://www.numeracle.com/insights/stir-shaken-doesnt-stop-improper-spam-labeling).

- There is no free, self-serve API that returns the actual carrier-displayed label across all three carriers. The free monitoring layer (IPQualityScore, Twilio Lookup + Nomorobo, Telnyx) returns a risk PROXY or a single engine's view, not the literal "Scam Likely" a recipient sees. Treat the automated monitor as an early-warning proxy, and use periodic manual app lookups plus live test calls for ground truth.

- Behavior is the real lever: cap each number at roughly 50-75 dials per day (never over 100-150), keep average call duration above about 30 seconds, avoid bursts of sub-10-second calls, cap redials to 2-3 per recipient per day spaced about 4 hours apart, and keep predictive-dialer abandonment at or under 2% (https://readymode.com/avoid-being-flagged-as-spam-in-outbound-calls/, https://www.numeracle.com/insights/best-practices-for-dialing-strategies).

- Rotation is no longer a shield. Modern reputation specialists warn that churning to brand-new no-history numbers reads as spammer "number hopping" and just gets the replacement flagged. Treat the 42 numbers as 42 reputations to protect: warm new numbers, rest flagged ones, and only retire after a second flag survives remediation (https://www.numeracle.com/insights/best-practices-for-dialing-strategies).

- Texting reputation is a separate scoreboard. SMS/MMS deliverability is governed by A2P 10DLC registration through The Campaign Registry (TCR), not STIR/SHAKEN (https://www.twilio.com/docs/messaging/compliance/a2p-10dlc). The pre-call MMS must run on a registered Standard brand/campaign, monitored via Twilio message error codes and opt-out rate. Spreading the same cold MMS across 42 numbers is "snowshoeing" and is carrier-penalized.

---

## 2. How carrier spam-labeling actually works

### The three engines and their carriers

US "Scam Likely / Spam Risk / Potential Spam" labels are produced by three independent analytics engines, each contracted to specific carriers. This engine-to-carrier mapping is the load-bearing fact of the whole system (it is confirmed by Hiya's own page, a primary source, in the verification):

- First Orion: T-Mobile, Metro, Boost. Produces "Scam Likely" / "Spam Likely." Powers T-Mobile Scam Shield (https://firstorion.com/products/free-number-registration).
- Hiya: AT&T, Cricket, and the native dialer on Samsung Android devices. Produces "Spam Risk" / "Fraud Risk" / "Scam." Powers AT&T ActiveArmor (https://blog.hiya.com/dont-pay-a-vendor-to-register-your-phone-numbers-across-carriers-use-free-caller-registry).
- TNS (Transaction Network Services) Call Guardian: Verizon and US Cellular. Produces "Potential Spam" / "Potential Fraud." Powers Verizon Call Filter (https://tnsi.com/solutions/communications/robocall-protection/).

Scores do NOT transfer between engines. A number can be clean on Hiya/AT&T and flagged on TNS/Verizon at the same time. But each engine pushes labels to its partner carriers near-continuously, and consumer reports propagate within hours (https://lineshield.theidudes.com/blog/hiya-tns-firstorion-spam-labels).

### What the engines score

The engines do not publish their algorithms, but the consensus of the FCC indicia, carrier-facing providers, and industry sources is consistent. Each engine blends:

- Call velocity and volume (large bursts of calls in a short time frame).
- Short-duration / hang-up ratios (low average call duration; bursts of sub-10-second calls). TNS and First Orion weight short-duration-hangup ratios heavily; First Orion weights hangup-before-ring patterns (https://leadsatscale.com/insights/cold-calls-flagged-as-spam-how-to-fix/).
- Answer / connect rate. A low single-digit answer rate at high volume is itself a robocall fingerprint.
- Consumer complaints and block/report presses, normalized to dial volume.
- STIR/SHAKEN attestation level.
- CNAM-to-entity consistency, number age, and ownership change.
- "Neighborhood" reputation of adjacent numbers.

The FCC's safe-harbor order explicitly names "large bursts of calls in a short time frame," "low average call duration," and "a large volume of complaints related to a suspect line," plus neighbor-spoofing patterns (https://www.dwt.com/insights/2020/08/fcc-robocall-blocking-safe-harbor-rules). Reputation is recency-weighted: Hiya's developer documentation states spam labeling is "dynamic for every phone call, not a static property of a phone number," favoring calls from the past several days or weeks (https://developer.hiya.com/docs/guides/voice-protection/number-reputation/get-number-reputation-data). That recency weighting is exactly why resting a number works and why a complaint spike flips a clean number fast.

### STIR/SHAKEN, briefly (full detail in Section 3)

STIR/SHAKEN is the FCC-mandated call-authentication framework. The originating service provider (Twilio here) cryptographically signs each call with an attestation level: A (full), B (partial), or C (gateway). Per RFC 8588 and ATIS-1000074, A means the provider authenticated the customer AND confirmed their right to use the calling number; B means known customer but unverified number; C means the call entered from an untrusted external source (https://www.rfc-editor.org/rfc/rfc8588.txt). Attestation is an input to the analytics engines, not a labeling mechanism itself. A lower level (especially C) raises flag likelihood; an A is NOT guaranteed to avoid a label (https://www.bandwidth.com/blog/abcs-of-attestation-and-analytics/).

### Why VOIP / Twilio numbers get flagged

The engines do not block VOIP categorically. But VOIP DIDs draw extra weight for several reasons that all apply to a 42-number foreclosure cold-call shop:

- Recycled DIDs inherit the prior owner's reputation. Twilio is explicit that "nuisance labels are not automatically removed from a number after the previous owner releases it" (https://help.twilio.com/articles/9375068873499-Outbound-calls-blocked-or-labeled-as-spam-or-scam-likely). A brand-new number can show "Scam Likely" on day one because of who held it before.
- High cold-call velocity on a distressed-homeowner list produces exactly the complaint, short-duration, and low-answer signals the engines weight most.
- A generic or mismatched CNAM such as "TWILIO INTERNATIONAL" plus VOIP origin raises suspicion (https://www.fcc.gov/call-authentication).
- Lower attestation on numbers not properly provisioned through Trust Hub is a negative signal.

The combination of carrier-of-record VOIP, high cold-call velocity, and the foreclosure category is a high-risk profile that earns labels fast.

---

## 3. Layer 1: Registration and branding

### 3a. Free Caller Registry, step by step

Free Caller Registry (FreeCallerRegistry.com) is operated jointly by First Orion, Hiya, and TNS. A single submission is distributed to all three engines simultaneously, each of which then independently vets it. It is completely free, and there is no need to register a number more than once (https://freecallerregistry.com/fcr/public/html/home.html). Registration reduces but does NOT guarantee against mislabeling, and it is not a whitelist.

The flow is a 3-step web form with email-code verification:

- Step 1, business and calling info: business name, street, city, state, ZIP, website, plus a contact name, phone, and email, plus the call purpose/category and outbound calls per month (https://www.freecallerregistry.com/fcr/).
- Step 2, numbers: up to 20 ten-digit US numbers typed into the form, OR a file upload (xlsx/txt, up to 100 KB) for larger sets. With 42 numbers, use the file upload, or split into two-plus typed submissions.
- Step 3, terms: accept the acknowledgement/authorization.
- Verification: a code is emailed to the address on the form and must be entered before the submission completes.

Recommended field values for a Twilio cold-call shop (medium confidence, from GoHighLevel's remediation guide): display name left blank, call/belief category "Other," Service Provider "Twilio," additional feedback left blank. You may resubmit as many times as you want, with no limits (https://help.gohighlevel.com/support/solutions/articles/155000005891-remediate-spam-likely-on-your-caller-id-using-free-caller-registry).

Correction on "Feedback ID": there is no documented public "Feedback ID" field in the FCR flow. Across FCR's own pages, Hiya, LiveVox, and CallRail, no such field is defined (https://help.livevox.com/en_US/voice/registering-with-the-free-caller-registry-solution). The identifiers you actually track are the email verification code and the per-engine acknowledgement and confirmation emails. A "Feedback ID" may appear as a case reference inside one engine's acknowledgement email, but it is not a portal-level concept. Verify by completing one live registration and reading the acknowledgement emails. To track status, contact the engines directly: FCRsupport@firstorion.com, freecallerregistry@hiya.com, communications@tnsi.com.

Per-carrier confirmations and timeline: you receive an acknowledgement email from each of First Orion, TNS, and Hiya, then a second confirmation after the numbers update. FCR/TNS cite roughly 2 business days to confirm and about 4 business days to clear across all three engines (https://tnsi.com/resource/com/first-orion-hiya-tns-launch-caller-registry-streamline-interactions-between-callers-blog/). Twilio and several CPaaS vendors more conservatively say the full process can take 2-3 weeks per carrier. The figures conflict, so plan for weeks, not days, on an already-labeled number.

### 3b. Per-engine dispute paths (for an already-applied label)

When a specific carrier labels you, file directly with that carrier's engine rather than waiting on FCR:

- Hiya / AT&T: https://hiyahelp.zendesk.com/hc/en-us/requests/new?ticket_form_id=824667
- First Orion / T-Mobile: https://firstorion.com/register-your-number/ (portal at firstorionenterprise.com)
- TNS / Verizon: https://reportarobocall.com/trf/ (also voicespamfeedback.com / communications@tnsi.com)

TNS has the most rigorous intake and slowest review of the three.

### 3c. STIR/SHAKEN A-attestation via Twilio (free)

Do this FIRST, because it is free and it is the cheapest behavioral lever. A business dialing through Twilio gets full A-level attestation only for Twilio-HOSTED numbers assigned to BOTH of the following (this correction matters):

1. An approved Business Profile in Trust Hub (vetting takes about 24 hours).
2. An approved STIR/SHAKEN Trust Product linked to that Business Profile (vetting about 72 hours).
3. The phone numbers assigned to BOTH the Business Profile AND the Trust Product. Only calls from those assigned numbers are signed "A" (https://www.twilio.com/docs/voice/trusted-calling-with-shakenstir/shakenstir-onboarding).

Critical correction: numbers that are merely "Verified Caller IDs" (non-Twilio numbers you verify you control) CANNOT receive A. The highest they reach is B, because Twilio cannot attest to the right to use a number it did not issue (https://www.twilio.com/en-us/blog/developers/best-practices/shaken-stir-sign-twilio-calls). The "TWILIO INTERNATIONAL" carrier string suggests these are Twilio-hosted, but confirm it. If SmrtPhone or ReadyMode owns the Twilio account, you may not directly control Trust Hub: ask them which Twilio (sub)account holds the numbers and whether A signing is enabled on it.

The Trust Hub Business Profile and STIR/SHAKEN Trust Product carry no documented setup fee; SHAKEN signing is applied at no separate charge (https://www.twilio.com/docs/trust-hub). Requires an EIN or DUNS, US address, authorized representative, and a secure business website.

### 3d. CNAM (caller name)

Set CNAM via Twilio for the business name. Setup is free; only lookups cost money ($0.01 per CNAM lookup). CNAM rules: up to 15 characters, uppercase, must start with a letter, only letters/numbers/periods/commas/spaces, must be unique and non-generic, US Twilio numbers only (https://www.twilio.com/docs/voice/brand-your-calls-using-cnam). Manage your presentation so it is a clean accurate name, not generic. Caveat: CNAM has limited impact on mobile, where display is controlled by the analytics engines, not legacy CNAM. Treat CNAM as a low-effort baseline, not the answer-rate lever.

### 3e. Branded / verified caller ID options (paid, phase 2)

Branded caller ID (name, logo, call reason via Rich Call Data) is a separate paid layer. The FCC's October 2025 rulemaking cited 73% of consumers answer when caller name shows, 76% with name plus logo, and 78% with name plus logo plus call reason (https://firstorion.com/products/inform-branded-calling). Treat vendor maximum claims (First Orion "up to 500%," Hiya "about 2x") cautiously; the FCC 73-78% intent figures are more reliable.

Options, smallest-operator-friendly first:

- First Orion INFORM Branded Calling: the only major vendor with genuine small-operation monthly tiers. Starter $31/mo (250 calls, 1 branded name, 6 phones), Basic $52/mo (500 calls, 2 names, 8 phones), Standard $104/mo (1,000 calls, 4 names, 18 phones); enterprise $1,200/mo minimum on annual agreement; overage $0.06 down to $0.035 per call. Self-service portal, about 10-minute onboarding (https://firstorion.com/inform-pricing).
- Hiya Connect Branded Call: no free tier. Eight volume packages from $29/mo (250 branded calls) up to $3,175/mo (50,000), one-time $25 security setup, $0.13 per overage call (https://www.hiya.com/products/connect/pricing).
- CTIA Branded Calling ID (BCID): the industry RCD standard, enterprise/aggregator-gated, up to about $0.12/call plus a monthly CTIA certificate and signing-agent fees (about $0.0004/call); logo PNG min 400x400. No clear small-business self-serve path (https://brandedcallingid.com/). Likely overkill for 42 numbers unless via an aggregator.
- Numeracle / TNS / TransUnion: enterprise, quote-based. Evaluate only if scaling well beyond 42 numbers.

Reputation caution specific to this use case: branding and A-attestation make cold foreclosure calls MORE identifiable and traceable. That is good for answer rates but raises the stakes if recipients complain, and complaints (not attestation) drive labels. Branded-calling vendors also vet call categories; cold, unsolicited foreclosure outreach may face approval friction. Treat paid branding as a phase-2, ROI-tested experiment on a subset of numbers, not a day-one buy.

### 3f. Renewal cadence (corrected)

Correction: there is NO fixed renewal cadence, and caller-ID registrations do NOT need to be re-registered roughly every 6 months. FreeCallerRegistry's own line is that a number only needs to be registered once (https://freecallerregistry.com/fcr/public/html/home.html). Treat any vendor blog claiming a hard 180-day or 90-day renewal as unverified. Re-submit only when:

- You add new or recycled numbers (recycled VOIP DIDs inherit the prior owner's labels and should be re-registered on acquisition).
- Your business name, branding, or call category changes.
- A number gets re-labeled (re-submission is a remediation remedy, not a renewal requirement).

Note: the "60 days to six months" figure that circulates in this space refers to temporarily RESTING a flagged number to let reputation recover, not to renewing a registration. The recurring cadence items that DO exist are provider-side: the FCC Robocall Mitigation Database recertifies every March 1 (that is Twilio's obligation, not yours, but Twilio's robocall-mitigation terms flow down to you), and Twilio Trust Hub may require re-verification on material business changes (https://www.womblebonddickinson.com/us/insights/alerts/annual-robocall-mitigation-database-recertifications-deadline-upcoming). Do not file your own RMD entry; you are a downstream customer, not a voice service provider.

The practical rule: register once, then MONITOR continuously (Layers 2-4) and re-submit on the triggers above. Set a quarterly calendar reminder to re-check, not an API renewal job.

---

## 4. Layer 2: Automated reputation monitoring

### 4a. The free / low-cost API stack (comparison)

No free, self-serve API returns the actual carrier-displayed label across all three carriers. The tools below return a risk proxy or a single engine's view. Prices and limits were read mid-2026 and shift; re-confirm in each vendor console.

| Tool | What it returns | Spam-label ground truth? | Free tier / cost | Best role |
|---|---|---|---|---|
| IPQualityScore (IPQS) Phone Validation | fraud_score 0-100, recent_abuse, spammer, do_not_call, risky, active, line_type, carrier, name, VOIP, prepaid, leaked, sms_pumping (30+ fields) | No, fraud/abuse PROXY | 1,000 lookups/mo shared credit pool, 35/day cap; a phone lookup costs about 3 credits (so roughly 333 phone lookups/mo). Paid: Startup about $99/mo (5,000) (https://www.ipqualityscore.com/plans) | Primary automated daily signal |
| Twilio Lookup + Nomorobo Spam Score add-on | Free format/validation; Line Type Intelligence (about $0.008, carrier bundled inside); Caller Name (about $0.01, US-only); Nomorobo add-on returns binary 0/1 robocall flag (about $0.003/lookup) | Nomorobo = a robocall-DB opinion, not the carrier label | Native to the existing Twilio/SmrtPhone stack (https://www.twilio.com/docs/messaging/api/message-resource for messaging; https://www.twilio.com/en-us/blog/detect-robocalls-with-twilio-lookup-and-nomorobo) | Cheap second independent opinion + CNAM/line-type tell |
| Telnyx Number Reputation | spam_risk low/medium/high, spam_category, maturity_score, connection_score, engagement_score, sentiment_score (0-100), last_refreshed_at. Powered by Hiya (AT&T view) | Closest of the free-ish tools, but Hiya/AT&T view only | Cached reads free; fresh check about $0.10; requires Enterprise registration + approval (https://developers.telnyx.com/docs/branded-calling/number-reputation) | Corroborating signal on flagged DIDs |
| Hiya for Developers Number Reputation API | Grade enum good/normal/poor/very_poor + engagement metrics + whether calls are being spam-labeled (Hiya/AT&T view) | Yes for AT&T, but gated | Per-number service fee; API key only after a signed service agreement (https://developer.hiya.com/docs/protect/business-partner-api/endpoints/get-reputation-for-phones) | Carrier-truth (AT&T) escalation if budget opens |
| Numverify / APILayer | valid, carrier, line_type, location | No spam signal | Free 100/mo (HTTPS paid-only) (https://numverify.com/pricing) | Carrier/line-type fallback only |
| AbstractAPI Phone Validation | valid, carrier, line type, format | No spam signal | Free about 100-250/mo, 10 req/min (https://www.abstractapi.com/api/phone-validation-api) | Carrier/line-type fallback only |
| Caller ID Reputation (CallerIDReputation.com) | 11 sources: T-Mobile/Verizon/AT&T, Hiya/YouMail/Nomorobo/RoboKiller, TrueSpam/Icehook/Sensor, FTC/FCC; per-DID flag report; API upload + webhook on status change | Yes, the real per-carrier labels | Quote-only, paid (https://calleridreputation.com/) | The "buy it" fallback if free signals miss flags |

Correction on app databases: Nomorobo, RoboKiller, Truecaller, and YouMail each offer BOTH a manual web lookup AND a programmatic reputation API. The real limit is that none offers a free, open, public reputation API: Nomorobo's API is paid/enterprise (also the Twilio add-on); RoboKiller's reputation APIs are enterprise (its web lookup is free at lookup.robokiller.com); Truecaller's API is gated and bars telemarketers; YouMail's RESTful Spam Caller API has a free starter key but requires a paid plan for commercial use (https://www.nomorobo.com/api/, https://www.robokiller.com/blog-enterprise/phone-number-reputation-api, https://data.youmail.com/documentation). Their consumer apps prohibit automated/bulk scraping, so treat them as manual spot-check tools only.

Recommendation: build the monitor on IPQS as the free primary signal, corroborate flagged DIDs with Nomorobo (about $0.003) and/or Telnyx fresh (about $0.10), and hold Numverify/AbstractAPI as carrier/line-type fallbacks. Do NOT build on the 100/mo tiers.

### 4b. The exact IPQS fields and how to interpret them

Endpoint (a simple authenticated GET):

```
GET https://www.ipqualityscore.com/api/json/phone/{API_KEY}/{PHONE_NUMBER}?country=US
```

Optional params: country (array of preferred countries), strictness (higher = stricter). Returns JSON (https://www.ipqualityscore.com/documentation/phone-number-validation-api/overview).

The fields that matter and how to read them (field names and bands confirmed against IPQS's own response-parameters docs):

- fraud_score (integer 0-100): IPQS's risk estimate. 85+ is risky, 90+ is high risk (https://www.ipqualityscore.com/documentation/phone-number-validation-api/response-parameters). This is your headline metric.
- recent_abuse (boolean): associated with recent or ongoing fraud. Treat true as a strong negative.
- spammer (boolean): recently reported for spam or harassing calls/texts. Treat true as a strong negative.
- do_not_call (boolean): listed on a DNC list (USA and Canada only). On an OUTBOUND number you own this is its own alert class and an odd signal worth investigating.
- risky (boolean): summary risk flag.
- active (boolean) / active_status: whether the line is live. A flip to active=false on a number you own is a tell that something changed upstream.
- line_type (Wireless, Landline, VOIP, Toll-Free, etc.) and VOIP (boolean): a sudden change is an early reputation-damage tell.
- carrier and name: a sudden CNAM/carrier change is an early tell.
- leaked, prepaid, sms_pumping (object): supplementary risk context.

Interpretation caveat: fraud_score/spammer/recent_abuse reflect how a number is perceived in fraud datasets, which is a PROXY for the carrier "Scam Likely" verdict, not the verdict itself. Do not treat a clean IPQS reading as proof the carrier shows no label.

### 4c. Batching to stay within free limits

The IPQS 35-lookups-per-day cap is the binding constraint and it dictates the colleague's "half the pool each day" pattern. The arithmetic:

- A phone lookup costs about 3 credits. Checking all 42 DIDs daily = 42 x 3 = 126 credits/day, which BREACHES the 35/day cap and burns the 1,000/mo pool in about 8 days.
- Correct pattern: deterministically split the pool so each DID is checked every other day. About 21 DIDs/day x 3 credits = 63 credits/day. That still breaches the 35/day cap.
- To stay strictly under both the 35/day cap AND the 1,000/mo pool on the free tier, check about 11 DIDs/day (11 x 3 = 33 credits/day, about 660/mo), so each of the 42 DIDs is checked roughly every 4 days.
- If you need daily full coverage of all 42, buy the about $99/mo Startup plan (5,000/mo, 250/day) (https://www.ipqualityscore.com/plans).

Note the 1,000 credits are a SHARED pool across IPQS services (IP, email, URL, phone). If you use IPQS elsewhere, it eats the same allowance.

Cost-cap rule for corroboration: only spend the about $0.003 Nomorobo or about $0.10 Telnyx fresh check on a DID that IPQS already flagged that day (fraud_score 75+). The bulk of the pool stays on the free IPQS tier and total spend tracks only the number of actually-degrading DIDs (a few dollars a month).

### 4d. Alert thresholds: Clean / Watch / Flagged

Alert on TWO conditions, not one reading, because a single bad lookup is noisy and the engines themselves weight recent TREND. Define three states:

- CLEAN (green): fraud_score under 75, spammer false, recent_abuse false, Nomorobo 0, Telnyx spam_risk low or null, line_type unchanged. No action. Still surface it in the daily digest so a silent failure is visible.
- WATCH (yellow): any one of fraud_score 75-89, spammer true, recent_abuse true, Nomorobo score 1, Telnyx spam_risk medium, do_not_call true, or a CNAM/line-type change. Pull the DID from heavy rotation, keep monitoring, corroborate with a paid check.
- FLAGGED / CRITICAL (red): fraud_score 90+, OR Telnyx spam_risk high, OR a number that is bad for 3 checks in a row. Rest the number and begin remediation (Section 7).

These bands come from IPQS's own documented thresholds (85+ risky, 90+ high risk) plus the Telnyx low/medium/high enum (https://www.ipqualityscore.com/documentation/phone-number-validation-api/response-parameters). They are proxy thresholds, not carrier-confirmed cutoffs, so tune them against your own Layer 4 answer-rate ground truth over the first few weeks.

---

## 5. Layer 3: Monthly community-site sweep

Carrier-engine labels (T-Mobile/AT&T/Verizon, Hiya, YouMail) are NOT in the free APIs, so a monthly manual sweep is the cheapest way to see the real on-screen label for the handful of DIDs already on WATCH or REST. Keep it a recurring checklist item, not code (these sites prohibit automated scraping).

Sites to check (manual web/app lookups, all free):

- Nomorobo lookup.
- RoboKiller lookup (lookup.robokiller.com).
- Hiya app / consumer lookup (AT&T/Samsung view).
- Truecaller app.
- Community boards: 800notes and similar reverse-lookup community sites.
- Bitdefender reverse lookup or similar reverse-lookup tools.
- The two engine feedback portals as a read check: voicespamfeedback.com (TNS/Verizon) and the First Orion/T-Mobile portal.

What to record per DID per sweep: the date, each site's verdict (clean / spam / scam / specific label text), the displayed CNAM/caller name, and whether the verdict changed since last sweep. Feed any new flag into the same WATCH/REST/RETIRE state machine the automated layer uses. Prioritize the DIDs already on WATCH/REST; spot-check a rotating sample of the CLEAN ones.

For true cross-carrier label visibility beyond a manual sweep, the only options are the engines' enterprise portals, a paid monitor (Caller ID Reputation), or periodic live TEST CALLS from each of the 42 numbers to a T-Mobile, an AT&T, and a Verizon test handset, logging the displayed label. Test calls are the single most authoritative ground-truth check and worth doing monthly on WATCH numbers.

---

## 6. Layer 4: Ground truth from the dialer

The truest live health signal is your own dialer's connect/answer rate. It is free, it is yours, and it reflects real recipient behavior rather than a third-party proxy.

Benchmarks:

- Healthy fresh lists run roughly 15-25% daily connect/contact rate on healthy DIDs (https://www.convoso.com/blog/how-to-improve-call-answer-rates-benchmarks-caller-id-reputation-and-outbound-dialing-best-practices/).
- A spam label drops answer rates 20-50% overnight. A sustained low single-digit answer rate at high volume is itself a flag trigger.
- Keep average call duration above about 30 seconds; watch for spikes in the sub-10-second-call ratio.

How to derive it from SmrtPhone / ReadyMode:

- Pull per-number daily call outcomes: dials attempted, connects (answered), and average call duration. SmrtPhone surfaces call and message activity; where the underlying Twilio subaccount is accessible, you can also pull per-call status programmatically.
- Compute, per DID per day: answer rate = connects / dials, average duration, and sub-10-second-call ratio.
- Alert thresholds to add to the same monitor: WARN when a DID drops below about 10% answer rate, or average duration falls under about 30 seconds, or the sub-10-second-call ratio spikes versus that DID's trailing baseline.
- Cross-reference: a DID whose Layer 2 proxy AND Layer 4 answer rate both deteriorate is a high-confidence flag. A DID that proxies clean but whose answer rate cratered is an early real-world warning the proxies have not caught yet.

ReadyMode (already in the stack) additionally offers paid DID Reputation Monitoring and Assisted Remediation; useful as a paid backstop, but the free DIY answer-rate signal plus IPQS covers most of the early-warning value (https://readymode.com/platform/did-reputation-monitoring/).

This layer is the colleague's "dialer connect/answer rate as ground truth" and it should be the tie-breaker the others defer to.

---

## 7. Prevention playbook

Behavioral scoring overrides registration. These are the concrete numbers (vendor and industry rules of thumb, NOT carrier-published limits, so tune against your Layer 4 data).

### Dials per number per day

- Target 60 dials/day/DID (midpoint of the 50-75 consensus). ReadyMode cites 75/day; Convoso uses about 50/day as conservative (https://readymode.com/avoid-being-flagged-as-spam-in-outbound-calls/).
- Treat 75/day as the hard ceiling and alert before any DID crosses it. Over 100/day draws scrutiny; over 150/day is near-certain labeling.

### Pool sizing

- Rule of thumb: 1 DID per 50-100 daily calls (https://www.tldcrm.com/knowledge-hub/outbound-dialing-strategy-reputation-ratios).
- With 42 numbers at 60/day, you can sustain about 2,520 dials/day safely. A tighter SDR benchmark (25-30 dials/number/day) brackets a very-safe floor of about 1,050/day.
- Keep about 8-10 numbers RESTED as a warmed reserve so you can swap instantly when one flags.

### Warming new numbers

- Warm every new or replacement DID over 3-10 days, starting at about 10-15 dials/day and ramping to full load (https://calleridreputation.com/blog/phone-number-management-best-practices-for-outbound-dialing/).
- Use local TN-metro area codes, set CNAM, and register the number during warm-up.

### Rotation, rest, and retire (concrete rules)

Stop treating the 42 numbers as a rotation shield. Modern reputation specialists (Numeracle, PhoneBurner, Caller ID Reputation) warn that churning to brand-new no-history numbers reads as spammer "number hopping" and just gets the replacement flagged (https://www.numeracle.com/insights/best-practices-for-dialing-strategies). The state machine:

- ACTIVE: normal rotation within the registered pool, capped at 60/day.
- WATCH (1 bad signal): pull from heavy rotation, keep monitoring, corroborate.
- REST (flagged): pull from active dialing immediately for 2-4 weeks (some sources cite 30-90 days; flags can take 60-90 days to clear if the number cools off and gets no further reports). File the free remediation. Swap in a warmed reserve DID. Keep monitoring the rested number.
- RETIRE: only after a SECOND flag survives remediation. Mark a retired_date and replace from the warmed reserve, never reflexively churn to a brand-new number.

Correction/nuance: resting alone is usually insufficient. You must also fix the behavior that caused the flag (volume caps, list quality, attestation) and actively remediate via FreeCallerRegistry or the per-engine portals, or the flag returns (https://developer.hiya.com/docs/guides/voice-protection/number-reputation/get-number-reputation-data).

### Call-pattern discipline

- Cap redials to 2-3 attempts per recipient per day, spaced no closer than about 4 hours (https://www.numeracle.com/insights/best-practices-for-dialing-strategies).
- Keep predictive-dialer abandonment at or under 2% (well inside the FTC TSR 3% safe harbor, measured per campaign over each 30-day period, where abandoned = not connected to a rep within 2 seconds of greeting).
- Avoid repeat-dialing no-answers and abrupt hang-ups; keep average duration above about 30 seconds.
- Keep callback numbers separate from heavy outbound numbers to protect reputation.

### Local presence

Legitimate (calling from numbers you own matched to the prospect's area code) and it lifts answer rates, but it burns numbers faster and the lift is often overstated due to neighbor-spoofing fatigue (https://symbo.ai/blog/what-is-local-presence-dialing/). For a TN shop, a small set of warmed local DIDs per metro beats one heavily-dialed local number, and any local-presence number must originate from the same Twilio trunk to avoid a B-attestation downgrade.

### List hygiene (non-negotiable for a TCPA-exposed shop)

- National DNC scrub at least every 31 days, ideally at point-of-dial. FY2026 access is $82 per area code (first 5 free, max $22,626 nationwide) (https://www.ftc.gov/news-events/news/press-releases/2025/08/telemarketer-fees-access-ftcs-national-do-not-call-registry-increase-2026). For a single TN metro, a handful of area codes keeps this cheap or free.
- Internal/company-specific DNC list, honored immediately and kept indefinitely, read by the dialer on every dial.
- Reassigned Numbers Database (reassigned.us) check within 30 days before dialing for the narrow TCPA safe harbor on an incorrect "No"; about $0.004/query at low volume (https://www.fcc.gov/reassigned-numbers-database).
- Litigator / serial-plaintiff scrub before dialing. Trestle is already in this stack, so the Trestle Litigator Check add-on (about $0.005/query) is the lowest-friction integration (https://trestleiq.com/litigator-check/).

---

## 8. Texting reputation (A2P 10DLC)

SMS/MMS deliverability is a SEPARATE scoreboard from voice. It is governed by A2P 10DLC registration through The Campaign Registry (TCR), not STIR/SHAKEN (https://www.twilio.com/docs/messaging/compliance/a2p-10dlc). A number can be clean on SMS yet flagged "Spam Likely" on voice, or vice versa; carriers do not publicly cross-wire the two scores, though high single-number volume hurts both.

Load-bearing facts for the pre-call auction-screenshot MMS:

- TCR Trust Score (0-100) maps directly to throughput. Standard brand SMS tiers: 75-100 = 225 total MPS (75 per carrier); 50-74 = 120 MPS; 1-49 = 12 MPS; Low Volume Standard = 3.75 MPS (https://help.justcall.io/en/articles/8484885-all-about-the-trust-scores-for-a2p-10dlc-in-the-us).
- T-Mobile enforces a separate daily cap keyed to Trust Score (resets midnight Pacific): 75-100 = 200,000/day; 50-74 = 40,000/day; 25-49 = 10,000/day; 1-24 = 2,000/day.
- As of March 18, 2026, MMS throughput became Trust-Score-tiered (previously a flat 1 MPS): 75+ up to 40 MPS, 50-74 up to 20 MPS, below 50 up to 5 MPS, with a gradual 5-6 week rollout (https://www.twilio.com/en-us/changelog/increased-mms-rate-limits-for-a2p-10dlc-phone-numbers-in-the-u-s0). Verify your account reflects the new tiers in Console.
- Carriers block 100% of unregistered 10DLC traffic (since Feb 1, 2025) and surface filtering through specific error codes: 30007 (filtered/blocked by content/policy), 30034 (unregistered A2P number), 30008 (generic carrier delivery failure), 30032 (toll-free not verified) (https://www.twilio.com/docs/api/errors/30007).
- Register as a Standard brand, NOT Sole Proprietor. Sole Prop caps (1 MPS, 3,000 SMS segments/day, 1,000/day to T-Mobile, 1 number) cannot legally cover 42 numbers, and the cold-MMS use case will be filtered (https://help.salesmessage.com/en/articles/6209896-message-throughput-mps-and-trust-scores-for-a2p-10dlc). Pursue secondary vetting to lift the Trust Score above 50, ideally 75+.
- Do NOT snowshoe. Spreading the single cold-MMS campaign across all 42 voice numbers is exactly the snowshoeing pattern carriers detect and penalize (T-Mobile up to $10,000 per content violation) (https://mobile-text-alerts.com/articles/ctia-messaging-principles-that-matter-most-for-promotional-sms). Send MMS only from the number(s) explicitly registered to the MMS campaign, identify the sender in the body, and use the permanent Dropbox ?raw=1 image link, never a public URL shortener.

What to monitor (a separate, easy free monitor):

- Poll the Twilio Message Resource REST API for the prior day's outbound messages, bucket by error_code, and compute the 30007 (filtered) and 30034 (unregistered) and 30008 rates per sending number and per carrier (https://www.twilio.com/docs/messaging/api/message-resource). error_code is null on success, populated on failed/undelivered. Slack-alert when the 30007 rate exceeds about 2-3% or any 30034 appears (a 30034 wall means a number fell out of the campaign).
- Track opt-out rate as a first-class metric, under 1%. Twilio's free Health Score flags opt-out over 1% as unhealthy and over 3% as carrier-filtering risk (https://www.twilio.com/docs/messaging/features/twilio-health-score-for-messaging).
- Alert on TCR campaign status change or vetting lapse (silent de-registration manifests as a sudden 30034 wall).
- SmrtPhone surfaces failures as "Undelivered" in the Inbox (with a cause pop-up) and in Webhooks/Logs; A2P registration via its Trust Center takes about 1-5 business days (https://help.smrtphone.io/are-there-common-errors-when-sending-text-messages-smrtphone-help-center). Confirm SmrtPhone exposes the underlying Twilio Message SID and error_code (or a webhook carrying them) so your monitor reads real per-message codes, not just SmrtPhone's friendlier labels.

---

## 9. Compliance guardrails that protect reputation

This is high-level, defensive, reputation-protecting awareness, NOT legal advice. Spam labeling is largely complaint-driven, so compliance discipline is the single biggest lever to keep complaint rates (and thus labels) low. Engage qualified TCPA/consumer-finance and state real-estate counsel before scaling, especially for distressed-homeowner outreach.

- National DNC and internal DNC: scrub the National DNC at least every 31 days; cold foreclosure leads create no Established Business Relationship and no consent, so DNC scrubbing is mandatory (https://www.ftc.gov/business-guidance/resources/qa-telemarketers-sellers-about-dnc-provisions-tsr-0). Maintain a separate internal DNC, honor opt-outs immediately, and keep them indefinitely (https://www.compliancepoint.com/marketing-compliance/understanding-tcpa-internal-dnc-requests-a-guide-for-businesses/). This maps onto the project's existing must_not suppression-tag pattern.
- Calling hours: federal window is 8am-9pm in the CALLED PARTY's local time; about 20 states are stricter (often 8am-8pm). Texas SB 140 (effective 9/1/2025) sets a 9am-9pm window and now covers TEXT/MMS (https://www.kixie.com/sales-blog/telemarketing-laws-by-state-2026/). Quiet-hours and over-dialing are top complaint triggers. Hard-code an 8am-9pm guard in the recipient's local time, defaulting to the stricter state window. This applies to the pre-call MMS too.
- Consent landscape: the FCC one-to-one consent rule was vacated by the 11th Circuit in January 2025 and is dead in 2026, reverting to ordinary prior-express-written-consent (https://www.wiley.law/alert-UPDATE-11th-Circuit-Vacates-FCCs-One-to-One-TCPA-Consent-Rule). This does NOT legalize unconsented cold calls; cold foreclosure outreach leans on DNC discipline and manual-dial posture, not consent. Counsel must confirm.
- Foreclosure-specific rules: the FTC MARS Rule / CFPB Regulation O (12 CFR 1015) governs anyone marketing foreclosure-relief services: it bans advance fees and false claims and mandates disclosures ("not associated with the government," "your lender may not agree," "you can stop anytime") (https://www.ftc.gov/business-guidance/resources/mortgage-assistance-relief-services-rule-compliance-guide-business). Whether a "we buy houses" cash purchase is covered depends on framing; a "we can help you stop the foreclosure" pitch likely is. Many states layer foreclosure-consultant licensing/bonding and equity-purchaser laws requiring written contracts, bold-type disclosures, and a multi-day right to cancel (CA 5 business days, CO/NJ/NY/MN variants) (https://oag.ca.gov/consumers/general/foreclosure_reg). Counsel must map the states you dial.
- Self-identification and accurate caller ID: state your name, the company, and that you are a private investor (NOT the homeowner's lender or a government agency), and display an accurate, non-spoofed caller ID with A-attestation and a clean CNAM. This both supports MARS/Reg O posture and lowers complaint/confusion risk (https://www.kixie.com/sales-blog/a-clear-guide-to-telemarketing-laws-state-by-state-for-2025/).
- Opt-out handling (gates go-live with the MMS): the FCC's April 11, 2025 rule requires honoring opt-out by ANY reasonable means (free-text intent, not just the STOP keyword) within 10 business days. Statutory damages are $500-$1,500 per violating message (https://www.bclplaw.com/en-US/events-insights-news/the-tcpas-new-opt-out-rules-take-effect-on-april-11-2025-what-does-this-mean-for-businesses.html). Keyword-only STOP detection is now legally insufficient. Write every opt-out to a permanent suppression list and tag the record so it drops out of both the MMS pull and the cadence.
- State mini-TCPA exposure for the MMS: TX SB 140 reaches text/image messages with a private right of action; FL FTSA gives a 15-day opt-out safe harbor; FL/OK/WA/TX allow private suits up to $1,500/violation (https://www.porterhedges.com/anti-corruption-and-compliance-blog/texas-expands-mini-tcpa-requirements-to-include-text-messages). Apply DNC, internal-suppression, quiet-hours, self-ID, and STOP discipline to the MMS, and do not bundle any "foreclosure relief" claim that would pull it under MARS/Reg O.

Treat complaint rate as the number that governs spam labels, and treat all of the above as the levers that hold it down.

---

## 10. Recommended system to build (improving on the colleague's version)

Build a small Python-on-Windows monitoring loop. Do NOT try to replicate a $100+/mo commercial monitor. Sample a few cheap/free signals per DID on a daily cadence and alert Slack on threshold breaches, mirroring the existing FTM-pipeline conventions (logs/, ledger, Slack alert, Register-ScheduledTask, verify Last Result == 0).

### Components

1. `_api/reputation_monitor.py` (the core loop): load the DID roster from SQLite, pick today's deterministic batch (so each DID is checked on a fixed cycle that respects IPQS limits), call IPQS per DID, corroborate flagged DIDs (75+) with Twilio Nomorobo and/or Telnyx fresh, pull Layer 4 answer-rate/duration from SmrtPhone/ReadyMode, upsert results to SQLite, compute the rolling trend, set each DID's state, render the dashboard, and post one Slack digest.
2. SQLite store (stdlib sqlite3): append-only history plus a derived current-status view.
3. Slack Incoming Webhook (Block Kit) alerting.
4. A registration/renewal tracker layer (Trust Hub status, FCR re-submit triggers, A2P/10DLC campaign status).
5. A static `status.html` dashboard rendered each run and dropped on the existing Dropbox so it is viewable anywhere without a server.
6. A monthly manual community-sweep checklist (Layer 3) and a separate A2P messaging monitor (Section 8) reading Twilio message error codes.

### Per-number data model (SQLite columns)

Two tables, because you need history to compute the trend that drives rest/retire (a single CSV snapshot cannot):

- numbers: did (E.164), carrier, line_type, area_code, cnam, registration_date_fcr, trust_hub_status, a2p_campaign_status, renewal_review_date, status (ACTIVE / WATCH / REST / RETIRED), days_on_watch, rest_started_date, retired_date, consecutive_bad_days, last_checked, dialer_pool (active/reserve).
- checks (one row per number per check): did, ts, source (ipqs/twilio_nomorobo/telnyx/dialer), fraud_score, spam_risk, spammer, recent_abuse, do_not_call, nomorobo_score, telnyx_spam_risk, line_type, answer_rate, avg_duration_sec, sub10s_ratio, raw_json.

### Scan schedule

- Free-tier-safe default: about 11 DIDs/day so each is checked roughly every 4 days (stays under the 35/day cap AND 1,000/mo pool). If full daily coverage is required, the about $99/mo IPQS Startup plan.
- Layer 4 dialer answer-rate pull: daily for all 42 (it is your own data, free).
- Corroboration (Nomorobo/Telnyx): only on DIDs IPQS flagged 75+ that day.
- Schedule with PowerShell `Register-ScheduledTask` (`-Execute`, `-WorkingDirectory`), NOT `schtasks /TR` (the spaced-path 0x80070002 bug). Set `StopIfGoingOnBatteries=$false` and `DisallowStartIfOnBatteries=$false` (the laptop battery-kill bug, 0xC000013A). Run at a time offset from the existing 10:00/10:30 ET FTM jobs (for example 11:00 ET) so it does not race the shared admin token. Verify by Last Result == 0 AND by reading the latest SQLite rows, never by "Ready" or exit-0 (golden rule #17).

### Slack alert design

- One daily digest message even when all-clean (so a silent failure is visible), color-coded green/yellow/red, batching all flagged DIDs into a single message to stay under Slack's about 1 msg/sec/channel limit (https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).
- Block Kit sections: a header (date, counts of CLEAN/WATCH/FLAGGED), then a block per WATCH and FLAGGED DID showing fraud_score, spam_risk, nomorobo, line_type, carrier, answer_rate, days-on-watch, and the recommended action (rest/retire).
- Two alert conditions, not one reading: WARN at fraud_score 75+ OR spammer true OR Nomorobo 1 OR Telnyx medium OR do_not_call true OR answer rate under about 10% OR avg duration under about 30s; CRITICAL at fraud_score 90+ OR Telnyx high OR bad 3 checks in a row.
- A separate registration-renewal reminder N days before any A2P/10DLC or branded-calling review, and an alert if the TCR campaign status changes.

### HTML dashboard

A single static `status.html` rendered from the latest SQLite snapshot: a color-coded table with DID, status, fraud_score, spam_risk, line_type, carrier, answer_rate, avg_duration, days-on-watch, registration/renewal date, and dialer pool. Generate with a Jinja or string template; host on the existing Dropbox.

### Specific improvements over the colleague's version

The colleague's 4-layer monitor is a strong base. Concrete upgrades:

1. Add answer-rate/duration ground truth (Layer 4) INTO the same automated loop and cross-reference it against the IPQS proxy, instead of treating dialer data as a separate manual check. The cross-reference (proxy AND answer-rate both down = high-confidence flag; proxy clean but answer-rate cratered = early warning the proxy missed) is more reliable than any single layer.
2. Add a second independent API opinion (Twilio Nomorobo binary flag, about $0.003, native to the existing Twilio stack) and a Hiya-view corroboration (Telnyx, cached free) on flagged DIDs, so you are not relying solely on IPQS's single proxy. The colleague's version uses IPQS only.
3. Replace flat-file storage with SQLite history so alerting fires on a 3-day rolling TREND, not a single noisy reading. This is the difference between resting a number that is genuinely degrading versus thrashing on one bad lookup.
4. Add an explicit state machine (ACTIVE -> WATCH -> REST -> RETIRE) persisted in the DB, with auto-pause of a CRITICAL number in the SmrtPhone/ReadyMode rotation rather than waiting for manual action. The colleague rests then retires manually; automating the state transition and the pool swap closes the gap.
5. Modernize the rotation philosophy: keep dial counts low (the colleague already does this) but treat the 42 numbers as reputations to protect with a warmed reserve, NOT a churn pool, because brand-new no-history numbers now draw MORE scrutiny.
6. Add the texting reputation monitor (A2P 10DLC error-code and opt-out-rate tracking) as a sibling, since this operation sends a pre-call MMS the colleague's voice-only monitor does not cover.
7. Add a registration/renewal tracker (Trust Hub, FCR re-submit triggers, TCR campaign status) instead of the colleague's fixed ~180-day renewal calendar, which is based on a renewal cadence that does not actually exist (corrected in Section 3f). Re-submit on real triggers (re-label, new/recycled number, branding change), not a clock.
8. Always-send a daily digest (even all-clean) and verify Last Result == 0 plus real SQLite rows, applying this codebase's hard-won "deployed != working" discipline that the colleague's version may not enforce.
9. Add periodic live test calls to T-Mobile/AT&T/Verizon handsets on WATCH numbers as the authoritative ground-truth check the free APIs cannot provide.

Budget the DIY signal cost at roughly $0 (IPQS free tier) to a few dollars/month (occasional Nomorobo/Telnyx corroboration on flagged DIDs only). Escalate to a paid monitor (Caller ID Reputation or Hiya Connect) ONLY if numbers keep burning despite resting, since those provide the real per-carrier labels the free APIs cannot.

---

## 11. Step-by-step rollout plan

1. Confirm hosting and control. Verify the 42 numbers are Twilio-hosted and determine whether you, SmrtPhone, or ReadyMode owns the Twilio account that holds them. If a partner owns it, get them to confirm STIR/SHAKEN A signing is enabled and ask for access to Trust Hub and the underlying message error codes.
2. Complete Twilio Trust Hub for A-attestation (free). Create/verify the Business Profile (about 24h), create the STIR/SHAKEN Trust Product (about 72h), and assign ALL 42 numbers to BOTH. Set a clean CNAM.
3. Register all 42 numbers at FreeCallerRegistry.com in one submission (file upload), Service Provider "Twilio," category "Other," accurate business name/address/website, real monthly call volume. Record the verification code and save every per-engine acknowledgement email.
4. Lock down list hygiene: National DNC subscription for your TN area codes, internal DNC wired into the dialer, RND check before dialing, and the Trestle Litigator Check add-on.
5. Set behavioral guardrails in SmrtPhone/ReadyMode: 60 dials/day/DID cap (75 ceiling alert), 2-3 redials per recipient per day spaced 4h, abandonment at or under 2%, 8am-9pm local quiet-hours (stricter per state), and a warmed reserve of 8-10 numbers.
6. Build the monitor MVP: `reputation_monitor.py` with the SQLite schema, the IPQS daily batch (free-tier-safe ~11/day), the Slack digest, and the state machine. Ship it before adding corroboration.
7. Wire Layer 4: pull per-DID answer rate and average duration from SmrtPhone/ReadyMode into the same loop and cross-reference.
8. Add corroboration: Nomorobo and Telnyx checks on flagged DIDs only; render the `status.html` dashboard to Dropbox.
9. Schedule it: `Register-ScheduledTask` at about 11:00 ET, battery-power settings disabled, verify Last Result == 0 AND real SQLite rows.
10. Stand up the A2P texting monitor: register the MMS program as a Standard brand/campaign in TCR (pursue secondary vetting for Trust Score 50+, ideally 75+), build the Twilio message-error-code + opt-out-rate monitor, and fix opt-out handling to the April 2025 any-reasonable-means/10-business-day standard BEFORE any real MMS send.
11. Establish the monthly cadence: the Layer 3 community-site sweep on WATCH/REST numbers, live test calls on WATCH numbers, and a quarterly FCR re-check reminder.
12. Get legal sign-off on the foreclosure-specific compliance posture (MARS/Reg O applicability, state licensing/equity-purchaser rules, mini-TCPA for the MMS) before scaling either channel.

---

## 12. Open decisions for the operator

- Who controls the Twilio account? If SmrtPhone or ReadyMode owns it, can you get Trust Hub access and the underlying message SIDs/error codes, or do you need direct subaccount API access?
- IPQS free tier or the about $99/mo Startup plan? Free forces every-4-day coverage of the 42 DIDs; $99 buys daily full coverage. Decide based on how fast numbers are degrading.
- How much paid corroboration? Confirm the live Nomorobo add-on price and Telnyx fresh-check price in-console, and set a monthly cap on corroboration spend.
- Buy a paid carrier-truth monitor? If free proxies keep missing real flags, decide whether to add Caller ID Reputation (quote-only) or Hiya Connect (per-number fee, AT&T view) for the actual per-carrier labels.
- Branded caller ID, yes or no? Weigh the answer-rate lift against the reputation/compliance risk of branding cold foreclosure outreach, and whether to A/B test First Orion INFORM ($31-104/mo) on a subset first.
- Rotation philosophy: confirm the shift from churn-to-dodge toward protect-and-rest, and set the exact rest window (2-4 weeks vs longer) and the retire trigger (second flag post-remediation).
- MMS posture: which subset of numbers carries the registered MMS campaign (to avoid snowshoeing and to isolate it from the heaviest voice-dial numbers), and how to handle same-day-auction recipients.
- Quiet-hours and spacing in the MMS sender loop: enforce randomized spacing and a hard 8am-9pm recipient-local guard in the sender before any real send (still an open item in the current MMS pipeline).
- State footprint: which states will you dial and text into, so counsel can map the foreclosure-consultant licensing/bonding, equity-purchaser, telemarketer-registration, and mini-TCPA obligations that vary widely by state.
- Test-call ground truth: do you set up your own T-Mobile/AT&T/Verizon test handsets for monthly live-label checks, or buy a service that does it?

This report uses only facts supported by the research and verification provided, applies the corrected versions where verification refuted a claim (FCR "Feedback ID" does not exist as a portal field; no fixed ~180-day renewal cadence; A-attestation only for Twilio-hosted numbers assigned to both Trust Hub products; community apps have gated APIs not just manual lookups; attestation contributes to but does not cause labeling), and contains zero em dashes or en dashes.
