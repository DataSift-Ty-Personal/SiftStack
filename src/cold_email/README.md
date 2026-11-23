# Cold email (Instantly)

B2C cold email to homeowners, built from the B2C Cold Email Lab course (Skool) and wired into SiftStack's
lanes, gate and Slack. Built, loaded (506 leads) and launched 2026-11-16, then paused the same evening with 0
sent: Ty is waiting one day for Instantly support to set the inbox profile photos. `hold.json` in the state
folder keeps the morning health check quiet until its date; `campaign.py launch --yes` resumes on Ty's word.
The course text itself is paid material and lives only in `output/cold_email/course/` (gitignored);
everything here is our own.

## What runs, and where

The four jobs are stages of the agents brain (`src/agents/autopilot/coldemail.py`, `team/autopilot.yaml`),
calling the code in this folder. Until the cutover they ran as Windows scheduled tasks on Ty's PC
("SiftStack Cold Email Replies/Top-up/Health/Stats"), which ran only while that PC was on.

| Stage | When | What | Spend |
|---|---|---|---|
| `cold_email_replies` (`replies.run_pass`) | every 5 minutes | Instantly replies, classified by `sms_agent.classify`; hand raises to the TEXT LEADS Slack channel | free |
| `cold_email_topup` (`daily.topup_run`) | nightly 8:15pm | keeps 3 sending days of checked leads queued (build + ZeroBounce + load) | outreach: acts on the free box only when listed in `AGENTS_LIVE_STAGES` |
| `cold_email_health` (`daily.health_run`) | weekdays 8:00am | inboxes, warmup, bounces, campaign, reply watcher; a problem goes to #issues, a healthy morning posts nothing | free |
| `cold_email_stats` (`daily.stats_text`) | Mondays 9:05am | last week's numbers, a report on the marketing board | free |

**One install runs them (`coldstate.py`).** Two reply watchers post every hand raise twice and two top-ups
race the ledger, so each job acts only where the state folder's `home.json` names this install
(`COLD_EMAIL_INSTALL`, else the Fly app's name, else `workstation`). With no `home.json` the workstation is
the home, so a PC that was never cut over keeps working and the brain does nothing. A command line run with
`--commit` or `--post` on an install that is not the home stops with exit code 3. On one machine an
operating system lock (`locks/replies.lock`, `locks/topup.lock`) keeps the clock's run and a run by hand from
overlapping; it lets go when the process ends, however it ends.

**The state folder:** `COLD_EMAIL_STATE_DIR`, else `AGENTS_DATA_DIR/cold_email` (`/data/cold_email` on the
box), else `output/cold_email`. It holds `ledger.json`, `rejected.json`, `replies_state.json`,
`replies.jsonl`, `hold.json`, `daily_state.json`, `home.json`, `replies_heartbeat.json` (the watcher's last
committed pass, read by the health check wherever it runs) and `lists/`.

**The cutover** (`deploy/cold_email_cutover.py`, dry by default, run from the checkout that holds the live
state): `--check` is the read-only preflight; `--commit` disables the four Windows tasks, waits out a run in
progress, names the box the home in the PC's own folder, copies the state into `/data/cold_email` (checked
by hash) and names the box the home there last; a failure while copying puts the PC back. The top-up then
needs its id in `AGENTS_LIVE_STAGES` (the command is printed with the stages already there; `--set-live`
runs it, which restarts the box). `--verify` reads the box's heartbeat; `--back --commit` moves it all back.

```bash
python src/cold_email/setup_accounts.py [--commit]              # inbox settings, sender name, signature
python src/cold_email/emailcopy.py                              # audit every combination of the copy
python src/cold_email/build_list.py --preset "Agency - 01 Queue" --target 600 [--verify]
python src/cold_email/campaign.py create|load|preview|launch --yes|pause|status
python src/cold_email/replies.py [--commit] | --render "reply text" | --init-state
python src/cold_email/daily.py topup|health|stats
python src/agents/cli.py autopilot run cold_email_replies      # the stage, as the brain runs it
python deploy/cold_email_cutover.py [--check | --commit | --verify | --back --commit]
```

## The pieces

- `instantly.py`, `zerobounce.py`: thin API clients. Keys `INSTANTLY_API_KEY`, `ZEROBOUNCE_API_KEY` (env or `.env`).
- `coldstate.py`: the state folder, this install's name, `home.json`, the run locks, the reply heartbeat,
  and whole-file JSON writes (the ledger is read by the reply watcher while a load writes it).
- `setup_accounts.py`: the course's inbox settings on every sending inbox (10 a day, 10-minute gap, slow
  ramp, warmup kept on at +1 a day matched to volume, 95% reply/open/spam-save/important, weekdays, read
  emulation), sender name and a name-only signature. Backs up every inbox first, patches one, reads it back.
- `emailcopy.py`: three versions of one email in Ty's voice ("Hey", short paragraphs, "Thanks"), spintext on
  every line and subject, one question, a reply-based opt-out, no links, never names the list. `audit()`
  renders all 21,870 combinations and fails on a dash, a link, a second question, a banned word or caps.
- `build_list.py`: preset rows, then the same gate every channel uses (on its own `email` channel: returned
  postal mail does not stop an email, Do Not Mail does), people only, Tennessee only (the copy says "a local
  buyer in <city>"), ONE email per owner whose name part carries the owner's name, ZeroBounce with Verify+
  ("valid" only), and the owner's other properties (a lead, a stop, a finished status, research, or an
  in-house lane anywhere takes the owner off). `ledger.json` stops an owner or address from being loaded
  twice; `rejected.json` keeps a ruled-out record from being read again for 30 days. A preset past the
  search's 10,000-row window is read in slices of its own `created` dates (newest first, cut down to the
  minute for a bulk-pull day), worked out once per list and kept in its state.
- `campaign.py`: one step, three versions; Mon-Fri 9 to 5 Eastern; every inbox; stop on reply; open and link
  tracking off; plain text; new leads first; no unsubscribe link. Launch needs `--yes`.
- `replies.py`: STOP (word patterns only) -> Instantly block, DataSift DNC list, a note. NOT INTERESTED and
  WRONG PERSON -> Instantly block, a note. INTERESTED -> the texting handoff in the text-leads channel
  (`SMS_AGENT_SLACK_WEBHOOK`, `SMS_AGENT_HANDOFF_NAME`) with the record's callable numbers and link; New Lead
  status only when `SMS_AGENT_HAND_RAISE_NEW_LEAD` is on (the texting side's switch). ASKING WHO, OTHER and
  anything sensitive -> the same channel as "needs an answer". Nothing is ever sent back by email.
  A reply is never lost: a hand raise the channel refuses, a failed block and a stop that did not reach
  the DNC list are all read again next pass, and after 6 failures the reply goes to a person. The state is
  saved after every reply, so a pass that dies halfway (a redeploy) never handles one twice. A committed
  pass refuses to start, reading nothing, without the text leads channel, without `replies_state.json`
  (a new install starts it with `--init-state`) or without a readable ledger.

## Measured (2026-11-16)

- DataSift owner emails are a loose list: median 5 per owner, about 97% of records carry some, about 70%
  carry one with the owner's name in it. Never email them all.
- First list: 1,580 Agency queue rows -> 1,140 owners with a named email -> ZeroBounce 570 valid (50%),
  392 invalid, 83 unknown (free), 65 known complainers, 30 catch-all or do-not-mail; 926 credits -> 506 leads
  after the other-property and Tennessee checks.
- Of the address we would send to: Yahoo 11%, AOL/Verizon 8%, AT&T 5% (Agency queue alone 34%). On 50 of
  those, a standard check passed 27; Verify+ found 2 of the 27 dead and 3 unconfirmable. MillionVerifier's
  own help center says disabled Yahoo and AOL accounts read as good. Hence ZeroBounce with Verify+.

## Traps

- Instantly's campaign schedule takes 102 fixed time zones and America/New_York is not one: use America/Detroit.
- A body of bare text with `<br>` is stripped to nothing on save, with no error. One `<div>` per line, and
  trust a body only after the read-back finds the words (`campaign.copy_problems`).
- `GET /campaigns/analytics?id=` answers an empty list; `ids=` works.
- `link_tracking: false` reads back as null.
- Signatures show only where the body carries `{{accountSignature}}`. Done-For-You profile photos have no
  API; Instantly support sets them.
- ZeroBounce's validate APIs take `verify_plus` per call; it overrides the account setting.
- On the box a reply pass waits behind whatever stage holds the brain's schedule lane (one at a time), so a
  hand raise can lag the 5-minute slot while a long stage runs. An Instantly `reply_received` webhook to a
  brain route would put replies on the people lane; not built.

## Next

1. Lanes beyond the Agency queue (Top 1,000 and first to market as an extra touch, timed against texts and
   calls), each with its own campaign and copy.
2. The postal address line: Ty took his own out; a PO box or virtual mailbox goes in `COLD_EMAIL_ADDRESS`.
3. An opt-out, a death or a sale found after a lead is loaded does not reach Instantly: the revocation stage
   pulls an owner out of texts and mail, not out of a running email campaign.
