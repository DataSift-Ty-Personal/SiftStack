# VA SOP — Philadelphia Pre-Foreclosure Pull (First-to-Market)

## Why this matters (read once)

When a bank sues a Philadelphia homeowner for foreclosure, that lawsuit is filed
at the court **12–18 months before** the property ever shows up at a sheriff sale
(Bid4Assets). Reaching the homeowner during that window — while they still have
time and options — means almost no competition. About **100–150 of these are
filed every week** in Philadelphia.

The court website blocks automated scraping, so this is pulled by hand. That
friction is exactly why it stays uncompetitive: most investors never do it.

**Your job:** twice a week, pull the newest foreclosure filings from the court
site and save them to a spreadsheet. That's it. The system does the rest
(finds the property address, phone numbers, and starts the marketing).

---

## Cadence

- **Twice a week** — Monday and Thursday mornings work well.
- Each time, pull filings for the **last 4 days** (so nothing slips through
  between pulls). Overlap is fine — duplicates get removed automatically.
- Budget ~30–45 minutes per session.

---

## Step 1 — Open the court search

1. Go to: **https://fjdefile.phila.gov/efsfjd/zk_fjd_public_qry_03.zp_dktrpt_frames**
2. You'll see a disclaimer / search page. If asked to **Accept** a disclaimer,
   click Accept.
3. Find the **"Person/Company Name Search"** (Court of Common Pleas – Civil).

---

## Step 2 — Search each lender

The court won't let you list "all foreclosures." You search by the **bank's
name**, one at a time. Run a search for **each** name on this list:

1. Wells Fargo
2. U.S. Bank
3. Bank of America
4. Specialized Loan
5. Wilmington Savings
6. NewRez
7. MidFirst Bank
8. M&T Bank
9. PNC
10. Carrington Mortgage
11. PHH Mortgage
12. Mr. Cooper
13. Nationstar
14. Selene Finance
15. Rocket Mortgage
16. PennyMac
17. Lakeview Loan
18. Freedom Mortgage
19. U.S. Bank Trust

For **each** name:

1. Type the lender name in the **Last Name / Company Name** box.
2. Set **Beginning Date** = 4 days ago, **End Date** = today.
   (Use the date pickers; format is the calendar control.)
3. If a **"I'm not a robot" / reCAPTCHA** checkbox or image challenge appears,
   solve it. This is the part robots can't do — it's why you're here.
4. Click **Submit**.
5. You'll get a **results list**: Docket # | Name | Role | Case Type | Filed Date | Status.

**Only keep rows where the Case Type is a mortgage foreclosure** (it will say
"Mortgage Foreclosure" or similar). Ignore anything else the lender is involved in.

---

## Step 3 — Copy the results into the spreadsheet

Use the shared spreadsheet (or the CSV template below). For each keeper row,
fill one line:

| Column | What to put | Example |
|--------|-------------|---------|
| `lender` | the bank you searched | Wells Fargo |
| `docket` | the docket number | 250701234 |
| `homeowner_name` | the **defendant** (the person being sued — NOT the bank) | John Q Smith |
| `filing_date` | the Filed Date | 07/21/2026 |
| `property_address` | the property address **if it's shown** — otherwise leave blank | 1234 Market St |

Notes:
- The **homeowner is the defendant** (the "vs." side), never the lender.
- If a case caption reads `WELLS FARGO vs. SMITH, JOHN` → homeowner = John Smith.
- **Property address is optional** — if it's not on the results page, leave it
  blank. The system looks it up automatically from the homeowner's name.
- If you see the **same docket twice** (across two lender searches or two days),
  it's fine to skip the repeat — the system also de-duplicates.

---

## Step 4 — Save it where the system can find it

Save the finished file as a CSV named:

```
philly_preforeclosure_YYYY-MM-DD.csv
```

(e.g. `philly_preforeclosure_2026-07-24.csv`), and drop it in the Dropbox folder:

```
Philadelphia / pre_foreclosure /
```

That's the finish line for you. When the file is imported, the system:
1. Looks up each homeowner's property address from city property records.
2. Adds phone numbers (skip trace) and scores them.
3. Loads them into DataSift under the **"Pre-Foreclosure"** list, where the
   marketing sequence takes over.

---

## CSV template (copy this into a new file for each pull)

```
lender,docket,homeowner_name,filing_date,property_address
Wells Fargo,250701234,John Q Smith,07/21/2026,1234 Market St
```

Keep the first line (the header) exactly as shown; add one line per case below it.

---

## Quick rules

- **Homeowner = the person being sued**, not the bank.
- **Keep only mortgage-foreclosure case types.**
- **Property address is optional** — blank is fine.
- **Twice a week, last 4 days, all 19 lenders.**
- Duplicates are OK — don't stress about them.

---

## Troubleshooting

- **"No records found"** for a lender — normal; that bank just had no new
  filings this window. Move on.
- **Locked out after a CAPTCHA** — wait a minute and retry; don't hammer it.
- **Results page looks different** — copy whatever you can into the same
  columns; the docket number + homeowner name + filing date are the
  must-haves. Leave anything you can't find blank.
