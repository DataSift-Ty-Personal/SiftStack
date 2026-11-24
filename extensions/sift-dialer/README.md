# SiftStack Dialer for DataSift (Chrome extension)

Puts a **Call** button beside every phone number in DataSift (app.reisift.io) and a small call bar in the
corner of the page. Calls go through your SiftStack dialer (`src/dialer`, a Twilio softphone); this
extension only passes messages between the DataSift tab and the dialer tab.

## Install (two minutes, no store listing needed)

1. In Chrome, open `chrome://extensions` and switch on **Developer mode** (top right).
2. Click **Load unpacked** and pick this folder (`extensions/sift-dialer`).
3. The settings page opens. The dialer's address defaults to `https://siftstack-dialer.fly.dev`; change it
   only if your dialer lives somewhere else (Chrome asks once to allow that address).
4. Sign in to the dialer with the link you were sent, and leave its tab open (the extension pins it).

## What it does and does not do

- Clicking **Call** sends the number, and the record it sits on, to the dialer tab. The dialer runs every
  check (calling hours, do-not-call, the gate) before anything rings.
- The call bar shows the live call (Mute, Hold, Hang up) and, when it ends, the outcome buttons. An outcome
  that needs a note or a time is finished on the dialer page.
- When the power dialer moves to the next record, the DataSift tab you used last opens that record
  (switch this off in the settings).
- It holds no passwords, tokens or keys, makes no network requests of its own, and reads nothing on the
  page except phone numbers and the record id in a link or the address bar.
- It never edits DataSift's own text: a button is added beside a number, and a redrawn part of the page is
  scanned again.

Permissions: `storage` (your three settings), `scripting` (only to reach a dialer at a custom address you
allow), and access to app.reisift.io and the dialer's address.
