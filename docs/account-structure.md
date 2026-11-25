# How the account is set up, step by step

This is the DataSift account SiftStack runs on, exported 2026-11-23: 21 folders and 103 presets, 9 boards, 28 sequences and 13 task presets. It is generated from the blueprint `skills/account-blueprint/blueprints/account_2026-11-23.json` and the autopilot playbook `team/autopilot.yaml`, so it matches the account as it was exported. Do not edit it by hand; see the last section to regenerate it.

Every record sits in one flow, and each flow has its own text, call and mail folders. The folders are numbered in the order the team works them, and DataSift sorts folders by name, so the number is the order.

## At a glance

| Folder | Presets | What it is for | Worked on its own by |
| --- | --- | --- | --- |
| 01. FORECLOSURE - TEXT | 2 | First-to-market foreclosures (tag FTM, Foreclosure list) waiting on their opening texts. The autopilot loads texts 1 to 3 into Smarter Contact on days 0 to 2, so a record leaves Ready to Text once its first text goes out. | ftm_death_screen (Diggs), ftm_skip_screen (Diggs, plan), ftm_attempts (Quinn), ftm_assign (Atlas), ftm_score (Diggs), ftm_texts (Quinn) and 2 more |
| 02. FORECLOSURE - CALL | 8 | The foreclosure call round, step by step on the dialer's own attempt counter: needs a skip trace, traced with no numbers, ready to call, call attempts 1 to 4, and the hot auction list of sales inside 45 days. | ftm_pipeline (Quinn), ftm_death_screen (Diggs), ftm_skip_screen (Diggs, plan), ftm_assign (Atlas), ftm_native_trace (Diggs), ftm_retrace (Diggs) and 5 more |
| 03. FORECLOSURE - MAIL | 12 | Two mail runs for foreclosures: the weekly letters that count down to the sale date (FTM - Weekly 01 to 06) and the monthly evergreen letters (FTM - Mail 01 to 06). | ftm_mail (Quinn, plan) |
| 04. ESTATES - TEXT | 2 | Probates waiting on their opening texts, written to the personal representative, never about the death or the court. | ftm_death_screen (Diggs), ftm_skip_screen (Diggs, plan), ftm_attempts (Quinn), ftm_assign (Atlas), ftm_score (Diggs), ftm_texts (Quinn) and 3 more |
| 05. ESTATES - CALL | 7 | One call round for the estates: probates, and obituary records once they are 30 days past the death (the tag Obituary 30+ Days). | ftm_pipeline (Quinn), ftm_assign (Atlas), ftm_native_trace (Diggs), ftm_retrace (Diggs), ftm_refresh (Diggs), ftm_death_screen (Diggs) and 6 more |
| 06. ESTATES - MAIL | 8 | Letters to the personal representative, counted from the probate filing (at once, two follow-ups, then monthly to month 24), and the obituary records ready for a family letter. | people, in the app |
| 07. CURATIVE TITLE | 2 | Heir houses worked on the Curative Title board: every living heir called over five days, the heir report on the record, no texts. Curative - All holds every tagged house; Ready to Call is the dial list. | curative_pipeline (Roma), heir_lane (Quinn) |
| 08. TOP 1000 - TEXT | 2 | The month's top 1,000 owners by deal odds, plus county records always put in (liens, tax sale and tax delinquent notices, condemned houses, evictions). Opening texts come first. | top1000_death_screen (Diggs), top1000_attempts (Quinn), top1000_assign (Atlas), top1000_score (Diggs), top1000_texts (Quinn), top1000_trace_refresh (Diggs) |
| 09. TOP 1000 - CALL | 7 | The Top 1,000 call round, step by step on the dialer's own attempt counter. | top1000_death_screen (Diggs), top1000_assign (Atlas), top1000_native_trace (Diggs), top1000_retrace (Diggs), top1000_trace_refresh (Diggs), top1000_attempts (Quinn) and 2 more |
| 10. TOP 1000 - MAIL | 6 | Monthly mail steps for the Top 1,000. Mail runs on the first-to-market records only, so these wait. | people, in the app |
| 11. AGENCY | 3 | Everyone else in the buy box. Each month the top 5,000 owners not sent in 90 days go to the calling agency from Agency - 01 Queue; Sent and Came Back track the file. | top1000_refresh (Atlas), top1000_feeds (Atlas), agency_export (Quinn), cold_email_topup (Quinn) |
| 12. DEEP PROSPECTING (ALL TIERS) | 7 | Records that need research or heir work before anyone can reach the owner, from every flow, plus Obituary - All (every obituary record, any age). | paid_lookups (Quinn, plan), ftm_exhausted (Diggs), ftm_pipeline (Quinn), obituary_lane (Diggs) |
| 13. REACTIVATION | 7 | Second chances: a Not Interested record comes back after 30 days (foreclosures, probates, the Top 1,000) or 90 days (the rest), and records that never answered return in the rehash dial file. | ftm_reactivate (Atlas), rehash_dial_file (Quinn) |
| 14. CLEAN UP | 2 | Upkeep views: traced records with no number, and sold houses kept out of marketing. | people, in the app |
| 15. Lead Management | 6 | The lead manager's task views, one per stage of the Lead Management board, plus tax sale owners who can still redeem. | lead_weekly (Quinn), research_queue (Diggs), tax_sale_results (Atlas) |
| 16. Acquisitions | 2 | Offer and follow-up task views for the acquisitions board. | people, in the app |
| 17. Transactions | 2 | Contract and closing task views for the transactions board. | people, in the app |
| 18. My Tasks | 3 | Each person's own overdue, due today and future tasks. | people, in the app |
| 19. QC | 4 | Quality checks on the boards: cards with no next task, overdue tasks, more than one open task, no confirmed number. | lead_tasks (Quinn) |
| 20. CALLER QUEUES | 6 | Each caller's own working views: what is assigned, what is being prospected, what has not been started. | people, in the app |
| 21. Dispo Sequential Marketing | 5 | Buyer outreach for a deal under contract: buyers ready to text, texted, ready to call, and call attempts. | people, in the app |

## Build it in your own account

The fastest way is the `account-blueprint` skill. On your DataSift Open API key it creates the lists, tags, custom fields, task presets, preset folders, presets and SiftMap presets from the blueprint and reads every object back. DataSift's published API has no route for custom statuses, boards, board columns or sequences, so its report lists those as a click-through checklist, in build order:

```bash
cd skills/account-blueprint/scripts
export REISIFT_API_KEY=...   # your account's Open API key; an account admin creates it in DataSift
python clone_account.py --phase plan  --blueprint ../blueprints/account_2026-11-23.json --target you@example.com
python clone_account.py --phase apply --blueprint ../blueprints/account_2026-11-23.json --target you@example.com --commit
python clone_account.py --phase verify --blueprint ../blueprints/account_2026-11-23.json --target you@example.com
```

`--target` is the email of the user the key belongs to. After the checklist is built in the app, run apply again: it creates whatever was waiting on those boards, columns and statuses.

By hand, work through the steps below in order: every later step names things the earlier ones create.

## Step 1: statuses, lists and tags

**Statuses:** DataSift's built-in statuses, plus 4 custom ones to add: Deep Prospecting, Auction Date Passed, Exhausted, Return Mail.

**Lists** the presets, sequences and SiftMap presets use (9): Agency, DNC, Dispo - Deal Blast, Dispo - Flip Buyers, Obituary, Negative Equity, Probate, Foreclosure, Low Equity. The source account holds 60 more for one-off pulls; they are in the blueprint but nothing here depends on them.

**Tags** the presets and sequences use (32): Auction Soon, Bulk Stacked, Buy Box - Blount, Buy Box - Knox, Buyers, Courthouse Data, Curative, Dispo Buyer, Dispo Traced, Do Not Mail, Do Not Market, FTM, Foreclosure, Heir Identified, Litigator, Mail Only, Not Single Family, Obituary, Obituary 30+ Days, Priority 1, Priority 2, Probate, Return Mail, Sold, Tax Delinquent, Tax Sale Redemption, Tier 2, Top 1000, Vacant, leadflowrei, probate, recently sold.

## Step 2: custom fields

- **Qualifying questions:** Timeline (text), Motivation (text), Condition (text), Asking Price (text), Occupancy (text), Mortgage Balance (text), Months Behind (text), HOA (text), Code Liens (text)
- **TN Public Notice:** Date Added (date), Notice Type (select), County (select), Owner Deceased (select), DM Confidence (select), Date of Death (text), Decedent Name (text), Decision Maker (text), DM Relationship (text), DM 2 Name (text), DM 2 Relationship (text), DM 3 Name (text), DM 3 Relationship (text), Obituary URL (url), Source URL (url), Outstanding Lien Amount (money), Total Delinquency (money), Est Equity (money), Debt vs Equity Pct (number), Upside Down (select), Liens Active vs Total (text), Lien Type (select), Upside Down Reason (text), Tax Auction Date (date)
- **Misc.:** Foreclosure Date (date), Personal Representative (text_input), Personal Representative Phone (phone), Obituary ID (text_input), Death Date (date), visual_distress_score (number), Tax delinquency amount (money), Number of years tax-linked (number), Notice Screenshot (text_input), Text Touch 1 (text), Text Touch 2 (text), Text Touch 3 (text), Text Touch 4 (text), Sent to Agency (date)
- **Deal Source:** Deal Lane (text), Deal List (text), Deal Channel (text), Days From First Touch (text), Lead Generated By (text), Lead Carried By (text), Deal Closed By (text)

## Step 3: the folders, in order

Create them with exactly these names: the number at the front is what puts them in this order.

- 01. FORECLOSURE - TEXT
- 02. FORECLOSURE - CALL
- 03. FORECLOSURE - MAIL
- 04. ESTATES - TEXT
- 05. ESTATES - CALL
- 06. ESTATES - MAIL
- 07. CURATIVE TITLE
- 08. TOP 1000 - TEXT
- 09. TOP 1000 - CALL
- 10. TOP 1000 - MAIL
- 11. AGENCY
- 12. DEEP PROSPECTING (ALL TIERS)
- 13. REACTIVATION
- 14. CLEAN UP
- 15. Lead Management
- 16. Acquisitions
- 17. Transactions
- 18. My Tasks
- 19. QC
- 20. CALLER QUEUES
- 21. Dispo Sequential Marketing

## Step 4: the presets, folder by folder

Each preset lists its filter in plain words. A counter such as texts sent or dialer call attempts is DataSift's own counter, so a record moves from one preset to the next as the counter climbs. A date window such as "from 36 months before a week ago up to a week ago" is DataSift's relative date: it moves with the calendar. Long exclusion lists that many presets share are named once in Step 5.

### 01. FORECLOSURE - TEXT

First-to-market foreclosures (tag FTM, Foreclosure list) waiting on their opening texts. The autopilot loads texts 1 to 3 into Smarter Contact on days 0 to 2, so a record leaves Ready to Text once its first text goes out.

**FTM - 01 Ready to Text**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- texts sent: 0
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 6

**FTM - 02 Texted, Not Yet Called**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- texts sent: 1 or more
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 11

### 02. FORECLOSURE - CALL

The foreclosure call round, step by step on the dialer's own attempt counter: needs a skip trace, traced with no numbers, ready to call, call attempts 1 to 4, and the hot auction list of sales inside 45 days.

**FTM - 00 Needs Skipped**

- has no phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has not been skip traced
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_assign, ftm_native_trace
- records in the source account at export: 0

**FTM - 01 Skipped No Numbers**

- has no phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_assign, ftm_retrace, ftm_refresh
- records in the source account at export: 0

**FTM - 02 Ready to Call**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 17

**FTM - 03 Call Attempt 1**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 1
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 0

**FTM - 04 Call Attempt 2**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 2
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 0

**FTM - 05 Call Attempt 3**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 3
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 0

**FTM - 06 Call Attempt 4**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 4
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 0

**FTM - Hot Auction (sale in 45 days)**

- has every one of these tags: FTM, Auction Soon
- is on every one of these lists: Foreclosure
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary, Probate
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_death_screen, ftm_skip_screen (plan), ftm_backlog_texts
- records in the source account at export: 13

### 03. FORECLOSURE - MAIL

Two mail runs for foreclosures: the weekly letters that count down to the sale date (FTM - Weekly 01 to 06) and the monthly evergreen letters (FTM - Mail 01 to 06).

**FTM - Mail 01 Handwritten Letter**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- mail pieces sent: 0
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- worked by: ftm_mail (plan)
- records in the source account at export: 17

**FTM - Mail 02 Family Postcard**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 1
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- worked by: ftm_mail (plan)
- records in the source account at export: 0 (approximate)

**FTM - Mail 03 Soft-Offer Check**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 2
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- worked by: ftm_mail (plan)
- records in the source account at export: 0 (approximate)

**FTM - Mail 04 Handwritten Letter**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 3
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- worked by: ftm_mail (plan)
- records in the source account at export: 0 (approximate)

**FTM - Mail 05 Family Postcard**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 4
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- worked by: ftm_mail (plan)
- records in the source account at export: 0 (approximate)

**FTM - Mail 06 Soft-Offer Check**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 5
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- worked by: ftm_mail (plan)
- records in the source account at export: 0 (approximate)

**FTM - Weekly 01 Handwritten Letter**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- mail pieces sent: 0
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 17

**FTM - Weekly 02 Options Letter**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 1
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**FTM - Weekly 03 Check Letter**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 2
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**FTM - Weekly 04 Street View Postcard**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 3
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**FTM - Weekly 05 Closing Letter**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 4
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**FTM - Weekly 06 Last Note**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 5
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC, Probate, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

### 04. ESTATES - TEXT

Probates waiting on their opening texts, written to the personal representative, never about the death or the court.

**Probate - 01 Ready to Text**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Probate
- has been skip traced
- texts sent: 0
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh, research_queue
- records in the source account at export: 154

**Probate - 02 Texted, Not Yet Called**

- has a phone number
- has any of these tags: FTM
- is on every one of these lists: Probate
- has been skip traced
- texts sent: 1 or more
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh, research_queue
- records in the source account at export: 129

### 05. ESTATES - CALL

One call round for the estates: probates, and obituary records once they are 30 days past the death (the tag Obituary 30+ Days).

**Estate - 00 Needs Skipped**

- has no phone number
- has any of these tags: probate, Obituary 30+ Days
- has not been skip traced
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_assign, ftm_native_trace
- records in the source account at export: 1

**Estate - 01 Skipped No Numbers**

- has no phone number
- has any of these tags: probate, Obituary 30+ Days
- has been skip traced
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_assign, ftm_retrace, ftm_refresh
- records in the source account at export: 30

**Estate - 02 Ready to Call**

- has a phone number
- has any of these tags: probate, Obituary 30+ Days
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh, obituary_lane
- records in the source account at export: 1059

**Estate - 03 Call Attempt 1**

- has a phone number
- has any of these tags: probate, Obituary 30+ Days
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 1
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh, obituary_lane
- records in the source account at export: 99

**Estate - 04 Call Attempt 2**

- has a phone number
- has any of these tags: probate, Obituary 30+ Days
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 2
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_texts, ftm_backlog_texts, ftm_refresh, obituary_lane
- records in the source account at export: 8

**Estate - 05 Call Attempt 3**

- has a phone number
- has any of these tags: probate, Obituary 30+ Days
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 3
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_backlog_texts, ftm_refresh, obituary_lane
- records in the source account at export: 12

**Estate - 06 Call Attempt 4**

- has a phone number
- has any of these tags: probate, Obituary 30+ Days
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 4
- does not have the tags: Exclusion set 8
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: ftm_pipeline, ftm_death_screen, ftm_skip_screen (plan), ftm_attempts, ftm_assign, ftm_score, ftm_backlog_texts, ftm_refresh
- records in the source account at export: 12

### 06. ESTATES - MAIL

Letters to the personal representative, counted from the probate filing (at once, two follow-ups, then monthly to month 24), and the obituary records ready for a family letter.

**10 Obituary - Ready to Mail**

- is on every one of these lists: Obituary
- not marked do not mail
- mail pieces sent: 0
- shows one property per owner
- does not have the tags: recently sold, Mail Only, Not Single Family, Do Not Market, Litigator, Heir Identified, Return Mail, Do Not Mail
- is not on the lists: Low Equity, Negative Equity, DNC, Probate
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 907

**Probate - Mail 01 Letter**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- mail pieces sent: 0
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 341

**Probate - Mail 02 Handwritten Note**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 1
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Probate - Mail 03 Options Letter**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a week ago up to a week ago
- mail pieces sent: 2
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Probate - Mail 04 Street View Postcard**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 3
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Probate - Mail 05 Letter**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 4
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Probate - Mail 06 Handwritten Note**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 5
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Probate - Mail 07+ Monthly**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 6 or more
- does not have the tags: Exclusion set 7
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

### 07. CURATIVE TITLE

Heir houses worked on the Curative Title board: every living heir called over five days, the heir report on the record, no texts. Curative - All holds every tagged house; Ready to Call is the dial list.

**Curative - All**

- has any of these tags: Curative
- records in the source account at export: 113

**Curative - Ready to Call**

- has a phone number
- has any of these tags: Curative
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: recently sold, Mail Only, Do Not Market, Litigator, Not Single Family
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: sold, closed, close_out, lost_deal, listed, under_contract, not_interested, dnc, opt_out, Dead Lead, buyer, buyer_found, buyer_lost, push_deal, refer_lead, transactions, underwrite, lead, new_lead, Cold Lead, Warm Lead, Hot Lead, Ghosting Lead, Exhausted, Return Mail, No Contact New Lead, follow_up, Auction Date Passed
- records in the source account at export: 82

### 08. TOP 1000 - TEXT

The month's top 1,000 owners by deal odds, plus county records always put in (liens, tax sale and tax delinquent notices, condemned houses, evictions). Opening texts come first.

**Priority - 01 Ready to Text**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- texts sent: 0
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_attempts, top1000_assign, top1000_score, top1000_texts, top1000_trace_refresh
- records in the source account at export: 1306

**Priority - 02 Texted, Not Yet Called**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- texts sent: 1 or more
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_attempts, top1000_assign, top1000_score, top1000_texts, top1000_trace_refresh
- records in the source account at export: 59

### 09. TOP 1000 - CALL

The Top 1,000 call round, step by step on the dialer's own attempt counter.

**Priority - 00 Needs Skipped**

- has no phone number
- has any of these tags: Top 1000
- has not been skip traced
- dialer call attempts: 0
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_assign, top1000_native_trace
- records in the source account at export: 0

**Priority - 01 Skipped No Numbers**

- has no phone number
- has any of these tags: Top 1000
- has been skip traced
- dialer call attempts: 0
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_assign, top1000_retrace, top1000_trace_refresh
- records in the source account at export: 3

**Priority - 02 Ready to Call**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 0
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_attempts, top1000_assign, top1000_score, top1000_texts, top1000_trace_refresh
- records in the source account at export: 1308

**Priority - 03 Call Attempt 1**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 1
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_attempts, top1000_assign, top1000_score, top1000_texts, top1000_trace_refresh
- records in the source account at export: 47

**Priority - 04 Call Attempt 2**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 2
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_death_screen, top1000_attempts, top1000_assign, top1000_score, top1000_texts, top1000_trace_refresh
- records in the source account at export: 10

**Priority - 05 Call Attempt 3**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 3
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_attempts, top1000_assign, top1000_score, top1000_trace_refresh
- records in the source account at export: 1

**Priority - 06 Call Attempt 4**

- has a phone number
- has any of these tags: Top 1000
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- dialer call attempts: 4
- does not have the tags: Exclusion set 9
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_attempts, top1000_assign, top1000_score, top1000_trace_refresh
- records in the source account at export: 5

### 10. TOP 1000 - MAIL

Monthly mail steps for the Top 1,000. Mail runs on the first-to-market records only, so these wait.

**Priority - Mail 01 Handwritten Letter**

- has any of these tags: Top 1000
- the owner's mailing address is not flagged vacant
- not marked do not mail
- mail pieces sent: 0
- does not have the tags: Exclusion set 10
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 1677

**Priority - Mail 02 Family Postcard**

- has any of these tags: Top 1000
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 1
- does not have the tags: Exclusion set 10
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Priority - Mail 03 Soft-Offer Check**

- has any of these tags: Top 1000
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 2
- does not have the tags: Exclusion set 10
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Priority - Mail 04 Handwritten Letter**

- has any of these tags: Top 1000
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 3
- does not have the tags: Exclusion set 10
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Priority - Mail 05 Family Postcard**

- has any of these tags: Top 1000
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 4
- does not have the tags: Exclusion set 10
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

**Priority - Mail 06 Soft-Offer Check**

- has any of these tags: Top 1000
- the owner's mailing address is not flagged vacant
- not marked do not mail
- last mailed from 36 months before a month ago up to a month ago
- mail pieces sent: 5
- does not have the tags: Exclusion set 10
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 2
- records in the source account at export: 0 (approximate)

### 11. AGENCY

Everyone else in the buy box. Each month the top 5,000 owners not sent in 90 days go to the calling agency from Agency - 01 Queue; Sent and Came Back track the file.

**Agency - 01 Queue**

- has a phone number
- has been skip traced
- has a number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- shows one property per owner
- does not have the tags: Mail Only, recently sold, Not Single Family, Do Not Market, Litigator, Heir Identified, FTM, Top 1000
- is not on the lists: Low Equity, Negative Equity, DNC, Obituary, Agency
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 1
- worked by: top1000_refresh, top1000_feeds, agency_export, cold_email_topup
- records in the source account at export: 18808

**Agency - 02 Sent**

- is on any of these lists: Agency
- records in the source account at export: 0

**Agency - 03 Came Back**

- is on any of these lists: Agency
- status is one of: new_lead, No Contact New Lead, Cold Lead, Warm Lead, Hot Lead, Ghosting Lead, follow_up, lead, underwrite, under_contract, push_deal, transactions, refer_lead
- records in the source account at export: 0

### 12. DEEP PROSPECTING (ALL TIERS)

Records that need research or heir work before anyone can reach the owner, from every flow, plus Obituary - All (every obituary record, any age).

**01 No / Bad Phone**

- has no phone number
- has been skip traced
- shows one property per owner
- does not have the tags: recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 3
- worked by: paid_lookups (plan)
- records in the source account at export: 1242

**02 Obituary / Deceased**

- is on every one of these lists: Obituary
- obituary date from 24 months before a quarter ago up to a quarter ago
- shows one property per owner
- does not have the tags: recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC, Probate
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 3

**03b No Live Number -> DP**

- has a phone number
- has been skip traced
- shows one property per owner
- dialer call attempts: 0 to 4
- does not have the tags: Mail Only, recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- has no number marked: CORRECT, CORRECT_DNC, NO_ANSWER, UNKNOWN
- status is not: Exclusion set 6
- worked by: ftm_exhausted, paid_lookups (plan)
- records in the source account at export: 29

**03 Exhausted Call -> DP**

- has a phone number
- has been skip traced
- shows one property per owner
- dialer call attempts: 5 or more
- does not have the tags: Mail Only, recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 6
- worked by: ftm_exhausted
- records in the source account at export: 3

**04 Return Mail -> DP**

- shows one property per owner
- mail campaign situation is one of: returned, undeliverable
- does not have the tags: recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 3
- records in the source account at export: 0

**05 Vacant -> DP**

- the owner's mailing address is flagged vacant
- shows one property per owner
- does not have the tags: recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 3
- records in the source account at export: 516

**Obituary - All**

- is on any of these lists: Obituary
- does not have the tags: recently sold, Not Single Family, Do Not Market, Litigator
- is not on the lists: Low Equity, Negative Equity, DNC, Probate
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- status is not: Exclusion set 3
- worked by: ftm_pipeline, obituary_lane
- records in the source account at export: 997

### 13. REACTIVATION

Second chances: a Not Interested record comes back after 30 days (foreclosures, probates, the Top 1,000) or 90 days (the rest), and records that never answered return in the rehash dial file.

**Not Interested - FTM Foreclosure (30d)**

- has any of these tags: FTM
- is on every one of these lists: Foreclosure
- the status last changed from 36 months before a month ago up to a month ago
- status is one of: not_interested
- does not have the tags: recently sold
- is not on the lists: Low Equity, Negative Equity
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- worked by: ftm_reactivate
- records in the source account at export: 20 (approximate)

**Not Interested - FTM Probate (30d)**

- has any of these tags: FTM
- is on every one of these lists: Probate
- the status last changed from 36 months before a month ago up to a month ago
- status is one of: not_interested
- does not have the tags: recently sold
- is not on the lists: Low Equity, Negative Equity
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- worked by: ftm_reactivate
- records in the source account at export: 62 (approximate)

**Not Interested - FTM rest (90d)**

- has any of these tags: FTM
- the status last changed from 36 months before a quarter ago up to a quarter ago
- status is one of: not_interested
- does not have the tags: recently sold
- is not on the lists: Low Equity, Negative Equity, Foreclosure, Probate
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- worked by: ftm_reactivate
- records in the source account at export: 0 (approximate)

**Not Interested - Priority 1 (30d)**

- has any of these tags: Priority 1
- the status last changed from 36 months before a month ago up to a month ago
- status is one of: not_interested
- does not have the tags: recently sold
- is not on the lists: Low Equity, Negative Equity
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- records in the source account at export: 654 (approximate)

**Not Interested - Priority 2 (30d)**

- has any of these tags: Priority 2
- the status last changed from 36 months before a month ago up to a month ago
- status is one of: not_interested
- does not have the tags: recently sold, Priority 1
- is not on the lists: Low Equity, Negative Equity
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- records in the source account at export: 130 (approximate)

**Not Interested - Priority 3 (90d)**

- the status last changed from 36 months before a quarter ago up to a quarter ago
- status is one of: not_interested
- does not have the tags: FTM, recently sold, Priority 2, Priority 1
- is not on the lists: Low Equity, Negative Equity
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- records in the source account at export: 238 (approximate)

**Rehash - Ready (never answered)**

- has a phone number
- has been skip traced
- dialer call attempts: 3 or more
- does not have the tags: recently sold, Mail Only
- is not on the lists: Low Equity, Negative Equity
- leaves out 15 neighborhoods of the source market (left out when the blueprint is applied)
- has no number marked: CORRECT, CORRECT_DNC
- status is not: sold, not_interested
- worked by: rehash_dial_file
- records in the source account at export: 688

### 14. CLEAN UP

Upkeep views: traced records with no number, and sold houses kept out of marketing.

**No Phone (skip-traced, no number)**

- has no phone number
- has been skip traced
- records in the source account at export: 4448

**Recently Sold (suppress)**

- has any of these tags: recently sold
- records in the source account at export: 29417

### 15. Lead Management

The lead manager's task views, one per stage of the Lead Management board, plus tax sale owners who can still redeem.

**0. Call New Lead Tasks**

- tasks: [{'label': 'task', 'title': 'Call New Lead', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

**1. Unqualified Lead Tasks**

- tasks: [{'label': 'task', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- status is one of: No Contact New Lead
- records in the source account at export: 0

**2. Qualified Lead Tasks**

- tasks: [{'label': 'task', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- status is one of: Cold Lead, Warm Lead, Hot Lead
- records in the source account at export: 0

**3. Leads Missing Tasks**

- open tasks: 0
- status is one of: new_lead, No Contact New Lead, Cold Lead, Warm Lead, Hot Lead
- worked by: lead_weekly
- records in the source account at export: 9

**4. Send Back to LM Tasks**

- tasks: [{'label': 'task', 'title': 'Send Back to LM', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

**Tax Sale Redemption**

- has any of these tags: Tax Sale Redemption
- worked by: research_queue, tax_sale_results
- records in the source account at export: 11

### 16. Acquisitions

Offer and follow-up task views for the acquisitions board.

**0. Make Offer Tasks**

- tasks: [{'label': 'task', 'title': 'Make Offer', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

**1. Offer Follow-Ups**

- tasks: [{'label': 'task', 'title': 'Offer Follow-Up', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

### 17. Transactions

Contract and closing task views for the transactions board.

**0. Process Contract**

- tasks: [{'label': 'task', 'title': 'Process Contract', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

**1. Title and Seller Follow-Ups**

- tasks: [{'label': 'task', 'title': 'Follow-Up with Seller and Title', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

### 18. My Tasks

Each person's own overdue, due today and future tasks.

**0. Overdue Tasks**

- tasks: [{'label': 'task', 'status': {'status': 'overdue'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

**1. Tasks Due Today**

- tasks: [{'label': 'task', 'status': {'status': 'due_today'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

**2. Future Tasks**

- tasks: [{'label': 'task', 'status': {'status': 'to_do'}, 'is_completed': False, 'any_assigned_to_role': ['me']}]
- records in the source account at export: 0

### 19. QC

Quality checks on the boards: cards with no next task, overdue tasks, more than one open task, no confirmed number.

**Board - 2+ Open Tasks**

- has a card on: Lead Management, Acquisitions, Transactions
- open tasks: 2 or more
- records in the source account at export: 3

**Board - Missing Correct Phone**

- has a card on: Lead Management, Acquisitions, Transactions
- has no number marked: CORRECT
- records in the source account at export: 15

**Board - No Tasks (no next action)**

- has a card on: Lead Management, Acquisitions, Transactions
- open tasks: 0
- worked by: lead_tasks
- records in the source account at export: 46

**Board - Overdue Tasks**

- tasks: [{'label': 'task', 'status': {'status': 'overdue'}, 'is_completed': False}]
- has a card on: Lead Management, Acquisitions, Transactions
- records in the source account at export: 63

### 20. CALLER QUEUES

Each caller's own working views: what is assigned, what is being prospected, what has not been started.

**Emerson - Actively Prospecting**

- is assigned to Emerson
- status is one of: prospecting
- does not have the tags: Do Not Market, Litigator, Heir Identified
- is not on the lists: DNC
- records in the source account at export: 1187

**Emerson - Assigned (All)**

- is assigned to Emerson
- does not have the tags: Do Not Market, Litigator, Heir Identified
- is not on the lists: DNC
- status is not: Exclusion set 4
- records in the source account at export: 3293

**Emerson - To Start**

- is assigned to Emerson
- does not have the tags: Do Not Market, Litigator, Heir Identified
- is not on the lists: DNC
- status is not: Exclusion set 5
- records in the source account at export: 2106

**Sloane - Actively Prospecting**

- is assigned to Sloane
- status is one of: prospecting
- does not have the tags: Do Not Market, Litigator, Heir Identified
- is not on the lists: DNC
- records in the source account at export: 0

**Sloane - Assigned (All)**

- is assigned to Sloane
- does not have the tags: Do Not Market, Litigator, Heir Identified
- is not on the lists: DNC
- status is not: Exclusion set 4
- records in the source account at export: 0

**Sloane - To Start**

- is assigned to Sloane
- does not have the tags: Do Not Market, Litigator, Heir Identified
- is not on the lists: DNC
- status is not: Exclusion set 5
- records in the source account at export: 0

### 21. Dispo Sequential Marketing

Buyer outreach for a deal under contract: buyers ready to text, texted, ready to call, and call attempts.

**Dispo - 01 Ready to Text**

- has a phone number
- is on any of these lists: Dispo - Deal Blast
- texts sent: 0
- status is not: not_interested
- records in the source account at export: 28

**Dispo - 02 Texted**

- has a phone number
- is on any of these lists: Dispo - Deal Blast
- texts sent: 1
- status is not: not_interested
- records in the source account at export: 27

**Dispo - 03 Ready to Call**

- has a phone number
- is on any of these lists: Dispo - Deal Blast
- dialer call attempts: 0
- status is not: not_interested
- records in the source account at export: 199

**Dispo - 04 Call Attempt 1**

- has a phone number
- is on any of these lists: Dispo - Deal Blast
- dialer call attempts: 1
- status is not: not_interested
- records in the source account at export: 0

**Dispo - 05 Call Attempt 2**

- has a phone number
- is on any of these lists: Dispo - Deal Blast
- dialer call attempts: 2
- status is not: not_interested
- records in the source account at export: 0

### The SiftStack stages that work these presets

Each runs on its own schedule in the agents brain. A stage in plan mode works out what it would do and writes nothing.

| Stage | Owner | Mode | When | What it does |
| --- | --- | --- | --- | --- |
| agency_export | Quinn | live | every 60 min 00:00-05:00 daily | The month's file for the calling agency, the top 5,000 owners not sent in 90 days in the format the agency already maps, staged for a person to send. |
| cold_email_topup | Quinn | live | 20:15 daily | Each night, when fewer than three sending days of checked leads are queued, the next cold email list is built from the agency queue (the same gate as every channel, one named email per owner, ZeroBounce with Verify+) and loaded into the campaign. |
| curative_pipeline | Roma | live | 02:50 Mon-Fri; 08:35 Mon-Fri; 17:00 Mon-Fri | Every curative title house (the owner died, no probate, every heir signs) on the Curative Title board: Research, Ready for Marketing, Day 1 to Day 5, Heir Engaged, Exhausted. At most daily_starts houses start a business day, the longest-waiting first; every living heir with a number is called each business day of the round, never texted. |
| ftm_assign | Atlas | live | every 30 min 08:00-18:00 Mon-Fri | New first-to-market records handed to a caller within 30 minutes of landing, balanced by open workload. |
| ftm_attempts | Quinn | live | every 60 min 09:00-19:00 Mon-Fri | The call counter of every first-to-market record written from the call log, and on the live board from the board's full attempts. |
| ftm_backlog_texts | Quinn | live | 08:50 Mon-Fri | Every active foreclosure and probate texted 1 to 3 from the day this stage first texts it, released on its own and loaded into Smarter Contact. |
| ftm_death_screen | Diggs | live | every 30 min 07:00-19:00 daily | New first-to-market records checked for a death before any text or assignment, and held for research when one may have happened. |
| ftm_exhausted | Diggs | live | 11:30 Mon-Fri | Called through with nothing left to dial, traced with a source not yet tried, then Diggs. |
| ftm_mail | Quinn | plan | 10:00 Mon-Fri | Foreclosures past their weekly auction run, mailed a month apart through DataSift's own mail, staged for a person to release. |
| ftm_native_trace | Diggs | live | every 30 min 07:00-19:00 daily | First-to-market records that land with no trace get DataSift's own skip trace, scoped to exactly them, estimate first. |
| ftm_pipeline | Quinn | live | 02:50 Mon-Fri; 08:35 Mon-Fri; 17:00 Mon-Fri | Every county record on the First to Market board, Research, Day 1 to Day 5, Exhausted, one column a business day and only after that day's call and text. |
| ftm_reactivate | Atlas | live | Mon 08:15 | Not-interested first-to-market records whose wait is over handed back to a caller. |
| ftm_refresh | Diggs | live | Sun 05:00 | Active first-to-market records last traced 180 or more days ago traced again by DataSift, free and outside the per-record cap. |
| ftm_retrace | Diggs | live | 11:00 Mon-Fri | First-to-market records DataSift traced with no number found, traced again with the next source nobody has tried. |
| ftm_score | Diggs | live | 08:30 Mon-Fri | Every unscored number on a first-to-market record in the call round tiered before the texts and the callers start. |
| ftm_skip_screen | Diggs | plan | 08:05 Mon-Fri | Every first-to-market record traced once through SmartSkip, so a death on file is marked before anyone calls and a probate's mail reaches the representative's own address. |
| ftm_texts | Quinn | live | 08:45 Mon-Fri | First-to-market owners texted the day they land and the next two days, released on its own and loaded into Smarter Contact. |
| heir_lane | Quinn | live | 09:15 Mon-Fri | Researched records worked one verified heir at a time by one caller, texted by name about the property, written to at the heir's own address. |
| lead_tasks | Quinn | live | 08:05 Mon-Fri | A lead with no open task gets the task its column's own sequence would have created. |
| lead_weekly | Quinn | live | Wed 08:20 | The weekly deeper pass on every lead, card against status, notes after conversations, overdue and undated tasks, and the check presets themselves. |
| obituary_lane | Diggs | live | 09:40 Mon-Fri | Obituary records held a month, one soft letter before month three, researched at three months, mailed monthly until then, matched against probate filings. |
| paid_lookups | Quinn | plan | 09:05 Mon-Fri | Records every source missed and worth the money queued for the assistant's paid people search, with a weekly task. |
| rehash_dial_file | Quinn | live | 1st 07:30 | Records that never answered, back in the power dialer every 90 days. |
| research_queue | Diggs | live | every 30 min 08:00-17:00 Mon-Fri | Research works one ranked queue inside its daily budget (foreclosures with a sale ahead, and the probates being texted); each record gets the tag Research Queued and one card. |
| tax_sale_results | Atlas | live | 07:50 Mon-Fri | The Knox tax sale results read row by row; every former owner who can still redeem tagged Tax Sale Redemption for the research queue with the deadline on the record, the winning bidders added to the buyer registry as cash buyers. |
| top1000_assign | Atlas | live | every 30 min 08:15-18:15 Mon-Fri | New Top 1,000 records handed to a caller, balanced by open workload, after first to market. |
| top1000_attempts | Quinn | live | every 60 min 09:30-19:30 Mon-Fri | The call counter of every Top 1,000 record written from the account's own call log, whichever dialer made the call. |
| top1000_death_screen | Diggs | live | every 30 min 07:15-19:15 daily | New Top 1,000 records checked for a death before any text or assignment, and held for research when one may have happened. |
| top1000_feeds | Atlas | live | 07:35 daily; 21:35 daily | Each morning the county records that arrived since yesterday (condemned, evictions, code violations, tax sale and tax delinquent notices, substitute trustee appointments, new liens) put in the Top 1,000 for its Smarter Contact texts. |
| top1000_native_trace | Diggs | live | every 30 min 07:15-19:15 daily | Top 1,000 records with no trace get DataSift's own skip trace, scoped to exactly them, estimate first. |
| top1000_refresh | Atlas | live | 1st 00:20; 1st 03:20; 2nd 00:20; 3rd 00:20 | The Top 1,000 owners re-ranked once a month on deal odds (the chance an investor buys one of their houses within six months, learned from what sold), one owner one slot; who would gain and lose the tag is listed, with the ranking as a file. |
| top1000_retrace | Diggs | live | 11:20 Mon-Fri | Top 1,000 records DataSift traced with no number found, traced again with the next source nobody has tried. |
| top1000_score | Diggs | live | 08:40 Mon-Fri | Every unscored number in the Top 1,000 text and call lane tiered before the texts and the callers start. |
| top1000_texts | Quinn | live | 08:55 Mon-Fri | Top 1,000 owners never texted before get texts 1 to 3 from their first text, 150 owners a day, released on their own into Smarter Contact. |
| top1000_trace_refresh | Diggs | live | Sun 05:20 | Active Top 1,000 records last traced 180 or more days ago traced again by DataSift, free and outside the per-record cap. |

## Step 5: the shared exclusion sets

**Exclusion set 1** (statuses left out, used by 29 presets): sold, closed, close_out, lost_deal, listed, under_contract, not_interested, dnc, opt_out, Dead Lead, buyer, buyer_found, buyer_lost, push_deal, refer_lead, transactions, underwrite, lead, new_lead, Cold Lead, Warm Lead, Hot Lead, Ghosting Lead, Deep Prospecting, Exhausted, Return Mail, No Contact New Lead, follow_up, Auction Date Passed.

**Exclusion set 2** (statuses left out, used by 26 presets): sold, closed, close_out, lost_deal, listed, under_contract, not_interested, dnc, opt_out, Dead Lead, buyer, buyer_found, buyer_lost, push_deal, refer_lead, transactions, underwrite, lead, new_lead, Cold Lead, Warm Lead, Hot Lead, Ghosting Lead, Deep Prospecting, Return Mail, No Contact New Lead, follow_up, Auction Date Passed.

**Exclusion set 3** (statuses left out, used by 5 presets): sold, closed, close_out, lost_deal, listed, under_contract, not_interested, dnc, opt_out, Dead Lead, buyer, buyer_found, buyer_lost, push_deal, refer_lead, transactions, underwrite, lead.

**Exclusion set 4** (statuses left out, used by 2 presets): sold, closed, close_out, lost_deal, listed, under_contract, not_interested, dnc, opt_out, Dead Lead.

**Exclusion set 5** (statuses left out, used by 2 presets): sold, closed, close_out, lost_deal, listed, under_contract, not_interested, dnc, opt_out, Dead Lead, prospecting.

**Exclusion set 6** (statuses left out, used by 2 presets): sold, new_lead, No Contact New Lead, Cold Lead, Warm Lead, Hot Lead, Ghosting Lead, follow_up, lead, underwrite, under_contract, push_deal, transactions, refer_lead, Dead Lead, not_interested, closed, close_out, lost_deal, dnc, opt_out, listed, buyer, buyer_found, buyer_lost, Auction Date Passed.

**Exclusion set 7** (tags left out, used by 19 presets): recently sold, Do Not Market, Litigator, Heir Identified, Not Single Family, Return Mail, Do Not Mail.

**Exclusion set 8** (tags left out, used by 19 presets): recently sold, Mail Only, Do Not Market, Litigator, Heir Identified, Not Single Family.

**Exclusion set 9** (tags left out, used by 9 presets): recently sold, Mail Only, Do Not Market, Litigator, Heir Identified, Not Single Family, FTM.

**Exclusion set 10** (tags left out, used by 6 presets): recently sold, Do Not Market, Litigator, Heir Identified, Not Single Family, Return Mail, Do Not Mail, FTM.

## Step 6: the SiftLine boards

- **Lead Management:** New Lead (Unqualified), No Contact New Lead (Unqualified), Nurture New Lead (Unqualified), COLD Leads (Qualified), WARM Leads (Qualified), HOT Leads (Qualified), Send to Acquisitions, Needs Review, Lost Deal, Became Buyer, Ghosting Lead, Not Interested Anymore, Dead Lead
- **Acquisitions:** Appointment Booked, Property Walked, Ran Numbers, Offer Made, Offer Follow-Up, Offer Rejected, Offer Accepted, Under Contract, Referred Lead, Lost Deal, Send Back to Lead Management
- **Deep Prospecting:** Call New Numbers, Call Relatives, Check Relatives, Research on Socials, Message on Socials, Doorknock
- **Transactions:** New Contract, Waiting on Title, Title Issues, Clear to Close, Closed, Fell Through
- **Wholesale:** New Contract, Pushing VIP Buyers, Pushing All Buyers, Buyer Found, Assigned, Closed, Fell Through
- **Flips:** New Flip, Demo, Rehab, Rehab Complete, Listed, Sold
- **Rentals:** New Rental, Demo, Rehab, Rehab Complete, Listed Rent, Rented
- **First to Market:** Research, Day 1, Day 2, Day 3, Day 4, Day 5, Exhausted
- **Curative Title:** Research, Ready for Marketing, Day 1, Day 2, Day 3, Day 4, Day 5, Heir Engaged, Exhausted

## Step 7: the sequences

| Sequence | Folder | Active | What it does |
| --- | --- | --- | --- |
| Lead Flow | default | yes | When a tag is added and the record has the tag leadflowrei: set the status to new_lead. |
| Sold Property Removal | default | no | When a tag is added and the record has the tag recently sold: set the status to sold. |
| Recently Sold to Sold Status | Transactions | yes | When a tag is added and the record has the tag recently sold: set the status to sold. |
| Sold Property Cleanup V2 | Transactions | no | When a tag is added and the record has the tag Sold: remove the lists Pre-Foreclosure, Probate, Tax Sale, Tax Delinquent, Absentee Owners and 35 more; remove the tags Auction Soon, Buy Box - Blount, Buy Box - Knox, Courthouse Data, Dataflik_AI and 109 more; delete all its tasks; clear the status. |
| Sold Property Cleanup | Transactions | yes | When a tag is added and the record has the tag Sold: set the status to sold. |
| Send to Deep Prospecting | Deep Prospecting | yes | When the status changes and the new status is deep prospecting: put a card in Deep Prospecting / Check Relatives; send the webhook SiftStack agents: deep prospect (made in the app for each account). |
| Close Deal Status | Transactions | yes | When a board card moves and the card lands in Transactions / Closed: set the status to Closed; delete all its tasks; clear the assignee. |
| [LOOP] Follow-Up with Seller and Title | Transactions | yes | When a task is completed and the task is Follow-Up with Seller and Title: create the task Follow-Up with Seller and Title. |
| [NEW] Follow-Up with Seller and Title | Transactions | yes | When a board card moves and the card lands in Transactions / Waiting on Title: create the task Follow-Up with Seller and Title. |
| [MOVE] Send back to LM | Acquisitions | yes | When a board card moves and the card lands in Acquisitions / Send Back to Lead Management: move the card to Lead Management / Needs Review; delete all its tasks; create the task Send Back to LM. |
| [MOVE] Under Contract | Acquisitions | yes | When a board card moves and the card lands in Acquisitions / Under Contract: move the card to Transactions / New Contract; delete all its tasks; create the task Process Contract; set the status to under_contract. |
| [LOOP] Offer Follow-Up | Acquisitions | yes | When a task is completed and the task is Offer Follow-Up: create the task Offer Follow-Up. |
| [NEW] Offer Follow-Up | Acquisitions | yes | When a board card moves and the card lands in Acquisitions / Offer Follow-Up: delete all its tasks; create the task Offer Follow-Up. |
| [CLEAR] Ghosting | Lead Management | yes | When a board card moves and the card lands in Lead Management / Ghosting Lead: delete all its tasks; set the status to Ghosting Lead. |
| [CLEAR] Dead Lead | Lead Management | yes | When a board card moves and the card lands in Lead Management / Dead Lead: delete all its tasks; set the status to Dead Lead. |
| [CLEAR] Not Interested | Lead Management | yes | When a board card moves and the card lands in Lead Management / Not Interested Anymore: delete all its tasks; set the status to not_interested. |
| [MOVE] Make Offer | Lead Management | yes | When a board card moves and the card lands in Lead Management / Send to Acquisitions: move the card to Acquisitions / Appointment Booked; delete all its tasks; create the task Make Offer. |
| [LOOP] Hot Lead Follow-Up | Lead Management | yes | When a task is completed and the task is HOT Follow-Up: create the task HOT Follow-Up. |
| [NEW] Hot Lead | Lead Management | yes | When a board card moves and the card lands in Lead Management / HOT Leads (Qualified): delete all its tasks; create the task HOT Follow-Up; set the status to Hot Lead. |
| [LOOP] Warm Lead Follow-Up | Lead Management | yes | When a task is completed and the task is WARM Follow-Up: create the task WARM Follow-Up. |
| [NEW] Warm Lead | Lead Management | yes | When a board card moves and the card lands in Lead Management / WARM Leads (Qualified): delete all its tasks; create the task WARM Follow-Up; set the status to Warm Lead. |
| [LOOP] Cold Lead Follow-Up | Lead Management | yes | When a task is completed and the task is COLD Follow-Up: create the task COLD Follow-Up. |
| [NEW] Cold Lead | Lead Management | yes | When a board card moves and the card lands in Lead Management / COLD Leads (Qualified): delete all its tasks; create the task COLD Follow-Up; set the status to Cold Lead. |
| [LOOP] Nurture New Lead | Lead Management | yes | When a task is completed and the task is Nurture New Lead: create the task Nurture New Lead. |
| [NEW] Nurture New Lead | Lead Management | yes | When a board card moves and the card lands in Lead Management / Nurture New Lead (Unqualified): delete all its tasks; create the task Nurture New Lead. |
| [LOOP] No Contact New Lead | Lead Management | yes | When a task is completed and the task is No Contact New Lead: create the task No Contact New Lead. |
| [NEW] No Contact New Lead | Lead Management | yes | When a board card moves and the card lands in Lead Management / No Contact New Lead (Unqualified): delete all its tasks; create the task No Contact New Lead; set the status to No Contact New Lead. |
| Call New Lead | Lead Management | yes | When the status changes and the new status is new_lead: assign it to Emerson; create the task Call New Lead; put a card in Lead Management / New Lead (Unqualified). |

## Step 8: the task presets

- **Acquisitions:** Ghosted Lead, Make Offer, Offer Follow-Up, Send Back to LM
- **Lead Management:** Call New Lead, COLD Follow-Up, HOT Follow-Up, No Contact New Lead, Nurture New Lead, WARM Follow-Up
- **Transactions:** Close out Deal, Follow-Up with Seller and Title, Process Contract

## Step 9: the SiftMap auto-add presets

40 SiftMap presets feed new records into the account. They carry the source market's county and addresses, so the blueprint creates them with auto-add switched off: point each one at your own market before you switch it on. Knox - Tier 2 - Tax Delinquent, Knox - Tier 2 - Vacant, Knox - Tier 2 - Absentee + Tax Delinquent, Knox - Obituary, Knox - Tier 2 - Probate, Knox - Tier 2 - Foreclosure (NOD), Knox - Recently Sold, Knox - Buyer Prospecting (3+ Owned), Blount - Tier 2 - Tax Delinquent, Blount - Tier 2 - Vacant, Blount - Tier 2 - Absentee + Tax Delinquent, Blount - Tier 2 - Probate, Blount - Tier 2 - Foreclosure (NOD), Blount - Recently Sold, Blount - Buyer Prospecting (3+ Owned), Obituary + Knox County, Blount Obituary Data, Knox - Priority 1 - AI 90+, Knox - Priority 1 - Absentee + Low Income, Knox - Priority 1 - Bad Credit + Free & Clear, Knox - Priority 1 - Absentee + Bad Credit, Knox - Priority 2 - AI 70+, Knox - Priority 2 - Bad Credit + Low Income, Knox - Priority 2 - Absentee + Tax Delinquent, Knox - Priority 2 - Tax Delinquent + Senior, Knox - Priority 2 - Free & Clear + Tax Delinquent, Blount - Priority 1 - AI 90+, Blount - Priority 1 - Absentee + Low Income, Blount - Priority 1 - Bad Credit + Free & Clear, Blount - Priority 1 - Absentee + Bad Credit, Blount - Priority 2 - AI 70+, Blount - Priority 2 - Bad Credit + Low Income, Blount - Priority 2 - Absentee + Tax Delinquent, Blount - Priority 2 - Tax Delinquent + Senior, Blount - Priority 2 - Free & Clear + Tax Delinquent, Dispo - Pending Flips - Knox, Dispo - Pending Flips - Blount, Knox-Priority 1- Free & Clear + Senior + Vacant, Priority 1- Free & Clear + Senior + Vacant, Prioity 1-Free & Clear + Senior + Vacant.

## Keeping this guide current

```bash
python src/clone_account.py --phase export        # a new blueprint from the live account
python tools/sync_account_blueprint_skill.py       # copy it into the account-blueprint skill
python tools/render_account_structure.py           # rewrite this guide
```
