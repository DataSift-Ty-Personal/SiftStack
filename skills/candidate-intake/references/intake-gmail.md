# Intake: Gmail

Pull emailed applications out of Gmail through Chrome.

## Steps

1. Open `mail.google.com` in Chrome. Confirm the account matches the one the
   user hires with (the account chip, top right).
2. Search with `config.gmail_search_query` (default
   `subject:(application OR applying OR resume) newer_than:7d`). If the user
   uses a label like `Hiring`, prefer `label:hiring newer_than:7d`.
3. Also ask the user if applicants reply to a specific job post email or
   address; add `to:` or `deliveredto:` terms if so.
4. Open each matching thread and capture:
   - Sender name and email address (from the expanded header, not the display
     name alone; click the small arrow to see the real address).
   - Date of the first application email -> `date_received`.
   - Body text -> `raw_text`. Pull experience claims into `role_fit_notes` and
     any stated years into `years_experience`.
   - **Attachments**: if a resume PDF/doc is attached, open the Gmail preview
     and read it in-page. Set `resume_url` to the thread's permalink URL.
   - Phone number if the signature or body has one.
   - If the thread already contains answers to the screener questions (the user
     may have emailed them earlier), copy them verbatim into
     `screener_answers`.
5. Judge `english_level` from their writing only if the role is
   writing-relevant; otherwise leave blank unless they state fluency.
6. Build one Candidate Record per person, `channel` = `Gmail`. Multiple emails
   from the same address are one candidate; use the earliest date.

## Gmail quirks

- The search only returns what matches; run a second broader pass
  (`has:attachment filename:pdf newer_than:7d`) if the user says someone is
  missing.
- Forwarded applications (a partner forwards a resume) carry the FORWARDER'S
  address in the header. Use the original applicant's email from the forwarded
  body; if it is not there, leave email blank and note it.
- Do not archive, label, or mark threads; read-only until outreach is
  approved.

## Outreach on this channel

Approved outreach is a Reply in the same thread (never a new compose, so
context is preserved), following `references/outreach.md`.
