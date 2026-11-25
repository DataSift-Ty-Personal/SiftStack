# Lessons from production

What broke while SiftStack ran a real investing business, why, and what changed. Newest first.

Many of these were found and fixed by the issue fixer (`tools/issue_fixer/`): every quarter hour it reads
the issues channel, finds the cause of each new problem, writes the fix and its test, runs every test
suite, and ships the fix on its own. Each fix it ships is published to this repository, with its lesson
here, as soon as it ships, so an install picks it up with its next update. The rest were found in the same
channel and fixed by hand. The rule behind each one is worth keeping even if you never run the code that
broke.

<!-- new lessons go here -->

## 2026-11-24: Seven public skills asked for a DataSift login

- **What happened:** The KPI engine asked users to copy a token out of their browser; the account blueprint applied with a pasted token or an email and password; the sequential presets, Sift sequences and Sift market research skills drove the DataSift app through a browser login; the phone validator's upload mode and the Sift operations guide did the same.
- **Why:** Each was built for the team's own accounts, where a staff login was at hand, and published as it was. DataSift's own terms restrict tools built on copied browser tokens, and a user should only ever need their Open API key.
- **The fix:** All seven run on the Open API key (`REISIFT_API_KEY`). The KPI engine reads the account activity log; Market Finder, presets and the phone tiers use the published routes; the account blueprint creates lists, tags, custom fields, task presets, preset folders and presets on the key and lists custom statuses, boards, board columns and sequences as a click-through in the app. Staff tools that still use a login are marked `staff-only`. A new check in `tools/check_docs.py` fails on login wording in any public skill, plugin or setup doc; it found and cleared 129 spots.
- **Found by:** Ty, while planning the AI Operator Bootcamp, fixed by hand.

## 2026-11-24: The buyer list shipped with city and state swapped

- **What happened:** In all 84,048 rows of the buyer prospector's bundled `nationwide_buyers.csv`, the column labeled `BuyerState` held the city and `BuyerCity` held the state. The script copied them as labeled into the mailing columns, so a list uploaded for skip trace carried every buyer's mailing city and state reversed, and the research guide sent people to the wrong state's business registry.
- **Why:** The two headers were written in the wrong order when the file was exported, and nothing checked what the columns held. Another tool in the repo had already worked around it by looking at the values, so the bug was known in one place and still shipped in another.
- **The fix:** The headers are corrected, and `analyze_buyers.py` now checks the values and repairs an older copy of the file a user may already have. The skill also says that `BuyerPurchases6MSum` is a regional total repeated on each county row, so it ranks buyers but never adds up across counties.
- **Found by:** a chain writer reading the skill while stacking it into the Deal Desk & Dispo chain, fixed by hand.

## 2026-11-24: Workflows listed as live that only ran on one machine

- **What happened:** After the curative gap, every workflow on the org chart was checked against a fresh install. Several read fine and could not run anywhere but the team's own workstation: the calling agency pools read DataSift's internal property index through a staff-only tool; the schema setup and the dispo buyer phases loaded a client from one desktop folder; the Tracerfy and Enformion re-skips picked their records from a file only that machine made and pointed at a scoring step that drove a browser; the phone validator's `--full-report` imported a report file that never shipped; and the Dream 100 pack looked for its logo on one desktop.
- **Why:** Each began as a tool for one account on one machine and was later listed as a workflow. Nothing checked what a workflow needs against what an install has.
- **The fix:** Each now runs on a user's own Open API key. The agency pools are built from SiftMap's published search, one AI score point at a time (`src/ai_pools.py`, `docs/agency-call-list.md`); the pull prices itself on DataSift's published quote; the re-skips read the account's own list and buy nothing without `--commit`; `src/phone_tiers.py` writes the dial tiers on the key; the report and the logo ship in the repository. The one-off ladder pull is retired, the builder land desk is labelled staff only, and the obituary campaign entries now point at the obituary lane that replaced them. A new check, `tools/workflow_audit.py`, fails on any shipped folder path from one machine, any import nothing installs, and any staff-only source behind an agent the org chart calls live; it runs in pytest and on every push to the public repository.
- **Found by:** an audit after a community member's report, fixed by hand.

## 2026-11-24: The curative board shipped without the research that feeds it

- **What happened:** The Curative Title board and its loader were published, but the ranking that finds the houses and the heir research that names every heir and share were not. A community member who installed SiftStack found the loader and nothing for it to load.
- **Why:** The ranking and the research began as one-off scripts in a research folder that is never published (it holds work in progress and owner and family names). The loader read its input from that folder, so it worked on the machine that built it and nowhere else.
- **The fix:** The whole workflow is now in the repository as one engine on a user's own Open API key: rank, family trees, the heir research (each house in its own Claude Code session), phone checks, the workbook, and the loader's hand-off (`src/curative_engine/`, `src/curative_title.py`, `docs/curative-title.md`). A test now fails if shipped code ever reads a research folder again (`tests/test_shipped_inputs.py`). Ship a workflow with every step that makes its input, and test it from a clean install.
- **Found by:** a community member, fixed by hand.

## 2026-11-24: Search results stopped carrying each house's flags

- **What happened:** SiftMap's property search began returning rows without their distress flags, presets and AI scores, though the published contract still shows them. Counts and filters kept working, so nothing failed.
- **Why:** A change on the data side between November 14 and 23. A ranking that read flags off the rows would have treated every house as having none: the Top 1,000's December sweep would have lost vacancy and tax delinquency, the two signals its model weighs most.
- **The fix:** When the rows come back without flags, the sweep searches for each flag the model reads and marks its members (`src/agents/autopilot/top1000.py`). Check that a field is present before trusting an empty one.
- **Found by:** building the live Doors per Deal ladder, fixed by hand.

## 2026-11-24: A list got credit for houses that sold before they joined it

- **What happened:** The monthly ladder check was about to report doors per deal for lists from houses that were added to the account, or to the list, after an investor bought them.
- **Why:** Of 927 investor purchases in six months, 890 are records now but only 36 were before the month they sold. 24 of those came in already owned by an investor, and a later list pull attached lists to houses that had sold the month before.
- **The fix:** A list counts for a sale only when the house's own history shows it attached before the month of the sale, and houses that came in on a buyer or sold list are left out (`src/agents/autopilot/laddercheck.py`).
- **Found by:** checking the idea against live data before building on it, fixed by hand.

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
