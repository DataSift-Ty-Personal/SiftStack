# SMS connectivity: what the data says, and what the alternate routes actually are

Written 2026-09-03, after connectivity on smrtPhone/Telnyx felt bad for a few weeks and
the community reported the same. Everything in the first half is measured from our own
production event log (`/data/sms_agent.db` on Fly, 2026-08-11 through 2026-09-03,
3,228 sends). Reproduce any number here with:

```bash
fly ssh sftp get /data/sms_agent.db sms_agent_prod.db -a siftstack   # run via PowerShell, Git Bash mangles /data
python src/sms_agent/cli.py deliverability --days 60 --db sms_agent_prod.db --full
```

## Finding 1: we were flying blind on purpose, and the fix is one line

The receiver deduped webhook events on `source:event:smsId`. A delivery callback
progresses sent -> delivered/undelivered under ONE smsId, so the FIRST callback won and
the final verdict was silently dropped for every message where "sent" arrived first.
Result: of 3,229 delivery callbacks, only 248 carry a final verdict (181 delivered,
67 undelivered). Every delivery number we have is a floor, not a truth.

Fixed in `receiver._dedupe_key` (status now part of the key for delivery callbacks,
retries of the same status still collapse). **Needs a `fly deploy` of the `siftstack`
app to take effect, ideally outside the 9am-6pm Eastern send window.** Until then the
blindness continues; the deliverability report prints a BLIND finding as long as final
verdicts stay under 50% of callbacks.

## Finding 2: the two concrete failures were ours, not the carrier's mood

- **Aug 14 + Aug 17: every one of the 19 Adriana numbers** was refused with "US 10DLC
  number that is not associated with an approved A2P 10DLC Campaign". 33 carrier
  refusals plus 55 rejections from smrtPhone's own API before the message ever left.
  That is a registration/campaign-association failure, 100% fixable, and it looks
  exactly like "bad connectivity" from the outside. It has not recurred since Aug 17,
  so association is presumably fixed, but the canary habit (one message per number,
  read the callback) is the only thing that proves it stays fixed.
- **Aug 25-28: a carrier content-filtering burst.** 22 sends flagged "objectionable by
  the carrier" across 13 distinct Adriana numbers in four days. Spread that wide is
  campaign/content-level filtering, not a burned DID. Rotating numbers will not fix
  this class; only content/campaign changes do. And 22 is the floor: the dedupe bug
  means flags that arrived after a "sent" callback were discarded.

## Finding 3: what the pools actually look like

| Week | Pool | Sends | Replies | Rate | Refused |
|---|---|---|---|---|---|
| W33 (Aug 10) | Adriana | 628 | 183 | 29% | 33 registration |
| W34 (Aug 17) | Adriana | 731 | 146 | 20% | 6 misc |
| W35 (Aug 24) | Adriana | 958 | 342 | 36% | 22 spam flags |
| W36 (Aug 31, partial) | Adriana | 546 | 81 | 15% | 4 misc |
| W36 (Aug 31) | Dispo | 364 | 83 | 23% | 1 spam flag |

Reply rate is the proxy metric (replies lag sends, so the current week always reads
low). The Adriana pool is noisy but not collapsing; the dispo blasts delivered and
converted (a full-ask offer on 158 Old State). One number reads cold:
**the line ending 0032 at 11% replies vs a 26% pool median** on 140 sends. Verify against
the smrtPhone log, then rest or replace it.

So the honest summary is: connectivity here is degraded at the margin by carrier
content filtering (real, measured, industry-wide) but the biggest single outage in our
own data was a registration lapse. Community members reporting "terrible connectivity"
should run this same split before blaming the carrier: registration failures, spam
flags, dead numbers and landlines each have a different fix.

## The routes people are talking about, and how they actually work

**textbee.dev (and sms-gate.app, httpSMS, SnowSms).** An app on an Android phone
exposes a REST API; your messages go out through the phone's SIM on a consumer plan.
Free tier 300/mo, $9.99 for 5,000, multi-device for scale. Because the carrier sees a
consumer number doing P2P traffic, there is no 10DLC registration and no A2P filter.
That is also the whole problem: at prospecting volume this is exactly what carriers
define as a gray route / SIM farm. T-Mobile's code of conduct names snowshoeing and
actively monitors for it; consumer SIMs sending hundreds of identical marketing texts
get suspended, and TCPA exposure is unchanged by the route. Verdict: fine for internal
alerts, ops pages, and low-volume personal automation. Not a prospecting channel, and
we will not build one on it.

**BlueBubbles.** Not SMS at all: an open-source iMessage bridge. A Mac signed into
your Apple ID runs a server that exposes the Messages database over a REST API with
webhooks. iMessage rides Apple's network, so carrier SMS filtering never sees it.
It is one Apple ID on one Mac, built for personal use; a fresh Apple ID texting
strangers gets flagged by Apple quickly. Verdict: the mechanism to understand, not the
tool to use.

**Sendblue (commercial iMessage API).** The scaled version of the BlueBubbles pattern:
Mac fleets, warmed Apple IDs, an API. Deliverability is genuinely high because
carriers are not in the loop; their own marketing claims 99%+ and 2-3x response rates.
Risks: it lives outside Apple's sanctioned Apple Messages for Business program, so the
platform risk is Apple deciding to care (the Beeper Mini precedent); it only reaches
iPhones, and the SMS fallback for Android recipients reinherits the A2P problem; and
TCPA still applies. Best-fit here if we ever trial it: **the dispo lane.** Small
lists, high value per send, investor recipients, and the seller pipeline stays on
compliant rails. ~$50-100 buys a real test.

**RCS Business Messaging.** Verified sender, branded, but consent-gated and
agent-verified. Not built for cold outreach; skip.

## The order to do things in

1. **Deploy the receiver fix** (off-window `fly deploy` of `siftstack`), then re-run
   `deliverability` after a week of real verdicts. Decisions after that run on real
   delivery rates instead of floors.
2. **Registration audit.** Confirm every DID in both pools is attached to an approved
   campaign, and that the registered use case and sample messages match what actually
   ships. A campaign registered as customer care carrying marketing copy is a
   filtering magnet. Canary one message per number after any pool change.
3. **Content pass on the seller variants.** The W35 spam burst says some fraction of
   the current copy is tripping content filters. We already run no-links, no-dollar
   amounts, one question, under 320 chars; next lever is more variant entropy (carriers
   fingerprint templates) and retiring any variant that co-occurs with flags once the
   receiver fix gives us per-message verdicts.
4. **Rest the line ending 0032**, and any number the report flags after verdicts are real.
5. **Provider A/B if degradation persists:** same copy, same cohort, 2-3 numbers on a
   second provider (Twilio or Bandwidth direct) vs Telnyx via smrtPhone. Only this
   distinguishes "Telnyx is having a bad quarter" from "cold SMS is being squeezed
   everywhere".
6. **Decision gate:** if real verdicts show systemic spam filtering on compliant,
   registered, well-behaved traffic, trial Sendblue on the dispo lane before touching
   the seller lanes. Gray routes via Android SIM gateways stay off the table at volume.
