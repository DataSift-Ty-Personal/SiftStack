---
name: phone-validator
description: >
  Score and validate phone numbers via Trestle's phone_intel API, then generate DataSift/REISift-ready CSVs with phone tags for upload. Use whenever someone wants to: validate phones, score activity, check if connected, generate phone tags for DataSift, prepare dial lists, prioritize a call list, identify dead numbers, check line types, or create tiered dial lists. Trigger for "phone validation", "validate phones", "activity score", "phone tags", "tag phones", "dial first", "Trestle", "phone_intel", "dead phones", "line type", "prioritize phones", "DataSift phone upload", "REISift phone tags", "score these phones", or "which numbers should I call first" — use this skill.
---

# Phone Validator & Tagger

Score phone numbers using Trestle's phone_intel API and produce DataSift/REISift-ready
CSVs with phone tags for prioritized dialing.

## Execution Mode Detection

Before starting, detect the execution environment for the DataSift upload step:

**Check 1:** Does `scripts/upload_phone_tags.py` exist in this skill's directory?
**Check 2:** Is Playwright available? Run: `python -c "from playwright.sync_api import sync_playwright; print('OK')"`
**Check 3:** Are credentials set? Check for `DATASIFT_EMAIL` and `DATASIFT_PASSWORD` in `.env` or environment.

### Automated Upload Mode (Claude Code CLI — all 3 checks pass)

After running `scripts/validate_phones.py` to generate the phone tags CSV, upload directly:

```bash
# Upload validated phone tags to DataSift
python scripts/upload_phone_tags.py --csv phone_tags_for_datasift.csv

# Headless mode (no visible browser)
python scripts/upload_phone_tags.py --csv phone_tags_for_datasift.csv --headless
```

The script handles login, navigating to Upload File -> Update Data -> "Tag phones by phone number", file upload, column mapping, and completion automatically.

### Manual Upload Mode (Co-Work or no Playwright — any check fails)

After generating the phone tags CSV, follow the manual DataSift upload instructions in the "Upload to DataSift" section below.

## What This Skill Does

This skill takes a CSV of phone numbers — typically a DataSift "Phone Enrichment" export
with Phone 1 through Phone 30 columns — runs each unique number through Trestle's Phone
Validation API to get an activity score and line type, then assigns a phone tag tier based
on configurable score thresholds. The output is a two-column CSV (`Phone Number`, `Phone Tag`)
formatted for direct upload to DataSift/REISift using their "Update Data → Tag phones by
phone number" workflow.

## The Pipeline

```
Input CSV (DataSift Phone Enrichment export or any CSV with phone columns)
  → Parse all phone columns (Phone 1 through Phone 30)
    → Deduplicate across all columns and rows (saves API cost)
      → ESTIMATE COST & GET USER CONFIRMATION
        → Trestle phone_intel API (activity_score + line_type)
          → Score-based tier assignment (Dial First, Dial Second, Dial Third, Dial Fourth, Drop)
            → DataSift-ready CSV (Phone Number | Phone Tag)
              → Upload to DataSift via "Update Data → Tag phones by phone number"
```

## How to Execute: Use the Bundled Script

The entire pipeline is handled by `scripts/validate_phones.py`. Always run this script
rather than reimplementing the API calls — it handles rate limiting, error recovery,
progress reporting, and the exact CSV format DataSift expects.

### Step 1: Install Prerequisites

```bash
pip install --break-system-packages requests
```

No other dependencies are needed.

### Step 2: Get the User's Trestle API Key

The script requires a Trestle API key. Check for it in this order:

1. Environment variable: `TRESTLE_API_KEY`
2. Ask the user to provide it

If the user doesn't have one, direct them to https://trestleiq.com to sign up —
they get 25 free queries per product on their trial.

### Step 3: Estimate Cost & Get User Confirmation

**This step is mandatory.** Before making any API calls, always run the script in
estimate mode first. This parses the CSV, counts unique phone numbers across all
columns, deduplicates, and calculates the cost at $0.015 per phone.

```bash
python3 SKILL_DIR/scripts/validate_phones.py \
  --input "path/to/phones.csv" \
  --estimate
```

This will output something like:

```
==================================================
  PHONE VALIDATION COST ESTIMATE
==================================================
  Input file:          Phone Enrichment.csv
  Total phone entries: 9,648
  Unique phones:       3,865
  Duplicates saved:    5,783
  Cost per phone:      $0.015
  ─────────────────────────────────────
  ESTIMATED COST:      $57.98
==================================================
```

Present this to the user and wait for their explicit confirmation before proceeding.
The deduplication savings are worth calling out — in a typical DataSift export, the
same person (and their phones) can appear on multiple rows because they own multiple
properties. The script only charges once per unique phone number.

For programmatic use, `--estimate-json` returns the same data as a JSON object.

### Step 4: Run the Validation

Once the user confirms the cost, run the full validation:

```bash
python3 SKILL_DIR/scripts/validate_phones.py \
  --input "path/to/phones.csv" \
  --output "path/to/output_directory/" \
  --api-key "$TRESTLE_API_KEY"
```

Replace `SKILL_DIR` with the actual path to this skill's directory. If you uploaded this skill to Claude Co-Work or a Claude Project, the path is managed automatically — Claude will resolve it. For manual terminal use, the path is wherever you extracted the `.skill` file (e.g., `~/skills/phone-validator/scripts/validate_phones.py`).

**Optional flags:**

| Flag | Default | What it does |
|------|---------|-------------|
| `--tiers` | `default` | Tier strategy: `default` (5 tiers) or `custom` |
| `--custom-tiers` | — | JSON string defining custom tier boundaries (see below) |
| `--batch-size` | `10` | Concurrent API requests (respect Trestle's rate limits) |
| `--delay` | `0.1` | Seconds between batches |
| `--phone-column` | auto-detect | Override phone column name |
| `--add-litigator` | `false` | Include litigator risk check (uses Trestle add-on) |
| `--full-report` | `false` | Generate a detailed XLSX report alongside the tag CSV |

### Step 5: Understand the Output

The script produces these files in the output directory. See the "Output Files"
section below for sample data and detailed column descriptions.

1. **`phone_tags_for_datasift.csv`** — Two-column CSV for DataSift upload (Phone Number + Phone Tag)
2. **`validation_results.csv`** — Full API results with activity score, line type, carrier, tier, litigator flag
3. **`summary.txt`** — Human-readable tier counts, score distribution, line type breakdown
4. **`errors.csv`** — Any phones that failed all retries
5. **`validation_report.xlsx`** — (if --full-report) Excel workbook with charts

## Input Format: DataSift Phone Enrichment Export

The script is built to work directly with DataSift's "Phone Enrichment" CSV export format.
This is a wide-format file where each record (property/contact) can have up to 30 phone
numbers, each with associated metadata columns:

```
Phone 1, Phone Type 1, Phone Status 1, Phone Tags 1, Phone Is Connected 1,
Phone 2, Phone Type 2, Phone Status 2, Phone Tags 2, Phone Is Connected 2,
...
Phone 30, Phone Type 30, Phone Status 30, Phone Tags 30, Phone Is Connected 30
```

The script automatically detects all `Phone N` columns (1-30) and ignores the metadata
columns (`Phone Type N`, `Phone Status N`, `Phone Tags N`, `Phone Is Connected N`).
It then extracts every phone number across all columns and rows, deduplicates them,
and sends only unique numbers to the API.

The existing Phone Type values from skip tracing (MOBILE, LANDLINE, etc.) are left
untouched — this skill only adds phone tags, it does not modify the type or status fields.

The script also works with simpler CSV formats that just have a `Phone` or `Phone Number`
column.

## Line Type Context

The Trestle API returns these line types, which matter for how you contact the number:

| Line Type | What It Means | Implication |
|-----------|--------------|-------------|
| Mobile | Cell phone | Best for calling AND texting |
| Landline | Traditional landline | Call only — cannot receive SMS |
| FixedVOIP | Fixed Voice over IP (cable phone, etc.) | Usually dialable, sometimes textable |
| NonFixedVOIP | Non-fixed VOIP (Google Voice, etc.) | May be temporary/disposable — lower priority |
| Tollfree | 800/888/etc. number | Skip — not a personal number |
| Premium | Premium-rate number | Skip — will cost money to call |
| Voicemail | Voicemail-only service | Skip — no live person |

A key insight from our research: 24% of numbers that Sift labels as "Landline" are actually
FixedVOIP or NonFixedVOIP when checked against Trestle. These are textable numbers being
miscategorized — the detailed `validation_results.csv` output surfaces this with the
`line_type` column so you can identify which "Landline" numbers are actually textable.

## DataSift Upload Workflow

After the script generates `phone_tags_for_datasift.csv`:

1. Log into your DataSift/REISift account
2. Go to **Upload** → select **Update Data**
3. Choose **"Tag phones by phone number"**
4. Upload the CSV file
5. Map `Phone Number` → Phone Number field
6. Map `Phone Tag` → Phone Tag field
7. Complete the upload

The tags will apply across ALL records that share each phone number. So if the same
number appears on 3 different property records, all 3 get the tag.

### Integration / Dialer Workflow

Once tagged, when sending to a dialer integration:

- Go to **Send To** → select your dialer
- Under phone tag filters, select the tier(s) you want to send
- **Important**: Each transfer should only include ONE phone tag tier, because the
  filter requires the number to have ALL selected tags. Send "Dial First" separately
  from "Dial Second", etc.

For non-integrated dialers, use **Export** → filter by the specific phone tag.

## API Failure Recovery

The script handles Trestle API failures automatically, but understanding the behavior
helps when troubleshooting large runs.

### Timeout Handling

Each API call has a 15-second timeout. On timeout, the script retries up to 3 times
with exponential backoff (1.5s, 3s, 6s). If all retries fail, the number is logged
to `errors.csv` and the batch continues — one timeout does not stop the run.

### Rate Limiting (HTTP 429)

Trestle allows 10 requests/second by default. The script sends requests in batches
(default batch size 10) with a 100ms delay between batches.

If you start getting 429 errors:
1. The script auto-retries with exponential backoff (1.5s, 3s, 6s)
2. If 429s persist, reduce batch size: `--batch-size 5`
3. Increase delay between batches: `--delay 0.5`

### Invalid Phone Format

Numbers that fail the 10-digit US format check (after stripping country code and
non-digit characters) are silently skipped during CSV parsing — they never hit the API.
Numbers that pass format checks but fail Trestle's `is_valid` check are tagged as
"Invalid" and excluded from the tier tagging CSV. Neither case fails the batch.

### Network Errors & Resume

On any `requests.RequestException` (DNS failure, connection reset, etc.), the phone
is logged to `errors.csv` and processing continues. For very large lists (10,000+),
if the run is interrupted (Ctrl+C, crash), re-run with the same input — the script
deduplicates before calling the API, so already-processed numbers in a previous
`validation_results.csv` could be used to filter. For mission-critical runs, consider
splitting the input CSV into chunks of 2,000-5,000 numbers.

### Monthly API Limits

Trestle plans have monthly query limits (check your account dashboard at
https://trestleiq.com). The `--estimate` flag helps you plan usage. Cost is $0.015
per unique phone number. Batch mode supports up to 50 phones per request on
higher-tier plans.

## Tier Strategies

### Default (5 Tiers)

Five priority buckets that give callers a clear work order without overcomplicating
things. Based on analysis of validated phone numbers against actual Sift call outcomes:

| Score Range | Tag | What to do |
|------------|-----|------------|
| 81-100 | Dial First | Your best numbers — highest activity, highest contact rate. Call these first. |
| 61-80 | Dial Second | Strong numbers with solid activity. Work these after your first batch. |
| 41-60 | Dial Third | Moderate activity — still worth calling if you have capacity. |
| 21-40 | Dial Fourth | Inconsistent activity — get to these last if there's still time on the clock. |
| 0-20 | Drop | Dead or disconnected — not worth the dial time. |

This gives callers a clear work order: burn through Dial First, move to Dial Second,
then Dial Third, and reach into Dial Fourth if there's still time on the clock. Drop
gets excluded entirely.

### Aggressive Strategy (Time-Sensitive Leads)

Use for foreclosures, tax sales, or any lead where speed-to-contact matters more
than efficiency. Casts a wider net by lowering the "dial" threshold.

```bash
--custom-tiers '{"Dial First": [71, 100], "Dial Second": [51, 70], "Drop": [0, 50]}'
```

| Score Range | Tag | Rationale |
|------------|-----|-----------|
| 71-100 | Dial First | Wider top tier catches more reachable numbers fast |
| 51-70 | Dial Second | Still worth attempting — some will connect |
| 0-50 | Drop | Only drop truly inactive numbers |

**When to use:** Foreclosure auctions in <30 days, tax sale deadlines, any lead
where a 48-hour response window matters.

### Conservative Strategy (Long-Term Nurture)

Use for probate, divorce, or campaigns where you have months to work the list.
Preserves dial time by being stricter about who gets called.

```bash
--custom-tiers '{"Dial First": [91, 100], "Dial Second": [71, 90], "Dial Third": [51, 70], "Mail Only": [21, 50], "Drop": [0, 20]}'
```

| Score Range | Tag | Rationale |
|------------|-----|-----------|
| 91-100 | Dial First | Only the highest-confidence numbers |
| 71-90 | Dial Second | Strong numbers, second priority |
| 51-70 | Dial Third | Moderate — call if capacity allows |
| 21-50 | Mail Only | Low activity — direct mail instead of phone |
| 0-20 | Drop | Dead or disconnected |

**When to use:** Probate lists (6-12 month follow-up), divorce leads, any campaign
where you want to maximize ROI per dial rather than speed.

### Decision Tree: Which Strategy?

Ask these two questions:

1. **What's the urgency?** If the lead has a deadline (auction date, tax sale, code
   violation compliance), use Aggressive. If it's a long-term nurture campaign, use
   Conservative. If unsure, use Default.

2. **How many records?** Under 500 records — Default is fine, you can call them all.
   Over 2,000 records — consider Conservative to avoid burning out your callers on
   low-probability numbers.

### Custom Tiers

If none of the presets fit, pass any JSON object mapping tag names to `[min, max]`
score ranges:

```bash
--custom-tiers '{"Priority": [80, 100], "Standard": [50, 79], "Low": [20, 49], "Remove": [0, 19]}'
```

## Litigator Risk Check

### What It Does

The `--add-litigator` flag adds Trestle's `litigator_checks` add-on to each API call.
This flags phone numbers associated with individuals who have filed TCPA (Telephone
Consumer Protection Act) litigation — people who sue businesses for unwanted calls/texts.

### When to Use

- **Always for SMS campaigns.** TCPA litigation risk is highest for text messages.
  A single TCPA violation can cost $500-$1,500 per message.
- **Optional for call-only campaigns.** Risk is lower but still present, especially
  for auto-dialed calls.
- **Skip for manual-dial-only campaigns** where a human initiates each call — TCPA
  risk is minimal.

### Cost

Additional $0.005 per phone number on top of the base $0.015 (total $0.020/number).
The `--estimate` flag includes this in the cost calculation when `--add-litigator`
is specified.

### Action on Litigator Match

When `is_litigator_risk` is `true` in the results:

1. **Remove from all SMS/text campaigns immediately.** Do not text this number.
2. **Call-only with documented consent.** If you must call, ensure you have prior
   express consent documented before dialing.
3. **Flag in DataSift.** The `validation_results.csv` includes the `is_litigator_risk`
   column. Filter for `true` values and apply a "Litigator Risk" tag or move to a
   separate "Do Not Text" list.

### CLI Usage

```bash
python3 SKILL_DIR/scripts/validate_phones.py \
  --input "phones.csv" \
  --output "./results/" \
  --api-key "$TRESTLE_API_KEY" \
  --add-litigator
```

## Prepaid Phone Handling

Prepaid and NonFixedVOIP phones are common in distressed property situations —
financially stressed homeowners often use prepaid plans. A prepaid phone does NOT
mean a bad lead.

### Key Insights

- **Activity score trumps line type.** A prepaid phone with a 90+ activity score is
  a highly active, reachable number. Tier it the same as any other phone.
- **NonFixedVOIP is not disposable by default.** Google Voice, TextNow, and similar
  services are primary phones for many people, especially younger demographics.
- **24% of "Landline" numbers are actually textable.** DataSift/Sift's skip trace
  labels many FixedVOIP and NonFixedVOIP numbers as "Landline". The Trestle
  `line_type` field in `validation_results.csv` reveals the true type — check this
  before excluding "Landline" numbers from SMS campaigns.

### What NOT to Do

- Do not automatically drop prepaid numbers
- Do not downgrade tier assignment based on `is_prepaid = true`
- Do not skip NonFixedVOIP numbers — score them by activity like any other number

### What TO Do

- Use the `line_type` column in `validation_results.csv` to identify which "Landline"
  numbers are actually FixedVOIP/NonFixedVOIP (and therefore textable)
- For SMS campaigns, filter by `line_type` in (`Mobile`, `FixedVOIP`, `NonFixedVOIP`)
  regardless of what Sift's Phone Type field says

## Output Files

### validation_results.csv — Sample Output

```csv
phone_number,activity_score,line_type,carrier,is_valid,is_prepaid,assigned_tag,is_litigator_risk
8651234567,94,Mobile,T-Mobile USA Inc.,True,False,Dial First,False
8659876543,72,Landline,AT&T Tennessee,True,False,Dial Second,
8653456789,55,FixedVOIP,Comcast Phone of Tennessee,True,False,Dial Third,
8657654321,31,NonFixedVOIP,Google (Grand Central) LLC,True,True,Dial Fourth,
8652223333,8,Mobile,Sprint Spectrum LP,True,False,Drop,
8650001111,,,,False,,,
```

Notes on the sample:
- Row 2: "Landline" from Sift skip trace, but Trestle shows this is actually a
  traditional landline (not textable in this case). Compare against Sift's Phone Type
  to find mismatches.
- Row 4: NonFixedVOIP (Google Voice) + prepaid = still Dial Fourth based on score 31.
  The prepaid flag does not affect tier assignment.
- Row 6: Invalid phone format — no API call made, all fields empty except phone_number.
- `is_litigator_risk` column is empty when `--add-litigator` was not used.

### phone_tags_for_datasift.csv

Two columns only — this is what gets uploaded to DataSift:

```csv
Phone Number,Phone Tag
8651234567,Dial First
8659876543,Dial Second
8653456789,Dial Third
8657654321,Dial Fourth
```

Invalid phones and Drop-tier phones are excluded from this file.

### summary.txt

Human-readable run summary with tier counts, score distribution histogram, line type
breakdown, and DataSift upload instructions.

### errors.csv

Any phones that failed all API retries, with error details for troubleshooting.

## Connect Rate Impact

Scoring phones before dialing dramatically improves connect rates compared to
blind-dialing every number from a skip trace.

| Metric | Blind Dialing | Post-Scoring (Tiers 1-2 Only) |
|--------|--------------|-------------------------------|
| Connect rate | 2-3% | 9.5% |
| Improvement | baseline | 4.75x |
| Dataset | 2,000 records | Same 2,000 records |

The 4.75x improvement comes from focusing dial time on Tiers 1-2 (Dial First + Dial
Second), which contain 92% of the numbers that actually connect but represent only
48.3% of the total phone list. In other words, you skip half the list and lose almost
none of the conversations.

**How the math works:** 9.5% / 2.0% = 4.75x. Tested on a 2,000-record dataset from
Knox County foreclosure and probate lists, comparing all-number dialing against
Tier 1-2-only dialing over a 30-day campaign.

## Trestle API Rate Limits & Pricing

| Parameter | Value |
|-----------|-------|
| Rate limit | 10 requests/second (default plan) |
| Batch mode | Up to 50 phones per request (higher-tier plans) |
| Monthly limit | Plan-dependent (check account dashboard) |
| Cost per phone | $0.015 per unique phone number |
| Litigator add-on | +$0.005 per phone ($0.020 total) |
| Free trial | 25 queries per product within 14 days |

The script defaults to batch size 10 with 100ms delay, which stays comfortably under
the 10 req/s limit. Increase `--batch-size` only if you have confirmed your plan
supports higher throughput.

## Handling Edge Cases

**Duplicate phone numbers**: The script deduplicates before calling the API to avoid
wasting queries. Each unique number is scored once, and the tag CSV includes one row
per unique number. In a typical DataSift export, the same owner's phones repeat across
multiple property rows — dedup catches all of these.

**Invalid/short numbers**: Numbers that fail the 10-digit format check are silently
skipped. Numbers that pass format but fail Trestle's `is_valid` check get tagged as
"Invalid" and excluded from the tier tagging.

**API errors / timeouts**: The script retries failed requests up to 3 times with
exponential backoff (1.5s base, doubling each retry). Permanently failed numbers
are logged to `errors.csv`. A single failure never stops the batch.

**Numbers already tagged in Sift**: Uploading new tags via "Update Data" ADDS to existing
tags — it does not replace them. If you need to remove old tags first, you'd do a
separate "Remove phone tags by phone number" upload.

**Large lists**: Trestle rate-limits at 10 req/s. The script defaults to 10 concurrent
requests with 100ms delay between batches. For lists over 10,000 numbers, consider
splitting into chunks or using Trestle's batch upload product.

**No API key available**: If the user doesn't have a Trestle API key and can't get one,
the script can run in `--dry-run` mode which generates the CSV template with placeholder
tags so they can see the format and manually fill in scores later.
