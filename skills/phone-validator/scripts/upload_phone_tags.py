"""Upload validated phone tags to DataSift via Playwright automation.

Takes the phone_tags_for_datasift.csv output from validate_phones.py
and uploads it to DataSift using the "Update Data" -> "Tag phones by
phone number" workflow.

Usage:
    python upload_phone_tags.py --csv phone_tags_for_datasift.csv
    python upload_phone_tags.py --csv phone_tags_for_datasift.csv --headless

Requires: playwright, python-dotenv
          DATASIFT_EMAIL and DATASIFT_PASSWORD in .env or environment
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

try:
    from datasift_core import (
        create_browser, login, dismiss_popups, screenshot, wait_for_spa,
        DATASIFT_RECORDS_URL,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from datasift_core import (
        create_browser, login, dismiss_popups, screenshot, wait_for_spa,
        DATASIFT_RECORDS_URL,
    )

logger = logging.getLogger(__name__)


async def upload_phone_tags(
    csv_path: str,
    headless: bool = False,
) -> dict:
    """Upload phone tag CSV to DataSift.

    Navigates through: Upload File -> Update Data ->
    "Tag phones by phone number" -> upload CSV -> map fields -> finish.

    Args:
        csv_path: Path to phone_tags_for_datasift.csv
        headless: Run browser headless

    Returns:
        {"success": bool, "message": str}
    """
    csv_file = Path(csv_path).resolve()
    if not csv_file.exists():
        return {"success": False, "message": f"CSV not found: {csv_file}"}

    result = {"success": False, "message": ""}

    async with create_browser(headless=headless) as (browser, context, page):
        # Login
        if not await login(page):
            result["message"] = "Login failed"
            return result

        # Navigate to Records
        await page.goto(DATASIFT_RECORDS_URL, wait_until="domcontentloaded")
        await wait_for_spa(page, 5000)
        await dismiss_popups(page)

        # Click "Upload File" in sidebar
        upload_link = page.locator(
            'a:has-text("Upload File"), '
            'text="Upload File", '
            '[class*="upload" i]:has-text("Upload")'
        )
        if await upload_link.count() > 0:
            await upload_link.first.click()
            await page.wait_for_timeout(2000)
        else:
            result["message"] = "Upload File link not found"
            await screenshot(page, "phone_tags_no_upload")
            return result

        await screenshot(page, "phone_tags_upload_page")

        # Click "Update Data" (not "Add Data")
        update_btn = page.locator('text="Update Data"')
        if await update_btn.count() > 0:
            await update_btn.first.click()
            await page.wait_for_timeout(2000)
            logger.info("Selected 'Update Data' mode")
        else:
            result["message"] = "Could not find 'Update Data' button"
            await screenshot(page, "phone_tags_no_update_data")
            return result

        # Open the "What are you going to update?" dropdown
        dropdown = page.locator('text="Select one or more options"')
        if await dropdown.count() == 0:
            dropdown = page.locator('[class*="select"], [class*="Select"], [class*="dropdown"]')
        if await dropdown.count() > 0:
            await dropdown.first.click()
            await page.wait_for_timeout(1500)

        await screenshot(page, "phone_tags_dropdown_opened")

        # Select phone tagging option
        tag_option = None
        for text_match in [
            "Tag phones by phone number",
            "Tagging phone numbers by phone number",
            "Tag phone numbers by phone number",
        ]:
            loc = page.locator(f'text="{text_match}"')
            if await loc.count() > 0:
                tag_option = loc.first
                break

        if tag_option is None:
            # Regex fallback
            loc = page.locator('text=/[Tt]ag.*phone.*number/')
            if await loc.count() > 0:
                for i in range(await loc.count()):
                    opt = loc.nth(i)
                    opt_text = await opt.text_content()
                    if "address" not in opt_text.lower():
                        tag_option = opt
                        break
                if tag_option is None:
                    tag_option = loc.first

        if tag_option is not None:
            await tag_option.click(force=True)
            await page.wait_for_timeout(2000)
            logger.info("Selected phone tagging option")
        else:
            result["message"] = "Could not find phone tagging option"
            await screenshot(page, "phone_tags_no_tag_option")
            return result

        # Click Next Step
        next_btn = page.locator(
            'button:has-text("Next Step"), '
            'button:has-text("Next"), '
            'button:has-text("Continue")'
        )
        if await next_btn.count() > 0:
            await next_btn.first.click()
            await page.wait_for_timeout(2000)

        # Upload the CSV file
        file_input = page.locator('input[type="file"]')
        if await file_input.count() > 0:
            await file_input.first.set_input_files(str(csv_file))
            await page.wait_for_timeout(3000)
            logger.info("Uploaded CSV: %s", csv_file.name)
        else:
            result["message"] = "File input not found"
            await screenshot(page, "phone_tags_no_file_input")
            return result

        await screenshot(page, "phone_tags_file_uploaded")

        # Click through remaining wizard steps (mapping, review, finish)
        for step_num in range(5):
            await page.wait_for_timeout(2000)
            await dismiss_popups(page)

            # Check for finish/completion
            finish_btn = page.locator(
                'button:has-text("Finish Upload"), '
                'button:has-text("Finish"), '
                'button:has-text("Complete")'
            )
            if await finish_btn.count() > 0:
                await finish_btn.first.click()
                await page.wait_for_timeout(3000)
                result["success"] = True
                result["message"] = f"Phone tags uploaded from {csv_file.name}"
                logger.info("Upload completed")
                await screenshot(page, "phone_tags_completed")
                return result

            # Click Next Step if available
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await page.wait_for_timeout(2000)
                await screenshot(page, f"phone_tags_step_{step_num + 1}")

        result["message"] = "Wizard did not complete in expected steps"
        await screenshot(page, "phone_tags_incomplete")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Upload validated phone tags to DataSift"
    )
    parser.add_argument(
        "--csv", required=True,
        help="Path to phone_tags_for_datasift.csv",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    result = asyncio.run(upload_phone_tags(
        csv_path=args.csv,
        headless=args.headless,
    ))

    if result["success"]:
        print(f"Success: {result['message']}")
    else:
        print(f"Failed: {result['message']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
