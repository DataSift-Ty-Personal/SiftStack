"""Network-free standalone test for scrape_all-level cross-run dedup —
lis pendens (JCD) AND probate (KCOJ) idempotency in one two-run sequence.

Covers spec 3c (the JCD seen-instrument cache wired through scrape_all so a 2nd
daily run over an unchanged window emits 0 new lis pendens notices — LP-01) AND
SCHED-01 (probate runs on the daily schedule too: a day-2 run emits 0 duplicate
probate notices via the PRE-EXISTING kcoj_seen_cases dedup). Probate dedup is
pre-existing — this test VERIFIES it on the schedule, it does not build it.

No pytest, no mock framework: module-level functions are monkeypatched BY
ASSIGNMENT (matches .planning/codebase/TESTING.md). scrape_all is async, so the
test runs under asyncio.run(...). The test never touches the network, browser,
credentials, or env vars, and never writes the real state files (all state-file
constants are redirected at tempfiles in a temp dir).

Run: .venv/Scripts/python.exe tests/test_jcd_scrape_all_dedup.py
"""

import os
import sys
import shutil
import asyncio
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import scraper
import jefferson_deeds_scraper as jcd
import kcoj_scraper as kcoj
import kcoj_repoll_queue as repoll
from notice_parser import NoticeData
from config import SavedSearch


# ── Network-free JCD (lis pendens) scaffolding ────────────────────────
# Two canned LP records carrying the dedup-key fields the in-scraper gate reads
# (instnum/year/db) plus the fields the loop builds NoticeData from.
_FAKE_JCD_RECORDS = [
    {
        "instnum": "200001", "year": "2026", "db": "P",
        "detail_url": "https://search.jeffersondeeds.com/pdetail.php?instnum=200001&year=2026&db=P",
        "grantor": "DOE JOHN", "grantees": "ACME BANK",
        "legal_desc": "LOT 1 BLOCK A FAKE SUBDIVISION", "case_num": "26-CI-100001",
        "date_filed": "05/01/2026", "book_page": "", "view_img": "imgkey-aaa",
    },
    {
        "instnum": "200002", "year": "2026", "db": "P",
        "detail_url": "https://search.jeffersondeeds.com/pdetail.php?instnum=200002&year=2026&db=P",
        "grantor": "ROE JANE", "grantees": "BETA CREDIT UNION",
        "legal_desc": "LOT 2 BLOCK B FAKE SUBDIVISION", "case_num": "26-CI-100002",
        "date_filed": "05/01/2026", "book_page": "", "view_img": "imgkey-bbb",
    },
]


def _fake_jcd_fetch_address(view_img):
    return ("123 FAKE ST", "Louisville", "40202", "CONST-PARCEL", "ocr")


# ── Network-free KCOJ (probate) scaffolding ───────────────────────────
# Two canned probate cases with stable case numbers. The fake mirrors the REAL
# scrape_kcoj_dockets dedup contract: consult `seen_cases`, emit ONLY cases not
# already present, then mark each emitted case in `seen_cases`. A 2nd run with
# the same dict therefore returns []. (This reproduces the pre-existing dedup
# the test must exercise without any network/browser.)
_FAKE_PROBATE_CASES = ["26-P-200001", "26-P-200002"]


async def _fake_scrape_kcoj_dockets(county, division, target_date,
                                    notice_type="probate", headless=True,
                                    seen_cases=None):
    out: list[NoticeData] = []
    if seen_cases is None:
        seen_cases = {}
    for case_no in _FAKE_PROBATE_CASES:
        if case_no in seen_cases:
            continue  # already emitted on a prior run → skip (pre-existing dedup)
        n = NoticeData(
            notice_type=notice_type, county=county, state="KY",
            case_number=case_no, decedent_name=f"DECEDENT {case_no}",
        )
        out.append(n)
        seen_cases[case_no] = "2026-05-01"  # mark seen AFTER emit (mirror real)
    return out


# ── State-file redirection (keep the real state files untouched) ──────
def _redirect_state_files(tmpdir: Path) -> dict:
    """Point every state-file constant scrape_all may write at a tempfile.
    Returns the originals so the caller can restore them."""
    orig = {
        ("config", "JCD_SEEN_FILE"): config.JCD_SEEN_FILE,
        ("config", "KCOJ_SEEN_CASES_FILE"): config.KCOJ_SEEN_CASES_FILE,
        ("config", "KCOJ_REPOLL_FILE"): config.KCOJ_REPOLL_FILE,
        ("scraper", "SEEN_IDS_FILE"): scraper.SEEN_IDS_FILE,
        ("scraper", "STATE_FILE"): scraper.STATE_FILE,
        ("scraper", "CAPTCHA_FAILED_IDS_FILE"): scraper.CAPTCHA_FAILED_IDS_FILE,
        ("kcoj", "KCOJ_SEEN_CASES_FILE"): kcoj.KCOJ_SEEN_CASES_FILE,
        ("repoll", "KCOJ_REPOLL_FILE"): repoll.KCOJ_REPOLL_FILE,
    }
    config.JCD_SEEN_FILE = tmpdir / "jcd_seen.json"
    config.KCOJ_SEEN_CASES_FILE = tmpdir / "kcoj_seen.json"
    config.KCOJ_REPOLL_FILE = tmpdir / "repoll.json"
    scraper.SEEN_IDS_FILE = tmpdir / "seen_ids.json"
    scraper.STATE_FILE = tmpdir / "last_run.json"
    scraper.CAPTCHA_FAILED_IDS_FILE = tmpdir / "captcha_failed.json"
    kcoj.KCOJ_SEEN_CASES_FILE = tmpdir / "kcoj_seen.json"
    repoll.KCOJ_REPOLL_FILE = tmpdir / "repoll.json"
    return orig


def _restore_state_files(orig: dict) -> None:
    mods = {"config": config, "scraper": scraper, "kcoj": kcoj, "repoll": repoll}
    for (mod_name, attr), value in orig.items():
        setattr(mods[mod_name], attr, value)


# ── Test ──────────────────────────────────────────────────────────────
async def test_scrape_all_two_run_zero_duplicate():
    """3c + SCHED-01: one two-run sequence proves cross-run dedup for BOTH
    sources. Run 1 emits 2 LP + 2 probate notices and fills both caches; run 2
    over the same unchanged window emits 0 LP AND 0 probate, and neither cache
    grows."""
    tmpdir = Path(tempfile.mkdtemp(prefix="jcd_scrape_all_test_"))

    # Snapshot every monkeypatched function so the test restores cleanly.
    orig_jcd_post = jcd._post
    orig_jcd_parse = jcd._parse_results_table
    orig_jcd_fetch = jcd._fetch_address_from_document
    orig_jcd_delay = jcd._delay
    orig_kcoj_scrape = kcoj.scrape_kcoj_dockets
    orig_files = _redirect_state_files(tmpdir)

    try:
        # JCD seam — network-free.
        jcd._post = lambda url, params: "HIT LIST"
        jcd._parse_results_table = lambda html: [dict(r) for r in _FAKE_JCD_RECORDS]
        jcd._fetch_address_from_document = _fake_jcd_fetch_address
        jcd._delay = lambda: None
        # KCOJ seam — network-free, mirrors the pre-existing seen_cases dedup.
        kcoj.scrape_kcoj_dockets = _fake_scrape_kcoj_dockets

        # Use the real configured searches (one jcd, one kcoj) when present;
        # otherwise construct minimal ones.
        jcd_search = next(
            (s for s in config.SAVED_SEARCHES if getattr(s, "source", "tnpn") == "jcd"),
            SavedSearch("Jefferson", "lis_pendens", "LIS PENDENS Jefferson County", source="jcd"),
        )
        kcoj_search = next(
            (s for s in config.SAVED_SEARCHES if getattr(s, "source", "tnpn") == "kcoj"),
            SavedSearch("Jefferson", "probate", "PROBATE Jefferson County",
                        source="kcoj", kcoj_division="District"),
        )

        jcd_seen: dict[str, str] = {}
        kcoj_seen_cases: dict[str, str] = {}
        collected: list[NoticeData] = []

        async def on_batch(batch):
            collected.extend(batch)

        # ── Run 1: both sources brand-new ──────────────────────────────
        r1 = await scraper.scrape_all(
            mode="daily",
            searches=[kcoj_search, jcd_search],
            on_batch=on_batch,
            since_date_override="2026-05-01",
            jcd_seen=jcd_seen,
            kcoj_seen_cases=kcoj_seen_cases,
            repoll_queue={},  # empty queue → drain is a no-op (network-free)
        )
        r1_lp = [n for n in r1 if n.notice_type == "lis_pendens"]
        r1_pb = [n for n in r1 if n.notice_type == "probate"]
        assert len(r1_lp) == 2, f"run 1 expected 2 LP notices, got {len(r1_lp)}"
        assert len(r1_pb) == 2, f"run 1 expected 2 probate notices, got {len(r1_pb)}"
        assert len(jcd_seen) == 2, f"run 1 expected 2 jcd_seen keys, got {len(jcd_seen)}: {jcd_seen}"
        assert len(kcoj_seen_cases) == 2, f"run 1 expected 2 kcoj_seen_cases keys, got {len(kcoj_seen_cases)}: {kcoj_seen_cases}"
        print("PASS run 1: 2 LP + 2 probate emitted; jcd_seen=2, kcoj_seen_cases=2")

        # ── Run 2: same window, same caches → zero duplicates ──────────
        collected = []

        async def on_batch2(batch):
            collected.extend(batch)

        r2 = await scraper.scrape_all(
            mode="daily",
            searches=[kcoj_search, jcd_search],
            on_batch=on_batch2,
            since_date_override="2026-05-01",
            jcd_seen=jcd_seen,                  # same dict
            kcoj_seen_cases=kcoj_seen_cases,    # same dict
            repoll_queue={},
        )
        r2_lp = [n for n in r2 if n.notice_type == "lis_pendens"]
        r2_pb = [n for n in r2 if n.notice_type == "probate"]
        # Per-source explicit assertions so a regression in EITHER path fails.
        assert len(r2_lp) == 0, f"run 2 expected 0 new LP notices (LP-01), got {len(r2_lp)}"
        assert len(r2_pb) == 0, f"run 2 expected 0 duplicate probate notices (SCHED-01), got {len(r2_pb)}"
        # on_batch is only fired when a search returns a non-empty batch, so the
        # collected list should stay empty for an unchanged window.
        c2_lp = [n for n in collected if n.notice_type == "lis_pendens"]
        c2_pb = [n for n in collected if n.notice_type == "probate"]
        assert c2_lp == [], f"run 2 on_batch emitted LP notices: {c2_lp}"
        assert c2_pb == [], f"run 2 on_batch emitted probate notices: {c2_pb}"
        # Neither cache grows for an unchanged window.
        assert len(jcd_seen) == 2, f"run 2 jcd_seen grew: {len(jcd_seen)}: {jcd_seen}"
        assert len(kcoj_seen_cases) == 2, f"run 2 kcoj_seen_cases grew: {len(kcoj_seen_cases)}: {kcoj_seen_cases}"
        print("PASS run 2: 0 LP + 0 probate (caches unchanged at 2 each)")
    finally:
        jcd._post = orig_jcd_post
        jcd._parse_results_table = orig_jcd_parse
        jcd._fetch_address_from_document = orig_jcd_fetch
        jcd._delay = orig_jcd_delay
        kcoj.scrape_kcoj_dockets = orig_kcoj_scrape
        _restore_state_files(orig_files)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Main runner ───────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(test_scrape_all_two_run_zero_duplicate())
    print("ALL PASS")
