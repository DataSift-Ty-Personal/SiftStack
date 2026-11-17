# Master Sheet Schema

One Google Sheet, one tab ("Candidates"), one row per unique person. Row 1 is
the header, frozen. Column order is fixed because `scripts/prepare_rows.py`
emits paste blocks in exactly this order.

## Columns (in order)

| # | Header | Source |
|---|--------|--------|
| 1 | Date Received | `date_received` (YYYY-MM-DD) |
| 2 | Full Name | `full_name` |
| 3 | First Name | `first_name` |
| 4 | Channel | `channel` |
| 5 | Email | `email` |
| 6 | Phone | `phone` |
| 7 | Location | `location` |
| 8 | Profile URL | `profile_url` |
| 9 | Resume URL | `resume_url` |
| 10 | Years Experience | `years_experience` |
| 11 | English Level | `english_level` |
| 12 | Score | `score` |
| 13 | Tier | `tier` |
| 14 | Score Reason | `score_reason` |
| 15 | Role Fit Notes | `role_fit_notes` |
| 16 | Screener Answers | `screener_answers` |
| 17 | Outreach Status | `outreach_status` |
| 18 | Notes | free text, never overwritten by the skill |

Header row to paste into A1 (tab-separated):

```
Date Received	Full Name	First Name	Channel	Email	Phone	Location	Profile URL	Resume URL	Years Experience	English Level	Score	Tier	Score Reason	Role Fit Notes	Screener Answers	Outreach Status	Notes
```

## Reading the sheet (before appending)

1. Open the sheet in Chrome.
2. File > Download > Comma-separated values, or select all data and copy it
   out. Save as `current_sheet.csv` in the working folder.
3. Pass that CSV to `prepare_rows.py`; it uses Email, Phone, and Profile URL
   columns to detect duplicates.

## Writing new rows

1. Run `prepare_rows.py`; it prints a tab-separated block of only-new rows.
2. In Chrome, click the first empty cell in column A.
3. Paste the block. Tabs split into columns, newlines split into rows.
4. Verify: the row count increased by the "added" number the script reported,
   and the last pasted row's name matches the script's last candidate.

## Duplicate handling

`prepare_rows.py` skips a candidate when their normalized email, phone (last
10 digits), or profile handle already exists in the sheet. When a duplicate
arrives on a NEW channel (applied on Indeed, then DMed on Facebook), do not add
a row; instead append a note to that person's Notes cell like
`Also applied via Facebook DM 2026-07-21`, since repeat interest is a real
signal.

## Manual columns

Outreach Status and Notes belong to the user once written. Update Outreach
Status only when the skill itself sends outreach (set it to `Contacted`).
Never overwrite Notes.
