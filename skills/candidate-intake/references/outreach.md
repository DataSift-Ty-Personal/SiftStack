# Outreach: screening messages

Runs ONLY after the user has seen the ranked review and named who to contact.
Outreach goes out on the channel each candidate arrived through, using the
templates in `config.outreach.templates`.

## The message

- Start from the channel's template, substitute `{first_name}` and `{role}`.
- Personalize one clause max from their application ("your call center
  experience sounds like a fit") so it reads human, not mail-merged.
- Keep it under 4 sentences. Casual register, first name only, no "Dear", no
  corporate sign-offs.
- The default ask (when `config.outreach.video_ask` is true) is a 1-2 minute
  self-intro video. It is the single best async filter for phone roles:
  it tests English, energy, and follow-through at once.
- **Zero em dashes and zero en dashes.** Check the final text for both
  characters before sending. Rewrite with commas or periods.

## Per-channel send mechanics

| Channel | How to send |
|---------|-------------|
| Indeed | Candidate detail page > Message. Sending typically marks them Contacted on Indeed too. |
| Gmail | Reply in the applicant's existing thread. Never start a new thread. |
| Facebook Post | Open a Messenger DM to the commenter (private), not a public comment reply. |
| Facebook DM | Reply inside the existing Messenger thread. |
| Manual | Gmail compose to their email if present; otherwise hand the message text to the user to send themselves. |

## The verify-before-send ritual (every message, no exceptions)

1. Type the message into the compose box but DO NOT send.
2. Read the compose state back from the page (screenshot or page text).
3. Confirm three things: the recipient name matches the intended candidate,
   the `{first_name}` in the text matches that recipient, and there are no
   em dashes or en dashes and no leftover `{placeholders}`.
4. Only then click send.
5. Confirm the message appears in the thread as sent.

Wrong-recipient is the catastrophic failure mode of this skill. The ritual is
cheap; skipping it is not.

## After sending

- Set the candidate's `Outreach Status` in the master sheet to `Contacted`
  (see `references/sheet-schema.md`).
- Report back one line per send: name, channel, sent/failed.
- If a send fails (message button disabled, thread locked), record it, tell
  the user, and move on; never retry through a different channel without
  asking, since that can look like pestering.

## Replies and follow-ups

When the user later reports replies ("Maria sent her video"), update
`Outreach Status` (`Replied`, `Video received`, `Scheduled`). If asked to
draft a follow-up for non-responders, wait at least 2 days after the first
touch, one follow-up max, same channel, same rules.
