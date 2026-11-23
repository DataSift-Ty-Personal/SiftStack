"""Selftest: Roma's mls_offers workflow against a fake engine. Zero network.

What it proves: a dry box screens and quotes but never adds or reads Zillow; a live box adds only
when the ask said add AND AGENTS_MLS_ADD_MAX allows it, and then never more than that number; Zillow
runs only live, asked, keyed and inside the caps, and its calls are booked as spend; a bad market is
a plain sentence, and so is an engine that stops (SystemExit); the engine's command-line notes reach
Slack in Slack's words; the Slack reply carries no phone number; the workbook and CSV land in files/.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

SETTINGS = ("AGENTS_MLS_ADD_MAX", "AGENTS_MLS_ZILLOW_MAX", "AGENTS_MLS_SHORTLIST", "AGENTS_MLS_RUN_CAP_USD",
            "AGENTS_ZILLOW_CALL_USD", "OPENWEBNINJA_API_KEY", "REISIFT_API_KEY", "AGENTS_MLS_MONTHS",
            "MLS_OFFERS_SENDER")


class FakeEngine:
    """Stands where src/mls_offers_api.py stands: resolve_market and run, recording every call."""

    def __init__(self, one_county_only: bool = False, run_error: Exception | None = None):
        self.calls, self.resolved = [], []
        self.one_county_only, self.run_error = one_county_only, run_error

    def resolve_market(self, county="", fips="", zips="", name=""):
        self.resolved.append((county, fips, zips))
        if "Nowhere" in county:
            raise ValueError("No county named Nowhere, ZZ was found; give it as County, ST")
        if self.one_county_only and ";" in county:
            raise ValueError("one county at a time")
        names = [c.split(",")[0].strip().lower() for c in county.split(";") if c.strip()]
        return {"name": "-".join(names) or "zips", "zips": [z for z in zips.split(",") if z],
                "counties": [{"fips": "47093", "name": n.title(), "state": "TN"} for n in names],
                "fips": [f for f in fips.split(",") if f] or (["47093"] if names else [])}

    def run(self, market, *, out=None, shortlist=60, add=False, commit=False, zillow=False, zillow_limit=0,
            months=24, policy=None, list_name="MLS Offers", tag="MLS Listing", client=None, log=None, sender=""):
        self.calls.append({"market": market, "out": out, "shortlist": shortlist, "add": add, "commit": commit,
                           "zillow": zillow, "zillow_limit": zillow_limit, "list_name": list_name, "tag": tag,
                           "client": client, "months": months, "sender": sender})
        if self.run_error:
            raise self.run_error
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        wb = out / ("%s_MLS_Offers_2026-11-23.xlsx" % out.name.title())   # the real engine names it after its folder
        wb.write_bytes(b"PK not really a workbook")
        csvp = out / "agent_outreach_2026-11-23.csv"
        csvp.write_text("rank,address,agent_phone\n1,100 Example St,(865) 555-0101\n", encoding="utf-8")
        if log:
            log("screened 40 listings; an agent line (865) 555-0109 rode along")
        added = 6 if (add and commit) else 0
        counts = {"listings": 40, "screened_only": 30, "shortlisted": 10, "in_account": 4, "added": added,
                  "detailed": 4 + added, "offers": 7, "cash": 5, "subject_to": 1, "seller_finance": 1,
                  "price_watch": 3, "investor_sales": 120}
        if zillow:
            counts["zillow_calls"] = 7
        # The rows as the real engine's _top shapes them: a cash-only row's lane is "Cash"; row 2 keeps the
        # creative_price of a lane it dropped (one that did not cash flow); row 3 is seller finance with no cash.
        top = [{"address": "%d00 Example St" % i, "city": "Knoxville", "list_price": 200000 + i,
                "cash_offer": None if i == 3 else 120000 + i,
                "lane": {1: "Subject-to", 3: "Seller finance"}.get(i, "Cash"),
                "creative_price": {1: 200000, 2: 199000, 3: 190000}.get(i),
                "agent": "Pat Agent %d" % i, "agent_phone": "(865) 555-01%02d" % i, "score": 90 - i,
                "why": "%d days on market" % (100 + i)} for i in range(1, 8)]
        notes = ["A seller wrote (865) 555-0102 in the remarks, which never reaches Slack."]
        if not (add and commit):
            # The real engine's own words (mls_offers_api.phase_add and phase_detail), flags and all.
            notes += ["6 shortlisted listings are not in your account; adding them would use 6 records of your "
                      "allowance. Re-run with --add --commit to add them.",
                      "6 shortlisted listings are not in your account, so their details were not read; add them "
                      "with --add --commit, or work them from the More listings sheet."]
        # Like the real engine, it quotes the add on every run, asked or not (a quote stores nothing).
        return {"market": market["name"], "out": str(out), "workbook": str(wb), "csv": str(csvp), "counts": counts,
                "quote": {"total": 6, "in_account": 0, "new": 6, "billable": 6, "allowance_left": 900},
                "notes": notes, "top": top}


def _task(i: int, live_source: bool = False, **inputs) -> dict:
    return {"id": i, "agent": "sales", "workflow": "mls_offers", "payload": json.dumps({"inputs": inputs}),
            "source": "agent:sales", "slack_channel": "C1", "slack_thread": "1.1"}


def run(check, tmp: Path) -> None:
    from . import store
    from .workflows import mls_offers as wf
    keep = (config.DATA_DIR, config.DB_PATH, config.MODE, config.RUNS_DIR, config.PUBLIC_URL, config.WEBHOOK_SECRET)
    hooks = (wf.ENGINE, wf.CLIENT)
    env = {k: os.environ.get(k) for k in SETTINGS}
    root = Path(tmp) / "mls_offers"
    config.DATA_DIR = root / "data"
    config.DB_PATH = config.DATA_DIR / "agents.db"
    config.RUNS_DIR = config.DATA_DIR / "runs"
    config.PUBLIC_URL, config.WEBHOOK_SECRET = "https://agents.example.test", "test-secret"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    for k in SETTINGS:
        os.environ.pop(k, None)
    try:
        _parse(check, wf)
        _notes(check, wf)
        _runs(check, wf, store)
        _client(check, wf)
    finally:
        wf.ENGINE, wf.CLIENT = hooks
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.DATA_DIR, config.DB_PATH, config.MODE, config.RUNS_DIR, config.PUBLIC_URL, config.WEBHOOK_SECRET = keep


def _no_phone(wf, text: str, task_id: int, files) -> bool:
    for name in files or []:
        text = text.replace(wf.slack.file_url(task_id, name), "")
    return not wf.PHONE_RX.search(text)


def _parse(check, wf) -> None:
    print("  reading the market from the ask")
    check(wf.parse_market("Knox, TN; Blount, TN") == ("Knox, TN; Blount, TN", "", ""),
          "counties separated by semicolons stay counties, commas and all")
    check(wf.parse_market("37914, 37917") == ("", "", "37914,37917") and wf.parse_market("37914 37917")[2] == "37914,37917",
          "a run of five-digit numbers is ZIP codes")
    check(wf.parse_market("fips 47093") == ("", "47093", "") and wf.parse_market("FIPS: 47093, 47009")[1] == "47093,47009",
          "fips names a county by its code")
    check(wf.parse_market("Knox, TN; 37914") == ("Knox, TN", "", "37914"), "a county and ZIP codes can be asked together")
    check(wf.parse_market("") == ("", "", ""), "an empty ask names nothing")


def _notes(check, wf) -> None:
    print("  the engine's command-line notes in Slack's words")
    check(wf.slack_note("6 shortlisted listings are not in your account; adding them would use 6 records of your "
                        "allowance. Re-run with --add --commit to add them.") == "",
          "a note the reply already says its own way is dropped")
    check(wf.slack_note("2 listing details could not be read; run the detail phase again.")
          == "2 listing details could not be read; ask again.", "a phase to run again becomes an ask again")
    check(wf.slack_note("3 added listings had not landed after 10 minutes; run the detail phase again later to read them.")
          == "3 added listings had not landed after 10 minutes; ask again later to read them.",
          "a phase to run again later becomes an ask again later")
    keep = "knox-tn has 12,000 rows, more than one search pages; it was split by estimated value."
    check(wf.slack_note(keep) == keep, "any other note passes unchanged")
    # The phrases slack_note reads must still be the engine's words, or its flags reach Slack again.
    src = Path(__file__).resolve().parents[1] / "mls_offers_api.py"
    text = src.read_text(encoding="utf-8") if src.is_file() else ""
    missing = [p for p in [wf.NOTE_SAID_HERE] + [cli for cli, _ in wf.NOTE_WORDS] if p not in text]
    check(bool(text) and not missing,
          "the engine still words its notes the way slack_note reads them (missing: %s)" % (missing or "none"))


def _runs(check, wf, store) -> None:
    print("  a dry box screens and quotes, and never adds")
    eng = FakeEngine()
    wf.ENGINE, wf.CLIENT = eng, object()
    os.environ["OPENWEBNINJA_API_KEY"] = "test-key-not-real"
    config.MODE = "dry"
    res = wf.run(_task(401, market="Knox, TN", add="yes", zillow="yes"))
    call = eng.calls[-1]
    check(res["ok"] and call["add"] is True and call["commit"] is False and call["zillow"] is False,
          "dry: the engine is asked to quote the add but never to commit it, and Zillow stays off: %r"
          % {k: call[k] for k in ("add", "commit", "zillow")})
    check(call["client"] is wf.CLIENT and call["list_name"] == "MLS Offers" and call["tag"] == "MLS Listing"
          and call["shortlist"] == 60, "the engine gets the box's SiftMap client, the list and tag, and a shortlist of 60")
    files = Path(res["run_dir"]) / "files"
    check(sorted(res["files"]) == ["Knox_MLS_Offers_2026-11-23.xlsx", "agent_outreach_2026-11-23.csv"]
          and all((files / n).is_file() for n in res["files"]), "the workbook and the CSV land in the run's files folder")
    msg = wf.render(_task(401), res)
    check("Nothing was added to the account: the brain is in dry mode for this ask." in msg
          and "Zillow was not read: the brain is in dry mode for this ask." in msg
          and "6 shortlisted listings were not read in full, so each has only a preliminary cash number" in msg
          and "6 of them are not records yet; adding them would use 6 records of the allowance" in msg,
          "dry: the reply says nothing was added, what the add would use, and why Zillow did not run: %r" % msg)
    check("--add" not in msg and "--commit" not in msg and "Re-run" not in msg and "not in your account" not in msg,
          "the engine's command-line notes never reach Slack: the reply says the same thing in Slack's words")
    check(msg.startswith("MLS offers for knox: 40 active listings screened, 10 shortlisted, 4 read in full")
          and "1. 100 Example St, Knoxville: list $200,001; cash $120,001 or subject-to at $200,000; agent Pat Agent 1." in msg
          and "6. 600 Example St" not in msg and "Top offers:" in msg,
          "the reply leads with the counts and the top five offers with the agent's name")
    check("2. 200 Example St, Knoxville: list $200,002; cash $120,002; agent Pat Agent 2." in msg
          and "3. 300 Example St, Knoxville: list $200,003; seller finance at $190,000; agent Pat Agent 3." in msg
          and "or cash at" not in msg,
          "a cash-only row is a cash offer alone (the price of a lane the engine dropped is never quoted), and a "
          "seller-finance row with no cash offer is that lane alone")
    check(Path(eng.calls[-1]["out"]).name == "knox" and Path(eng.calls[-1]["out"]).parent.name == "mls_offers-401",
          "the engine works in a folder named for the market, beside files/, so the workbook is named like the "
          "command line's: %s" % eng.calls[-1]["out"])
    check("https://agents.example.test/files/401/" in msg and "Nothing was sent" in msg,
          "the reply links both files and says nothing was sent")
    check(_no_phone(wf, msg, 401, res["files"]) and "agent_phone" not in json.dumps(res["top"])
          and not wf.PHONE_RX.search(json.dumps(res["notes"]) + json.dumps(res["log_tail"])),
          "no phone number reaches Slack or the stored result, though the engine's rows, notes and log carried them")
    check(store.spent(task_id=401) == 0, "a dry run books no spend")

    plain = wf.run(_task(402, market="Knox, TN"))
    call = eng.calls[-1]
    pmsg = wf.render(_task(402), plain)
    check(call["add"] is False and call["commit"] is False and bool(plain["quote"]),
          "with no add asked the engine is asked neither to add nor to commit; its quote still comes back")
    os.environ["MLS_OFFERS_SENDER"] = " Sam Sender "
    wf.run(_task(415, market="Knox, TN"))
    signed = eng.calls[-1]["sender"]
    os.environ.pop("MLS_OFFERS_SENDER", None)
    check(signed == "Sam Sender" and call["sender"] == "",
          "the drafts are signed by MLS_OFFERS_SENDER, the setting the command line reads, and by nobody when it is unset")
    check("6 shortlisted listings were not read in full" in pmsg and "needs a live box" in pmsg,
          "the reply says how many were not read in full and what adding them would take: %r" % pmsg)

    print("  a live box adds only inside AGENTS_MLS_ADD_MAX")
    config.MODE = "live"
    zero = wf.run(_task(403, market="Knox, TN", add="yes"))
    check(eng.calls[-1]["commit"] is False and "AGENTS_MLS_ADD_MAX is 0 on this box" in wf.render(_task(403), zero),
          "live but AGENTS_MLS_ADD_MAX unset: nothing is added, and the reply says why")
    os.environ["AGENTS_MLS_ADD_MAX"] = "25"
    added = wf.run(_task(404, market="Knox, TN", add="yes", shortlist="60"))
    call = eng.calls[-1]
    check(call["add"] is True and call["commit"] is True and call["shortlist"] == 25,
          "live, asked and allowed: the engine adds, with the shortlist cut to the most one ask may add: %r"
          % {k: call[k] for k in ("add", "commit", "shortlist")})
    amsg = wf.render(_task(404), added)
    check("6 of them added now" in amsg and "cut to 25" in amsg and "were not read in full" not in amsg,
          "the reply counts what was added and says the shortlist was cut")
    not_asked = wf.run(_task(405, market="Knox, TN"))
    check(eng.calls[-1]["add"] is False and eng.calls[-1]["commit"] is False and not_asked["ok"],
          "live and allowed, but the ask did not say add: nothing is added")
    check("Ask again with add=yes to add them." in wf.render(_task(405), not_asked),
          "and the reply says how to add them from Slack")

    print("  Zillow only live, asked, keyed and inside the caps")
    os.environ["AGENTS_MLS_ZILLOW_MAX"] = "10"
    z = wf.run(_task(406, market="Knox, TN", zillow="yes"))
    call = eng.calls[-1]
    check(call["zillow"] is True and call["zillow_limit"] == 10 and abs(store.spent(task_id=406) - 0.07) < 1e-9,
          "live, asked and keyed: Zillow runs capped at AGENTS_MLS_ZILLOW_MAX and its 7 calls are booked as spend")
    check("Zillow: about $0.07." in wf.render(_task(406), z), "the reply says what Zillow cost")
    os.environ.pop("OPENWEBNINJA_API_KEY", None)
    nokey = wf.run(_task(407, market="Knox, TN", zillow="yes"))
    check(eng.calls[-1]["zillow"] is False and "OPENWEBNINJA_API_KEY is not set on this box" in wf.render(_task(407), nokey),
          "with no Zillow key Zillow is not read, and the reply says so")
    os.environ["OPENWEBNINJA_API_KEY"] = "test-key-not-real"
    os.environ["AGENTS_MLS_RUN_CAP_USD"] = "0.05"
    capped = wf.run(_task(408, market="Knox, TN", zillow="yes"))
    check(eng.calls[-1]["zillow"] is False and "would pass the cap" in wf.render(_task(408), capped)
          and store.spent(task_id=408) == 0, "Zillow calls that would pass the run cap are not made or booked")
    os.environ.pop("AGENTS_MLS_RUN_CAP_USD", None)

    print("  asks that cannot run answer in a sentence")
    big = wf.run(_task(409, market="Knox, TN", shortlist="500"))
    check(eng.calls[-1]["shortlist"] == 150 and "capped at 150 a run" in wf.render(_task(409), big),
          "a shortlist over 150 is cut to 150 and the reply says so")
    bad = wf.run(_task(410, market="Nowhere, ZZ"))
    bmsg = wf.render(_task(410), bad)
    check(not bad["ok"] and bmsg == "MLS offers did not run: No county named Nowhere, ZZ was found; give it as County, ST.",
          "a county the engine cannot find is a plain sentence: %r" % bmsg)
    empty = wf.render(_task(411), wf.run(_task(411)))
    check(empty.startswith("MLS offers did not run: name the market"), "an ask with no market asks for one")
    one = FakeEngine(one_county_only=True)
    wf.ENGINE = one
    two = wf.run(_task(412, market="Knox, TN; Blount, TN"))
    check(two["ok"] and two["market"] == "knox-blount" and len(one.resolved) == 3
          and [c["fips"] for c in one.calls[-1]["market"]["counties"]] == ["47093", "47093"],
          "an engine that reads one county at a time still takes two: each is resolved alone and merged")
    wf.ENGINE = FakeEngine(run_error=RuntimeError("401 on POST /properties/search/. The Api-Key was rejected."))
    keyfail = wf.render(_task(413), wf.run(_task(413, market="Knox, TN")))
    check(keyfail == "MLS offers did not run: 401 on POST /properties/search/. The Api-Key was rejected.",
          "a rejected key is said in a sentence, not a traceback: %r" % keyfail)
    wf.ENGINE = FakeEngine(run_error=SystemExit("Freddie Mac series did not download"))
    stopped = wf.render(_task(414), wf.run(_task(414, market="Knox, TN")))
    check(stopped == "MLS offers did not run: Freddie Mac series did not download.",
          "an engine that stops (SystemExit, a download it could not finish) is a sentence too: %r" % stopped)
    config.MODE = "dry"


def _client(check, wf) -> None:
    print("  the box's SiftMap client counts every request against the shared budget")
    import siftmap_standalone
    from .autopilot import budget
    wf.ENGINE, wf.CLIENT = FakeEngine(), None
    os.environ.pop("REISIFT_API_KEY", None)
    res = wf.run(_task(420, market="Knox, TN"))
    check(not res["ok"] and res["problem"] == "REISIFT_API_KEY is not set on this box, so SiftMap cannot be read",
          "with no Open API key the run stops before any read and says why")
    os.environ["REISIFT_API_KEY"] = "test-key-not-real"
    order = []
    real_request, real_acquire = siftmap_standalone.SiftMapClient._request, budget.acquire
    siftmap_standalone.SiftMapClient._request = lambda self, path, method="GET", body=None, auth=True, timeout=40.0: (
        order.append(("request", path)), {"data": [], "total_results": 0})[1]
    budget.acquire = lambda *a, **k: (order.append(("budget", "")), 0.0)[1]
    try:
        c = wf._client()
        got = c.search({"filters": {}})
        c.get_detail("123")
    finally:
        siftmap_standalone.SiftMapClient._request, budget.acquire = real_request, real_acquire
    check(got == {"data": [], "total_results": 0} and order == [("budget", ""), ("request", "/properties/search/"),
                                                                 ("budget", ""), ("request", "/properties/detail/123/")],
          "search and detail each take a slot in the shared request budget before they go out: %r" % order)
