# Notice screenshots (retired 2026-08-14)

Proof-of-source screenshots of tnpublicnotice.com notice detail pages. The idea
was that the published legal notice travelled with the record into DataSift as a
clickable link, to add legitimacy to outreach.

**Retired per Ty:** the team does not use them for anything sourced from TN
Public Notice any more.

Removing the capture was also a real speedup. It was the slowest step in a
foreclosure notice (screenshot, then host it on Drive/Dropbox/KVS, then re-write
the CSV so the URL flowed through), which matters directly on a 12-month
backfill of a few thousand notices.

## What was removed from the live path

- `scraper.py` no longer captures a screenshot after parsing a kept notice.
- `main.py` (CLI) no longer hosts screenshots on Google Drive.
- `config.py` no longer defines `CAPTURE_NOTICE_SCREENSHOTS`,
  `NOTICE_SCREENSHOT_TYPES` or `NOTICE_SCREENSHOT_DIR`.

## What was deliberately KEPT

- `NoticeData.notice_screenshot_path` / `notice_screenshot_url` fields
- the `notice_screenshot_url` CSV column and the DataSift "Notice Screenshot"
  custom field mapping

They cost nothing when empty, and records uploaded before this date still carry
real URLs. Ripping the column out would break the DataSift field mapping and
every historical CSV for no gain.

The Apify Actor path in `main.py` still contains its own Dropbox/KVS hosting
block. It is inert because nothing sets `notice_screenshot_path` any more, and
that path is itself being retired in favour of the Fly deployment.

## Files here

| file | what it did |
|---|---|
| `notice_screenshot.py` | capture a full-page PNG, name it, host it on Drive |
| `backfill_screenshots.py` | re-open old notices via Scrapfly to backfill missing PNGs |
| `test_notice_screenshot.py` | live capture test |

To bring it back: restore these to `src/`, re-add the config constants, and
restore the capture block in `scraper.py` after the county-filter check.
