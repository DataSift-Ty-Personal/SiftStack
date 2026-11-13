# Smarter Contact setup: Priority 1 empathy campaign

Built 2026-10-20 for ty+2's Priority 1 tier (5,334 records, 5,102 with a phone),
already connected to Smarter Contact through the DataSift integration.

Source of truth for every message: `src/smarter_contact_templates.py`. Never edit
copy in Smarter Contact first. Edit the module, run `--check`, then paste.

```bash
python src/smarter_contact_templates.py --check        # every template against every rule
python src/smarter_contact_templates.py --export       # paste-ready CSVs in output/smarter_contact/
python src/smarter_contact_templates.py --tag-no-name  # dry; --commit tags new Priority 1 records
python src/smarter_contact_templates.py --tag-control 0.5   # dry; --commit splits the test arms
```

## What the copy does

Five touches, the text-touch-builder shape with an empathy front end:

| Touch | Day | Job |
|---|---|---|
| 1 | 0 | Acknowledge the noise ("probably getting blown up about {addr}"), then ask if they are the owner |
| 2 | 1 | The ask: if it's yours, would you ever sell? (no second "is it still yours?") |
| 3 | 2 | The pitch: as-is, no repairs, no showings, no agent fees. "Worth a quick call?" |
| 4 | 21 | Breakup for now (never "last one ever", because touch 5 exists) |
| 5 | 60 | Fresh check-in for people who never answered |

The empathy line acknowledges the OUTREACH, never the owner's situation. Priority
1 is built from Absentee + Low Income, Low Credit + Free & Clear, Absentee + Low
Credit and investor score 90+, so the checker refuses money, credit, debt and
equity words as hard as it refuses foreclosure and probate.

**Why three days in a row and not four.** Ty wants the opening burst on
consecutive days. Smarter Contact allows a contact at most 3 texts per 7 days,
followed by a 14-day cooldown (support.smartercontact.com/drip-campaigns, dated
2026-08-11). A fourth day in a row is not possible, and anything scheduled into
the cap simply does not send. A drip campaign cannot do even three: it will not
send message #3 before day 3, and it raises a shorter wait silently. So the
burst runs as **follow-up campaigns**, which are exempt from the drip floors but
not from the cap. Touch 4 on day 21 clears the cooldown whether it starts at the
third text or at the end of the 7-day window (the help pages do not say which).
`--check` refuses any schedule that breaks the cap.

Every touch fits one segment at the p99 name and street length with 26
characters held for the opt-out footer Smarter Contact appends.

**Nobody publishes reply rates for this opener** (searched 2026-10-20), so it
ships as a split test against the industry-standard ownership check
(`TOUCH1_CONTROL`). Touches 2-5 are identical across both arms, so the clean read
is replies in the 24 hours after touch 1, before touch 2 goes out the next day.

**Watch the burst.** The SMS agent's seller campaign was switched off on
2026-09-11 partly for texting sellers four days running. Three in a row is
aggressive. Smarter Contact flags a number Blocked at a 5% carrier block rate,
and 3% opt-outs is the usual ceiling. Read both after day 2 of the first wave.
If either is over, move touch 3 to day 3 before the next wave.

## What is live in Smarter Contact (built 2026-10-20)

Built over the app's own GraphQL by `src/smarter_contact_setup.py`, every object
read back after writing. Rerunning `--phase build` is a no-op once the account
matches (last run: 0 to create, 75 already there).

| Built | Count | Notes |
|---|---|---|
| Message templates | 42 | `P1 T<touch> <arm> <Named/No Name> NN`. Touch 1 = Initial (19), the rest Follow-up. Opt-out footer OFF on all 42 (Ty, 2026-10-21; `SC_OPT_OUT` in the module). Every touch still fits 1 segment even if it comes back. If carrier blocks climb, turn it back on for Touch 1 first |
| Quick replies | 17 | Messenger > Quick Replies |
| Labels | 5 new | Property Listed, Property Sold, Owner Deceased, Tenant, Spanish Speaking. Your existing Lead, Not interested, Wrong Number, Nurture, Correct DNC, Unknown DNC are reused, Low/Medium/High untouched |
| DataSift mapping rows | 10 added | Existing rows untouched (table below) |
| Contact Workflows | 4 | Inactive until you switch them on. Waits verified in the editor (45 / 75 / 120 days etc.), Mon-Fri, noon to 6pm |

**DataSift mapping as it stands** (Workflows > Integrations > DataSift):

| Label | Property status | Phone status | Phone tag |
|---|---|---|---|
| Lead | New Lead | | |
| Not interested | Not Interested (added: only the dialer's "Not interested 📞" was mapped, so text replies labeled Not interested never reached DataSift) | | |
| Nurture | Follow Up | | |
| Property Listed | Listed | | |
| Property Sold | Sold | | |
| Owner Deceased | | | yes |
| Wrong Number | | Wrong | |
| Tenant | | Wrong | yes |
| Correct DNC | | Correct DNC | |
| Unknown DNC | | DNC (unknown owner) | |
| Do Not Call | | Wrong DNC (yours, left as set) | |
| Spanish Speaking | | | yes |

Lead is the important one. ty+2 already has a sequence that assigns a record to
whoever owns new leads when its status becomes New Lead, so a hot text reply
reaches a caller with no new wiring.

**Not built yet:** the group split and the campaigns. Both wait on the split
decision below, and launching is always your call.

## Groups

Your push landed as **Priority 1- Attempt 1** (9,954 verified textable contacts,
one per phone) plus **Priority 1- Attempt 1_landline** (7,403, never texted).

A template cannot fall back when `<First Name>` is empty, and it prints
DataSift's raw field (Smarter Contact only fixes capitalization). 301 Priority 1
records would render "Hi ,", "Hi F," or "Hi Maria G Fuentes,". They carry the tag
**SC No First Name** in ty+2 and need their own group on the no-name pools. The
control arm of the split test needs a group too (`--tag-control 0.5 --commit`
tags a stable 50/50 split in DataSift first).

## Campaigns

No drip. Each touch is its own campaign, so the burst can run on consecutive
days:

- **Day 0, Monday:** a single-message initial campaign per group (P1 Empathy,
  P1 Control, P1 No Name), using that group's touch 1 templates (empathy 10,
  control 4, no-name 3).
- **Day 1 and day 2:** a follow-up campaign built off the previous one, filtered
  to contacts who did not respond. Touch 2, then touch 3. The no-name group uses
  the no-name templates. Up to 25 source campaigns can feed one follow-up.
- **Day 21:** a follow-up campaign with touch 4. **Day 60:** one with touch 5.
- **Start on a Monday,** so days 0-2 land Monday to Wednesday. A follow-up can
  only be built once its source campaign shows Completed.
- Phone numbers: rotate all of them. "Send all messages at once": off.
- Delivery window: Monday to Friday, 12:00pm to 6:00pm Eastern. 88% of these
  phones are Eastern and 8% Central, but 4% are Mountain or Pacific, and noon
  Eastern is the earliest start that is 9am for all of them.

**Contact Workflows:** add a contact from the conversation when you set the
matching label (Not interested -> Soft No Check-Back, Property Listed -> Listed
Check-Back, Nurture -> Asked For Later, Owner Deceased -> Family After A Loss).
Any reply pulls them out automatically. Switch each workflow to Active first.

## Launch log

`src/smarter_contact_launch.py` (plan / groups / campaign) runs the waves.
`plan` splits the textable group "Priority 1- Attempt 1" (9,954 contacts, one
per phone) into 158 no-name contacts and 10 waves of ~1,000 named contacts,
grouped by property (the DataSift record id the integration writes into the
APN field, ~3 phones per owner), so one owner never gets the sequence on two
schedules. Smarter Contact needs 1 opener per 50 contacts in a campaign (max
8), which is why the pools now hold 8+ named variants for touches 1-3.

- 2026-10-27: Memphis numbers released (3); 10 Knoxville numbers remain.
- 2026-10-27: scheduled for Wed 2026-10-28, split by the phone's area code
  because Smarter Contact's window runs on the account's (Eastern) clock and
  the rule is 9am-6pm in the recipient's time too: Eastern phones start 9:00
  with a 9-6 window, everyone else noon with a 12-6 window (11am Central, 9am
  Pacific). "P1 Wave 1 East T1" 829, "P1 Wave 1 Other TZ T1" 163, "P1 No Name
  East T1" 119, "P1 No Name Other TZ T1" 39. All 10 numbers, 5 texts/min. The
  first noon-only versions were paused and removed before sending anything.
- Next: Thu Touch 2 and Fri Touch 3 as follow-up campaigns to non-responders
  (createFollowUpCampaign {campaigns, mode}), built once each source campaign
  shows Completed. Read carrier blocks (<5%) and opt-outs (<3%) on Wave 1
  before starting Waves 2-4 (Mon/Tue/Wed starts, so no burst straddles a
  weekend).

### Touch 2 and later: audit, then build on a clean group (2026-10-28)

**Never send a follow-up straight from Smarter Contact's follow-up builder.** It snapshots
its recipients into a hidden group that cannot be edited ("Group not found" on removal), so it
can never drop a record that went Sold, Not Interested or DNC. The Touch 2 audit found **478
of 1,054** draft recipients (45%) that must not be texted: 157 Not Interested, 146 Sold, 102 on
the national DNC registry, 39 on the DNC list / Do Not Market, 35 in Deep Prospecting, 34
undelivered or suppressed. All of them had received Touch 1, because the Priority 1 push into
Smarter Contact did not filter on Sift status or DNC. Build every later touch, and every new
wave, from an audited group:

```bash
python src/smarter_contact_audit.py --drafts "<campaign names>" --suppression <fly suppression json>
python src/smarter_contact_launch.py --phase touch --wave 1 --tz east --touch 2 --source-date 2026-10-28 --start "2026-10-29 09:00" --commit --schedule
```

`touch` rebuilds the group (Touch N-1's group, minus anyone who replied by text or call, minus
everything the audit flagged), creates a STANDARD campaign on it, and deletes a same-named
follow-up draft that never sent. No 24-hour floor applies to a standard campaign.

**Guarantees against texting someone who replied:** every opt-out and every wrong number goes on
Smarter Contact's own DNC the moment it arrives (the platform blocks only the literal STOP
keyword itself), and every reply is pulled out of the groups of all campaigns not yet
finished. Whether a scheduled campaign re-reads its group at send time is not yet known:
removing someone dropped the group count but not the campaign's own count. Check after the
first run.

### Follow-up touches

`python src/smarter_contact_launch.py --phase followup --wave 1 --tz east --touch 2 --source-date 2026-10-28 --start "2026-10-29 09:00" [--commit [--schedule]]`
builds touch N off the finished touch N-1 campaign, to non-responders only (Smarter Contact's
own follow-up, mode NO_RESPONSE, so anyone who replied or opted out is left out by the
platform). `--commit` makes a DRAFT; only `--schedule` starts it.

**A follow-up cannot start until 24 hours after its source campaign ENDED** (measured
2026-10-28: an earlier start is silently raised to that mark, a later one sticks). So the
burst slides by however long each touch takes to finish: Wave 1 East touch 1 ran 9:00am to
11:45am, so its touch 2 starts no earlier than 11:45am the next day. A slower touch pushes
every later one back.

## Running the inbox

- **Answer inside five minutes** during the window. A positive reply goes cold
  fast, and the person who signs the texts is the person who answers them.
- **Label every conversation the same day.** An unlabeled reply never reaches
  DataSift.
- **The agent answers the routine replies** once its switches are on (next
  section). Adriana takes interested owners and anything it leaves alone.
- **Hot reply:** label Lead, then Messenger > Export to CRM right away. The
  sync is MANUAL. Nothing reaches DataSift until someone exports it, and it
  matches records by property address.
- **End of day:** Campaigns > Completed Campaigns > Export to CRM for everything
  else.
- **STOP or hostile:** no reply at all, label Unknown DNC (Correct DNC if they confirmed they own it). No quick reply exists for it
  on purpose.
- **Never quote a number by text.** Every price question goes to a call.
- **Don't double-text.** The SMS agent's seller campaign has been off since
  2026-09-11. Callers should not also send the smrtPhone Text Touch fields to
  Priority 1 records while they are in this campaign.

## The text leads agent on Smarter Contact (built 2026-10-27)

Every reply Smarter Contact's webhook delivers runs through the SMS agent on Fly
(`src/sms_agent/sc_inbound.py`). It reads the real Smarter Contact thread first,
so a bare "yes" is judged against the question it answers, then:

| Reply | Agent does | Slack (text leads channel) |
|---|---|---|
| Interested (price, call me, an opening) | No text back. Tags + assigns the record to Adriana, pinned note, phone CORRECT, thread paused | Tags Adriana: "yours, call within 5 minutes", thread is in Smarter Contact, agent has stopped texting it |
| Who is this / how did you get my number | One answer as Adriana, a local person (below). A second ask goes to a person | Nothing (sits in the inbox) |
| "Yes, it's mine" | Asks once whether they would ever sell. Phone CORRECT in Sift | Nothing |
| Not interested (any kind of no) | No text back (Ty, 2026-10-28). Sift status **Not Interested** (unless the record is sold, contracted or a live lead, which keeps its status and gets a note), phone CORRECT, note, thread closed, label Not interested | Nothing |
| Wrong number | Apology, label Wrong Number, phone WRONG in DataSift | Nothing |
| STOP / take me off / "Stop it" / a sentence ending in Stop | No text back. Phone DNC in Sift, tag Do Not Market, Sift list **DNC** (every record carrying the number), label Unknown DNC, smrtPhone do-not-text | Nothing |
| A death | No text back, thread paused, pinned note (a probate lead). With a STOP in it, the opt-out wins and the death is still pinned | Nothing (Ty, 2026-08-26) |
| A lawyer, harassment, a minor | No text back, number suppressed, DataSift DNC | Nothing (Ty, 2026-08-28) |
| Crisis language (self-harm) | No text back, thread paused | Always posts |
| Anything unclear | No text back, left for a person | Nothing |
| Smarter Contact login or sends failing | Retries 3 times | Always posts, at most every 3 hours |

**The copy is fixed, not written by a model** (`src/sms_agent/knowledge/sc_replies.py`):
no company, no price or offer, nothing about the owner's situation, no promise.
It passes the same validator as every other outbound text plus Smarter
Contact's filtered words. Sample: "Sorry, should have said! It's Adriana. I'm
local and texting a few homeowners around Knoxville to see if anyone might sell.
Is 4100 Landon Dr yours?"

**Guards:** at most 2 auto replies per thread; a 90 to 240 second delay; inside
9am to 6pm Eastern AND 9am to 6pm where the owner lives; a queued reply is
dropped if anything was sent after their text (a person in the inbox, a
campaign touch); the agent never texts a thread a person has typed in, a thread
already closed or paused, or anyone interested. Replies leave through Smarter
Contact on the thread's own number, never smrtPhone.

**Two switches, both ON since 2026-10-28** (Fly secrets on `siftstack`, secrets beat `fly.toml`):

```bash
fly secrets set -a siftstack SMARTERCONTACT_AUTO_REPLY=1 SMARTERCONTACT_AUTO_LABEL=1   # on
fly secrets set -a siftstack SMARTERCONTACT_AUTO_REPLY=0 SMARTERCONTACT_AUTO_LABEL=0   # off
```

**Smarter Contact labels do NOT reach Sift on their own.** The DataSift integration maps them
(Not interested to Not Interested, Wrong Number to WRONG, Unknown DNC to DNC) but only sends on a
manual Export to CRM. So the agent writes every disposition to Sift itself, on the record the
contact was pushed from (the record uuid in its APN field), not on whichever record the phone
was first mapped to: a phone can sit on several records. `sc_inbound.resync_sift(commit=True)`
re-applies dispositions for past replies (only what differs).

Replies that arrive while the switches are off are not lost: `sc_inbound.catch_up(commit=True, since=...)`
answers the newest reply per thread exactly once, skipping any thread a person already answered (dry by
default). `sc_inbound.recheck_opt_outs(commit=True)` re-applies the opt-out rules after they change.

With both off the agent still classifies, writes DataSift and pages Adriana on
interested replies. Turn them on once real replies have shown up in the events
table (`source='smartercontact'`). Switching off cancels anything still queued.

**How it reaches Smarter Contact:** the web app's own GraphQL, signed in as Ty's
account with a Firebase refresh token (`SMARTERCONTACT_REFRESH_TOKEN` +
`SMARTERCONTACT_FIREBASE_API_KEY`). Not an official integration, so it can change
without notice. **A password change or "sign out of all devices" kills the
token**: run `python src/smarter_contact_token.py --save` (reads the session from
the saved browser profile, log in there first if it lapsed) and push the two
values to Fly with the one-line command in its docstring.

## Decisions that are Ty's

1. **Company name.** Smarter Contact and the 10DLC guidance want the brand name
   in the first text. The standing rule here is to never name a company. The
   templates follow the standing rule. If carrier blocks climb, the brand name
   is the first lever.
2. **Split.** 50/50 reads the test fastest. 80/20 keeps most people on the
   empathy opener.
3. **Who signs.** The export signs as Adriana (`--sender` changes it). Whoever
   signs must answer the inbox, and callbacks to the Smarter Contact numbers
   must ring that same person.
4. **No voicemail drops.** Smarter Contact suggests ending a drip with one. The
   FCC ruled in 2022 that ringless voicemail is a prerecorded call under the
   TCPA, which needs consent this list does not have.

## Smarter Contact contracts (read from the live app, 2026-10-20)

None of these are in its public docs. Each was read from the app bundle, from
Apollo validation errors, or from a live read-back.

- **One GraphQL endpoint:** `POST api.smartercontact.com/gql`. No public API,
  introspection off, Zapier read-only. `smarter_contact_setup.py --phase login`
  opens a window for a person to sign in; the session lives in a gitignored
  browser profile (Firebase keeps its tokens in IndexedDB, which storage_state
  files do not carry).
- **Merge tags** are a literal swap of `<First Name>`, `<Address>`, `<City>`.
  `<Property Address>` is NOT a tag.
- **Opt-out footer:** "Reply STOP to unsubscribe." (27 characters with its
  separator), appended to any template with opt-out on.
- **Banned words in templates and workflow messages** (refused at create):
  now, free, offer, loan, lender, winner, irs, abuse, harass, gang, whitelist,
  911, "limited access", political names, jew/jewish; grass, smoke, crack, weed
  and church only warn. Quick replies are not filtered. The module's checker
  carries this list.
- **Workflow waits are milliseconds.** The editor shows floor(ms as days) and
  adds 1-30 random minutes. A step's allowed `days` must be non-empty (ISO
  weekdays, 1 = Monday).
- **UpdateCrmIntegration replaces the whole config.** An empty input passes
  validation, so a careless call wipes the DataSift mapping and API key. The
  build reads the config, appends rows and sends everything back, then reads
  it back.
- **Response removal** in this version is a suppression list of phone numbers,
  not keywords. STOP and the app's AI opt-out detection are native; the
  module's `RESPONSE_REMOVAL` words are the inbox's "label DNC now" triggers.
- **Plan:** Enterprise 20K messaging.
- **Still unconfirmed:** whether the delivery window runs on the account's
  timezone or the recipient's. Until known, noon to 6pm Eastern keeps every
  timezone inside 9am to 6pm local.

## Land campaigns (live 2026-11-10: 1,999 touch 1 texts, 0 refunded)

Land texts name the parcel, because 405 of the 1,119 Knox pilot lots have no
house number ("the lot on Rutledge Pike" is not something an owner recognizes).
Smarter Contact has land merge tags (`findMergeTags`, read live): `<APN>`,
`<Parcel acres>`, `<Parcel county>`, `<Parcel state>`. **But the DataSift push
writes its RECORD UUID into APN and leaves acres and county empty**, so a land
template would print a uuid until the contacts are patched.

Templates: 94 land variants in `src/smarter_contact_templates.py` (`LAND_LOT`,
`LAND_TRACT`, `LAND_LATE`), checked on a land profile (same gates as Priority 1,
except "vacant" is allowed because on a land list it names the asset; tracts
never say vacant because some carry a farmhouse). Titles `Land Lot T1 Named 01`
and `Land Tract T1 ...`. Paste-ready: `output/smarter_contact/sc_land_templates.csv`.

What actually ran on 2026-11-10 (first land campaigns, 1,860 contacts):

1. Land records into ty+2 with `src/pull_by_ids.py` (lists `Land - Builder Lots`,
   `Land - Entitlement Tracts`, tag `Land Pilot 1`; 1,268 new records, 25 existing).
2. **DataSift's push did not work.** `POST /api/internal/property/smarter_contact/`
   (what the Send To button calls) answered `{"count": 1}` on a one-record probe
   list and created the Smarter Contact group at once, but no contact ever arrived
   (two probes, 30 minutes, none of the probe owner's 9 numbers anywhere in
   Smarter Contact). Groups `Land SC Probe` / `Land SC Probe 2` are those probes.
3. So contacts are created directly: `python src/smarter_contact_land.py --phase
   create` (dry), `--commit`. It reads each record, applies the Priority 1 audit's
   exclusions BEFORE anyone is texted (Smarter Contact suppressions, any number
   already a Smarter Contact contact, DNC/wrong/dead phone status, landlines, the
   registry DNC flag off the search row, the DNC list, Do Not Market, deceased or
   opted-out owners, a dead status) and writes APN, acres, county, state and Sift
   Record at creation. Max 3 numbers an owner, best Trestle tier first.
   **Never wrap it in `timeout`**: a killed run stops before grouping;
   `--phase group --commit` rebuilds every group from Smarter Contact itself.
4. Templates: `--phase templates --commit` (HTTP, no browser). Smarter Contact
   refuses a template too similar to one already in the account
   (TEMPLATE_DUPLICATE_ERROR at 0.95, accepted at 0.88); the audit's
   `SC_SIMILAR` guard is 0.90.
5. Test text first: `--phase test --phone <your number> --commit`, then a
   one-person campaign on group `Land Test`.
6. `python src/smarter_contact_launch.py --phase campaign --http --land lot|tract
   --wave 1 (0 = no-name) --tz east|other --group "<group>" --per-min N
   --start "YYYY-MM-DD HH:MM" --commit --schedule`. `--http` uses the agent's
   own login, so the browser session lapsing does not block a launch.

Reply routing: `sc_client.contact_record_uuid` reads `Sift Record` first, then
APN, so patched land contacts and old Priority 1 contacts both route to the
right record. `smarter_contact_audit.py` resolves the record the same way (APN
holds the parcel on a land contact, so an APN-only audit flags every one).

Late contacts (owners added after the groups were already sending) go into their
own groups via `create --group-suffix "Late 1110"`, so a running campaign never
picks up a contact mid-send.

### Deal cards: the numbers behind every land reply

`python src/smarter_contact_land.py --phase deals --commit` (`--first <uuid>` to
probe one, `--refresh` to rewrite fields already written) puts the land model's
numbers on every land record, for the team and never for the agent:

- A **pinned Sift note** starting `Land deal card`: lane, how we make money on it,
  offer range, what it could sell for and why, the spread, and the buyers.
- Smarter Contact fields **Land Offer / Land Value / Land Notes**, so the number
  is in front of whoever opens the conversation.
- **Buyers** (`BuyerIndex`, three reads): builders who fit (from builder_land's
  `analysis.json` matches, top 3 with distance, buys in 12 months, price paid,
  resale and whether we hold phones), land buyers nearby (scored.jsonl sales in
  3 years inside the lane's radius, 1 mi lot to 10 mi tract, size 0.4x to 2.5x,
  houses and BUILT parcels excluded, because they put a $237,000 median on a 0.14
  acre lot), and new homes built within 2 mi since 2023.

Live 2026-11-10: 1,293 records, 1,984 contacts, read back on the first of each.

**The Slack hot lead carries the card.** `worker.py` calls
`crm.land_deal_card(record)` for every non-dispo escalation and `escalate.hot_lead`
adds it as its own block under "internal numbers, never quote them by text".
A record with no card posts exactly as before. The agent itself never sees it,
so a price can still never reach a text.

### Touch 2 (day 2, 9am East / noon Other TZ)

Per group, Late 1110 groups included:

1. `python src/smarter_contact_launch.py --phase followup --http --land lot|tract
   --group "<group>" --source-date 2026-11-10 --commit` (drafts, no `--schedule`).
2. `python src/smarter_contact_audit.py --drafts ...` on every draft.
3. `--phase touch ... --commit --schedule`.

`--group` is required for land: followup and touch otherwise build the Priority 1
group names and find nothing.

## Sources

- Smarter Contact help center (support.smartercontact.com): message templates,
  drip campaigns, quick replies, labels, contact workflows, suppression lists,
  the DataSift integration, 10DLC.
- DataSift: intercom.help/reisift/en/articles/8881995.
- Copy evidence: BatchLeads first-text word list, Launch Control (3-5 messages,
  end on a question), Real Estate Skills ("never sell, or just not right now?").
- Compliance: the FCC opt-out rules effective April 2025; the one-to-one consent
  rule vacated January 2025; the line of district court cases holding that offers
  to buy a house are not telephone solicitations (Aussieker v. Aghazadeh, E.D.
  Cal. 2025).
