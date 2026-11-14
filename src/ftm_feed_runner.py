"""A notice-site feed that runs alone, with its own state and its own verdict (build plan 1.10).

The tax sale and tax delinquent saved searches already sit in the notice site's dropdown, and a
public sale / court order feed can be added the same way once its saved searches exist. This
module runs any one of them as its OWN runner stage (`tax_sale`, `tax_delinquent`,
`public_notices`), never folded into the foreclosure and probate scrape. Four reasons, each a way
a new feed could have hurt the feed that already works:

  * ITS OWN UPLOAD. The core stage uploads one file and persists its seen ids only when that
    upload lands. A parsing bug in a new notice type inside the same file would fail the proven
    feeds with it.
  * ITS OWN EGRESS ADDRESS. The site blocks an address by VOLUME (about 204 notice views). Each
    feed takes the next proxy session, so a new feed never spends the core feed's budget.
  * ITS OWN SEEN FILE. The scrape records a notice as seen BEFORE its filters run. The foreclosure
    search matches broadly ("notice of sale"), so it opens tax sale notices, rejects them as not
    a trustee sale, and marks them seen. Sharing one seen file would hide every one of those from
    the tax feed for good.
  * ITS OWN CLOCK. Each feed keeps its own last-run date, so adding a feed late in a run cannot
    move the cutoff the core feed reads.

A QUIET DAY IS NORMAL HERE, A BLIND ONE IS NOT. The core stage calls zero notices a failure,
because foreclosures and probates publish continuously. A tax sale runs twice a year, so "found
something new today" cannot be this feed's proof of life. The proof is that every configured
saved search was actually OPENED: its results grid was read and held rows (scraper.SEARCH_STATS).
A search whose grid read zero rows is reported EMPTY, which fails the run, because that is what a
renamed or deleted saved search looks like and it is otherwise identical to a quiet day.

Nothing is uploaded without --commit, and seen ids and the last-run date move only after the
upload landed (or after a read that honestly found nothing to send).
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftm_feed_runner")

FIRST_RUN_LOOKBACK_DAYS = 7
SEEN_PRUNE_DAYS = 400          # the site keeps 12 months; a seen id older than that can never reappear
LOGIN_EGRESS_ATTEMPTS = 3
STAGES = ("tax_sale", "tax_delinquent", "public_notices")

# Scoped to the head of the notice on purpose. These rules were tuned on the results grid's short
# snippet; run over a whole notice, "storage" drops a house with a storage building.
_HEAD_CHARS = 400
_JUNK_RE = re.compile(r"vehicle|self.?storage|storage (unit|facility|lien)|\bVIN\b|impound|\btow(ing)?\b|marina|boat|vessel", re.I)
_NOISE_RE = re.compile(
    r"advertisement for bids|sealed bids|invites proposals|request for proposals|specifications and contract|"
    r"annual meeting|notice of public meeting|public hearing|department of transportation|beer (board|permit)", re.I)
_VENUE_RE = re.compile(r"chancery|court|circuit|clerk|400 (w\.? |west )?main|city.county building|courthouse", re.I)


@dataclass
class Spec:
    stage: str
    searches: list           # [(county, notice_type, dropdown label), ...]
    lists: dict              # notice_type -> DataSift list name
    csv_prefix: str
    always_screen: bool = False     # the buy box always applies (a junk-prone feed has no "today" to preserve)
    drop_junk: bool = False
    problems: list = field(default_factory=list)


def spec_for(stage: str) -> Spec | None:
    """What one feed stage scrapes and where its records land, from the feed table."""
    import ftm_feeds

    feeds = ftm_feeds.feeds_for_stage(stage)
    if not feeds or stage not in STAGES:
        return None
    lists = {f.notice_type: f.list_name for f in feeds}
    if stage == "public_notices":
        searches = ftm_feeds.public_notice_searches()
        problems = [] if searches else [
            "no saved search is named for this feed. Create the public sale and court order searches on the "
            "notice site, then set FTM_PUBLIC_NOTICE_SEARCHES (County|public_sale|label;County|order|label)."]
        return Spec(stage, searches, lists, feeds[0].csv_prefix, always_screen=True, drop_junk=True, problems=problems)
    searches = [(f.county, f.notice_type, f.search_label) for f in feeds if f.search_label]
    return Spec(stage, searches, lists, feeds[0].csv_prefix)


# ── per-feed state ────────────────────────────────────────────────────


def _state_dir() -> Path:
    import config
    return config.STATE_DIR


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_seen(stage: str, today: str, state_dir: Path | None = None) -> dict:
    data = _read_json((state_dir or _state_dir()) / ("seen_ids_%s.json" % stage), {})
    if not isinstance(data, dict):
        return {}
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=SEEN_PRUNE_DAYS)).strftime("%Y-%m-%d")
    return {k: v for k, v in data.items() if str(v) >= cutoff}


def save_seen(stage: str, seen: dict, state_dir: Path | None = None) -> None:
    _write_json((state_dir or _state_dir()) / ("seen_ids_%s.json" % stage), seen)


def last_run(stage: str, state_dir: Path | None = None) -> str:
    return str((_read_json((state_dir or _state_dir()) / ("last_run_%s.json" % stage), {}) or {}).get("last_run_date") or "")


def save_last_run(stage: str, today: str, state_dir: Path | None = None) -> None:
    _write_json((state_dir or _state_dir()) / ("last_run_%s.json" % stage), {"last_run_date": today})


def since_date(stage: str, today: str, override: str | None = None, state_dir: Path | None = None) -> str:
    """The publication-date cutoff: an explicit override, else this feed's own last good run, else
    a week back. Never the core feed's last-run date."""
    if override:
        return override
    got = last_run(stage, state_dir)
    if got:
        return got
    return (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")


# ── filters that belong to a feed, not to the pipeline ────────────────


def is_junk(notice) -> str:
    """Why a public sale or court order notice is not a property lead, or "" when it may be one."""
    head = (getattr(notice, "raw_text", "") or "")[:_HEAD_CHARS]
    if _JUNK_RE.search(head):
        return "vehicle, storage or boat sale"
    if _NOISE_RE.search(head):
        return "bid, meeting or agency notice"
    addr = (getattr(notice, "address", "") or "").strip()
    if not addr:
        return "no property address in the notice"
    if not addr[0].isdigit() or _VENUE_RE.search(addr):
        return "the only address is the courthouse or sale venue"
    return ""


def unread_searches(spec: Spec, stats: dict) -> list[str]:
    """Saved searches whose grid was never read with rows on it."""
    return [label for _c, _t, label in spec.searches
            if int((stats.get(label) or {}).get("rows") or 0) <= 0]


# ── default wiring (every piece can be replaced in a test) ─────────────


def _default_scrape(searches, since, seen, proxy_url, max_notices):
    import config
    from scraper import scrape_all

    objs = [config.SavedSearch(county, ntype, label) for county, ntype, label in searches]
    return asyncio.run(scrape_all(
        mode="daily", searches=objs, proxy_url=proxy_url,
        llm_api_key=config.ANTHROPIC_API_KEY or None, since_date_override=since,
        max_notices=max_notices, seen_ids=seen, persist_state=False, raise_on_login_failure=True))


def _default_stats() -> dict:
    import scraper
    return dict(scraper.SEARCH_STATS)


def _default_enrich(notices, label: str):
    from data_formatter import deduplicate
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    notices = run_enrichment_pipeline(notices, PipelineOptions(
        skip_parcel_lookup=True, skip_obituary=True, source_label=label))
    return deduplicate(notices)


def write_feed_csv(notices, spec: Spec, stamp: str, extras: dict | None = None, out_dir: Path | None = None) -> str:
    """The upload file for one feed: the pipeline's own row, with the feed's list name and any
    intake columns added. `extras` maps id(notice) to extra column values."""
    import config
    import ftm_intake
    from datasift_formatter import DATASIFT_COLUMNS, _build_row

    path = (out_dir or config.OUTPUT_DIR) / ("%s_%s.csv" % (spec.csv_prefix, stamp))
    columns = DATASIFT_COLUMNS + [c for c in ftm_intake.EXTRA_COLUMNS if c not in DATASIFT_COLUMNS]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for n in notices:
            row = _build_row(n)
            row["Lists"] = spec.lists.get(n.notice_type, row.get("Lists", ""))
            # apply_extras, not a plain update: the screen's tags and note have to be MERGED into
            # the row's own, and its private keys must not become columns.
            w.writerow(ftm_intake.apply_extras(row, (extras or {}).get(id(n))))
    return str(path)


def _default_upload(path: str, commit: bool) -> dict:
    import config
    from datasift_api_upload import upload_csv
    return upload_csv(path, commit=commit, out_dir=str(config.OUTPUT_DIR), new_filing=True)


def _keep_rejects(rejected: list, stage: str, stamp: str, out_dir: Path | None, warnings: list) -> str:
    """Write what this feed left out, with each reason, beside the upload file.

    The stage's detail line carries the counts. This keeps the notices themselves, because a
    junk rule can only be tuned by somebody who can see what it threw away. Named so that no
    reader of upload files (the health check, the Appeared Again ledger) can mistake it for one.
    Never fatal: the upload matters more than its audit file."""
    if not rejected:
        return ""
    try:
        import config
        import ftm_intake

        path = (out_dir or config.OUTPUT_DIR) / ("ftm_intake_rejected_%s_%s.csv" % (stage, stamp))
        ftm_intake.write_rejects(rejected, path)
        logger.info("%s: %d notice(s) left out, listed in %s", stage, len(rejected), path)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        warnings.append("The list of left-out notices could not be written (%s)." % type(exc).__name__)
        return ""


def run(args, stage: str, *, next_suffix=None, resolve_proxy=None, scrape=None, stats=None, enrich=None,
        screen=None, write_csv=None, upload=None, today: str | None = None, state_dir: Path | None = None,
        out_dir: Path | None = None, all_dropped_suspect: int = 5) -> dict:
    """Run one notice feed end to end. Returns a plain result dict the runner turns into a stage
    result: name, status (ok | failed | empty), detail, records, uploaded, seconds, csv_path, warnings."""
    t0 = time.time()
    res = {"name": stage, "status": "failed", "detail": "", "records": 0, "uploaded": 0, "seconds": 0.0,
           "csv_path": "", "warnings": []}
    unread: list[str] = []

    def done(status: str, detail: str) -> dict:
        # One search that could not be read must turn the stage red even when the others
        # uploaded fine. What the readable searches produced is still sent (it is not held
        # hostage), and the verdict says both things.
        if status == "ok" and unread:
            status = "empty"
            detail = ("%s. BUT %d saved search(es) could not be read or hold nothing at all: %s. A renamed or "
                      "deleted search looks exactly like this (python src/main.py list-searches)."
                      % (detail, len(unread), ", ".join(unread)))
        res.update(status=status, detail=detail, seconds=round(time.time() - t0, 1))
        return res

    spec = spec_for(stage)
    if spec is None:
        return done("failed", "%s is not a notice feed" % stage)
    if spec.problems:
        return done("failed", spec.problems[0])
    searches = list(spec.searches)
    if getattr(args, "counties", None):
        want = {c.strip().lower() for c in args.counties.split(",") if c.strip()}
        searches = [s for s in searches if s[0].lower() in want]
    if not searches:
        return done("failed", "no saved search of this feed matched --counties")
    spec = Spec(spec.stage, searches, spec.lists, spec.csv_prefix, spec.always_screen, spec.drop_junk)

    if today is None:
        import config
        today = config.run_date()
    seen = load_seen(stage, today, state_dir)
    before = set(seen)
    since = since_date(stage, today, getattr(args, "since", None), state_dir)
    no_proxy = bool(getattr(args, "no_proxy", False))
    commit = bool(getattr(args, "commit", False))

    def revert() -> None:
        for k in [k for k in seen if k not in before]:
            seen.pop(k, None)

    from captcha_solver import NoticeAccessBlocked
    from scraper import LoginFailed, SubscriptionExpired

    scrape = scrape or _default_scrape
    notices = None
    attempts = 1 if no_proxy else LOGIN_EGRESS_ATTEMPTS
    for attempt in range(1, attempts + 1):
        suffix = "" if (no_proxy or next_suffix is None) else next_suffix()
        proxy_url = None
        if not no_proxy:
            if resolve_proxy is None:
                from proxy_resolver import resolve_proxy_url as resolve_proxy
            proxy_url = resolve_proxy(session_suffix=suffix)
        try:
            notices = scrape(searches, since, seen, proxy_url, int(getattr(args, "max_notices", 0) or 0))
            break
        except SubscriptionExpired as exc:
            revert()
            return done("failed", "SUBSCRIPTION EXPIRED: %s." % exc)
        except LoginFailed as exc:
            revert()
            if attempt < attempts:
                logger.warning("%s: login unreachable through this egress address (attempt %d of %d); "
                               "rotating to a new proxy session", stage, attempt, attempts)
                continue
            return done("failed", "LOGIN UNREACHABLE through %d different egress addresses: %s. "
                                  "Nothing was scraped." % (attempts, exc))
        except NoticeAccessBlocked as exc:
            # Reported as this stage's failure, not as a blocked run: the stages after this one
            # do not use the notice site, and the core feed already ran on its own address.
            revert()
            return done("failed", "EGRESS BLOCKED on this feed's address: %s" % exc)

    notices = list(notices or [])
    got = (stats or _default_stats)()
    unread.extend(unread_searches(spec, got))
    if len(unread) == len(searches):
        revert()
        names = ", ".join(unread)
        unread.clear()
        return done("empty", "none of this feed's saved searches could be read, or they hold nothing at all: %s. "
                             "A renamed or deleted search looks exactly like this "
                             "(python src/main.py list-searches)." % names)
    rows_read = sum(int((got.get(label) or {}).get("rows") or 0) for _c, _t, label in searches)

    def settle() -> None:
        """Persist this feed's seen ids and last-run date. Only a committed run may: a dry run
        that moved the cutoff would make the next real run skip what the dry run only looked at.
        Never while a search is unread, or the date would move past notices nobody opened."""
        if commit and not getattr(args, "no_upload", False):
            save_seen(stage, seen, state_dir)
            if not unread:
                save_last_run(stage, today, state_dir)

    if not notices:
        settle()
        return done("ok", "nothing new: %d notice(s) on the grid, all seen before or published before %s"
                    % (rows_read, since))

    scraped = len(notices)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dropped: dict[str, int] = {}
    rejected: list = []            # (notice, why) for everything this feed's own rules left out
    if spec.drop_junk:
        kept = []
        for n in notices:
            why = is_junk(n)
            if why:
                dropped[why] = dropped.get(why, 0) + 1
                rejected.append((n, why))
            else:
                kept.append(n)
        notices = kept

    if notices:
        notices = (enrich or (lambda ns: _default_enrich(ns, "FTM feed %s" % stage)))(notices)

    extras: dict = {}
    if notices:
        import ftm_intake
        if spec.always_screen or ftm_intake.enabled():
            verdict = (screen or ftm_intake.screen)(notices, force=spec.always_screen)
            notices, extras = verdict["kept"], verdict["extras"]
            for why, n in verdict["dropped"].items():
                dropped[why] = dropped.get(why, 0) + n
            rejected.extend(verdict.get("rejected") or [])
            res["warnings"].extend(verdict.get("warnings") or [])
        ftm_intake.text_extras(notices, extras)        # adds nothing unless FTM_NOTICE_TEXT is on

    _keep_rejects(rejected, stage, stamp, out_dir, res["warnings"])
    res["records"] = len(notices)
    said_dropped = ("; left out: " + ", ".join("%d %s" % (n, why) for why, n in sorted(dropped.items()))) if dropped else ""

    if not notices:
        if scraped >= all_dropped_suspect and not spec.drop_junk:
            revert()
            return done("failed", "%d notices scraped and every one was filtered out: read the enrichment log "
                                  "before trusting this%s" % (scraped, said_dropped))
        settle()
        return done("ok", "%d notice(s) scraped, none passed the filters, nothing to upload%s" % (scraped, said_dropped))

    path = (write_csv or write_feed_csv)(notices, spec, stamp, extras)
    res["csv_path"] = path

    if getattr(args, "no_upload", False):
        revert()
        return done("ok", "%d records written, upload skipped (--no-upload)%s" % (res["records"], said_dropped))

    up = (upload or _default_upload)(path, commit)
    res["uploaded"] = up.get("created", 0)
    res["warnings"].extend(up.get("warnings") or [])
    if not commit:
        revert()
        return done("ok", "%d records ready (dry run, nothing uploaded)%s" % (res["records"], said_dropped))
    if not up.get("created"):
        revert()
        return done("failed", "%d records but DataSift created 0 (failed=%s)" % (res["records"], up.get("failed")))
    # ONLY here: the records are in the CRM, so skipping these notices next run is correct.
    settle()
    return done("ok", "%d records, %d into DataSift, %d failed%s"
                % (res["records"], up.get("created", 0), up.get("failed", 0), said_dropped))
