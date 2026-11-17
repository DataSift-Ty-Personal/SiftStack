"""Unattended first-to-market run: scrape -> enrich -> CSV -> DataSift -> Slack.

This is the single entry point the scheduler calls. Everything it needs comes
from environment variables, it writes its state to a directory that can live on
a mounted volume, and it exits non-zero on any outcome that a human needs to
look at.

    python src/ftm_runner.py                      # dry run, nothing written
    python src/ftm_runner.py --commit             # the real thing
    python src/ftm_runner.py --stages notices     # one stage only
    python src/ftm_runner.py --doctor             # wiring check, no scraping

WHY A SEPARATE RUNNER instead of `main.py daily --upload-datasift`:

  * The upload path in main.py drives the DataSift web UI with Playwright.
    That is fine when a human is watching a browser and hopeless at 4am: the
    wizard changes, a modal steals focus, and the run hangs instead of failing.
    This runner pushes over the API (datasift_api_upload), which returns counts.
  * A scheduled run has to be loud about doing nothing. Every stage here
    reports a status, and "0 notices" is a FAILURE, not a quiet success. The
    scrape sat dead for 19 days in July 2026 while every run reported success.
  * The scheduler needs one exit code for the whole chain.

EXIT CODES
  0  everything that ran, worked
  1  a stage failed (details in the summary and in Slack)
  2  misconfiguration caught before any work started (doctor-style failure)
  3  the run was blocked by egress (the site refuses this IP), actionable and
     distinct, because no amount of retrying fixes it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from captcha_solver import NoticeAccessBlocked  # noqa: E402

logger = logging.getLogger("ftm_runner")

# Order matters when several are named: `liens` stages what `county` uploads, and
# `sold` runs last so a lead uploaded this run onto an old Sold record is revived
# in the same run instead of sitting dead until tomorrow.
#
# The stages after the first five are OFF: each one is a switch, and it runs only
# when its name is added to --stages (FTM_ARGS). The same ordering rule holds for
# every stage that only PREPARES rows (appointments, tax_sale_report,
# code_violations): it belongs BEFORE `county`, which uploads what they staged and
# only then marks it handled. `deeds` reads the day's notice file and reuses the
# recorder session `liens` opened, so it goes after both.
TODAY_STAGES = ("notices", "capture", "liens", "county", "sold")
ALL_STAGES = ("notices", "tax_sale", "tax_delinquent", "public_notices",
              "capture", "liens", "appointments", "tax_sale_report", "code_violations",
              "county", "deeds", "sold_reentry", "sold_audit", "sold")
# "capture" is in the default set even though it uploads nothing. The Knox City
# condemnation agendas are OVERWRITTEN at the source: a cycle that is not
# pulled while it is live cannot be recovered later, unlike foreclosure and
# probate which carry a 12 month archive. Capturing daily is cheap and losing
# a cycle is permanent.
DEFAULT_STAGES = ("notices", "capture")


# ── result plumbing ───────────────────────────────────────────────────


@dataclass
class StageResult:
    name: str
    status: str = "skipped"        # ok | failed | skipped | empty
    detail: str = ""
    records: int = 0
    uploaded: int = 0
    seconds: float = 0.0
    csv_path: str = ""
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")


@dataclass
class RunResult:
    started: str = ""
    finished: str = ""
    committed: bool = False
    stages: list = field(default_factory=list)
    blocked: bool = False

    @property
    def failed(self) -> bool:
        return any(not s.ok for s in self.stages)

    def exit_code(self) -> int:
        if self.blocked:
            return 3
        return 1 if self.failed else 0


# ── stage: tnpublicnotice scrape ──────────────────────────────────────


# How many different proxy sessions a run may try when the login page itself
# cannot be reached. Each attempt costs up to ~4 minutes of timeouts.
LOGIN_EGRESS_ATTEMPTS = 3
# Zero survivors out of this many scraped notices (or more) is treated as a
# broken enrichment step. Fewer is an ordinary quiet day.
ALL_DROPPED_SUSPECT = int(os.environ.get("FTM_ALL_DROPPED_SUSPECT", "5") or 5)

# Start warning this many days before the site subscription runs out. The
# account has been on both a 365-day and a 30-day product, so the window has to
# be short enough to mean something on the monthly one.
# The account is on the 30 Day product set to auto-renew (Ty, 2026-11-11), so a
# 14-day window would warn on half of all days and stop meaning anything. Three
# days is late enough that a renewal which has NOT happened is worth a line.
SUBSCRIPTION_WARN_DAYS = int(os.environ.get("SUBSCRIPTION_WARN_DAYS", "3") or 3)


def _subscription_warning(res, today: datetime | None = None) -> None:
    """Add a run warning when the Smart Search subscription is close to lapsing."""
    import scraper

    expires = (scraper.SUBSCRIPTION or {}).get("expires") or ""
    if not expires:
        return
    try:
        left = (datetime.strptime(expires, "%Y-%m-%d").date()
                - (today or datetime.now()).date()).days
    except ValueError:
        return
    if left <= SUBSCRIPTION_WARN_DAYS:
        res.warnings.append(
            f"The tnpublicnotice subscription expires {expires} ({left} days left). "
            "Renew it before then or the scrape stops."
        )


def _next_proxy_session_suffix() -> str:
    """Return a rotating suffix so each run gets a different upstream IP.

    The counter lives on the state volume, so rotation survives a redeploy.
    Apify session ids allow only letters, digits, `_` and `.`, so the suffix is
    plain digits; proxy_resolver sanitizes anyway.
    """
    path = config.STATE_DIR / "proxy_session.json"
    n = 0
    try:
        n = int(json.loads(path.read_text(encoding="utf-8")).get("n", 0))
    except Exception:
        n = 0
    n = (n + 1) % 1000
    try:
        path.write_text(json.dumps({"n": n}), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not persist proxy session counter: %s", exc)
    return f"_{n}"


def _revert_seen(seen_ids: dict, before: set) -> None:
    """Drop ids added during a failed run so they are re-scraped next time."""
    added = [k for k in seen_ids if k not in before]
    for k in added:
        seen_ids.pop(k, None)
    if added:
        logger.warning(
            "Reverted %d seen-id(s) from the failed run so they are not skipped "
            "next time (they were scraped but never uploaded)", len(added),
        )


def _new_filing() -> dict:
    """The extra argument the uploader needs for the Appeared Again rule, and nothing when the
    rule is off. Returning no argument at all (not new_filing=False) keeps the default upload
    call identical to the one in production before the rule existed."""
    if not os.environ.get("FTM_TAG_APPEARED_AGAIN", "").strip():
        return {}                      # the default: nothing is imported, nothing is passed
    try:
        import ftm_intake
        return {"new_filing": True} if ftm_intake.appeared_again_on() else {}
    except Exception:
        return {}


def _intake(notices: list, res: "StageResult", stamp: str, screen=None):
    """Run the intake switches that are on. Returns (notices, extras, what was left out).

    `extras` is None when no intake switch is set, which tells the caller to write the upload
    file the way it always has. A failure in here never costs the run its upload: the notices
    come back untouched and the reason goes in the stage's warnings."""
    if not (os.environ.get("FTM_INTAKE_SCREEN", "").strip() or os.environ.get("FTM_NOTICE_TEXT", "").strip()):
        return notices, None, ""       # the default: the intake module is not even imported
    try:
        import ftm_intake
        if not (ftm_intake.enabled() or ftm_intake.notice_text_mode() != "off"):
            return notices, None, ""
        extras: dict = {}
        said = ""
        if ftm_intake.enabled():
            verdict = (screen or ftm_intake.screen)(notices)
            res.warnings.extend(verdict.get("warnings") or [])
            if verdict.get("rejected"):
                path = config.OUTPUT_DIR / f"ftm_intake_rejected_{stamp}.csv"
                ftm_intake.write_rejects(verdict["rejected"], path)
                said = "intake left out " + ", ".join(
                    "%d %s" % (n, why) for why, n in sorted(verdict["dropped"].items()))
                logger.info("Intake screen: %s (%s)", said, path)
            notices, extras = verdict["kept"], verdict["extras"]
        ftm_intake.text_extras(notices, extras)
        return notices, extras, said
    except Exception as exc:
        logger.warning("Intake did not run: %s", exc)
        res.warnings.append(f"Intake did not run ({type(exc).__name__}: {exc}); the notices were uploaded unscreened.")
        return notices, None, ""


def _stage_notices(args) -> StageResult:
    """Scrape the saved searches, enrich, write the upload CSV, push it."""
    from data_formatter import deduplicate
    from datasift_formatter import write_datasift_csv
    from proxy_resolver import describe as describe_proxy, resolve_proxy_url
    from scraper import load_seen_ids, save_seen_ids, scrape_all

    res = StageResult(name="notices")
    t0 = time.time()

    searches = list(config.SAVED_SEARCHES)
    if args.counties:
        want = {c.strip().lower() for c in args.counties.split(",") if c.strip()}
        searches = [s for s in searches if s.county.lower() in want]
    if args.types:
        want = {t.strip().lower() for t in args.types.split(",") if t.strip()}
        searches = [s for s in searches if s.notice_type.lower() in want]
    if not searches:
        res.status = "failed"
        res.detail = "no saved searches matched --counties/--types"
        return res

    logger.info(
        "Searches: %s", ", ".join(s.saved_search_name for s in searches)
    )
    # Rotate the upstream IP per process. The site blocks an IP by VOLUME: a
    # backfill month burned one sticky Apify IP after ~204 notices and then
    # every later month failed instantly against that same dead IP. Each run
    # now takes the next session id, so consecutive jobs land on different
    # upstream addresses instead of grinding one into the ground.
    from scraper import LoginFailed, SubscriptionExpired

    # seen_ids is NOT persisted by the scrape. It marks a notice as "already
    # handled", but the upload happens after the whole scrape finishes, so
    # persisting mid-scrape means an abort leaves notices flagged as done that
    # were never sent anywhere. That is exactly what the egress block caused:
    # 204 notices marked seen, zero uploaded, and a retry would have skipped
    # every one of them. The runner persists at the end, only on success.
    seen_ids = load_seen_ids()
    before_seen = set(seen_ids)

    # A LOGIN THAT TIMES OUT IS A DEAD PROXY IP, NOT AN EMPTY FEED. The scraper
    # retries the login page three times on the SAME upstream address, which
    # cannot help when that address is the problem, and then returned [] so the
    # run was reported as "0 notices scraped, check the saved searches". Seen
    # live 2026-11-11: one session timed out for 255.9s while the sessions on
    # either side of it logged in within 10 seconds, and that morning's
    # scheduled run had "found nothing" in 255.8s. So a failed login rotates to
    # the next session and tries again, and only after every attempt fails is
    # it reported, as what it is.
    login_attempts = 1 if args.no_proxy else LOGIN_EGRESS_ATTEMPTS
    notices = None
    for attempt in range(1, login_attempts + 1):
        session_suffix = "" if args.no_proxy else _next_proxy_session_suffix()
        proxy_url = None if args.no_proxy else resolve_proxy_url(session_suffix=session_suffix)
        logger.info("Egress: %s", "DIRECT (--no-proxy)" if args.no_proxy
                    else describe_proxy(session_suffix))
        try:
            notices = asyncio.run(scrape_all(
                mode=args.mode,
                searches=searches,
                proxy_url=proxy_url,
                llm_api_key=config.ANTHROPIC_API_KEY or None,
                since_date_override=args.since,
                max_notices=args.max_notices,
                backfill_months=args.backfill_months,
                backfill_offset=args.backfill_offset,
                seen_ids=seen_ids,
                persist_state=False,
                raise_on_login_failure=True,
            ))
            break
        except SubscriptionExpired as exc:
            # Not an egress problem and not an empty feed: say exactly what it
            # is. Reported as EMPTY, this cost 39 days in October 2026.
            _revert_seen(seen_ids, before_seen)
            res.status = "failed"
            res.detail = f"SUBSCRIPTION EXPIRED: {exc}."
            res.seconds = round(time.time() - t0, 1)
            return res
        except LoginFailed as exc:
            _revert_seen(seen_ids, before_seen)
            if attempt < login_attempts:
                logger.warning(
                    "Login unreachable through this egress IP (attempt %d/%d): %s. "
                    "Rotating to a new proxy session.", attempt, login_attempts, exc)
                continue
            res.status = "failed"
            res.detail = (
                f"LOGIN UNREACHABLE through {login_attempts} different egress IPs: {exc}. "
                "Nothing was scraped. Check the proxy pool and the site login, "
                "not the saved searches."
            )
            res.seconds = round(time.time() - t0, 1)
            return res
        except NoticeAccessBlocked as exc:
            res.status = "failed"
            res.detail = f"EGRESS BLOCKED: {exc}"
            res.seconds = round(time.time() - t0, 1)
            # Roll the cache back so nothing scraped-but-unsent is marked done.
            _revert_seen(seen_ids, before_seen)
            raise _Blocked(res) from exc

    _subscription_warning(res)

    if not notices:
        # Deliberately a failure. Foreclosure and probate notices publish
        # continuously in Knox and Blount; a run that finds nothing means the
        # gate, the login, or the saved search changed under us. Reporting this
        # as success is exactly how 19 days of dead runs went unnoticed.
        res.status = "empty"
        res.detail = (
            "0 notices scraped. Not a normal quiet day: check the notice gate, "
            "egress IP, login, and that the saved searches still exist "
            "(python src/main.py list-searches)."
        )
        res.seconds = round(time.time() - t0, 1)
        return res

    logger.info("Scraped %d notices", len(notices))
    scraped = len(notices)

    # Probate notices carry no property address; resolve it before enrichment.
    probate = [n for n in notices
               if n.notice_type == "probate" and n.decedent_name and not n.address]
    if probate:
        try:
            from property_lookup import lookup_decedent_properties
            logger.info("Resolving property addresses for %d probate notices", len(probate))
            asyncio.run(lookup_decedent_properties(probate))
        except Exception as exc:
            logger.warning("Probate property lookup failed: %s, continuing", exc)
            res.warnings.append(f"probate property lookup: {exc}")

    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    notices = run_enrichment_pipeline(notices, PipelineOptions(
        skip_parcel_lookup=True,       # scraped notices carry no parcel id
        skip_obituary=not args.deep_heirs,
        source_label="FTM scheduled run",
    ))
    notices = deduplicate(notices)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # Intake (build plan 1.11, 3.3): the single family and value screen, the debt and equity
    # figures, the notice text. Every part is OFF until its switch is set, and with all of them
    # off `intake` is None and the file below is written exactly as it always was.
    notices, intake, left_out = _intake(notices, res, stamp)
    res.records = len(notices)

    if intake is None:
        csv_path = write_datasift_csv(notices, filename=f"ftm_notices_{stamp}.csv")
    else:
        import ftm_intake
        csv_path = ftm_intake.write_csv(notices, intake, f"ftm_notices_{stamp}.csv")
    res.csv_path = str(csv_path)
    logger.info("Wrote %s", csv_path)

    if args.no_upload:
        res.status = "ok"
        res.detail = f"{res.records} records written, upload skipped (--no-upload)"
        if left_out:
            res.detail += "; " + left_out
        res.seconds = round(time.time() - t0, 1)
        # Nothing was sent, so nothing may be marked as handled.
        _revert_seen(seen_ids, before_seen)
        return res

    from datasift_api_upload import upload_csv

    # With the Appeared Again rule on, the uploader is told these rows are new filings. With it
    # off (the default) the call is exactly the one this stage has always made.
    up = upload_csv(str(csv_path), commit=args.commit, out_dir=str(config.OUTPUT_DIR), **_new_filing())
    res.uploaded = up["created"]
    res.warnings.extend(up["warnings"])

    if not args.commit:
        res.status = "ok"
        res.detail = f"{res.records} records ready (dry run, nothing uploaded)"
        _revert_seen(seen_ids, before_seen)
    elif res.records == 0 and scraped < ALL_DROPPED_SUSPECT:
        # A quiet day whose one or two notices were filtered out (an LLC owner,
        # a vacant lot, a probate with no property found). Nothing failed and
        # there was nothing to send, so this is not an upload failure: the first
        # unattended run after the November catch-up scraped ONE notice, the
        # filter dropped it, and the whole run read red. The notices are marked
        # handled exactly as a dropped notice is on a day when others survive.
        res.status = "ok"
        res.detail = f"{scraped} notice(s) scraped, none passed the filters, nothing to upload"
        save_seen_ids(seen_ids)
        if args.mode == "daily" and not args.backfill_months:
            from scraper import save_last_run_date
            save_last_run_date()
    elif res.records == 0:
        # Many notices and not one survivor is a broken enrichment step, not a
        # property of the notices (the Smarty lapse dropped every probate this
        # way). Fail and keep them unhandled so the fixed run picks them up.
        res.status = "failed"
        res.detail = (f"{scraped} notices scraped and every one was filtered out: "
                      "read the enrichment log before trusting this")
        _revert_seen(seen_ids, before_seen)
    elif up["created"] == 0:
        res.status = "failed"
        res.detail = f"{res.records} records but DataSift created 0 (failed={up['failed']})"
        _revert_seen(seen_ids, before_seen)
    else:
        res.status = "ok"
        res.detail = (
            f"{res.records} records, {up['created']} into DataSift, "
            f"{up['fielded']} with custom fields, {up['failed']} failed"
        )
        # ONLY here. The records are in the CRM, so skipping them next run is
        # correct. Persisting any earlier is how an aborted run silently
        # orphaned 204 notices: flagged as done, never sent.
        save_seen_ids(seen_ids)
        if args.mode == "daily" and not args.backfill_months:
            from scraper import save_last_run_date
            save_last_run_date()

    if left_out:
        res.detail += "; " + left_out
    res.seconds = round(time.time() - t0, 1)
    return res


# ── stages: notice feeds that run alone (build plan 1.10) ─────────────


def _result(d: dict) -> StageResult:
    """A stage result from the plain dict a helper module returns."""
    return StageResult(name=d["name"], status=d["status"], detail=d.get("detail", ""),
                       records=int(d.get("records") or 0), uploaded=int(d.get("uploaded") or 0),
                       seconds=float(d.get("seconds") or 0.0), csv_path=d.get("csv_path", ""),
                       warnings=list(d.get("warnings") or []))


def _stage_feed(name: str):
    """A stage function for one notice feed (tax_sale, tax_delinquent, public_notices).

    Each runs as its own scrape, on its own proxy session, with its own seen file and its own
    verdict, so a new feed can never fail or starve the foreclosure and probate stage."""
    def run(args) -> StageResult:
        import ftm_feed_runner
        out = _result(ftm_feed_runner.run(
            args, name, next_suffix=_next_proxy_session_suffix, all_dropped_suspect=ALL_DROPPED_SUSPECT))
        _subscription_warning(out)
        return out
    run.__name__ = "_stage_" + name
    return run


# ── stage: Knox county multi-source pull ──────────────────────────────


def _stage_county(args) -> StageResult:
    """Knox FTM county pull (liens, condemnations).

    Degrades with a stated reason rather than failing the whole run: it needs
    the SiftMap client for the buy-box enrichment, and that lives in the Deal
    Room checkout, which a cloud box does not have.
    """
    res = StageResult(name="county")
    t0 = time.time()

    try:
        import knox_ftm_pull  # noqa: F401
    except Exception as exc:
        res.status = "skipped"
        res.detail = f"knox_ftm_pull unavailable: {exc}"
        return res

    try:
        # Either client will do: knox_ftm_pull falls back to the standalone one,
        # which needs nothing but REISIFT_API_KEY.
        try:
            from clients.siftmap_api import SiftMapClient  # noqa: F401
        except Exception:
            from siftmap_standalone import SiftMapClient  # noqa: F401
    except Exception:
        res.status = "skipped"
        res.detail = (
            "SiftMap client not importable (Deal Room _api checkout absent). "
            "The county pull needs it for buy-box enrichment; run this stage "
            "from the workstation until the client is vendored."
        )
        return res

    out = config.OUTPUT_DIR / f"knox_ftm_pull_{datetime.now():%Y-%m-%d}.csv"
    # Point the pull at the same scratch the capture stage writes into. Without
    # this it defaults to the CWD, finds none of the source JSON, and produces
    # an empty CSV while still looking like it ran.
    scratch = str(config.STATE_DIR / "knox")
    # The staged feeds this run may upload: only those whose preparing stage is in this run's
    # --stages. With today's stages the list is empty, the flag is not passed, and the command
    # line is exactly the one this stage has always run.
    staged = _staged_feeds(args)
    rc = os.system(
        f'{sys.executable} "{Path(__file__).parent / "knox_ftm_pull.py"}" '
        f'--scratch "{scratch}" --out "{out}" '
        f'--rejects "{config.OUTPUT_DIR / "knox_ftm_rejected.csv"}"'
        + (f' --staged-feeds "{",".join(staged)}"' if staged else "")
    )
    if rc != 0 or not out.exists():
        res.status = "failed"
        res.detail = f"knox_ftm_pull exited {rc}"
        res.seconds = round(time.time() - t0, 1)
        return res

    import csv as _csv

    with open(out, encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    res.records = len(rows)
    res.csv_path = str(out)

    if args.no_upload or not rows:
        res.status = "ok"
        res.detail = f"{len(rows)} records written, upload skipped"
        # Nothing passed the buy box, so there is nothing to send and the
        # staged liens are settled. (--no-upload settles nothing.)
        if args.commit and not args.no_upload:
            _mark_liens_uploaded()
            _mark_staged_uploaded(staged)
        res.seconds = round(time.time() - t0, 1)
        return res

    from datasift_api_upload import upload_csv

    # Appeared Again (off by default): the rule's own ledger is what keeps the condemnation rows
    # this stage re-sends every morning from being tagged. With the rule off the call is exactly
    # the one this stage has always made.
    up = upload_csv(str(out), commit=args.commit, out_dir=str(config.OUTPUT_DIR), **_new_filing())
    res.uploaded = up["created"]
    res.status = "ok" if (not args.commit or up["created"]) else "failed"
    res.detail = f"{len(rows)} records, {up['created']} into DataSift"
    # Only after a committed upload that landed. A dry run or a failed upload
    # leaves the staged liens to be sent tomorrow.
    if args.commit and res.status == "ok":
        _mark_liens_uploaded(str(out))
        _mark_staged_uploaded(staged)
    res.seconds = round(time.time() - t0, 1)
    return res


def _staged_feeds(args) -> tuple:
    """The staged feeds whose preparing stage is in this run (see ftm_staging.STAGE_OF)."""
    stages = [s.strip() for s in str(getattr(args, "stages", "") or "").split(",") if s.strip()]
    if not any(s in ("appointments", "tax_sale_report", "code_violations") for s in stages):
        return ()                      # today's run: nothing is imported, nothing is passed
    try:
        import ftm_staging
        return ftm_staging.feeds_for_stages(stages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Staged feeds not read: %s", exc)
        return ()


def _mark_staged_uploaded(feeds: tuple = ()) -> None:
    """Settle what the newer county-stage feeds staged (appointments, the tax sale report, the
    code violation file), and only the ones this run loaded. Only ever called after a real
    upload, the same rule as the liens. With none of those stages in the run it does nothing."""
    if not feeds:
        return
    try:
        import ftm_staging

        moved = ftm_staging.mark_all_uploaded(str(config.STATE_DIR / "knox"), feeds=feeds)
        for feed, n in moved.items():
            logger.info("%s: %d filing(s) marked handled", feed, n)
    except Exception as exc:  # the upload already succeeded; never fail the run on this
        logger.warning("Could not mark staged feeds handled: %s", exc)


def _stage_liens(args) -> StageResult:
    """Knox Register of Deeds liens: pull, then stage the debtors that are new.

    Uploads nothing itself. It leaves `knox_lien_parcels.json` in the scratch
    folder for the county stage, which applies the buy box and uploads, and
    which marks the liens handled only once that upload has succeeded. So this
    stage belongs BEFORE `county` in --stages.

    The recorder is a PAID subscription. It lapsed on 2026-08-26, the search
    answered with a browser alert nobody was listening for, and no lien reached
    DataSift for 11 weeks while every run stayed green. A lapse now fails this
    stage by name.
    """
    res = StageResult(name="liens")
    t0 = time.time()
    if not (config.PAXSUB_USERNAME and config.PAXSUB_PASSWORD):
        res.status = "skipped"
        res.detail = "PAXSUB_USERNAME / PAXSUB_PASSWORD not set (Knox Register of Deeds login)"
        return res

    import knox_liens_daily
    import knox_rod

    scratch = str(config.STATE_DIR / "knox")
    try:
        stats = knox_liens_daily.prepare(scratch)
    except knox_rod.RodSubscriptionExpired as exc:
        res.status = "failed"
        res.detail = f"REGISTER OF DEEDS SUBSCRIPTION EXPIRED. {exc}"
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
    else:
        res.status = "ok"
        res.records = stats["with_parcels"]
        res.detail = (
            f"{stats['new_debtors']} new lien debtors ({stats['window']}), "
            f"{stats['with_parcels']} matched to {stats['parcels']} parcels"
        )
        days = stats.get("subscription_days")
        if days is not None and days <= SUBSCRIPTION_WARN_DAYS:
            res.warnings.append(
                f"The Register of Deeds subscription expires in {days} day(s). "
                "Renew it at paxsub.knoxrod.org or the lien pull stops."
            )
        if stats.get("lookup_errors"):
            res.warnings.append(
                f"{stats['lookup_errors']} lien debtor(s) could not be checked "
                "against the county tax roll (lookup error)."
            )
        # Releases (Ty, 2026-11-11): a lien that has been paid off must come
        # back off the DataSift record. The pull above just refreshed the
        # release index, so compare it against every lien record we hold.
        try:
            import knox_lien_releases

            rel = knox_lien_releases.reconcile(
                scratch, commit=bool(args.commit and not args.no_upload), log=logger.info)
            if rel["to_update"]:
                verb = "updated" if rel["committed"] else "would update"
                done = rel["updated"] if rel["committed"] else rel["to_update"]
                res.detail += (f"; {verb} {done} record(s) for released or restated liens "
                               f"({rel['fully_released']} now fully released)")
            bad = rel["failed"] + rel["mismatch"]
            if bad:
                res.warnings.append(
                    f"{bad} lien record(s) could not be updated after a release; "
                    "they are retried on the next run."
                )
        except Exception as exc:
            res.warnings.append(f"Lien release update did not run: {type(exc).__name__}: {exc}")
    res.seconds = round(time.time() - t0, 1)
    return res


def _mark_liens_uploaded(csv_path: str = "") -> None:
    """Record the staged liens as handled. Only ever called after a real upload."""
    scratch = str(config.STATE_DIR / "knox")
    # FIRST, while the staged debtor file still holds the instrument lists:
    # remember which liens now sit on which record, so a later release can
    # find its way back to it. mark_uploaded() below empties that file.
    if csv_path:
        try:
            import knox_lien_releases

            added = knox_lien_releases.register_uploaded(scratch, csv_path)
            if added:
                logger.info("Liens: %d record(s) added to the release ledger", added)
                # The upload states a record from this month's filings alone.
                # Where the ledger knows older liens on the same house, put
                # the full figure back now instead of leaving it short a day.
                fixed = knox_lien_releases.reconcile(scratch, commit=True, log=logger.info)
                if fixed["updated"]:
                    logger.info("Liens: %d record(s) restated to their full lien total",
                                fixed["updated"])
        except Exception as exc:
            logger.warning("Liens: release ledger not updated: %s", exc)
    try:
        import knox_liens_daily

        moved = knox_liens_daily.mark_uploaded(scratch)
        if moved:
            logger.info("Liens: %d instrument(s) marked handled", moved)
    except Exception as exc:  # the upload already succeeded; never fail the run on this
        logger.warning("Liens: could not mark instruments handled: %s", exc)


class _Blocked(Exception):
    """Carries the StageResult for an egress block up to the top level."""

    def __init__(self, stage: StageResult):
        super().__init__(stage.detail)
        self.stage = stage


def _stage_capture(args) -> StageResult:
    """Snapshot the sources that overwrite themselves: the condemnation agendas.

    This stage does not upload anything. Its whole job is to get the data onto
    the volume before the county erases it: the agendas live at fixed URLs that
    are overwritten each cycle. The eviction docket was captured here too until
    2026-08-25, when evictions were retired.

    Deliberately never fatal to the run. A capture failure is worth an alert,
    but it must not stop the notices stage from uploading.
    """
    res = StageResult(name="capture")
    t0 = time.time()
    scratch = str(config.STATE_DIR / "knox")
    os.makedirs(scratch, exist_ok=True)

    counts, problems = {}, []

    try:
        import knox_condemnations
        paths = knox_condemnations.fetch(scratch)
        # collect() owns the definition of a problem: an agenda not fetched, one
        # that parsed to nothing without being a cancellation notice, or a set
        # too old to be live. That last one is the check this stage lacked for
        # months while it re-read a frozen June file and reported OK.
        fresh, capture_problems = knox_condemnations.collect(paths)
        allrec = knox_condemnations.merge(scratch, fresh)
        with open(os.path.join(scratch, "condemnations.json"), "w", encoding="utf-8") as fh:
            json.dump(allrec, fh, indent=1)
        counts["condemnations"] = f"{len(fresh)} this cycle, {len(allrec)} cumulative"
        problems.extend(capture_problems)
    except Exception as exc:
        problems.append(f"condemnations: {type(exc).__name__}: {exc}")

    # The eviction sweep was retired 2026-08-25 (Ty). It worked, but it only
    # ever paid off if it ran forever: the General Sessions docket keeps just
    # the current week, so the list existed only as our own accumulation, and
    # each landlord still needed the name-to-parcel join to become a lead.
    # knox_evictions.py is kept; re-wiring is restoring this block.

    res.records = sum(1 for _ in counts)
    res.seconds = round(time.time() - t0, 1)
    res.warnings = problems
    detail = "; ".join(f"{k}: {v}" for k, v in counts.items()) or "nothing captured"

    # Any problem fails the stage. An earlier version only failed when NOTHING
    # was captured, so a run that downloaded every PDF but parsed none of them
    # (pdfminer missing from the image) reported OK. On a source that is
    # overwritten at origin, a quiet zero is the most expensive possible bug.
    if problems:
        res.status = "failed"
        res.detail = detail + " | " + "; ".join(problems)
    else:
        res.status = "ok"
        res.detail = detail
    return res


def _stage_sold(args) -> StageResult:
    """Sold guard: a record is Sold only when the house sold AFTER it became a lead.

    Until 2026-11-12 two SiftMap presets tagged every in-account record whose
    last sale was on or after a FIXED date (2023-01-01) and a sequence set the
    status to Sold, so any owner who had bought in the last 46 months was
    marked Sold the night after the record arrived. Those presets are off.
    This stage is now the only thing that hands out the tag: it revives a lead
    whose "sale" is the owner's own purchase and marks the real sales, by
    comparing the last sale with the day the record became a lead
    (src/sold_guard.py). Never fatal to the run.
    """
    res = StageResult(name="sold")
    t0 = time.time()
    try:
        import sold_guard
        st = sold_guard.daily(commit=bool(args.commit))
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
        res.seconds = time.time() - t0
        return res
    errors = st.get("repair_errors", 0) + st.get("mark_errors", 0)
    res.status = "failed" if st.get("rc") else "ok"
    res.records = st.get("restored", 0) + st.get("marked", 0)
    res.detail = (f"{st.get('restored', 0)} leads revived, {st.get('marked', 0)} real sales marked "
                  f"({st.get('repair_judged', 0) + st.get('mark_judged', 0)} judged); "
                  f"{st.get('held_back', 0)} wait for the next run; {st.get('for_a_person', 0)} for a person")
    if errors:
        res.warnings.append(f"sold guard could not read {errors} record(s)")
    if st.get("deed_problem"):
        res.warnings.append("sold guard deeds: " + str(st["deed_problem"]))
    if st.get("detect_note"):
        res.warnings.append("sale search: " + str(st["detect_note"]))
    res.seconds = time.time() - t0
    return res


# ── stages that are off until named in --stages ───────────────────────


def _stage_appointments(args) -> StageResult:
    """Substitute trustee appointments from the Register of Deeds (build plan 1.9).

    Uploads nothing itself: it stages the appointments it could follow to a house, and the
    county stage uploads them and marks them handled. So it belongs BEFORE `county`, and after
    `liens`, whose recorder session it reuses instead of logging in a second time.
    """
    res = StageResult(name="appointments")
    t0 = time.time()
    if not (config.PAXSUB_USERNAME and config.PAXSUB_PASSWORD):
        res.status = "skipped"
        res.detail = "PAXSUB_USERNAME / PAXSUB_PASSWORD not set (Knox Register of Deeds login)"
        return res

    import knox_appointments
    import knox_rod

    try:
        stats = knox_appointments.prepare(str(config.STATE_DIR / "knox"))
    except knox_rod.RodSubscriptionExpired as exc:
        res.status = "failed"
        res.detail = f"REGISTER OF DEEDS SUBSCRIPTION EXPIRED. {exc}"
    except knox_appointments.AppointmentsUnusable as exc:
        res.status = "failed"
        res.detail = f"SUBSTITUTE TRUSTEE FEED CANNOT RUN: {exc}"
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
    else:
        res.status = "ok"
        res.records = stats["staged"]
        res.detail = knox_appointments.describe(stats)
        days = stats.get("subscription_days")
        if days is not None and days <= SUBSCRIPTION_WARN_DAYS:
            res.warnings.append(
                f"The Register of Deeds subscription expires in {days} day(s). "
                "Renew it at paxsub.knoxrod.org or the recorder pulls stop.")
    res.seconds = round(time.time() - t0, 1)
    return res


def _stage_tax_sale_report(args) -> StageResult:
    """The Knox County Trustee's tax sale documents (build plan 1.10). Prepares only; `county`
    uploads. Any problem fails the stage, for the same reason the capture stage does: a zero on
    this source reads exactly like "no sale is scheduled"."""
    res = StageResult(name="tax_sale_report")
    t0 = time.time()
    try:
        import knox_tax_sale
        stats = knox_tax_sale.prepare(str(config.STATE_DIR / "knox"))
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
        res.seconds = round(time.time() - t0, 1)
        return res
    res.records = stats["staged"]
    res.warnings = list(stats["problems"])
    detail = knox_tax_sale.describe(stats)
    if stats["problems"]:
        res.status = "failed"
        res.detail = detail + " | " + "; ".join(stats["problems"])
    else:
        res.status = "ok"
        res.detail = detail
    res.seconds = round(time.time() - t0, 1)
    return res


def _stage_code_violations(args) -> StageResult:
    """Code violations from the file a person drops on the volume (build plan 1.10). Prepares
    only; `county` uploads. Fails by name when a file cannot be read, or when the file has
    stopped arriving."""
    res = StageResult(name="code_violations")
    t0 = time.time()
    try:
        import code_violations
        stats = code_violations.prepare(str(config.STATE_DIR / "knox"))
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
        res.seconds = round(time.time() - t0, 1)
        return res
    res.records = stats["staged"]
    res.warnings = list(stats["problems"])
    detail = code_violations.describe(stats)
    if stats["problems"]:
        res.status = "failed"
        res.detail = detail + " | " + "; ".join(stats["problems"])
    else:
        res.status = "ok"
        res.detail = detail
    res.seconds = round(time.time() - t0, 1)
    return res


def _stage_deeds(args) -> StageResult:
    """Put the deed records on today's Knox foreclosures (build plan 3.3).

    Reads the notice files this run wrote, looks up the deed of trust each notice cites, and adds
    a "Deed records check" note and the debt to the record, where research reads them. Needs the
    notice text on the row (FTM_NOTICE_TEXT) and belongs after `notices` and `liens`. Writes only
    on a committed run. Never fatal to the run's uploads: they are already done.
    """
    res = StageResult(name="deeds")
    t0 = time.time()
    if not (config.PAXSUB_USERNAME and config.PAXSUB_PASSWORD):
        res.status = "skipped"
        res.detail = "PAXSUB_USERNAME / PAXSUB_PASSWORD not set (Knox Register of Deeds login)"
        return res
    import csv as _csv
    import glob as _glob

    today = datetime.now().strftime("%Y-%m-%d")
    rows: list = []
    for path in sorted(_glob.glob(str(config.OUTPUT_DIR / f"ftm_notices_{today}_*.csv"))):
        with open(path, encoding="utf-8") as fh:
            rows.extend(_csv.DictReader(fh))
    try:
        import rod_lookup

        stats = rod_lookup.annotate(rows, str(config.STATE_DIR / "knox"),
                                    commit=bool(args.commit and not args.no_upload))
    except Exception as exc:
        name = "REGISTER OF DEEDS SUBSCRIPTION EXPIRED. " if type(exc).__name__ == "RodSubscriptionExpired" else ""
        res.status = "failed"
        res.detail = f"{name}{type(exc).__name__}: {exc}"
        res.seconds = round(time.time() - t0, 1)
        return res
    res.status = "ok"
    res.records = stats["written"]
    wrote = bool(args.commit and not args.no_upload)
    res.detail = (f"{stats['looked_up']} Knox foreclosure(s) looked up, {stats['with_debt']} with a debt figure, "
                  + (f"written to {stats['written']} record(s)" if wrote else "nothing written (dry run)"))
    if stats["no_text"]:
        res.warnings.append(f"{stats['no_text']} foreclosure row(s) carry no notice text, so their deed records "
                            "could not be looked up. Set FTM_NOTICE_TEXT=field.")
    if stats["failed"] or stats["not_found"]:
        res.warnings.append(f"{stats['failed']} lookup(s) failed and {stats['not_found']} record(s) were not found "
                            "in DataSift; they are tried again tomorrow.")
    if stats["left_for_tomorrow"]:
        res.detail += f", {stats['left_for_tomorrow']} left for tomorrow"
    res.seconds = round(time.time() - t0, 1)
    return res


def _stage_sold_reentry(args) -> StageResult:
    """The January pass of the sold guard (build plan 0.7): a record sold more than three years
    ago comes back as the new owner's record.

    DRY EVEN ON A COMMITTED RUN unless SOLD_REENTRY_COMMIT=1. Naming the stage shows what it
    would put back; the second switch is what lets it write, because the first pass can touch
    thousands of records. Outside January it reports that it is not due."""
    res = StageResult(name="sold_reentry")
    t0 = time.time()
    live = bool(args.commit) and os.environ.get("SOLD_REENTRY_COMMIT", "").strip() == "1"
    try:
        import argparse as _argparse

        import sold_guard

        ns = _argparse.Namespace(commit=live, limit=0, include_distress=False, fresh=False, force=False, stats={})
        rc = sold_guard.reentry(ns)
        st = ns.stats
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
        res.seconds = round(time.time() - t0, 1)
        return res
    res.status = "failed" if rc else "ok"
    if not st.get("reentry_due"):
        res.detail = "not due: the sold window moves on January 1"
    else:
        res.records = st.get("reentered", 0)
        if live:
            res.detail = (f"{st.get('reentered', 0)} record(s) put back into marketing "
                          f"({st.get('reentry_judged', 0)} judged, {st.get('reentry_waiting', 0)} waiting)")
        else:
            res.detail = (f"{st.get('reentry_would', 0)} record(s) would come back "
                          f"({st.get('reentry_judged', 0)} judged, {st.get('reentry_waiting', 0)} waiting); "
                          "nothing written (SOLD_REENTRY_COMMIT is not set)")
        if st.get("reentry_errors"):
            res.warnings.append(f"sold re-entry could not read {st['reentry_errors']} record(s)")
        # The pass only runs in January. Records still waiting when the month runs out would
        # sit Sold for another year, and nothing else would ever say so.
        if st.get("reentry_waiting") and datetime.now().day >= 25:
            res.warnings.append(
                f"{st['reentry_waiting']} Sold record(s) are still waiting to be judged and the January window is "
                "closing. Raise SOLD_REENTRY_MAX_PER_RUN or set SOLD_REENTRY_MONTHS=2.")
    res.seconds = round(time.time() - t0, 1)
    return res


def _stage_sold_audit(args) -> StageResult:
    """The monthly sold audit (build plan 0.7): last month's sold leads and how far we got on
    each. Read-only on the account; runs once a month and says "already written" after that."""
    res = StageResult(name="sold_audit")
    t0 = time.time()
    try:
        import argparse as _argparse

        import sold_guard

        ns = _argparse.Namespace(commit=False, limit=0, month="", force=False, stats={})
        rc = sold_guard.monthly_audit(ns)
        st = ns.stats
    except Exception as exc:
        res.status = "failed"
        res.detail = f"{type(exc).__name__}: {exc}"
        res.seconds = round(time.time() - t0, 1)
        return res
    res.status = "failed" if rc else "ok"
    s = st.get("audit_summary") or {}
    if st.get("audit_ran"):
        res.records = s.get("were_our_leads", 0)
        res.detail = sold_guard.audit_text(s).replace("\n", " ")
    else:
        res.detail = f"audit for {st.get('audit_month', '?')} is already written"
    res.seconds = round(time.time() - t0, 1)
    return res


STAGE_FUNCS = {"notices": _stage_notices, "capture": _stage_capture,
               "liens": _stage_liens, "county": _stage_county, "sold": _stage_sold,
               # Off unless named in --stages:
               "tax_sale": _stage_feed("tax_sale"), "tax_delinquent": _stage_feed("tax_delinquent"),
               "public_notices": _stage_feed("public_notices"),
               "appointments": _stage_appointments, "tax_sale_report": _stage_tax_sale_report,
               "code_violations": _stage_code_violations, "deeds": _stage_deeds,
               "sold_reentry": _stage_sold_reentry, "sold_audit": _stage_sold_audit}


# ── doctor ────────────────────────────────────────────────────────────

# Every switch this pipeline reads beyond FTM_ARGS, so the wiring check can say which ones THIS
# process actually sees. On Fly a secret overrides the file's [env] block, and reading the file
# has told the opposite of the truth before.
SWITCH_NAMES = ("FTM_RATE_BUDGET", "FTM_INTAKE_SCREEN", "FTM_NOTICE_TEXT", "FTM_TAG_APPEARED_AGAIN",
                "FTM_FEED_HEALTH", "FTM_FEED_LAG_ALARM_DAYS", "FTM_HEALTH_ALARM_FEEDS", "SOLD_REENTRY_COMMIT",
                "SOLD_GUARD_EXTRA_UPLOAD_LISTS", "FTM_PUBLIC_NOTICE_SEARCHES", "FTM_UPLOAD_BROWSER")


def switch_report() -> list[str]:
    """What is switched on beyond today's run: the extra stages FTM_ARGS names, the feeds that
    makes live, and each setting this process sees. Reads the environment only."""
    lines: list[str] = []
    try:
        import ftm_feeds

        asked = ftm_feeds.requested_stages()
        extra = [s for s in asked if s not in TODAY_STAGES]
        lines.append("\n  stages beyond today's five: " + (", ".join(extra) if extra else "none"))
        unknown = [s for s in asked if s not in ALL_STAGES]
        if unknown:
            lines.append("    NOT A STAGE (the run would exit 2): " + ", ".join(unknown))
        for f in ftm_feeds.live_feeds(asked):
            if f.stage not in TODAY_STAGES:
                lines.append(f"    live feed: {f.label} -> list {f.list_name!r}")
        staged = [s for s in ("appointments", "tax_sale_report", "code_violations") if s in asked]
        if staged and "county" not in asked:
            lines.append("    WARNING: " + ", ".join(staged) + " only stage rows; without `county` nothing uploads them")
        late = [s for s in staged if "county" in asked and asked.index(s) > asked.index("county")]
        if late:
            lines.append("    WARNING: " + ", ".join(late) + " is listed AFTER `county`, so its rows wait a day")
    except Exception as exc:
        lines.append(f"\n  feed table unreadable: {exc}")
    on = [f"{n}={os.environ[n]}" for n in SWITCH_NAMES if os.environ.get(n)]
    lines.append("  settings switched on: " + (", ".join(on) if on else "none"))
    return lines


def doctor() -> int:
    """Check the wiring without scraping anything. Cheap and safe to run."""
    from proxy_resolver import describe as describe_proxy

    print("\nSiftStack FTM runner, wiring check\n" + "=" * 52)

    rows: list[tuple[str, bool, str]] = []

    def add(label: str, ok: bool, note: str = "") -> None:
        rows.append((label, ok, note))

    add("TNPN_EMAIL / TNPN_PASSWORD",
        bool(config.TNPN_EMAIL and config.TNPN_PASSWORD), "site login")
    add("CAPTCHA_API_KEY", bool(config.CAPTCHA_API_KEY), "2Captcha, solves the Turnstile gate")
    add("DATASIFT_EMAIL / DATASIFT_PASSWORD",
        bool(config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD),
        "the uploader mints its own JWT from these")
    add("ANTHROPIC_API_KEY", bool(config.ANTHROPIC_API_KEY),
        "LLM notice parsing; probate loses the PR/decedent without it")
    add("SLACK_WEBHOOK_URL", bool(config.SLACK_WEBHOOK_URL),
        "run summaries and dead-run alerts")

    proxied = bool(config.PROXY_URL or (config.APIFY_TOKEN and config.APIFY_PROXY_GROUPS))
    add("egress proxy", proxied, describe_proxy())

    state_ok = os.access(config.STATE_DIR, os.W_OK)
    add(f"state dir writable ({config.STATE_DIR})", state_ok,
        "last_run.json, seen_ids.json, cookies.json")

    for label, ok, note in rows:
        print(f"  [{'OK ' if ok else 'MISS'}]  {label}")
        if note:
            print(f"          {note}")

    print("\n  saved searches configured:")
    for s in config.SAVED_SEARCHES:
        print(f"    - {s.saved_search_name:24}  {s.county}/{s.notice_type}")
    print("    verify against the live site: python src/main.py list-searches")

    for line in switch_report():
        print(line)

    print(f"\n  gate: {config.CAPTCHA_KIND}  sitekey={config.TURNSTILE_SITEKEY}")
    print(f"  output dir: {config.OUTPUT_DIR}")

    hard = [label for label, ok, _ in rows
            if not ok and label.split(" /")[0] in (
                "TNPN_EMAIL", "CAPTCHA_API_KEY", "DATASIFT_EMAIL")]
    if hard:
        print(f"\n  BLOCKING: {', '.join(hard)}\n")
        return 2
    if not proxied:
        print("\n  WARNING: no proxy configured. tnpublicnotice.com refuses "
              "notice pages from most IPs; expect exit code 3.\n")
    print()
    return 0


# ── reporting ─────────────────────────────────────────────────────────


def _summary_text(run: RunResult) -> str:
    mode = "COMMIT" if run.committed else "DRY RUN"
    icon = ":rotating_light:" if run.failed else ":white_check_mark:"
    lines = [f"{icon} *SiftStack FTM run* ({mode})"]
    for s in run.stages:
        mark = {"ok": "OK", "failed": "FAILED", "empty": "EMPTY", "skipped": "skipped"}[s.status]
        lines.append(f"  *{s.name}* [{mark}] {s.detail or ''} ({s.seconds}s)")
        for w in s.warnings[:5]:
            lines.append(f"      warn: {w}")
    if run.blocked:
        lines.append(
            "  This is an EGRESS block, not a scraping bug: the site refuses "
            "notice pages from the run's IP. Fix SIFTSTACK_PROXY_URL / Apify "
            "proxy settings; retrying will not help."
        )
    return "\n".join(lines)


def _notify(run: RunResult) -> None:
    if not config.SLACK_WEBHOOK_URL:
        return
    try:
        from slack_notifier import _send_webhook
        _send_webhook(_summary_text(run))
    except Exception as exc:
        logger.warning("Slack notify failed: %s", exc)


def source_state(today: datetime | None = None) -> dict:
    """What this run learned about the paid sources: when each subscription runs out.

    The notice site prints its expiry date on the dashboard and the recorder prints days left in
    its header. Both are read during the run and both used to be thrown away unless they were
    within three days, so "how long until either lapses" could only be answered by logging in."""
    out: dict = {}
    day = (today or datetime.now()).strftime("%Y-%m-%d")
    try:
        # Read off the module only when a stage already loaded it: this must never be the
        # reason the browser library gets imported.
        sub = (sys.modules["scraper"].SUBSCRIPTION or {}) if "scraper" in sys.modules else {}
        expires = sub.get("expires") or ""
        if expires:
            left = (datetime.strptime(expires, "%Y-%m-%d").date() - datetime.strptime(day, "%Y-%m-%d").date()).days
            out["tnpublicnotice"] = {"expires": expires, "days_left": left, "read": day,
                                     "product": sub.get("product") or ""}
    except Exception:
        pass
    try:
        if "knox_rod" in sys.modules:
            sub = sys.modules["knox_rod"].SUBSCRIPTION or {}
            if sub.get("days") is not None or sub.get("state"):
                out["register_of_deeds"] = {"days_left": sub.get("days"), "state": sub.get("state") or "", "read": day}
    except Exception:
        pass
    return out


def _write_source_state() -> None:
    """Keep the paid-source readings where the health check can find them (build plan 1.12).

    Only with FTM_FEED_HEALTH=1. A source that was not read this run keeps its last reading, so
    a day when the lien stage is skipped does not erase the recorder's days-left figure."""
    if os.environ.get("FTM_FEED_HEALTH", "").strip() != "1":
        return
    try:
        path = config.STATE_DIR / "ftm_sources.json"
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            held = {}
        held.update(source_state())
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(held, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug("Could not write source state: %s", exc)


def _write_run_log(run: RunResult) -> None:
    """Append the run to a JSONL on the state volume, so history survives."""
    try:
        path = config.STATE_DIR / "ftm_runs.jsonl"
        payload = asdict(run)
        payload["stages"] = [asdict(s) if not isinstance(s, dict) else s
                             for s in run.stages]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception as exc:
        logger.debug("Could not write run log: %s", exc)


# ── entry point ───────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ftm_runner",
        description="Unattended first-to-market run (scrape, enrich, upload).",
    )
    p.add_argument("--commit", action="store_true",
                   help="Actually write to DataSift and advance run state. "
                        "Without it nothing is written anywhere.")
    p.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                   help=f"Comma-separated subset of: {', '.join(ALL_STAGES)}")
    p.add_argument("--mode", default="daily", choices=["daily", "historical"])
    p.add_argument("--counties", default=None, help='e.g. "Knox,Blount"')
    p.add_argument("--types", default=None, help='e.g. "foreclosure,probate"')
    p.add_argument("--since", default=None, help="YYYY-MM-DD cutoff override")
    p.add_argument("--max-notices", type=int, default=0,
                   help="Cost ceiling: stop after this many notices (0 = no limit)")
    p.add_argument("--backfill-months", type=int, default=0, metavar="N",
                   help=(
                       "Backfill the last N months by running each saved search "
                       "once per calendar month. Required to reach anything older "
                       "than a few weeks: the site truncates every result set at "
                       "1000 rows newest-first. It only retains 12 months, so "
                       "N above 12 buys nothing. Resumable: the seen-ID cache "
                       "skips what earlier runs already captured."
                   ))
    p.add_argument("--capture-days", type=int, default=30,
                   help="how far back the eviction sweep looks. Retention is "
                        "irregular, so a wider sweep sometimes recovers an "
                        "older court date that is still up.")
    p.add_argument("--backfill-offset", type=int, default=0, metavar="M",
                   help=(
                       "Shift the backfill window back M months. Use with "
                       "--backfill-months 1 to run a single month at a time, so "
                       "a long backfill is a series of short resumable jobs "
                       "instead of one 19-hour process."
                   ))
    p.add_argument("--deep-heirs", action="store_true",
                   help="Run obituary/heir resolution (billed per record)")
    p.add_argument("--no-upload", action="store_true",
                   help="Scrape and write the CSV but never call DataSift")
    p.add_argument("--no-proxy", action="store_true",
                   help="Use this machine's own IP (blocked from most networks)")
    p.add_argument("--doctor", action="store_true",
                   help="Check credentials, egress and state dir, then exit")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if args.doctor:
        return doctor()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in stages if s not in STAGE_FUNCS]
    if unknown:
        print(f"Unknown stage(s): {', '.join(unknown)}. Known: {', '.join(ALL_STAGES)}")
        return 2

    run = RunResult(started=datetime.now().isoformat(timespec="seconds"),
                    committed=args.commit)
    logger.info("FTM run starting (%s) stages=%s",
                "COMMIT" if args.commit else "DRY RUN", ",".join(stages))

    for name in stages:
        logger.info("--- stage: %s ---", name)
        try:
            run.stages.append(STAGE_FUNCS[name](args))
        except _Blocked as blocked:
            run.stages.append(blocked.stage)
            run.blocked = True
            logger.error("%s", blocked.stage.detail)
            break                      # every later stage would fail identically
        except Exception as exc:
            logger.error("Stage %s crashed: %s", name, exc)
            logger.debug("%s", traceback.format_exc())
            run.stages.append(StageResult(
                name=name, status="failed",
                detail=f"{type(exc).__name__}: {exc}",
            ))

    run.finished = datetime.now().isoformat(timespec="seconds")

    print("\n" + "=" * 60)
    print(_summary_text(run).replace(":rotating_light:", "!!").replace(":white_check_mark:", "OK"))
    print("=" * 60 + "\n")

    _write_run_log(run)
    _write_source_state()
    _notify(run)
    return run.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
