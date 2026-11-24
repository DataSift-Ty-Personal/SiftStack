# Lessons from production

What broke while SiftStack ran a real investing business, why, and what changed. Newest first.

Many of these were found and fixed by the issue fixer (`tools/issue_fixer/`): every quarter hour it reads
the issues channel, finds the cause of each new problem, writes the fix and its test, runs every test
suite, and ships the fix on its own. Each fix it ships is published to this repository, with its lesson
here, as soon as it ships, so an install picks it up with its next update. The rest were found in the same
channel and fixed by hand. The rule behind each one is worth keeping even if you never run the code that
broke.

<!-- new lessons go here -->

## 2026-11-23: A card move read as a failure because the account moved it again

- **What happened:** The lead clock moved a card to its next column and then reported that the card did not move.
- **Why:** Moving a SiftLine card through the API fires the account's own "card moved" sequences, and they act within a second. A sequence had already carried the card on to another board when the clock read the column back.
- **The fix:** A card that has left its old column counts as moved, wherever it went next (`src/agents/autopilot/leadclock.py`). Any read-back after an API write has to allow for the account's own automation acting on it first.
- **Found by:** the issues channel, fixed by hand.

## 2026-11-22: Tax notice feeds searched a single day

- **What happened:** The tax sale and tax delinquent feeds read no rows on their searches and reported them as renamed or deleted.
- **Why:** Each feed passed the day of its last run to the search as an exact start date, the way a date a person types works, so a Sunday run searched only Saturday to Sunday. A notice the site posted a day or more after its publication date was missed for good.
- **The fix:** The search now tells the two apart: a stored day searches the full daily window (the seen-notice cache drops repeats), and only a date a person types is exact (`src/scraper.py`, `src/ftm_feed_runner.py`).
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-22: One dropped request stopped a whole county's tax check

- **What happened:** The weekly tax check left 7,398 of the 7,471 records due that week unchecked.
- **Why:** A single timeout on the county trustee's tax roll stopped the county for the whole run, and the stage runs once a week.
- **The fix:** A dropped connection, a timeout or a 502, 503 or 504 is asked again after 10 seconds and then 30; any other answer still stops the county at once (`src/agents/autopilot/taxwatch.py`).
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-20: A skip trace vendor refused every request

- **What happened:** The re-trace stage bought nothing from Tracerfy on its first live day.
- **Why:** Tracerfy's Cloudflare answers Python urllib's default User-Agent with HTTP 403 ("error code: 1010") before it reads the API key, so every request failed exactly like a bad key.
- **The fix:** Every call to Tracerfy names its own User-Agent (`src/agents/autopilot/sources.py`). Any new urllib call to a vendor should do the same.
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-20: One reset connection failed a whole day of lien pulls

- **What happened:** The daily lien stage failed for the day.
- **Why:** One "connection reset by peer" on a single Register of Deeds search, and the client made one attempt.
- **The fix:** A search is tried up to three times on a connection error or a timeout (after 10 seconds, then 20); an HTTP error still stops at once (`src/knox_rod.py`).
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-20: Completed records were never skip traced

- **What happened:** The nightly stage that completes owner names fixed the names but never sent a single batch to DataSift's skip trace.
- **Why:** The trace takes only records DataSift counts as complete, and a completed name can still leave a record incomplete (a blank mailing street or ZIP code). The dry run covered 85 of 100 records, the stage required all 100, and refused the whole batch every night.
- **The fix:** It sends only the records the search counts as complete, keeps the rest queued, and after three misses lists a record for a person (`src/agents/autopilot/complete.py`).
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-20: Rereading held records cost six hours a day

- **What happened:** Scheduled stages started up to an hour late.
- **Why:** The assignment stage read about 780 records it was holding back on every half-hour run, 351 minutes of work a day, all to reach the same answer.
- **The fix:** A held record is remembered for three hours before it is read again (`src/agents/autopilot/stages.py`).
- **Found by:** the issues channel, fixed by hand.

## 2026-11-20: A test run turned the morning health check red

- **What happened:** The first-to-market health check reported a failed run on a day the real run had worked.
- **Why:** A dry run started by hand on the box wrote a row in the run history, and the health check judged every row.
- **The fix:** The health check reads committed runs only (`src/ftm_health.py`).
- **Found by:** the issues channel, fixed by hand.

## 2026-11-19: A report line read as nonsense

- **What happened:** The account checks report said open tasks sat with "made by older than the log copy 3".
- **Why:** Tasks older than the local copy of the account log have no known maker, and that gap was printed as if it were a person.
- **The fix:** Those tasks are counted on their own ("3 too old for the log to show who made them"), never as a maker (`src/agents/autopilot/accountchecks.py`).
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-19: Email replies were lost when a note failed

- **What happened:** Three replies to a cold email campaign were never handled: a question that needed an answer, a no, and a request to stop.
- **Why:** Writing each reply's note to the CRM failed, and the reply checker moved its read-from point past those replies anyway, so they were never read again.
- **The fix:** The read-from point stops just before the earliest reply that failed, the hand raise is posted before any CRM write, and a reply that keeps failing goes to a person (`src/cold_email/replies.py`). Never move a bookmark past work that did not finish.
- **Found by:** the issues channel, fixed by hand.

## 2026-11-19: Interested sellers were filed away

- **What happened:** A seller who answered "Yes, but" with a money problem, and an owner's child explaining that the parents had died, were both filed away instead of handed to a person.
- **Why:** The reply model was told to escalate when torn, and on the live path every escalation short of a crisis was filed as mail only. A family member was treated as a wrong number.
- **The fix:** A yes after our selling question is a hand raise even with a problem attached, and estate and family words go to a person with the number tagged Relative, never wrong number (`src/sms_agent/classify.py`, `src/sms_agent/dispositions.py`). Audit lead capture by reading every reply that was not handed off, not by counting verdicts.
- **Found by:** the issues channel, fixed by hand.

## 2026-11-18: Notes stopped saving when an API route was retired

- **What happened:** Research notes and possible-death notes failed on every record; one stage tagged 43 records for review with no note to say why.
- **Why:** DataSift retired `POST /property/{uuid}/add-notes/` (it now answers a Not Found page), and the documented place for a note is the property's message board.
- **The fix:** Notes go to `POST /api/internal/property/{uuid}/message/` (`src/agents/autopilot/crm.py`). A documented route can still be retired; a write that fails has to say so, which is how this one was caught the same morning.
- **Found by:** the issue fixer, which fixed, tested and shipped it on its own.

## 2026-11-18: A capped search hid a week of probate notices

- **What happened:** The daily probate pull found nothing new for a week while the site held dozens of fresh notices.
- **Why:** The saved 12-month search stops at 1,000 rows, and the site fills that cap across the whole year, so the newest notices fell outside it. Every run worked and found nothing.
- **The fix:** Daily runs search an explicit date window, at least 14 days back, and a search that cannot apply its window is a warning (`src/scraper.py`). A quiet feed is worth a look: zero is a result, not proof.
- **Found by:** the issues channel, fixed by hand.

## 2026-11-18: The post rewriter changed what a post meant

- **What happened:** A crash in the calls check was rewritten for the team as "no one was called yesterday", which was false.
- **Why:** A small model rewrites team posts in plain words, and the second look that checked it was the same model.
- **The fix:** Code checks every rewrite: each number must come from the original, nothing new may be claimed (no "now", no new labels), and on any doubt the plain original is posted instead (`src/agents/voice.py`).
- **Found by:** the issues channel, fixed by hand.
