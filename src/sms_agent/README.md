# Two-way SMS agent

Sends outreach, reads replies in real time, classifies them, writes the result
back to DataSift, and hands positive responses to a prospector in Slack.

## The constraint that shapes everything

**DataSift webhooks cannot see an inbound text.** The sequence trigger list is
ten CRM state changes (status, tags, lists, assignee, tasks, SiftLine cards).
There is no SMS-received trigger, no conversation event, and DataSift does not
send SMS itself, it hands off to smrtPhone / Twilio / Plivo.

smrtPhone does have what we need. Its webhooks fire on `smsIncoming`,
`smsOutgoing`, `smsDeliveryCallback`, `addNumberToDNT` and `addNumberToDNC`.

So: **the inbound leg runs on smrtPhone webhooks, the CRM leg runs on DataSift
webhooks, and both post to the same receiver.**

### Sending: two transports

smrtPhone has a public API for **text** SMS (`POST /sms/send`, header
`X-Auth-smrtPhone`, key from Admin > API Tokens). What it has no API for is
**MMS** - that is what forced the browser route on the original auction
screenshot send. Replies carry no image, so the API is the right transport here.

Because that key has not been proven live on this account yet, both paths exist:

| `SMS_AGENT_TRANSPORT` | Behavior |
|---|---|
| `auto` (default) | API first, fall back to the browser session on a transport failure. A 4xx is a rejected request, so it never falls back on one. |
| `api` | API only |
| `session` | Playwright against the web app's Compose Message modal, using `smrtphone_state.json` |

`doctor` reports which transports are actually live. **The session path sends
from the account default caller ID only**, so on that path the sticky sender and
per-number caps do not apply; `doctor` says so out loud rather than pretending
the pool is in use.

```
seed send  ->  smrtPhone  ->  homeowner
                   |
              smsIncoming
                   v
   receiver.py   verify -> persist -> 200 (nothing slow)
                   v
   worker.py     drain events, then drain the outbox
                   v
   engine.py     resolve record -> classify -> act
                   |
        +----------+-----------+--------------+
        v          v           v              v
   phone status  DNT/DNC   AI reply       escalate
   CORRECT/       opt-out   (gated)       status + Slack
   WRONG/DEAD
```

## The autonomy ladder

`SMS_AGENT_PHASE` controls how much the agent is allowed to do. It exists
because the phone number is the asset: carriers filter a number into silence
long before anyone sues.

| Phase | What it does | What goes out |
|---|---|---|
| 1 | Classify inbound, write phone status, honor opt-outs | Nothing |
| 2 | + escalate positive replies, flip CRM status | Nothing |
| 3 | + draft replies, held in Slack for approval | Nothing until approved |
| 4 | + auto-send the highest-confidence intents only | Narrow, gated |

**Phase 2 already gives a working prospector handoff with zero AI-authored text
having gone out.** That is the point of stopping there first.

Independently, `SMS_AGENT_DRY_RUN=1` blocks every CRM write and every send, so
the whole pipeline can be run against production data without touching a record
or a phone.

## Guardrails

Each of these exists because of a specific way this goes wrong.

- **Human takeover wins instantly.** An `smsOutgoing` event we did not author
  means a person typed it, so the agent pauses the thread and cancels every
  queued *and held* message for that number. Two of us texting the same seller
  is the worst thing this system can produce.
- **Opt-outs are decided by regex, never by a model.** Carrier keywords plus
  natural language ("stop texting me", "take me off your list"). Local
  suppression, smrtPhone DNT, and the Do Not Market tag all fire.
- **Turn cap.** Six AI turns, then a human takes it.
- **Two send windows, and a message needs both.** Ty's rule since 2026-08-28 is
  9am to 6pm Eastern, and that is our own window in one fixed timezone
  (`SMS_AGENT_BUSINESS_*`), so a reply never lands when nobody here can take the
  callback it invites. On top of it sits the recipient-local window
  (`SMS_AGENT_QUIET_*`), 9am to 6pm in *their* timezone from their area code,
  which is the compliance half and cannot be written as a fixed zone: 9am
  Eastern is 6am in California. The queue waits for the overlap and wakes with
  up to 30 minutes of jitter, so a night's backlog does not fire as one
  09:00:00 burst.
- **Sticky sender.** A conversation keeps the number it started on even when
  that number is at its cap. Switching mid-thread reads as a spam farm.
- **Per-number daily cap and pacing**, well under the 10DLC ceiling.
- **Output validator.** A drafted reply is blocked before it can queue if it
  names a dollar amount, **names the list** (foreclosure, auction, probate, tax,
  lien, eviction, "behind on"), carries a link, contains a zip code, exceeds 320
  characters, asks more than one question, or self-identifies as automated. The
  prompt sets intent; the validator is what holds the line.
- **The responder is given almost nothing.** Owner first name, street line,
  city, county. Valuation, equity, distress flags, vacancy and every list tag
  are withheld from the prompt entirely, so there is nothing to leak.
- **No invented identity.** The thread signs as the person assigned to the
  record. An unmapped assignee means unsigned, never a guess. (It made up
  "Alex" the first time this was tested.) No company name is ever said.
- **Confidence floor.** Below 0.80, or outside the narrow auto-send intent set,
  the reply drafts to Slack instead of sending.
- **System tags are prefixed `sys_`** so DataSift sequence conditions can
  exclude them and our own writes never re-trigger the sequences that called us.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the SMS_AGENT_* block
python src/sms_agent/cli.py doctor
```

`doctor` prints the exact webhook URLs to paste and the events to subscribe to.

**1. smrtPhone.** Admin > API Tokens for `SMRTPHONE_API_KEY`; Admin > Phone
Numbers for the pool (put them in `config/sms_numbers.json`). Then Admin >
Webhooks > New Webhook, endpoint `https://YOUR_HOST/hooks/<secret>/smrtphone`,
subscribed to `smsIncoming`, `smsOutgoing`, `smsDeliveryCallback`,
`addNumberToDNT`, `addNumberToDNC`.

**2. DataSift.** Settings > Integrations > Webhooks > Add Webhook, pointed at
`https://YOUR_HOST/hooks/<secret>/datasift`. Then build a sequence whose action
is that webhook. Payload shape is undocumented, so Phase 0 is to point it here
and read the `events` table.

**3. Map the current campaign.** An inbound reply carries only a phone number,
so the agent resolves it through `phone_map`. Sends register the mapping
automatically; backfill the campaign you already texted:

```bash
python src/sms_agent/cli.py map --csv output/mms_send_queue.csv
```

**4. Run it locally.**

```bash
python src/sms_agent/cli.py serve            # receiver
python src/sms_agent/cli.py work --loop      # worker (separate process)
```

## Deploying (Fly.io)

The receiver needs a public address that is always up, because smrtPhone gives
no retry guarantee: a reply that arrives while the box is down is a lost lead
with no trace of it. Config is in [fly.toml](../../fly.toml) and
[deploy/Dockerfile](../../deploy/Dockerfile).

```bash
fly launch --no-deploy --copy-config
fly volumes create sms_agent_data --size 1 --region atl
fly secrets set   SMS_AGENT_WEBHOOK_SECRET=...   SMRTPHONE_API_KEY=...   REISIFT_API_KEY=...   ANTHROPIC_API_KEY=...   SMS_AGENT_SLACK_WEBHOOK=...
fly deploy
fly logs
curl https://siftstack.fly.dev/health
```

Then paste the two webhook URLs (from `doctor`) into smrtPhone and DataSift.

Four deliberate choices in that config:

- **One machine, not two.** The worker runs as a thread inside the receiver
  (`SMS_AGENT_INLINE_WORKER=1`). SQLite is single-writer, so splitting web and
  worker across two machines would mean two machines wanting the same volume.
  Split them when the store moves to Postgres, not before.
- **`auto_stop_machines = false`.** A stopped machine drops the webhook, and
  there is no replay to recover it.
- **A persistent volume at `/data`.** The event log is the system of record:
  smrtPhone purges its own webhook logs after 30 days.
- **No Playwright in the image.** The API transport is verified working, so the
  browser fallback stays a local-only path and the image stays small.

`config/sms_numbers.json` and `config/sms_senders.json` go on the volume at
`/data/` so they can be edited without a redeploy:

```bash
fly ssh console -C "cat > /data/sms_senders.json" < config/sms_senders.json
```

## Outreach: the front half of the loop

Everything above handles replies. `seed` starts the conversations, rendering a
touch from the same proven pools the `text-touch-builder` skill uses and
queueing it through the **same outbox as every AI reply**. That is the design
point: outreach gets no private send path, so suppression, quiet hours,
per-number caps, pacing and the sticky sender all apply to it automatically.

```bash
python src/sms_agent/cli.py seed --csv export.csv --touch 1           # preview
python src/sms_agent/cli.py seed --csv export.csv --touch 1 --queue   # stage as HELD
python src/sms_agent/cli.py release --touch 1                         # the go/no-go
```

Seeding also registers the phone in `phone_map`, which is how the eventual
reply finds its record. Records are held back rather than sent when the number
is suppressed, the thread already has a reply in it, there is no assigned
caller to sign as, or the rendered copy fails the human-voice check.

Name hygiene is applied here: "C Eugene Suthard" becomes "Hi Eugene", while
"E A Henry" and any LLC or trust get the owner-of-the-address wording instead.

## Testing without sending anything

```bash
python src/sms_agent/cli.py selftest              # over 600 checks, zero network
```

`selftest` runs the whole pipeline against a throwaway database with every
outbound edge stubbed, and **asserts** rather than prints. It is safe to run at
any time with production credentials loaded: since 2026-11-13 the CRM client and
the network socket are both replaced for the run, and the last check asserts
that nothing reached the network. (Before that the CRM writes were stubbed but a
dozen record READS were not, and each run sent them to the live account as
404s on invented ids.) It covers deterministic
classification, opt-out on all four surfaces, wrong-number and dead-number
writes, hot-lead escalation, human takeover, the output validator, timezone
resolution, sticky senders, outreach copy, seeding guards, and the worker's
refusal to send to a suppressed number.

It has already caught two real bugs: the stdlib module shadow that silently
degraded every classification, and a name-hygiene bug (shared with the skill)
where an initials-only owner got greeted by their surname.

Per-piece tools:

```bash
python src/sms_agent/cli.py classify "how much are you offering"
python src/sms_agent/cli.py simulate 8652548712 "who is this"
python src/sms_agent/cli.py thread 8652548712
python src/sms_agent/cli.py draft 8652548712
python src/sms_agent/cli.py approve 8652548712     # release a held draft
python src/sms_agent/cli.py pause 8652548712       # stop the agent on one thread
python src/sms_agent/cli.py digest                 # the daily readout
python src/sms_agent/cli.py status
```

`simulate` pushes a synthetic `smsIncoming` through the real pipeline, so it
exercises classification, CRM writes, escalation and drafting exactly as a live
webhook would.

## The daily digest

`digest` is two things in one message. The funnel on top (sent, replies, how
they read, suppressions, pool capacity) and the **work queue** underneath:
drafts waiting on approval, threads a human took over, soft nos old enough to
work again, send failures, and a warning if webhook events have gone
unprocessed for a day, which means the worker is not running.

The queue half matters more. A draft nobody approves is a conversation nobody
is having. `--post` sends it to the escalation channel.

Soft nos are recorded separately from hard nos, because the proven playbook
works them again later rather than discarding them.

## Voice and identity

`knowledge/playbook.md` is the system prompt. Its rules come from the
**`text-touch-builder` skill's message recipe** (a top-performing SMS
wholesaler's identity-check drip system, blended with cold-email copy rotation)
plus the reply-handling cheat sheet. That outbound sequence is proven copy;
this is the inbound half. Edit the file to change how the agent talks, no code
change needed. Any additional `.md` in the folder is appended in filename order.

What that means in practice:

- **Warm and positive, properly capitalized.** "I hope your week is going
  great." Not clipped, not lowercase-cool.
- **Never name the list.** Never foreclosure, auction, probate, inherited, tax,
  lien, code violation, eviction, divorce, bankruptcy, or "behind on". The
  seller should feel found, not targeted. Enforced by the validator, not just
  the prompt, and those words are never put in the prompt in the first place.
- **Street line only.** "158 Old State Rd", never with city, state and zip. A
  zip code in a text means someone pasted a database row; the validator blocks
  it.
- **One easy question per message**, under 160 characters where possible.
- **Name hygiene.** First real name token only; initials-only, companies,
  trusts and estates get owner-of-the-address wording instead of "Hi FirstName".
- **Soft no versus hard no** is noted on every NOT_INTERESTED, because soft nos
  become follow-ups later.
- **STOP or hostility gets no reply at all**, not even an apology.

### Who the text is from

**The thread is signed by the person actually assigned to the record.** The
record's `assigned_to` uuid resolves through `config/sms_senders.json` to a
first name, so if Adriana owns the record, the text says Adriana and Adriana is
who calls.

```bash
python src/sms_agent/cli.py senders                      # what resolves today
python src/sms_agent/cli.py senders --record <uuid>      # who would sign this one
```

An unmapped uuid means the thread goes out **unsigned**. That is deliberate:
the agent is never allowed to guess a name. On the first test with no name
configured it introduced itself as "Alex", and a fabricated name is a lie the
seller finds out about the moment a real person calls.

**We never say a company name.** A named company is litigation bait and gives a
hostile recipient something concrete to file against. The agent describes
itself by locality built from the record's own county: "a local buyer here in
Blount County".

### What the responder is allowed to know

Deliberately thin: owner first name, street line, city, county. That is all.
Valuation, equity, tax delinquency, foreclosure dates, liens, vacancy, beds,
baths, square footage and every list tag are **withheld from the prompt
entirely**. The responder cannot leak what it was never given.

The flywheel worth building next: the three coach skills (`cold-call-coach`,
`lead-manager-coach`, `closer-coach`) already grade human conversations against
a rubric. Point that engine at the agent's own threads and it becomes a
measurable, improvable texter rather than a prompt someone tweaks on vibes.

## Backfill: test the classifier on real replies

smrtPhone already syncs inbound SMS into the CRM as `owner.sms.received`
activity events, so real homeowner replies exist before a single webhook is
wired. `backfill` replays them through the live classifier. Read-only.

```bash
python src/sms_agent/cli.py backfill --queue output/mms_send_queue.csv
python src/sms_agent/cli.py backfill --queue ... --apply --commit   # terminal intents only
```

`--apply` writes only the OPT_OUT and WRONG_NUMBER consequences. Old INTERESTED
replies are deliberately not escalated: paging a prospector about a
conversation that went cold in June is noise, not a lead.

**What the first run found (24 records from the June send, 9 real replies):**

- The classifier got all nine right. Five resolved on the deterministic rules,
  four needed the model.
- **5 of the 9 replies came from a different number than the one we texted.**
  That is the finding that changed the code: people answer from whichever line
  is in their hand. Mapping only the target number would have left the majority
  of replies unroutable. `crm.map_all_phones` now maps every phone on a record,
  and `map --all-phones` backfills it (219 extra numbers across those 24
  records).
- One reply, *"I'd like it get the house tho in auction if it's cheap enough"*,
  is a **buyer**, not a seller. The model read it as OTHER at 0.55 confidence
  and it falls under the floor, so it drafts for a human instead of being
  treated as a lead. That is the case worth protecting against.

## Build plan switches (2026-11-13)

Nine build-plan packages landed in this app (0.1, 0.4, 0.14, 0.17, 0.22, 0.23,
2.2, 2.3, 4.7). Every one of them sits behind a setting that defaults to what
the app did the day before, so a deploy that sets none of them behaves exactly
as the last one did. `cli.py switches` prints the list and what each is set to.
Fly secrets override `fly.toml`, so ask the running app, not the file.

| Setting | Default | What turning it on changes |
|---|---|---|
| `SMS_AGENT_RATE_BUDGET` | `off` | `count`: every DataSift request is counted and reported, nothing is delayed. `on`: the count is also the limit, 10 a minute (`SMS_AGENT_RATE_PER_MIN`), shared by every process on this database. Reply work goes first and is delayed, never dropped. |
| `SMS_AGENT_GATE` | `off` | `report`: the suppression gate (`gate_rules.py`) says what it would hold in each batch and at release. `enforce`: it holds them. |
| `SMS_AGENT_GATE_READ_FIELDS` | `0` | The gate also reads the Owner Deceased custom field (one more request a record). |
| `SMS_AGENT_OWNER_REVOKE` | `0` | A stop, a no, a hand raise or a reported death on one number pulls the queued and held texts to every other number of that owner, and takes them out of pending Smarter Contact campaigns. |
| `SMS_AGENT_SEND_RECHECK` | `off` | `local`: right before each send, ask again whether the owner stopped on another number (no network). `full`: also read the record and run the gate (one read a send). |
| `SMS_AGENT_DISPOSITION_TABLE` | `0` | Both texting paths disposition a reply from one table (`dispositions.py`). A smrtPhone "not interested" sets Not Interested and marks the number Correct; "not now" goes to Follow Up; a relative is kept, not marked wrong. |
| `SMARTERCONTACT_EXTRA_LABELS` | `0` | Adds the labels Correct Number, Wrong DNC, Relative and Nurture. Create them in Smarter Contact first (`cli.py labels-check`). |
| `SMS_AGENT_HAND_RAISE_NEW_LEAD` | `0` | An interested reply sets New Lead, never over a status that already means lead. |
| `SMS_AGENT_OVER_VALUE` | `not_interested` | `cold_lead`: a reply asking well over value is passed as a Cold lead with an Ask Over Value tag (decision D14, not made). |
| `SMS_AGENT_LISTED_STATUS` | empty | A status name (`follow_up`) sends an interested reply on a listed house to nurture. |
| `SMS_AGENT_SPEED_TO_LEAD` | `off` | `held`: a call-is-coming text is staged about three minutes after a hand raise, for a person to release. `auto`: it sends on its own. |
| `SMS_AGENT_TOUCH_FEED` | `0` | `GET /hooks/<secret>/touches?day=YYYY-MM-DD` returns the day's sent texts for the touch audit: record id, last four digits, time, system. Never a message and never a whole number. |
| `SMS_AGENT_REST_FLAGGED` | `0` | A number on the rest list takes no new conversation and sends no outreach until its rest ends. |

The order to turn them on, each step readable before the next:

1. `SMS_AGENT_RATE_BUDGET=count`. Nothing changes but the counting. After a
   week the digest says how many requests this app really makes and how many a
   limit of 10 a minute would have delayed.
2. `SMS_AGENT_GATE=report`. Each batch, each release and each loader run prints
   what the gate would hold. Nobody is held.
3. `SMS_AGENT_OWNER_REVOKE=1` and `SMS_AGENT_SEND_RECHECK=local`. No network,
   and the only effect is fewer texts: the owner's other numbers stop when one
   of them says stop. The digest counts what was pulled.
4. `SMS_AGENT_GATE=enforce`, then `SMS_AGENT_SEND_RECHECK=full` (one record
   read for each send).
5. `SMS_AGENT_DISPOSITION_TABLE=1`, after reading `cli.py dispositions`. This
   is the one that changes what a reply writes to Sift.
6. `SMARTERCONTACT_EXTRA_LABELS=1`, once `cli.py labels-check` finds all four.
7. `SMS_AGENT_SPEED_TO_LEAD=held`, then `auto` once the staged texts read right.
8. `SMS_AGENT_HAND_RAISE_NEW_LEAD=1` only when the handoff alert and the New
   Lead sequence name the same person. It was switched off on 2026-08-11
   because they did not.
9. `SMS_AGENT_RATE_BUDGET=on` once the share is known. `SMS_AGENT_TOUCH_FEED`
   and `SMS_AGENT_REST_FLAGGED` can go on whenever something reads the one or
   rests on the other. `SMS_AGENT_OVER_VALUE` and `SMS_AGENT_LISTED_STATUS`
   wait on a decision.

```bash
python src/sms_agent/cli.py switches                       # every switch and what it is set to
python src/sms_agent/cli.py dispositions --text "not right now"
python src/sms_agent/cli.py gate --record <uuid> --phone <number>   # reads DataSift, writes nothing
python src/sms_agent/cli.py touches --day 2026-11-12       # the day's sent texts, no network
python src/sms_agent/cli.py budget                         # this app's use of the DataSift share
python src/sms_agent/cli.py number-health                  # who to rest, the rest list, today's line check
python src/sms_agent/cli.py number-health --rest <number> --reason "carrier flagged"
python src/sms_agent/cli.py number-health --canary --to <our own handset>   # stages HELD
python src/sms_agent/cli.py lead-text --record <uuid>      # or --release <phone>
python src/sms_agent/cli.py labels-check                   # reads Smarter Contact, writes nothing
```

What each piece is, in one line: `gate_rules.py` is the suppression gate, a
copy of the brain's (`src/agents/autopilot/gate.py`) with no dependencies, held
to it rule by rule by `tests/test_sms_gate_parity.py`. `gatekeeper.py` is how
this app asks it. `dispositions.py` is the one table for every no.
`revocation.py` is the owner-wide stop and the last look before a send.
`ratebudget.py` is the request budget. `speedtolead.py` and
`knowledge/lead_texts.py` are the call-is-coming text. `numberhealth.py` is the
rest list and the canary. `touchlog.py` is the touch feed.

## Known gaps

- **DataSift webhook payload shape is unverified.** `handle_datasift` resolves
  defensively and logs everything; it writes nothing until the shape is known.
- **smrtPhone's DNT write route is undocumented.** Only the `addNumberToDNT`
  *webhook* is documented. `smrtphone.add_to_dnt` tries the plausible paths and
  reports honestly when none work; local suppression always applies, and a
  remote failure raises a Slack alert telling someone to add it by hand.
- **smrtPhone webhooks are unsigned** and its own logs purge after 30 days.
  Hence the secret path, the optional IP allowlist, and the local event log.
- **Retry behavior is undocumented.** Dedupe is on the event key, not arrival
  order, so replays collapse safely.
- **Slack is post-only.** One-click "I've got this" buttons need a real Slack
  app with interactive components; `escalate._post` is the seam for that.

## Compliance notes

10DLC is live on the sending numbers, but a conversational two-way profile is
different traffic from a one-shot blast: confirm the registered campaign use
case and the throughput tier cover it. TN has covered text solicitations under
its DNC register since 2024-07-01. The agent honors natural-language opt-outs,
not just the literal STOP keyword, because in a back-and-forth a system that
visibly understands English cannot credibly ignore "stop texting me".
