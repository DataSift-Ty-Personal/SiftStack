# Intake: Indeed

Pull applicants from the Indeed employer dashboard through Chrome. Indeed is
the richest channel: it usually has a resume, screener answers, and location.

## Steps

1. Go to `employers.indeed.com` in Chrome. If it lands on a login page, ask the
   user to log in, then continue.
2. Open **Candidates** for the job posting that matches `config.role`. If more
   than one posting is live, confirm which one with the user.
3. Filter to the candidates you have not ingested: sort by "Apply date" and
   work newest first. If the sheet's most recent Indeed candidate is known,
   stop when you reach applicants older than that date.
4. For each candidate, open their detail view and capture:
   - Name, location, apply date
   - **Resume**: open the resume tab and read it in-page. Extract relevant
     experience for `role_fit_notes` and `years_experience`.
   - **Screener answers**: Indeed shows employer questions and the candidate's
     answers on the profile. Copy them verbatim into `screener_answers`.
   - **Contact info**: email and phone are often masked until a status change.
     Take what is visible. Never guess a masked value; leave it blank and note
     `contact masked by Indeed` in `role_fit_notes`.
   - **Profile URL**: copy the candidate detail page URL into `profile_url`.
     This is also the dedupe key when contact info is masked.
5. Build one Candidate Record per applicant, `channel` = `Indeed`.

## Indeed quirks

- The candidate list is virtualized: it loads more rows as you scroll. Scroll
  until the list stops growing before counting "new" applicants.
- Do NOT change a candidate's status (Reviewed / Interested / Rejected) while
  reading; status changes can notify the candidate. Read-only until the user
  approves outreach.
- Resumes are sometimes images or PDFs rendered in a viewer. Read what renders;
  if unreadable, set `resume_url` to the resume tab URL and note it.
- Indeed masks emails as relay addresses (`...@indeedemail.com`). A relay
  address is still valid for dedupe and for email outreach; record it as-is.

## Outreach on this channel

Approved outreach to Indeed candidates goes through Indeed's own message
composer on the candidate page (see `references/outreach.md`), which also moves
them to "Contacted" on Indeed's side.
