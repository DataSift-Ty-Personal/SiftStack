# Intake: Facebook (group post + Messenger)

Two sub-channels: comments on a job post in a Facebook group, and Messenger
DMs. Both are read through Chrome on `facebook.com`.

## Group post comments

1. Open each URL in `config.facebook_post_urls`. If the list is empty, ask the
   user for the post link (Profile or Group > their post > timestamp link).
2. Expand ALL comments: click "View more comments" / "Most relevant" and switch
   to "All comments" until no more load.
3. For each commenter who is applying (skip tags of friends, emoji-only
   replies, and the user's own replies):
   - Name -> `full_name`; comment date -> `date_received`.
   - Comment text -> `raw_text`.
   - Profile link (right-click the name, copy link) -> `profile_url`. This is
     the main dedupe key on Facebook; emails are rare here.
   - Location or experience only if their comment states it. Do NOT open
     profiles to mine personal data; that is fabrication-adjacent and slow.
     What they chose to say in the comment is the application.
4. `channel` = `Facebook Post`.

## Messenger DMs

1. Open `facebook.com/messages` (or the Meta Business inbox if the post ran on
   a page). Check both the main inbox and **Message requests**, where strangers
   land by default.
2. For each conversation that is a job inquiry:
   - Name, date of first message, full message text.
   - Conversation URL -> `profile_url`.
   - If they sent a voice note or video, note it in `role_fit_notes` and judge
     `english_level` from it if listenable.
   - If screener questions were already asked in the thread, copy the answers
     verbatim.
3. `channel` = `Facebook DM`.

## Facebook quirks

- Comment sections default to "Most relevant", which HIDES comments. Always
  switch to "All comments" or the count will silently be wrong.
- Names are display names; two "Maria Garcia"s are different people with
  different profile URLs. The profile URL, not the name, is identity.
- Someone who commented AND DMed is one candidate: keep the DM as the primary
  record (richer), note the comment in `role_fit_notes`.
- Do not react, like, or reply while reading. Read-only until outreach is
  approved.

## Outreach on this channel

Post commenters get a Messenger DM (not a public comment reply, to keep it
private). DM candidates get a reply in the existing thread. See
`references/outreach.md`; the verify-before-send rule matters most here since
Messenger threads look alike.
