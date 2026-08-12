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
- **Recipient-local quiet hours.** 8am to 9pm in *their* timezone, derived from
  their area code. Queued messages wake with up to 30 minutes of jitter so a
  night's backlog does not fire as one 08:00:00 burst.
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
python src/sms_agent/cli.py selftest              # 57 assertions, zero network
```

`selftest` runs the whole pipeline against a throwaway database with every
outbound edge stubbed, and **asserts** rather than prints. It is safe to run at
any time with production credentials loaded. It covers deterministic
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
