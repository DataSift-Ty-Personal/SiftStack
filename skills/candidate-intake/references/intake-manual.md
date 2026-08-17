# Intake: Manual (pasted email, message, or resume text)

The user pastes one or more applications directly into chat: a forwarded email,
a text message screenshot, a resume, or "here are three people who applied".
This channel has no browser steps; it is careful parsing.

## Steps

1. **Split** the paste into individual candidates. Boundaries are usually
   obvious (separate emails, "---", numbered list, distinct names). If a split
   is ambiguous, ask rather than merging two people into one record.
2. **Extract** into the Candidate Record:
   - `full_name` and `first_name` from the signature, header, or the user's
     framing ("this one is from Jake").
   - `email` / `phone` only if literally present in the text.
   - `date_received`: the date on the pasted email if visible, otherwise
     today's date.
   - `raw_text`: the entire pasted text for that person, verbatim. Never trim
     it; it is the audit trail.
   - `years_experience`, `location`, `role_fit_notes`: only from explicit
     statements in the text.
   - `screener_answers`: only if the paste contains answers to the configured
     screener questions.
3. **Ask one gap question max.** If a high-value field (email/phone) is missing
   and the user likely has it, ask once for the batch ("Do you have contact
   info for Jake and Maria?"). Do not interrogate field by field.
4. `channel` = `Manual`. If the user says where it originally came from
   ("this was an Indeed email"), use that channel name instead so the sheet
   reflects reality and outreach goes to the right place.

## Screenshots

If the paste is an image (screenshot of a text or DM), read the visible text
exactly. Note `from screenshot` in `role_fit_notes`. If part of the message is
cut off, record only what is visible and note the truncation.

## Hard rule reminder

Manual intake is where fabrication risk peaks, because the text is messy and
gap-filling is tempting. A blank field is correct. An inferred phone number or
a guessed email domain is a hard-rule violation.

## Outreach on this channel

Manual candidates often have no reachable channel in-system. If an email or
phone exists, outreach goes there (Gmail compose, or the user texts them). If
not, tell the user outreach for this candidate is on them, and mark
`outreach_status` accordingly when they confirm they sent it.
