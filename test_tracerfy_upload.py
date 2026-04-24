"""Test Tracerfy skip trace + DataSift upload with phone/email columns.

1. Creates a few test NoticeData records
2. Runs Tracerfy batch skip trace on them
3. Generates DataSift CSV with phone/email columns
4. Uploads to DataSift and checks column mapping
"""
import asyncio
import csv
import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "src")

from models import NoticeData
from tracerfy_skip_tracer import batch_skip_trace
from datasift_formatter import DATASIFT_COLUMNS, _build_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Create test records
notices = [
    NoticeData(
        notice_type="foreclosure",
        county="Knox",
        owner_name="Eric Yopp",
        address="1942 Tree Tops Ln",
        city="Seymour",
        state="TN",
        zip="37865",
        date_added="2026-03-25",
        source_url="https://tnpublicnotice.com/test/001",
    ),
    NoticeData(
        notice_type="foreclosure",
        county="Knox",
        owner_name="John Smith",
        address="123 Main St",
        city="Knoxville",
        state="TN",
        zip="37902",
        date_added="2026-03-25",
        source_url="https://tnpublicnotice.com/test/002",
    ),
]

# Step 1: Run Tracerfy batch skip trace
print("\n=== STEP 1: Tracerfy Batch Skip Trace ===")
stats = batch_skip_trace(notices)
print(f"\nStats: {stats}")

# Step 2: Show what phone/email data was populated
print("\n=== STEP 2: Phone/Email Data on Records ===")
for n in notices:
    print(f"\n{n.owner_name}:")
    print(f"  primary_phone: {n.primary_phone}")
    print(f"  mobile_1: {n.mobile_1}")
    print(f"  mobile_2: {n.mobile_2}")
    print(f"  landline_1: {n.landline_1}")
    print(f"  landline_2: {n.landline_2}")
    print(f"  landline_3: {n.landline_3}")
    print(f"  email_1: {n.email_1}")
    print(f"  email_2: {n.email_2}")
    print(f"  email_3: {n.email_3}")

# Step 3: Generate DataSift CSV
print("\n=== STEP 3: Generate DataSift CSV ===")
csv_path = Path("output/test_tracerfy_upload.csv")
csv_path.parent.mkdir(exist_ok=True)
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
    writer.writeheader()
    for n in notices:
        row = _build_row(n)
        writer.writerow(row)

print(f"CSV written to: {csv_path}")
print(f"Columns ({len(DATASIFT_COLUMNS)}):")
for i, col in enumerate(DATASIFT_COLUMNS):
    print(f"  {i+1}. {col}")

# Read back and verify phone columns have data
print("\n=== STEP 4: Verify CSV Content ===")
with open(csv_path, "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        name = f"{row['Owner First Name']} {row['Owner Last Name']}"
        phones = [row.get(f"Phone {i}", "") for i in range(1, 10)]
        emails = [row.get(f"Email {i}", "") for i in range(1, 6)]
        phones_filled = [p for p in phones if p]
        emails_filled = [e for e in emails if e]
        print(f"\n{name}:")
        print(f"  Phones: {phones_filled}")
        print(f"  Emails: {emails_filled}")

# Step 5: Upload to DataSift (headed browser for visibility)
print("\n=== STEP 5: Upload to DataSift ===")
print("Uploading CSV with phone/email columns to DataSift...")
print("Watch the browser to verify column auto-mapping in Step 4 of the wizard.")

async def upload_test():
    from datasift_uploader import (
        login, upload_csv,
    )
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        # Login
        import config as cfg
        login_ok = await login(page, cfg.DATASIFT_EMAIL, cfg.DATASIFT_PASSWORD)
        if not login_ok:
            print("ERROR: DataSift login failed")
            await browser.close()
            return

        # Upload CSV to existing Foreclosure list
        result = await upload_csv(
            page,
            csv_path,
            list_name="Foreclosure",
            existing_list=True,
        )
        print(f"\nUpload result: {result}")

        # Pause so user can inspect the result in the browser
        print("\nBrowser will stay open for 30s so you can verify column mapping...")
        await page.wait_for_timeout(30000)
        await browser.close()

asyncio.run(upload_test())
