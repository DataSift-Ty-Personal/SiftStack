# Cold email (Instantly)

B2C cold email to homeowners, built from the B2C Cold Email Lab course (Skool) and wired into SiftStack's
lanes, gate and Slack. Built, loaded (506 leads) and launched 2026-11-16, then paused the same evening with 0
sent: Ty is waiting one day for Instantly support to set the inbox profile photos. `output/cold_email/hold.json`
keeps the morning health check quiet until its date; `campaign.py launch --yes` resumes on Ty's word. The course text itself
is paid material and lives only in `output/cold_email/course/` (gitignored); everything here is our own.

## What runs, and where

| Job | When | What |
|---|---|---|
| `replies.py --commit` | every 5 minutes | Instantly replies, classified by `sms_agent.classify`; hand raises to the TEXT LEADS Slack channel |
| `daily.py topup --commit` | nightly 8:15pm | keeps 3 sending days of checked leads queued (build + ZeroBounce + load) |
| `daily.py health --post` | weekdays 8:00am | inboxes, warmup, bounces, campaign, reply watcher; posts to the issues channel only when wrong |
| `daily.py stats --post` | Mondays 9:05am | last week's numbers to the pipeline channel |

Today these are Windows scheduled tasks on Ty's workstation ("SiftStack Cold Email ..."), so they run only
while that PC is on. Moving them into the agents brain is the next step (below).

```bash
python src/cold_email/setup_accounts.py [--commit]              # inbox settings, sender name, signature
python src/cold_email/emailcopy.py                              # audit every combination of the copy
python src/cold_email/build_list.py --preset "Agency - 01 Queue" --target 600 [--verify]
python src/cold_email/campaign.py create|load|preview|launch --yes|pause|status
python src/cold_email/replies.py [--commit] | --render "reply text"
python src/cold_email/daily.py topup|health|stats
```

## The pieces

- `instantly.py`, `zerobounce.py`: thin API clients. Keys `INSTANTLY_API_KEY`, `ZEROBOUNCE_API_KEY` (env or `.env`).
- `setup_accounts.py`: the course's inbox settings on every sending inbox (10 a day, 10-minute gap, slow
  ramp, warmup kept on at +1 a day matched to volume, 95% reply/open/spam-save/important, weekdays, read
  emulation), sender name and a name-only signature. Backs up every inbox first, patches one, reads it back.
- `emailcopy.py`: three versions of one email in Ty's voice ("Hey", short paragraphs, "Thanks"), spintext on
  every line and subject, one question, a reply-based opt-out, no links, never names the list. `audit()`
  renders all 21,870 combinations and fails on a dash, a link, a second question, a banned word or caps.
- `build_list.py`: preset rows, then the same gate every channel uses (on the mail channel; returned postal
  mail does not stop an email), people only, Tennessee only (the copy says "a local buyer in <city>"), ONE
  email per owner whose name part carries the owner's name, ZeroBounce with Verify+ ("valid" only), and the
  owner's other properties (a lead, a stop, a finished status, research, or an in-house lane anywhere takes
  the owner off). `ledger.json` stops an owner or address from being loaded twice; `rejected.json` keeps a
  ruled-out record from being read again for 30 days.
- `campaign.py`: one step, three versions; Mon-Fri 9 to 5 Eastern; every inbox; stop on reply; open and link
  tracking off; plain text; new leads first; no unsubscribe link. Launch needs `--yes`.
- `replies.py`: STOP (word patterns only) -> Instantly block, DataSift DNC list, a note. NOT INTERESTED and
  WRONG PERSON -> Instantly block, a note. INTERESTED -> the texting handoff in the text-leads channel
  (`SMS_AGENT_SLACK_WEBHOOK`, `SMS_AGENT_HANDOFF_NAME`) with the record's callable numbers and link; New Lead
  status only when `SMS_AGENT_HAND_RAISE_NEW_LEAD` is on (the texting side's switch). ASKING WHO, OTHER and
  anything sensitive -> the same channel as "needs an answer". Nothing is ever sent back by email.

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

## Next: into the SiftStack pipeline (a new session)

1. Run the four jobs in the agents brain instead of on the workstation: `replies` as a 5-minute stage (or an
   Instantly `reply_received` webhook to a brain route), `topup`, `health`, `stats` as scheduled stages.
   The files they keep (`ledger.json`, `rejected.json`, `replies_state.json`, `replies.jsonl`, `hold.json`)
   move to the brain's volume. Each job must run in exactly ONE place: copy the state, start the job on the
   box, and disable its Windows task in the same step. Two reply watchers would double-post hand raises and
   two top-ups would race the ledger.
2. An `email` channel in `agents/autopilot/gate.py` instead of borrowing `mail`, so a stage can lift or add
   rules for email on its own.
3. Lanes beyond the Agency queue (Top 1,000 and first to market as an extra touch, timed against texts and
   calls), each with its own campaign and copy.
4. Rows past the 10,000-row search window: slice the preset by `created` dates (about 3,000 leads are
   reachable in the first 10,000 rows, roughly 12 sending days).
5. The postal address line: Ty took his own out; a PO box or virtual mailbox goes in `COLD_EMAIL_ADDRESS`.
