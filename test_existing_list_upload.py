"""Upload repair CSV to DataSift to fix incomplete records (cleaned names)."""
import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "src")
from datasift_uploader import login, _screenshot, _dismiss_popups


async def upload_repair_csv(page, csv_path, list_name="Foreclosure"):
    """Upload a repair CSV with manual wizard step handling."""
    result = {"success": False, "message": ""}

    # Navigate to records
    if "/records" not in page.url:
        await page.goto("https://app.reisift.io/records/properties", wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await _dismiss_popups(page)

    # Step 1: Click Upload File
    print("Step 1: Opening upload wizard...")
    upload_btn = page.locator('text="Upload File"')
    if await upload_btn.count() > 0:
        await upload_btn.first.click()
        await page.wait_for_timeout(3000)
    else:
        print("ERROR: Upload File button not found")
        return result

    await _dismiss_popups(page)
    await _screenshot(page, "repair_step1_opened")

    # Select "Add Data"
    add_btn = page.locator('text="Add Data"')
    if await add_btn.count() > 0:
        await add_btn.first.click()
        await page.wait_for_timeout(1500)
        print("  Selected Add Data")

    # Open dropdown and select existing list
    dropdown = page.locator('text="Select one option"')
    if await dropdown.count() > 0:
        await dropdown.first.click()
        await page.wait_for_timeout(1500)
        existing_opt = page.locator('text="Adding properties to an existing list inside DataSift"')
        if await existing_opt.count() > 0:
            await existing_opt.first.click()
            await page.wait_for_timeout(1500)
            print("  Selected existing list mode")

    # Select the list
    list_dropdown = page.locator('text="Select a list"')
    if await list_dropdown.count() > 0:
        await list_dropdown.first.click()
        await page.wait_for_timeout(2000)
        match = page.locator(f'text="{list_name}"')
        if await match.count() > 0:
            await match.last.click()
            await page.wait_for_timeout(1500)
            print(f"  Selected list: {list_name}")

    await _screenshot(page, "repair_step1_filled")

    async def click_next():
        """Click Next Step button using Playwright native click."""
        await page.wait_for_timeout(1000)
        # Try multiple selectors
        for selector in [
            'button:has-text("Next Step")',
            'button:has-text("Next")',
            'text="Next Step"',
        ]:
            btn = page.locator(selector)
            if await btn.count() > 0:
                try:
                    await btn.first.click(timeout=5000)
                    await page.wait_for_timeout(2500)
                    print(f"    Clicked: {selector}")
                    return True
                except Exception as e:
                    print(f"    Click failed ({selector}): {e}")
                    # Try force click
                    try:
                        await btn.first.click(force=True, timeout=3000)
                        await page.wait_for_timeout(2500)
                        print(f"    Force clicked: {selector}")
                        return True
                    except Exception:
                        pass
        print("    WARNING: No Next Step button found")
        return False

    # Step 1 -> Step 2
    print("  Advancing to step 2...")
    await click_next()

    # Step 2: Tags - type Courthouse Data
    print("Step 2: Adding Courthouse Data tag...")
    await page.wait_for_timeout(1500)
    tag_input = page.locator('input[placeholder*="Search or add a new tag"]')
    if await tag_input.count() > 0:
        await tag_input.first.click()
        await page.wait_for_timeout(500)
        await tag_input.first.fill("")
        await page.wait_for_timeout(300)
        await tag_input.first.type("Courthouse Data", delay=50)
        await page.wait_for_timeout(1500)

        # Select from autocomplete
        tag_option = page.locator('text="Courthouse Data"')
        count = await tag_option.count()
        if count > 1:
            # Click the one that's in the dropdown (not the input)
            for i in range(count):
                box = await tag_option.nth(i).bounding_box()
                if box and box["y"] > 300:
                    await tag_option.nth(i).click()
                    print("  Tag selected from dropdown")
                    break
        elif count == 1:
            await tag_option.first.click()
            print("  Tag selected")
        else:
            await tag_input.first.press("Enter")
            print("  Tag added via Enter")
        await page.wait_for_timeout(1000)
    else:
        print("  WARNING: Tag input not found")

    await _screenshot(page, "repair_step2_tags")

    # Step 2 -> Step 3
    print("  Advancing to step 3...")
    await click_next()

    # Step 3: Upload file
    print("Step 3: Uploading CSV file...")
    await page.wait_for_timeout(1500)
    file_input = page.locator('input[type="file"]')
    if await file_input.count() == 0:
        await page.wait_for_timeout(3000)
        file_input = page.locator('input[type="file"]')

    if await file_input.count() > 0:
        await file_input.first.set_input_files(str(csv_path))
        print(f"  File selected: {csv_path.name}")
        await page.wait_for_timeout(3000)
    else:
        print("ERROR: No file input found")
        await _screenshot(page, "repair_step3_no_input")
        return result

    await _screenshot(page, "repair_step3_uploaded")

    # Step 3 -> Step 4
    print("  Advancing to step 4...")
    await click_next()

    # Step 4: Column mapping
    print("Step 4: Column mapping...")
    await page.wait_for_timeout(3000)
    await _screenshot(page, "repair_step4_mapping")

    # Check what's mapped and unmapped
    unmapped_text = await page.evaluate("""() => {
        const cards = document.querySelectorAll('div');
        const unmapped = [];
        for (const card of cards) {
            const rect = card.getBoundingClientRect();
            // Left side is unmapped columns (x < 500)
            if (rect.x > 50 && rect.x < 500 && rect.width > 100 && rect.width < 400) {
                const text = card.textContent?.trim();
                if (text && text.length < 100 && !text.includes('Search') && !text.includes('REUPLOAD')) {
                    unmapped.push(text.substring(0, 60));
                }
            }
        }
        return unmapped;
    }""")
    print(f"  Unmapped columns: {unmapped_text}")

    # Step 4 -> Step 5
    print("  Advancing to step 5...")
    clicked = await click_next()
    if not clicked:
        # Try scrolling and clicking
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)
        clicked = await click_next()
    print(f"  Next Step clicked: {clicked}")

    await page.wait_for_timeout(2000)
    await _screenshot(page, "repair_step5_review")

    # Step 5: Click Finish Upload
    print("Step 5: Finishing upload...")
    finish_clicked = await page.evaluate("""() => {
        const els = document.querySelectorAll('*');
        for (const el of els) {
            const text = el.textContent?.trim();
            if (text === 'Finish Upload' || text === 'Finish Upload >' || text === 'Finish Upload  >') {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    el.click();
                    return true;
                }
            }
        }
        const btns = document.querySelectorAll('button');
        for (const btn of btns) {
            if (btn.textContent?.includes('Finish Upload')) {
                btn.click();
                return true;
            }
        }
        return false;
    }""")
    print(f"  Finish Upload clicked: {finish_clicked}")

    if finish_clicked:
        await page.wait_for_timeout(5000)
        await _screenshot(page, "repair_step5_done")
        result["success"] = True
        result["message"] = "Upload submitted — check Activity tab"
    else:
        await _screenshot(page, "repair_step5_no_finish")
        # Maybe we're still on step 4 — try clicking Next one more time
        print("  Trying Next Step again...")
        await click_next()
        await page.wait_for_timeout(2000)
        await _screenshot(page, "repair_step5_retry")

        finish_clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                if (btn.textContent?.includes('Finish')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        if finish_clicked:
            await page.wait_for_timeout(5000)
            result["success"] = True
            result["message"] = "Upload submitted after retry"
        else:
            result["message"] = "Could not complete wizard — check screenshots"

    return result


async def main():
    from playwright.async_api import async_playwright

    repair_csv = Path("output/datasift_repair_names_full.csv")
    if not repair_csv.exists():
        print(f"ERROR: {repair_csv} not found")
        return

    print(f"=== Uploading {repair_csv.name} to fix incomplete records ===")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            email = os.environ.get("DATASIFT_EMAIL", "")
            password = os.environ.get("DATASIFT_PASSWORD", "")

            print("Logging in...")
            logged_in = await login(page, email, password)
            if not logged_in:
                print("LOGIN FAILED!")
                return
            print("Login OK\n")

            result = await upload_repair_csv(page, repair_csv)
            print(f"\nResult: {result}")

        finally:
            print("\nClosing browser in 5s...")
            await page.wait_for_timeout(5000)
            await browser.close()


asyncio.run(main())
