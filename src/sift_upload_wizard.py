"""Uploader for the CURRENT DataSift 6-step upload wizard (verified 2026-06-19).

The wizard is: Setup -> Enrichment -> Add tags -> Upload the file -> Map columns -> Review.
Rather than hardcode the step order (it has changed before — an Enrichment step was added),
this drives a STEP-MACHINE that detects each step by the controls present on the page:

  * Finish Upload button  -> Review     (finish, or stop if do_finish=False)
  * "Add Data" text       -> Setup      (pick "new list", purchase/phone dropdowns, list name)
  * tag input             -> Add tags   (apply the tag set — this is how FTM lands; the CSV
                                          Tags/Lists columns are NOT mappable targets here)
  * file input            -> Upload file (set_input_files, wait for "File uploaded!")
  * anything else         -> Next Step  (Enrichment, Map columns — required address fields
                                          auto-map, so mapping needs no action)

Because the Add-tags step applies the SAME tags to every record in the upload, callers should
upload ONE list per uniform tag set (e.g. per county). Use do_finish=False to stop at Review
and screenshot for verification before committing.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RECORDS_URL = "https://app.reisift.io/records"
_NEXT = ['button:has-text("Next Step")', 'button:has-text("Next")']
_POPUPS = ['button:has-text("NO, THANKS")', 'button:has-text("No, thanks")', 'button:has-text("No, Thanks")']


async def _shot(page, base, label):
    if not base:
        return
    try:
        await page.screenshot(path=f"{base}_{label}.png")
    except Exception as e:
        logger.debug("screenshot %s failed: %s", label, e)


async def _dismiss_popups(page):
    for sel in _POPUPS:
        try:
            b = page.locator(sel)
            if await b.count() > 0:
                await b.first.click()
                await page.wait_for_timeout(400)
        except Exception:
            pass


async def _click_next(page):
    for sel in _NEXT:
        b = page.locator(sel)
        if await b.count() > 0:
            try:
                await b.first.click(timeout=8000)
                await page.wait_for_timeout(2500)
                return True
            except Exception as e:
                logger.debug("Next click %s failed: %s", sel, e)
    return False


async def _fill_setup(page, list_name, existing_list=False, has_phones=False):
    """Fill the Setup step. If existing_list, select 'Adding properties to an existing
    list' and pick the list named `list_name`; else create a new list named `list_name`.

    `has_phones` answers "DOES DATA CONTAIN PHONE NUMBERS?". Answering No on a CSV
    that carries Phone columns makes DataSift silently DROP the phones while still
    applying tags/addresses (root cause of the 2026-08-04 zero-phone pool: skip-trace
    merges reported OK, emails/tags landed, phones never did)."""
    b = page.locator('text="Add Data"')
    if await b.count() > 0:
        await b.first.click()
        await page.wait_for_timeout(1000)
    dd = page.locator('text="Select one option"')
    if await dd.count() > 0:
        await dd.first.click()
        await page.wait_for_timeout(900)
        if existing_list:
            opt = page.locator('text="Adding properties to an existing list inside DataSift"')
            if await opt.count() == 0:
                opt = page.locator('text="Adding properties to an existing list"')
        else:
            opt = page.locator('text="Uploading a new list not in DataSift yet"')
        if await opt.count() > 0:
            await opt.first.click()
            await page.wait_for_timeout(900)
    try:
        pd = page.locator('text="WHERE DID YOU PURCHASE THIS LIST?"').locator('..').locator('text="Select an option"')
        if await pd.count() > 0:
            await pd.first.click()
            await page.wait_for_timeout(400)
            o = page.locator('text="Other"')
            if await o.count() > 0:
                await o.first.click()
            await page.wait_for_timeout(400)
    except Exception:
        pass
    try:
        # Label is "DOES THIS DATA CONTAIN PHONE NUMBERS?" — the old exact-text
        # locator (without THIS) never matched, so the answer silently stayed on
        # the form default "No, not skiptraced" on every run ever.
        q = page.locator('text=/DOES (THIS )?DATA CONTAIN PHONE NUMBERS/i')
        if await q.count() > 0:
            dd = q.first.locator('..').locator('text=/Select an option|not skiptraced|skiptraced/i')
            if await dd.count() == 0:
                dd = q.first.locator('..')
            await dd.last.click(force=True)
            await page.wait_for_timeout(600)
            want = "Yes" if has_phones else "No"
            n = page.locator(f'text=/^{want}\\b/')
            if await n.count() > 0:
                await n.first.click(force=True)
                logger.info("Setup: DOES THIS DATA CONTAIN PHONE NUMBERS? -> %s", want)
            else:
                logger.warning("Setup: could not find %r option for phone question", want)
            await page.wait_for_timeout(400)
        else:
            logger.warning("Setup: phone question not found on page")
    except Exception as e:
        logger.warning("Setup: phone-question step failed: %s", e)
    if existing_list:
        # "ASSOCIATE DATA WITH LIST" -> open the "Select a list" dropdown and pick list_name
        dropdown = page.locator('text="Select a list"')
        if await dropdown.count() > 0:
            await dropdown.first.click()
            await page.wait_for_timeout(1200)
            # type to filter if there's a search box inside the dropdown
            search = page.locator('input[placeholder*="Search"]')
            if await search.count() > 0:
                try:
                    await search.last.fill(list_name)
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass
            # click the exact-text option for the list
            opt = page.locator(f'text="{list_name}"')
            if await opt.count() > 0:
                await opt.last.click()
                await page.wait_for_timeout(800)
    else:
        li = page.locator('input[placeholder*="Enter new list name"], input[placeholder*="list name"]')
        if await li.count() > 0:
            await li.first.fill(list_name)
            await page.wait_for_timeout(400)


async def _chip_present(page, tag):
    """True if `tag` is committed as a chip (exact-text leaf element)."""
    return await page.evaluate(
        """(tag) => [...document.querySelectorAll('div,span')].some(el => {
            const r = el.getBoundingClientRect();
            return el.textContent.trim() === tag && r.width > 0 && r.height > 0 && el.children.length === 0;
        })""",
        tag,
    )


async def _add_tag(page, tag, tag_input):
    """Add one tag in the Add-tags step via the 'Add' control (works for both new and
    existing tags — DataSift dedupes by name), with an Enter fallback. Verifies a chip
    landed and retries once with Enter if not. Returns the method(s) used."""
    async def _type():
        await tag_input.click()
        await page.wait_for_timeout(200)
        await tag_input.fill("")
        await page.wait_for_timeout(150)
        await tag_input.type(tag, delay=40)
        await page.wait_for_timeout(1100)

    await _type()
    how = await page.evaluate(
        """() => {
            for (const el of document.querySelectorAll('div,span,a,button,p')) {
                const r = el.getBoundingClientRect();
                if (el.textContent.trim() === 'Add' && r.width > 0 && r.width < 90
                    && r.x > 600 && r.y > 90 && r.y < 500) { el.click(); return 'add'; }
            }
            return '';
        }"""
    )
    if not how:
        await tag_input.press("Enter")
        how = "enter"
    await page.wait_for_timeout(900)
    if not await _chip_present(page, tag):
        await _type()
        await tag_input.press("Enter")
        await page.wait_for_timeout(900)
        how += "+retry"
    return how


async def open_upload_wizard(page):
    if "/records" not in page.url:
        await page.goto(RECORDS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(8000)
    await _dismiss_popups(page)
    ub = page.locator('text="Upload File"')
    if await ub.count() == 0:
        return False
    await ub.first.click()
    await page.wait_for_timeout(3500)
    await _dismiss_popups(page)
    return True


async def upload_csv_v2(page, csv_path, list_name, tags, *, do_finish=False, existing_list=False, shot_base=None):
    """Drive the wizard for one CSV + uniform tag set. Returns a result dict.
    existing_list=True selects an EXISTING list named `list_name` instead of creating one."""
    csv_path = Path(csv_path)
    result = {
        "success": False, "finished": False, "list_name": list_name,
        "tags": list(tags), "tags_added": [], "message": "",
    }
    # Does this CSV actually carry phone numbers? Drives the Setup answer and the
    # Review-step mapping check — a phones CSV whose Phone columns didn't map must
    # NEVER be finished silently.
    has_phones = False
    try:
        import csv as _csv
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rd = _csv.DictReader(fh)
            phone_cols = [c for c in (rd.fieldnames or []) if c.strip().lower().startswith("phone")]
            if phone_cols:
                for row in rd:
                    if any((row.get(c) or "").strip() for c in phone_cols):
                        has_phones = True
                        break
    except Exception as e:
        logger.warning("Could not inspect %s for phone columns: %s", csv_path.name, e)
    result["has_phones"] = has_phones
    setup_done = tags_done = file_done = False

    for i in range(16):
        await page.wait_for_timeout(1200)
        await _dismiss_popups(page)

        finish = page.locator('button:has-text("Finish Upload")')
        setup_marker = page.locator('text="Add Data"')
        tag_input = page.locator('input[placeholder*="Search or add a new tag"]')
        file_input = page.locator('input[type="file"]')

        if await finish.count() > 0:
            await _shot(page, shot_base, "review")
            present = {t: (await page.locator(f'text="{t}"').count() > 0) for t in tags}
            missing = [t for t, ok in present.items() if not ok]
            result["review_tags_present"] = present
            logger.info("Review reached (list=%r). tags present=%s | missing=%s",
                        list_name, present, missing)
            if do_finish and missing:
                result["success"] = False
                result["message"] = f"NOT finished — tags missing at review: {missing}"
                logger.warning(result["message"])
                return result
            if has_phones:
                # The Review pane lists every MAPPED column as "csvcol / target"
                # (e.g. "Phone 1 / Phone 1"). Match that row format specifically —
                # a bare "Phone 1" can be leftover DOM from the Map step. If the
                # Phone columns didn't map, finishing would upsert the records
                # WITHOUT their phones — refuse instead of finishing.
                phone_mapped = await page.locator('text=/Phone \\d+ \\//').count() > 0
                result["review_phone_mapping"] = phone_mapped
                logger.info("Review phone-mapping check: %s", "OK" if phone_mapped else "MISSING")
                if do_finish and not phone_mapped:
                    result["success"] = False
                    result["message"] = ("NOT finished — CSV carries phones but Phone "
                                         "columns are not mapped at review")
                    logger.warning(result["message"])
                    return result
            if do_finish:
                await finish.first.click()
                await page.wait_for_timeout(7000)
                await _shot(page, shot_base, "finished")
                result["finished"] = True
                result["success"] = True
                result["message"] = "Finished upload"
                logger.info("Clicked Finish Upload for %r", list_name)
            else:
                result["success"] = True
                result["message"] = ("Stopped at Review — all tags present" if not missing
                                     else f"Stopped at Review — MISSING {missing}")
            return result

        if (not setup_done) and await setup_marker.count() > 0:
            logger.info("Setup: list=%r (existing=%s)", list_name, existing_list)
            await _fill_setup(page, list_name, existing_list=existing_list,
                              has_phones=has_phones)
            await _shot(page, shot_base, "setup")
            setup_done = True
            await _click_next(page)
            continue

        if (not tags_done) and await tag_input.count() > 0:
            logger.info("Add tags: %s", tags)
            for t in tags:
                how = await _add_tag(page, t, tag_input.first)
                result["tags_added"].append(f"{t}({how})")
                logger.info("  + %r -> %s", t, how)
            await _shot(page, shot_base, "tags")
            tags_done = True
            await _click_next(page)
            continue

        if (not file_done) and await file_input.count() > 0:
            logger.info("Upload file: %s", csv_path.name)
            await file_input.first.set_input_files(str(csv_path))
            uploaded = False
            for _ in range(25):
                await page.wait_for_timeout(1000)
                if (await page.locator('text="File uploaded!"').count() > 0
                        or await page.locator('text="100%"').count() > 0):
                    uploaded = True
                    break
            await _shot(page, shot_base, "file")
            if not uploaded:
                logger.warning("File upload progress not confirmed; continuing anyway")
            file_done = True
            await page.wait_for_timeout(1500)
            await _click_next(page)
            continue

        # Enrichment / Map columns / unknown -> advance
        logger.info("Pass-through step (iter %d) -> Next", i)
        if not await _click_next(page):
            result["message"] = f"Stuck at iter {i}: no Next/Finish (setup={setup_done} tags={tags_done} file={file_done})"
            await _shot(page, shot_base, f"stuck_{i}")
            logger.warning(result["message"])
            return result

    result["message"] = "Exceeded step budget (16 iters)"
    return result


async def _fill_phone_setup(page):
    """Setup step for the 'Tag phones by phone number' flow: Update Data -> select
    'Tagging phones by phone numbers'."""
    ud = page.locator('text="Update Data"')
    if await ud.count() > 0:
        await ud.first.click()
        await page.wait_for_timeout(1000)
    # "What are you going to update?" is a MULTI-select ("Select one or more options")
    dd = page.locator('text="Select one or more options"')
    if await dd.count() == 0:
        dd = page.locator('text="Select one option"')
    if await dd.count() > 0:
        await dd.first.click()
        await page.wait_for_timeout(1000)
        for label in ("Tagging phones by phone numbers", "Tag phones by phone number",
                      "Tagging phones"):
            opt = page.locator(f'text="{label}"')
            if await opt.count() > 0:
                await opt.first.click()
                await page.wait_for_timeout(900)
                break
        # close the multi-select dropdown (click the section heading, not Escape)
        try:
            await page.locator('text="WHAT ARE YOU GOING TO UPDATE?"').first.click()
            await page.wait_for_timeout(500)
        except Exception:
            pass


async def upload_phone_tags(page, csv_path, *, do_finish=False, shot_base=None):
    """Drive the Update-Data 'Tagging phones by phone numbers' wizard to apply per-phone
    tier tags from a Phone Number / Phone Tags CSV. Step-machine; stops at Review unless
    do_finish."""
    csv_path = Path(csv_path)
    result = {"success": False, "finished": False, "message": ""}
    setup_done = file_done = False
    for i in range(14):
        await page.wait_for_timeout(1200)
        await _dismiss_popups(page)
        finish = page.locator('button:has-text("Finish Upload")')
        update_marker = page.locator('text="Update Data"')
        file_input = page.locator('input[type="file"]')

        if await finish.count() > 0:
            await _shot(page, shot_base, "review")
            if do_finish:
                await finish.first.click()
                await page.wait_for_timeout(7000)
                await _shot(page, shot_base, "finished")
                result.update(finished=True, success=True, message="Finished phone-tag upload")
            else:
                result.update(success=True, message="Stopped at Review (do_finish=False)")
            return result

        if (not setup_done) and await update_marker.count() > 0:
            logger.info("Phone-tag setup: Update Data -> Tagging phones by phone numbers")
            await _fill_phone_setup(page)
            await _shot(page, shot_base, "setup")
            setup_done = True
            await _click_next(page)
            continue

        if (not file_done) and await file_input.count() > 0:
            logger.info("Phone-tag upload file: %s", csv_path.name)
            await file_input.first.set_input_files(str(csv_path))
            for _ in range(25):
                await page.wait_for_timeout(1000)
                if (await page.locator('text="File uploaded!"').count() > 0
                        or await page.locator('text="100%"').count() > 0):
                    break
            await _shot(page, shot_base, "file")
            file_done = True
            await page.wait_for_timeout(1500)
            await _click_next(page)
            continue

        # Map columns / pass-through
        await _shot(page, shot_base, f"step_{i}")
        logger.info("Phone-tag pass-through step %d -> Next", i)
        if not await _click_next(page):
            await _shot(page, shot_base, f"stuck_{i}")
            result["message"] = f"Stuck at step {i} (setup={setup_done} file={file_done})"
            return result

    result["message"] = "Exceeded step budget"
    return result


async def run_phone_tag_upload(csv_path, *, do_finish=False, headless=True, shot_base=None):
    """Open a browser, log into DataSift, and run the phone-tag upload wizard."""
    import config
    from datasift_core import create_browser, login

    async with create_browser(headless=headless) as (browser, context, page):
        if not await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD):
            return {"success": False, "message": "DataSift login failed"}
        if not await open_upload_wizard(page):
            return {"success": False, "message": "Could not open the upload wizard"}
        return await upload_phone_tags(page, csv_path, do_finish=do_finish, shot_base=shot_base)


async def run_upload(csv_path, list_name, tags, *, do_finish=False, existing_list=False, headless=True, shot_base=None):
    """Open a browser, log into DataSift (DATASIFT_EMAIL), and run the wizard."""
    import config
    from datasift_core import create_browser, login

    async with create_browser(headless=headless) as (browser, context, page):
        ok = await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        if not ok:
            return {"success": False, "message": "DataSift login failed"}
        if not await open_upload_wizard(page):
            return {"success": False, "message": "Could not open the upload wizard"}
        return await upload_csv_v2(page, csv_path, list_name, tags,
                                   do_finish=do_finish, existing_list=existing_list, shot_base=shot_base)


def build_phones_by_address_csv(merge_csvs, out_path):
    """Union the Phone 1-9 columns of Add-Data merge CSVs into one
    phones-by-address CSV (Property address + Phone 1..10 per address)."""
    import csv as _csv
    from collections import defaultdict
    phones = defaultdict(list)
    meta = {}
    for f in merge_csvs:
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for r in _csv.DictReader(fh):
                a = (r.get("Property Street Address") or "").strip()
                if not a:
                    continue
                meta[a] = (r.get("Property City"), r.get("Property State"),
                           r.get("Property ZIP Code"))
                for i in range(1, 10):
                    p = (r.get(f"Phone {i}") or "").strip()
                    if p and p not in phones[a]:
                        phones[a].append(p)
    if not phones:
        return None, 0
    maxn = min(max(len(v) for v in phones.values()), 10)
    fields = (["Property Street Address", "Property City", "Property State",
               "Property ZIP Code"] + [f"Phone {i}" for i in range(1, maxn + 1)])
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for a, ps in phones.items():
            row = {"Property Street Address": a, "Property City": meta[a][0],
                   "Property State": meta[a][1], "Property ZIP Code": meta[a][2]}
            for i, p in enumerate(ps[:maxn], 1):
                row[f"Phone {i}"] = p
            w.writerow(row)
    return out_path, sum(len(v[:maxn]) for v in phones.values())


async def upload_phones_by_address(page, csv_path, *, shot_base=None):
    """Update Data -> 'Upload phone numbers by property address'.

    THE working path for writing phones onto existing records. The Add-Data
    merge wizard maps Phone columns at Review yet DataSift drops them on
    ingest (verified 2026-08-04: emails/tags from the same file land, phones
    do not) — this purpose-built flow is what actually writes them."""
    result = {"success": False, "message": ""}
    await page.goto(RECORDS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    await _dismiss_popups(page)
    ub = page.locator('text="Upload File"')
    if await ub.count() == 0:
        result["message"] = "no Upload File link"
        return result
    await ub.first.click()
    await page.wait_for_timeout(2500)
    await page.locator('text="Update Data"').first.click()
    await page.wait_for_timeout(2000)
    dd = page.locator('text="Select one or more options"')
    if await dd.count() > 0:
        await dd.first.click()
        await page.wait_for_timeout(1500)
    opt = page.locator('text="Upload phone numbers by property address"')
    if await opt.count() == 0:
        result["message"] = "phones-by-address option not found"
        return result
    await opt.first.click(force=True)
    await page.wait_for_timeout(1200)
    try:  # close the multi-select via the heading (Escape clears it)
        await page.locator('text="WHAT ARE YOU GOING TO UPDATE?"').first.click()
        await page.wait_for_timeout(500)
    except Exception:
        pass
    file_done = False
    for step in range(10):
        await page.wait_for_timeout(1500)
        await _dismiss_popups(page)
        finish = page.locator('button:has-text("Finish Upload")')
        if await finish.count() > 0:
            mapped = await page.locator('text=/Phone \\d+ \\//').count()
            await _shot(page, shot_base, "review")
            if mapped == 0:
                result["message"] = "Phone columns not mapped at review"
                logger.warning(result["message"])
                return result
            await finish.first.click()
            await page.wait_for_timeout(6000)
            result.update(success=True,
                          message=f"phones-by-address finished ({mapped} phone cols mapped)")
            logger.info(result["message"])
            return result
        fi = page.locator('input[type="file"]')
        if (not file_done) and await fi.count() > 0:
            await fi.first.set_input_files(str(csv_path))
            file_done = True
            for _ in range(20):
                await page.wait_for_timeout(1000)
                if await page.locator('text="File uploaded!"').count() > 0:
                    break
        nb = page.locator('button:has-text("Next Step")')
        if await nb.count() > 0:
            await nb.first.click()
            await page.wait_for_timeout(2500)
        else:
            result["message"] = f"stuck at step {step}"
            return result
    result["message"] = "step budget exceeded"
    return result
