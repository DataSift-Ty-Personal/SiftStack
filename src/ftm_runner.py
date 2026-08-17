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

ALL_STAGES = ("notices", "county")
DEFAULT_STAGES = ("notices",)


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
    session_suffix = "" if args.no_proxy else _next_proxy_session_suffix()
    proxy_url = None if args.no_proxy else resolve_proxy_url(session_suffix=session_suffix)
    logger.info("Egress: %s", "DIRECT (--no-proxy)" if args.no_proxy
                else describe_proxy(session_suffix))

    # seen_ids is NOT persisted by the scrape. It marks a notice as "already
    # handled", but the upload happens after the whole scrape finishes, so
    # persisting mid-scrape means an abort leaves notices flagged as done that
    # were never sent anywhere. That is exactly what the egress block caused:
    # 204 notices marked seen, zero uploaded, and a retry would have skipped
    # every one of them. The runner persists at the end, only on success.
    seen_ids = load_seen_ids()
    before_seen = set(seen_ids)

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
        ))
    except NoticeAccessBlocked as exc:
        res.status = "failed"
        res.detail = f"EGRESS BLOCKED: {exc}"
        res.seconds = round(time.time() - t0, 1)
        # Roll the cache back so nothing scraped-but-unsent is marked done.
        _revert_seen(seen_ids, before_seen)
        raise _Blocked(res) from exc

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
    res.records = len(notices)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = write_datasift_csv(notices, filename=f"ftm_notices_{stamp}.csv")
    res.csv_path = str(csv_path)
    logger.info("Wrote %s", csv_path)

    if args.no_upload:
        res.status = "ok"
        res.detail = f"{res.records} records written, upload skipped (--no-upload)"
        res.seconds = round(time.time() - t0, 1)
        # Nothing was sent, so nothing may be marked as handled.
        _revert_seen(seen_ids, before_seen)
        return res

    from datasift_api_upload import upload_csv

    up = upload_csv(str(csv_path), commit=args.commit, out_dir=str(config.OUTPUT_DIR))
    res.uploaded = up["created"]
    res.warnings.extend(up["warnings"])

    if not args.commit:
        res.status = "ok"
        res.detail = f"{res.records} records ready (dry run, nothing uploaded)"
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

    res.seconds = round(time.time() - t0, 1)
    return res


# ── stage: Knox county multi-source pull ──────────────────────────────


def _stage_county(args) -> StageResult:
    """Knox FTM county pull (liens, condemnations, trustee deeds, evictions).

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
        from clients.siftmap_api import SiftMapClient  # noqa: F401
    except Exception:
        res.status = "skipped"
        res.detail = (
            "SiftMap client not importable (Deal Room _api checkout absent). "
            "The county pull needs it for buy-box enrichment; run this stage "
            "from the workstation until the client is vendored."
        )
        return res

    out = config.OUTPUT_DIR / f"knox_ftm_pull_{datetime.now():%Y-%m-%d}.csv"
    rc = os.system(f'{sys.executable} "{Path(__file__).parent / "knox_ftm_pull.py"}" --out "{out}"')
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
        res.seconds = round(time.time() - t0, 1)
        return res

    from datasift_api_upload import upload_csv

    up = upload_csv(str(out), commit=args.commit, out_dir=str(config.OUTPUT_DIR))
    res.uploaded = up["created"]
    res.status = "ok" if (not args.commit or up["created"]) else "failed"
    res.detail = f"{len(rows)} records, {up['created']} into DataSift"
    res.seconds = round(time.time() - t0, 1)
    return res


class _Blocked(Exception):
    """Carries the StageResult for an egress block up to the top level."""

    def __init__(self, stage: StageResult):
        super().__init__(stage.detail)
        self.stage = stage


STAGE_FUNCS = {"notices": _stage_notices, "county": _stage_county}


# ── doctor ────────────────────────────────────────────────────────────


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
    _notify(run)
    return run.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
