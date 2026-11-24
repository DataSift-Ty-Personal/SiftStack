# SiftStack dialer

Your own dialer on Twilio, inside DataSift: a browser softphone, a Call button beside every phone
number in DataSift, and a single-line power dialer that walks a DataSift call preset. Every call
writes back to the record and tells the texting app (Smarter Contact) what happened.

Code: `src/dialer/` (the app), `extensions/sift-dialer/` (the Chrome extension),
`src/sms_agent/dialer_hook.py` (the texting app's half). Fly app `siftstack-dialer`.

## What it does

| | smrtPhone / Kixie | SiftStack dialer |
|---|---|---|
| Click to call from DataSift | yes | yes: a Call button beside every number (extension) |
| Power dialer | yes (Kixie multi-line) | single line: one call at a time, the caller on the line first |
| Call list | their own lists | any DataSift call preset, every record through the gate first |
| Dispositions to the CRM | phone status | phone status, record status, tags, lists, a call-back task, a note, the attempt counter |
| Recording | yes | yes, two channels, never in an all-party-consent state |
| Voicemail drop | yes | built, OFF (see Compliance) |
| Hold, warm transfer, listen / whisper / barge | yes | yes (a conference per call) |
| Call-backs ring the caller | yes | yes, then voicemail; missed calls wait on the phone page |
| Texts beside the call | smrtPhone only | the Smarter Contact thread for the number, live |
| Hand raises | no | a texted "interested" lands at the top of every caller's page |
| Calling hours, do-not-call, the gate | no | refused before anything rings |

## How it is built (and why)

- **Twilio's own open-source SDKs do the hard parts**: the Voice JavaScript SDK (Apache-2.0) in the
  browser, twilio-python (MIT) on the server, libphonenumber (Apache-2.0) for numbers, time zones and
  states. No open-source dialer was mature enough to adopt (the review is below).
- **A conference per call.** The caller's browser joins a conference named for the call; the server
  dials the homeowner into it. That one choice is what makes voicemail drop, hold, transfer and a
  manager listening in possible.
- **The phone lives in a normal tab of our own site**, not inside the extension. Chrome does not show
  a microphone prompt in an extension side panel or offscreen document, and keeping the phone in our
  own page means the extension holds no keys and never talks to the server: it only passes messages
  between the DataSift tab and the dialer tab, and draws a small call bar on the DataSift page.
- **Its own app.** A deploy of the dialer never restarts the texting app, and the reverse. The issue
  fixer never deploys it on its own (a restart mid-call drops the call's callbacks).

## Setup, once

1. **Twilio** (only you can do this): make an account at twilio.com, upgrade it from trial, and buy
   one local number per caller (Phone Numbers, Buy a number, area code 865). In Trust Hub, create the
   business profile and turn on SHAKEN/STIR and CNAM, so calls carry your business name and full
   attestation (free).
2. Put `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` (the console's front page) in `.env`, plus:
   ```
   DIALER_NUMBERS=8655550100,8655550101
   DIALER_TEST_NUMBERS=<your own cell>
   DIALER_CALLERS=[{"identity":"casey","name":"Casey","number":"8655550100"},{"identity":"morgan","name":"Morgan","number":"8655550101","role":"manager"}]
   ```
3. `fly auth login` (a browser sign-in), then:
   ```
   python deploy/dialer_up.py            # the plan, changes nothing
   python deploy/dialer_up.py --commit   # Twilio app + key, Fly app, secrets, deploy, callers, sign-in links
   ```
4. Each caller opens their sign-in link once (it works once, on that browser), and installs the
   extension: `chrome://extensions`, Developer mode, Load unpacked, pick `extensions/sift-dialer`.

## Going live, one switch at a time

The app starts with `DIALER_CALLS=test` (only `DIALER_TEST_NUMBERS` can be dialed) and
`DIALER_WRITEBACK=dry` (outcomes are logged, nothing is written to DataSift).

1. Call your own cell from the phone page and from a Call button in DataSift. Hang up, pick an
   outcome, read `python -m dialer.cli calls --today` on the box.
2. `DIALER_WRITEBACK=live` (a Fly secret). One call to a test record; read the record in DataSift.
3. `DIALER_CALLS=live`. The calling hours and the gate now decide who rings.
4. Pair the texting app (your go, it restarts the box that takes seller replies): set on `siftstack`
   `SMS_AGENT_DIALER_TOKEN` = `DIALER_TEXTING_TOKEN`, `SMS_AGENT_DIALER_HANDRAISE_TOKEN` =
   `DIALER_HANDRAISE_TOKEN`, `SMS_AGENT_DIALER_URL` = the dialer's address, and deploy it.

Fly secrets override `fly.dialer.toml`'s `[env]`. Ask the running app: `/health` says what it is doing.

## Compliance (not legal advice; counsel keeps these)

- **Hours**: the house hours (9am to 7pm) on the business clock AND in every time zone the number
  may be in (an area code that straddles a zone line checks both). The legal 8am to 9pm never moves.
  A number that called in within two hours may be called back outside the house hours.
- **State rules** (`rules.STATE_RULES`): Florida, Oklahoma and Maryland cap an owner at 3 dials in 24
  hours and the power dialer waits for the caller's click there (their autodialer definitions can
  reach a dialer that moves on by itself); Pennsylvania: no Sunday calls; Washington: no recorded
  voicemail. A state reaches a call by the number's area code or the owner's mailing state.
- **Recording**: never when the number or the owner's mailing state is all-party consent
  (`config.ALL_PARTY_STATES`). `DIALER_RECORD=off` stops it everywhere.
- **Voicemail drop is OFF** (`DIALER_VM_DROP`). A recorded voicemail is a "prerecorded voice" under
  the TCPA, and courts have let claims proceed over agent-triggered drops (2026) and over
  prerecorded house-buying calls (2023). A voicemail the caller speaks live is always fine. Turn it on
  only with counsel's sign-off.
- **The do-not-call registry flag** is honored by default (the gate's `registry_flag`); the texting
  playbook lifted it for texts on 11-17. 2026 cases treat some "we buy houses" outreach as
  telemarketing, so the dialer keeps the stricter default until counsel says otherwise
  (`DIALER_GATE={"registry_flag": false}` to match the playbook).
- **One line, the caller on it first**: no abandoned calls, which is what the 3% rule polices.

## Costs (Twilio, US, 2026)

About $0.024 a minute of conversation (outbound $0.014, the browser leg $0.004, the conference
$0.0018 per participant, recording $0.0025), about $0.003 for a dial nobody answers, $1.15 a month a
number. One caller making 300 dials a day, 15% answered at two minutes each, is about $3 a day;
two callers over 22 working days come to about $130 a month.

## The open-source review (2026-11-19)

| Piece | License | Verdict |
|---|---|---|
| Twilio Voice JavaScript SDK (@twilio/voice-sdk) | Apache-2.0 | Adopted. Pinned at 2.18.5, hash checked at build. |
| twilio-python | MIT | Adopted. Tokens, TwiML, the REST calls, the webhook signature. |
| libphonenumber (phonenumbers) | Apache-2.0 | Adopted. Numbers, time zones, states. |
| Twilio's own MV3 extension sample (twilio-voice.js /extension) | Apache-2.0 | Read, not used: it runs the phone in an offscreen document, where Chrome shows no microphone prompt. |
| twilio-labs function-templates | MIT | Patterns only (transfer, voicemail, conference). |
| Community Twilio dialers (OpenDialer, open-dialer and others) | MIT, mostly | Rejected: 1 to 16 stars, a few commits each. Their conference and disposition shape matches ours. |
| Frappe CRM telephony | AGPL-3.0 | Read only (AGPL): a real Voice SDK integration with call logs. |
| Chatwoot, Twenty, EspoCRM, SuiteCRM, Odoo | various | Rejected: voice is paid, absent, or needs SIP over WebSocket, which Twilio does not offer. |
| VICIdial, FreeSWITCH, Asterisk, jambonz, Fonoster, Wazo | GPL, MPL, MIT | Rejected for now: running our own phone switch, for multi-line predictive dialing we are not doing. |
| Pipecat (voice AI on Twilio Media Streams) | BSD-2 | Later, for live call notes or an AI voicemail detector. |

## Commands

```
python -m dialer.cli doctor | callers | calls --today
python -m dialer.cli add-caller casey Casey --number 8655550100 [--role manager] [--user <DataSift user uuid>]
python -m dialer.cli link casey          # a new one-time sign-in link
python -m dialer.cli revoke casey        # signs them out everywhere
python -m dialer.cli undo <call id> [--commit]   # put back what a wrong outcome wrote
```

## Tests

`python -m dialer.selftest` (zero network, a fake Twilio and DataSift; also in `pytest tests/test_dialer.py`),
`python -m dialer.ui_check` (the page in a real browser, screenshots at 360 / 768 / 1280),
`python -m dialer.ext_check` (the extension loaded in Chromium against a stand-in DataSift page).
The texting side is in the texting selftest's `_dialer` part.
