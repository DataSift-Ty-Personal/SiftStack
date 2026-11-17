"""KPI tracker sinks (autopilot/kpisinks.py): the brain's own page and one CSV per tab at links that
never change (the default, no credential of any kind), the optional Google Sheet, and the one Slack
message a day. Zero network: a fake Google (an in-memory spreadsheet that keeps Google's starting grid
limit and answers in Google's own error shapes) and a fake Slack (messages it can edit or lose) stand
where the real transports stand, and a wall that refuses every call sits behind both. Sealed like
selftest.py: every Google and Slack setting this machine's .env may carry is blanked for the section
and put back after. Every name, number, email and street here is invented.
"""
from __future__ import annotations

import base64
import copy
import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config

ENV_KEYS = ("AGENTS_GOOGLE_SA_JSON", "AGENTS_GOOGLE_CLIENT_ID", "AGENTS_GOOGLE_CLIENT_SECRET",
            "AGENTS_GOOGLE_REFRESH_TOKEN", "AGENTS_KPI_SHEET_ID", "AGENTS_KPI_SHEET_SHARE", "AGENTS_KPI_TITLE",
            "AGENTS_KPI_SLACK")
CONFIG_KEYS = ("DATA_DIR", "DB_PATH", "PUBLIC_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_PIPELINE_CHANNEL",
               "SLACK_ISSUES_CHANNEL", "SLACK_VOICE", "SLACK_WEBHOOK")
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN = "https://oauth2.googleapis.com/token"
SA_EMAIL = "kpi-writer@example-project.iam.gserviceaccount.com"
NOT_FOUND = (404, {"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}})
# Monday 2026-11-16 in Eastern time: 7:45 AM, 12:30 PM, and 7:30 PM (which is already Tuesday in UTC).
MORNING, NOON, EVENING = "2026-11-16T12:45:00+00:00", "2026-11-16T17:30:00+00:00", "2026-11-17T00:30:00+00:00"


class Wall:
    """Stands where requests stands, for kpisinks and slack_app alike: any call that gets past the hooks
    is recorded and refused."""

    def __init__(self):
        self.hits = []

    def request(self, *a, **k):
        self.hits.append(a[:2])
        raise AssertionError("the kpisinks selftest tried to reach the network")

    post = get = request


class FakeGoogle:
    """An in-memory Google: a token endpoint, spreadsheets whose tabs hold values inside Google's starting
    grid (1,000 rows by 26 columns, and a write past it is refused the way Google refuses it), and Drive
    permissions. Records every call; `fail` holds one-shot answers for (method, url test)."""

    def __init__(self):
        self.calls, self.books, self.perms, self.fail, self.n = [], {}, [], [], 0

    def __call__(self, method, url, json=None, data=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": copy.deepcopy(json), "data": dict(data or {}),
                           "headers": dict(headers or {})})
        for i, (m, match, answer) in enumerate(self.fail):
            if m == method and match(url):
                del self.fail[i]
                return answer
        if url == TOKEN:
            self.n += 1
            return 200, {"access_token": "tok-%d" % self.n, "expires_in": 3599, "token_type": "Bearer"}
        if not str((headers or {}).get("Authorization", "")).startswith("Bearer tok-"):
            return 401, {"error": {"code": 401, "message": "Request is missing valid authentication.", "status": "UNAUTHENTICATED"}}
        u = urlparse(url)
        if u.netloc == "www.googleapis.com":
            parts = u.path.split("/")
            if parts[-1] == "permissions" and len(parts) > 4 and parts[4] in self.books:
                self.perms.append((parts[4], copy.deepcopy(json), parse_qs(u.query)))
                return 200, {"id": "perm-%d" % len(self.perms)}
            return NOT_FOUND
        if method == "POST" and url == SHEETS:
            return 200, self._create(json)
        tail = u.path[len("/v4/spreadsheets/"):]
        if "/" in tail:
            sid, op = tail.split("/", 1)
        elif ":" in tail:
            sid, op = tail.split(":", 1)
        else:
            sid, op = tail, ""
        book = self.books.get(sid)
        if book is None:
            return NOT_FOUND
        if method == "GET":
            return 200, self._meta(book)
        if op == "batchUpdate":
            return self._batch(book, json)
        if op == "values:batchClear":
            for rng in json["ranges"]:
                tab = book["tabs"].get(self._title(rng))
                if tab is not None:
                    tab["rows"] = []
            return 200, {"spreadsheetId": sid}
        if op == "values:batchUpdate":
            for d in json["data"]:
                title, start = self._title(d["range"]), 1 if d["range"].endswith("!A2") else 0
                tab, vals = book["tabs"].get(title), d["values"]
                if tab is None:
                    return 400, {"error": {"code": 400, "message": "Unable to parse range: %s" % d["range"],
                                           "status": "INVALID_ARGUMENT"}}
                if start + len(vals) > tab["grid"][0] or max([len(r) for r in vals] + [0]) > tab["grid"][1]:
                    return 400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message":
                                           "Range (%s) exceeds grid limits. Max rows: %d, max columns: %d"
                                           % (title, tab["grid"][0], tab["grid"][1])}}
                for i, row in enumerate(vals):
                    while len(tab["rows"]) <= start + i:
                        tab["rows"].append([])
                    cur = tab["rows"][start + i]
                    for j, v in enumerate(row):
                        while len(cur) <= j:
                            cur.append("")
                        cur[j] = v
            return 200, {"spreadsheetId": sid}
        return 400, {"error": {"code": 400, "message": "unknown call"}}

    @staticmethod
    def _title(rng: str) -> str:
        t = rng.split("!")[0]
        return t[1:-1].replace("''", "'") if t.startswith("'") and t.endswith("'") else t

    def _add(self, book, title, index, grid=None) -> dict:
        for t in book["tabs"].values():
            if t["index"] >= index:
                t["index"] += 1
        grid = grid or {}
        tab = {"sheetId": book["next"], "index": index, "rows": [], "frozen": 0,
               "grid": [int(grid.get("rowCount") or 1000), int(grid.get("columnCount") or 26)]}
        book["next"] += 1
        book["tabs"][title] = tab
        return tab

    def _props(self, title, tab) -> dict:
        return {"sheetId": tab["sheetId"], "title": title, "index": tab["index"],
                "gridProperties": {"rowCount": tab["grid"][0], "columnCount": tab["grid"][1], "frozenRowCount": tab["frozen"]}}

    def _create(self, body) -> dict:
        sid = "sheet-%d" % (len(self.books) + 1)
        book = {"id": sid, "title": body["properties"]["title"], "tabs": {}, "next": 100, "formats": []}
        for i, s in enumerate(body.get("sheets") or []):
            p = s["properties"]
            self._add(book, p["title"], p.get("index", i), p.get("gridProperties"))
        self.books[sid] = book
        return self._meta(book)

    def make_book(self, sid: str, tabs: list[str]) -> None:
        """A sheet a person made by hand before the tracker ever ran."""
        book = {"id": sid, "title": "Made by hand", "tabs": {}, "next": 100, "formats": []}
        for i, t in enumerate(tabs):
            self._add(book, t, i)
            book["tabs"][t]["rows"] = [["kept by hand"]]
        self.books[sid] = book

    def _meta(self, book) -> dict:
        tabs = sorted(book["tabs"].items(), key=lambda kv: kv[1]["index"])
        return {"spreadsheetId": book["id"], "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/%s/edit" % book["id"],
                "properties": {"title": book["title"]}, "sheets": [{"properties": self._props(t, tab)} for t, tab in tabs]}

    @staticmethod
    def _by_gid(book, gid):
        return next((t for t in book["tabs"].values() if t["sheetId"] == gid), None)

    def _batch(self, book, body):
        replies = []
        for req in body["requests"]:
            if "addSheet" in req:
                p = req["addSheet"]["properties"]
                if p["title"] in book["tabs"]:
                    return 400, {"error": {"code": 400, "message": "A sheet with the name \"%s\" already exists." % p["title"]}}
                tab = self._add(book, p["title"], p.get("index", len(book["tabs"])), p.get("gridProperties"))
                replies.append({"addSheet": {"properties": self._props(p["title"], tab)}})
                continue
            if "updateSheetProperties" in req:
                u = req["updateSheetProperties"]
                tab = self._by_gid(book, u["properties"]["sheetId"])
                if tab is None:
                    return 400, {"error": {"code": 400, "message": "No grid with id"}}
                gp = u["properties"].get("gridProperties") or {}
                if "gridProperties.rowCount" in u["fields"]:
                    tab["grid"][0] = gp["rowCount"]
                if "gridProperties.columnCount" in u["fields"]:
                    tab["grid"][1] = gp["columnCount"]
                if "gridProperties.frozenRowCount" in u["fields"]:
                    tab["frozen"] = gp["frozenRowCount"]
            else:
                rng = (next(iter(req.values())) or {}).get("range") or {}
                tab = self._by_gid(book, rng.get("sheetId"))
                if tab is None:
                    return 400, {"error": {"code": 400, "message": "No grid with id: %s" % rng.get("sheetId")}}
                cols_end = rng.get("endIndex", 0) if rng.get("dimension") == "COLUMNS" else rng.get("endColumnIndex", 0)
                if rng.get("endRowIndex", 0) > tab["grid"][0] or cols_end > tab["grid"][1]:
                    return 400, {"error": {"code": 400, "message": "Range exceeds grid limits"}}
                book["formats"].append(copy.deepcopy(req))
            replies.append({})
        return 200, {"spreadsheetId": book["id"], "replies": replies}


class FakeSlack:
    """Slack's Web API, in memory: posts get a ts, an edit needs a message that still exists, a join is
    accepted. `fail` holds one-shot errors by method."""

    def __init__(self):
        self.calls, self.msgs, self.n, self.fail, self.posted = [], {}, 0, {}, []

    def __call__(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if self.fail.get(method):
            return {"ok": False, "error": self.fail.pop(method)}
        if method == "chat.postMessage":
            self.n += 1
            ts = "1763300000.%06d" % self.n
            self.msgs[(payload["channel"], ts)] = payload["text"]
            self.posted.append((payload["channel"], ts))
            return {"ok": True, "ts": ts, "channel": payload["channel"]}
        if method == "chat.update":
            key = (payload["channel"], payload["ts"])
            if key not in self.msgs:
                return {"ok": False, "error": "message_not_found"}
            self.msgs[key] = payload["text"]
            return {"ok": True, "ts": payload["ts"]}
        if method == "conversations.join":
            return {"ok": True}
        return {"ok": False, "error": "unknown_method"}

    def sent(self, method="chat.postMessage", channel=None) -> list:
        return [p for m, p in self.calls if m == method and (channel is None or p.get("channel") == channel)]


class NoModel:
    """Stands where the voice rewrite's model stands: the KPI message must never reach it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        return "rewritten", 1, 1


class _Emp:
    display_name, icon_emoji = "Quinn", ":clipboard:"


class _Stage(dict):
    id, owner = "kpi_tracker", "ops"


class _Pol:
    tz = "America/New_York"


class _Out:
    stage = "kpi_tracker"


class _Ctx:
    def __init__(self, publish=None):
        self.stage = _Stage({"publish": publish} if publish else {})
        self.policy, self.out = _Pol(), _Out()
        self.now = datetime(2026, 11, 16, 12, 45, tzinfo=timezone.utc)


def snap(when=MORNING, run="morning", dials=412, failed=(), lines=None, notes=None, extra=(), wide=0, problems=None):
    tabs = [{"name": "Scoreboard", "note": "Month so far against last month.", "columns": ["Metric", "Nov", "Oct"],
             "rows": [["Dials", dials, 9020], ["Leads", 3, 31]]},
            {"name": "Callers", "note": "Yesterday, by caller.", "columns": ["Caller", "Dials", "Conversations"],
             "rows": [["Robin", 200, 11], ["Sam", 212, 10]]},
            {"name": "How it is counted", "note": "", "columns": ["Term", "Meaning"],
             "rows": [["Dial", "one outbound call"], ["Conversation", "a call of a minute or more"]]}]
    if wide:
        tabs[0]["columns"] = ["Metric"] + ["Week %d" % i for i in range(1, wide)]
        tabs[0]["rows"] = [["Dials"] + [100 + i for i in range(1, wide)]]
    tabs += [copy.deepcopy(t) for t in extra]
    for t in tabs:
        if t["name"] in failed:
            t["failed"], t["note"] = True, "the dial log could not be read"
    return {"refreshed": when, "run": run, "tz": "America/New_York", "tabs": tabs, "problems": list(problems or []),
            "notes": list(notes or []),
            "slack": {"lines": lines if lines is not None else [
                "KPI tracker refreshed Mon, Nov 16 at 7:45 AM.",
                "Friday final: %d dials, 21 conversations, 3 new leads (Robin 2, Sam 1)." % dials]}}


def _csv(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(Path(path).read_text(encoding="utf-8"))))


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _section(page: str, slug: str) -> str:
    return page.split('id="t-%s"' % slug, 1)[1].split("</section>", 1)[0] if ('id="t-%s"' % slug) in page else ""


def _parts(url: str) -> tuple[str, str]:
    p = urlparse(url).path.split("/")
    return p[2], p[3]


def run(check, tmp: Path) -> None:
    from . import slack_app, store, voice
    from .autopilot import kpisinks as k
    keep_cfg = {n: getattr(config, n) for n in CONFIG_KEYS}
    keep_env = {n: os.environ.get(n) for n in ENV_KEYS}
    keep_mod = (k.GOOGLE, k.SLACK, k._http, k._employee, k._atomic_write)
    keep_voice = (voice.ASK, voice.CHECK)
    keep_slack_http = slack_app._http
    wall = Wall()
    try:
        for n in ENV_KEYS:
            os.environ.pop(n, None)
        # Its own database and volume, always: config's paths are fixed when it loads, so a section run on
        # its own would otherwise write into the real agents.db (selftest_slack_split, 2026-11-15).
        config.DATA_DIR = Path(tmp) / "kpisinks"
        config.DB_PATH = config.DATA_DIR / "agents.db"
        config.PUBLIC_URL = "https://agents.example.test"
        config.SLACK_BOT_TOKEN, config.SLACK_WEBHOOK, config.SLACK_VOICE = "xoxb-test", "", "plain"
        config.SLACK_CHANNEL, config.SLACK_PIPELINE_CHANNEL, config.SLACK_ISSUES_CHANNEL = "CHOME", "CPIPE", "CISSUE"
        store.init()
        k._http = slack_app._http = wall
        k._TOKENS.clear()
        k._employee = lambda owner: _Emp()
        _checks(check, k, wall, Path(tmp))
    finally:
        for n, v in keep_cfg.items():
            setattr(config, n, v)
        for n, v in keep_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        k.GOOGLE, k.SLACK, k._http, k._employee, k._atomic_write = keep_mod
        voice.ASK, voice.CHECK = keep_voice
        slack_app._http = keep_slack_http
        k._TOKENS.clear()


def _checks(check, k, wall: Wall, tmp: Path) -> None:
    from . import voice
    site = config.DATA_DIR / "kpi" / "site"
    quiet = _Ctx(publish={"slack": "off"})          # the page and Google checks say nothing in Slack

    # ------------------------------------------------------------------ 1. the page and the CSVs
    print("  the page and the CSVs, with no credential")
    g, s = FakeGoogle(), FakeSlack()
    k.GOOGLE, k.SLACK = g, s
    r1 = k.publish(quiet, snap())
    check(r1["page"].startswith("https://agents.example.test/kpi/") and r1["page"].endswith("/tracker.html")
          and (site / "tracker.html").is_file()
          and sorted(p.name for p in site.glob("*.csv")) == ["callers.csv", "how-it-is-counted.csv", "scoreboard.csv"],
          "with no credential of any kind, the brain writes its own page and one CSV per tab")
    check(g.calls == [] and r1["sheet"] is None and not any("Google" in p for p in r1["problems"]) and s.calls == [],
          "with no Google credential nothing reaches Google and nothing is said about it")
    rows = _csv(site / "scoreboard.csv")
    check(rows[0][0] == "Refreshed Monday, November 16 at 7:45 AM Eastern",
          "every tab opens with one plain Refreshed line, in the business time zone")
    check(rows[1][0] == "Month so far against last month." and rows[3] == ["Metric", "Nov", "Oct"]
          and rows[4] == ["Dials", "412", "9020"],
          "under it the note, a blank row, the column headers, then the rows")
    check(len({len(r) for r in rows}) == 1,
          "every CSV row has the same width, so Excel's From Web keeps every column")
    r2 = k.publish(quiet, snap(when=NOON, run="noon", dials=450))
    check(r2["page"] == r1["page"] and r2["files"] == r1["files"],
          "the page and every CSV keep the same link from one run to the next")
    rows = _csv(site / "scoreboard.csv")
    check(rows[0][0] == "Refreshed Monday, November 16 at 12:30 PM Eastern" and rows[4][1] == "450",
          "a later run replaces each file in place: the new numbers under the new Refreshed line")

    # ------------------------------------------------------------------ 2. the signed links and the route
    print("  signed links and the brain's route")
    psig, pname = _parts(r2["page"])
    csig, cname = _parts(r2["files"]["Scoreboard"])
    check(k.served_file(psig, pname) == site / "tracker.html" and k.served_file(csig, cname) == site / "scoreboard.csv",
          "a signed link opens its own file")
    check(k.served_file("0" * 32, pname) is None and k.served_file(csig, "callers.csv") is None
          and k.served_file(csig, "../link.key") is None and k.served_file("", pname) is None
          and k.served_file(k._sign(k.link_key(), "missing.csv"), "missing.csv") is None,
          "a wrong signature, another file's signature, a path and a missing file all open nothing")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()

        @app.get("/kpi/{sig}/{name}")
        def kpi_files(sig: str, name: str):     # the exact route proposed for brain.py
            from .autopilot import kpisinks
            return kpisinks.serve(sig, name)

        c = TestClient(app)
        rp, rc = c.get(urlparse(r2["page"]).path), c.get(urlparse(r2["files"]["Scoreboard"]).path)
        rx = c.get("/kpi/%s/tracker.html" % ("0" * 32))
        check(rp.status_code == 200 and rp.headers["content-type"].startswith("text/html")
              and "attachment" not in rp.headers.get("content-disposition", "")
              and rp.headers.get("cache-control") == "no-store" and rp.headers.get("referrer-policy") == "no-referrer"
              and "default-src 'none'" in rp.headers.get("content-security-policy", ""),
              "the route serves the page inline, never cached, with no script allowed and no referrer sent")
        check(rc.status_code == 200 and rc.headers["content-type"].startswith("text/csv") and "Refreshed" in rc.text,
              "and a tab's CSV as text/csv, which IMPORTDATA and Excel read")
        check(rx.status_code == 404, "a wrong link is a plain 404")
    except ImportError as exc:
        check(False, "the route check needs fastapi and httpx: %s" % exc)

    # ------------------------------------------------------------------ 3. what the page says
    print("  the page")
    page = (site / "tracker.html").read_text(encoding="utf-8")
    check('<meta name="referrer" content="no-referrer">' in page and "noindex" in page and "<script" not in page.lower(),
          "the page sends no referrer, asks search engines to stay away and runs no script, because its link is the key")
    how = _section(page, "how-it-is-counted")
    check(all(('=IMPORTDATA(&quot;%s&quot;)' % r2["files"][n]) in how for n in ("Scoreboard", "Callers", "How it is counted")),
          "the How it is counted section carries the exact IMPORTDATA formula for every tab, each with its own CSV link")
    check(all(k.served_file(*_parts(r2["files"][n])) for n in r2["files"]),
          "and every one of those links opens its tab")
    check(k.formulas() == r2["mirror"] and len(r2["mirror"]) == 3
          and r2["mirror"][0] == 'Scoreboard: =IMPORTDATA("%s")' % r2["files"]["Scoreboard"],
          "the same formulas come back from publish and from the volume, for the stage's notes or a Slack ask")
    check('<a href="../%s/scoreboard.csv">' % csig in _section(page, "scoreboard"),
          "each tab links its own CSV from the page")

    # ------------------------------------------------------------------ 4. a tab that could not be refreshed
    print("  a tab that could not be refreshed")
    r3 = k.publish(quiet, snap(when=EVENING, run="evening", dials=470, failed=("Callers",)))
    rows = _csv(site / "callers.csv")
    check(rows[0][0] == "Refreshed Monday, November 16 at 12:30 PM Eastern" and rows[4][:2] == ["Robin", "200"],
          "a failed tab keeps its last good CSV, Refreshed line and all")
    page = (site / "tracker.html").read_text(encoding="utf-8")
    sec = _section(page, "callers")
    check("Not refreshed at 7:30 PM: the dial log could not be read. These are the numbers from Monday, November 16 "
          "at 12:30 PM Eastern." in sec and ">Robin<" in sec,
          "the page shows those last good numbers under a note that says when they are from")
    check(_csv(site / "scoreboard.csv")[4][1] == "470", "while the tabs that did refresh move on")
    check(r3["kept"] == ["Callers"] and r3["problems"][0] == "these tabs still show their last good numbers: Callers",
          "the run says which tabs kept old numbers, first among its problems")
    deals = {"name": "Deals", "note": "the deal board could not be read", "columns": ["Deal"], "rows": [], "failed": True}
    k.publish(quiet, snap(when=EVENING, extra=[deals]))
    page = (site / "tracker.html").read_text(encoding="utf-8")
    check(_csv(site / "deals.csv")[1][0] == "Not filled yet: the deal board could not be read."
          and "Not filled yet: the deal board could not be read." in _section(page, "deals"),
          "a tab that fails before it ever had numbers says it is not filled yet, on the page and in its CSV")

    # ------------------------------------------------------------------ 5. the guards
    print("  the guards")
    leads = {"name": "Leads", "note": "Call 865-555-0199 back", "columns": ["Lead", "Phone", "Where", "Record", "Formula", "Change"],
             "rows": [["<b>bold</b>", 8655550123, "12 Example St", "https://app.example.test/records/abc", "=HYPERLINK(1)", "+12%"],
                      ["Plain", "(865) 555-0101", "2 calls on the way", "", "@cmd", -3]]}
    r5 = k.publish(quiet, snap(when=EVENING, extra=[leads]))
    text = (site / "leads.csv").read_text(encoding="utf-8")
    page = (site / "tracker.html").read_text(encoding="utf-8")
    check("555-0199" not in text and "8655550123" not in text and "555-0101" not in text and text.count("[number]") == 3
          and "555-01" not in page,
          "a phone number in any cell or note becomes [number], in the CSV and on the page")
    check("12 Example St" not in text and "[street]" in text and "2 calls on the way" in text and "12 Example St" not in page,
          "a street address becomes [street] on the public files, while plain words are left alone")
    check("'=HYPERLINK(1)" in text and "'@cmd" in text and "+12%" in text and "'+12%" not in text and "'-3" not in text,
          "a CSV cell that would run as a formula is neutralized, while +12% and -3 stay as they are")
    check("&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page,
          "the page escapes what it shows: markup in a cell is printed as text")
    check('<a href="https://app.example.test/records/abc" rel="noreferrer noopener"' in page,
          "a record link opens without handing over the page's own link")
    check(any("phone number" in n for n in r5["notes"]) and any("street address" in n for n in r5["notes"]),
          "the run notes how many cells the guards changed, so a snapshot that leaks can be fixed")

    # ------------------------------------------------------------------ 6. a page that could not be saved
    print("  a page that could not be saved")
    before = (site / "tracker.html").read_text(encoding="utf-8")
    real = k._atomic_write

    def full_disk(path, body):
        if Path(path).name == "tracker.html":
            raise OSError(28, "No space left on device")
        return real(path, body)
    k._atomic_write = full_disk
    try:
        r6 = k.publish(quiet, snap(when=EVENING, dials=999))
    finally:
        k._atomic_write = real
    check(any(p.startswith("the tracker page could not be saved (No space left on device)") for p in r6["problems"])
          and (site / "tracker.html").read_text(encoding="utf-8") == before,
          "a page that could not be saved is a problem, and the last good page stays up")
    check(k.read_site("Scoreboard")[4][:2] == ["Dials", "999"] and k.read_site("No such tab") == [],
          "a tab's CSV reads back from the volume, for the tracker's --check")

    # ------------------------------------------------------------------ 7. layout and house rules
    print("  layout and house rules")
    k.publish(quiet, snap(when=EVENING, wide=14))
    page = (site / "tracker.html").read_text(encoding="utf-8")
    texts = [page] + [p.read_text(encoding="utf-8") for p in site.glob("*.csv")]
    check(all(chr(0x2014) not in t and chr(0x2013) not in t for t in texts), "no em or en dash on the page or in any CSV")
    check("overflow-wrap:anywhere" in page and "overflow-x" not in page and "nowrap" not in page,
          "the page never scrolls sideways: any cell may wrap and nothing is held to one line")
    check('<table class="w' in _section(page, "scoreboard") and "@media (max-width:" in page,
          "a table too wide for a phone folds into one card per row below the width it needs")
    keep_url = config.PUBLIC_URL
    config.PUBLIC_URL = ""
    try:
        r7 = k.publish(quiet, snap(when=EVENING))
    finally:
        config.PUBLIC_URL = keep_url
    page = (site / "tracker.html").read_text(encoding="utf-8")
    check(r7["page"] == "" and any("AGENTS_PUBLIC_URL" in n for n in r7["notes"]) and "The links appear here once" in page
          and '<a href="../' in page,
          "without a public address there is no link to give, said as a note; the page's own CSV links still work")
    prev = tmp / "kpisinks_preview.md"
    calls, before = (len(g.calls), len(s.calls)), (site / "tracker.html").read_text(encoding="utf-8")
    rp = k.publish(quiet, snap(dials=1), preview=prev)
    body = prev.read_text(encoding="utf-8")
    check(body.count("\n## ") == 3 and "| Dials | 1 | 9,020 |" in body and (len(g.calls), len(s.calls)) == calls
          and (site / "tracker.html").read_text(encoding="utf-8") == before and rp["slack"] == "not sent: preview",
          "a preview writes every tab as text and touches neither the page, Google nor Slack")

    # ------------------------------------------------------------------ 8. the optional Google Sheet: signing in
    print("  the optional Google Sheet: signing in")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:
        check(False, "the service account check needs the cryptography package: %s" % exc)
        return
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    sa = {"type": "service_account", "client_email": SA_EMAIL, "private_key_id": "key-1", "private_key": pem,
          "token_uri": TOKEN}
    os.environ["AGENTS_GOOGLE_SA_JSON"] = base64.b64encode(json.dumps(sa).encode()).decode()
    g = FakeGoogle()
    k.GOOGLE = g
    k._TOKENS.clear()
    ra = k.publish(quiet, snap(when=MORNING))
    tok = [c for c in g.calls if c["url"] == TOKEN]
    form = tok[0]["data"] if tok else {}
    head64, claims64, sig64 = (form.get("assertion") or "..").split(".")[:3]
    try:
        key.public_key().verify(_unb64(sig64), ("%s.%s" % (head64, claims64)).encode(), padding.PKCS1v15(), hashes.SHA256())
        signed = True
    except Exception:  # noqa: BLE001
        signed = False
    claims = json.loads(_unb64(claims64) or b"{}") if claims64 else {}
    check(len(tok) == 1 and form.get("grant_type") == "urn:ietf:params:oauth:grant-type:jwt-bearer" and signed
          and claims.get("iss") == SA_EMAIL and claims.get("aud") == TOKEN and claims.get("exp", 0) - claims.get("iat", 0) == 3600
          and set(str(claims.get("scope")).split()) == {"https://www.googleapis.com/auth/spreadsheets",
                                                       "https://www.googleapis.com/auth/drive.file"},
          "a service account trades a JWT signed with its own key: the right grant, issuer, audience, scopes, one hour")
    check(ra["sheet"] and not ra["problems"], "and with it the Google Sheet is written")
    os.environ.pop("AGENTS_GOOGLE_SA_JSON")
    (config.DATA_DIR / "kpi" / "sheet.json").unlink(missing_ok=True)

    os.environ.update({"AGENTS_GOOGLE_CLIENT_ID": "test-client-id", "AGENTS_GOOGLE_CLIENT_SECRET": "test-client-secret",
                       "AGENTS_GOOGLE_REFRESH_TOKEN": "test-refresh-token", "AGENTS_KPI_TITLE": "Test KPI tracker"})
    g = FakeGoogle()
    k.GOOGLE = g
    k._TOKENS.clear()
    rb = k.publish(quiet, snap(when=MORNING))
    tok = [c for c in g.calls if c["url"] == TOKEN]
    check(len(tok) == 1 and tok[0]["data"] == {"client_id": "test-client-id", "client_secret": "test-client-secret",
                                               "refresh_token": "test-refresh-token", "grant_type": "refresh_token"},
          "an OAuth refresh token builds the right request: client id, secret, refresh token, grant refresh_token")

    # ------------------------------------------------------------------ 9. the optional Google Sheet: writing
    print("  the optional Google Sheet: writing")
    made = [c for c in g.calls if c["method"] == "POST" and c["url"] == SHEETS]
    check(len(made) == 1 and made[0]["json"]["properties"]["title"] == "Test KPI tracker"
          and [x["properties"]["title"] for x in made[0]["json"]["sheets"]] == ["Scoreboard", "Callers", "How it is counted"],
          "with a credential, the Google Sheet is made once, titled from the setting, with every tab in order")
    sid = urlparse(rb["sheet"]).path.split("/")[3]
    book = g.books[sid]
    check(book["tabs"]["Scoreboard"]["rows"][0][0] == "Refreshed Monday, November 16 at 7:45 AM Eastern"
          and book["tabs"]["Scoreboard"]["rows"][4] == ["Dials", 412, 9020],
          "each tab in the sheet holds the same layout as the page: the Refreshed line on top, numbers as numbers")
    how_rows = [r[0] for r in book["tabs"]["How it is counted"]["rows"] if r]
    check(any(str(x).startswith('Scoreboard: =IMPORTDATA("https://agents.example.test/kpi/') for x in how_rows),
          "the sheet's How it is counted tab carries the mirror formulas too")
    g.calls.clear()
    rc = k.publish(quiet, snap(when=NOON, dials=450))
    batch = [r for c in g.calls if c["url"].endswith(":batchUpdate") for r in (c["json"] or {}).get("requests", [])]
    clears = [x for c in g.calls if c["url"].endswith("/values:batchClear") for x in c["json"]["ranges"]]
    check(not [c for c in g.calls if c["method"] == "POST" and c["url"] == SHEETS] and any(c["method"] == "GET" for c in g.calls)
          and rc["sheet"] == rb["sheet"],
          "the next run opens the remembered sheet and never makes a second one")
    check("'Scoreboard'" in clears and not [r for r in batch if "addSheet" in r]
          and book["tabs"]["Scoreboard"]["rows"][0][0] == "Refreshed Monday, November 16 at 12:30 PM Eastern"
          and book["tabs"]["Scoreboard"]["rows"][4][1] == 450,
          "each tab is rewritten in place: cleared and written again under the new Refreshed line, no tab added")
    check(not [c for c in g.calls if c["url"] == TOKEN], "the token is reused within its hour, not asked for on every run")
    frozen = [r for r in batch if r.get("updateSheetProperties", {}).get("properties", {}).get("gridProperties", {})
              .get("frozenRowCount") == 4]
    bold = [r for r in batch if "repeatCell" in r and r["repeatCell"]["range"].get("startRowIndex") == 3
            and r["repeatCell"]["cell"]["userEnteredFormat"]["textFormat"].get("bold")]
    dump = json.dumps(batch)
    check(frozen and bold and "backgroundColor" not in dump and "addBanding" not in dump,
          "the header row is bold and frozen, and nothing is banded or colored in")
    g.books[sid]["tabs"].pop("Callers")
    g.calls.clear()
    rd = k.publish(quiet, snap(when=NOON))
    batch = [r for c in g.calls if c["url"].endswith(":batchUpdate") for r in (c["json"] or {}).get("requests", [])]
    check(not [r for r in batch if "addSheet" in r] and "Callers" not in g.books[sid]["tabs"]
          and any("Callers tab was deleted" in n for n in rd["notes"]) and not any("Callers" in p for p in rd["problems"]),
          "a tab a person deleted from the Google Sheet is never added back, and that is a note, not a problem")
    before_rows = copy.deepcopy(g.books[sid]["tabs"]["Scoreboard"]["rows"])
    g.calls.clear()
    k.publish(quiet, snap(when=EVENING, failed=("Scoreboard",)))
    clears = [x for c in g.calls if c["url"].endswith("/values:batchClear") for x in c["json"]["ranges"]]
    ranges = [d["range"] for c in g.calls if c["url"].endswith("/values:batchUpdate") for d in c["json"]["data"]]
    after = g.books[sid]["tabs"]["Scoreboard"]["rows"]
    check("'Scoreboard'" not in clears and "'Scoreboard'!A2" in ranges and "'Scoreboard'!A1" not in ranges
          and after[0] == before_rows[0] and after[4] == before_rows[4]
          and str(after[1][0]).startswith("Not refreshed at 7:30 PM: the dial log could not be read. These are the numbers from"),
          "a failed tab is left as it is in the Google Sheet: only its note cell changes, saying when its numbers are from")
    os.environ["AGENTS_KPI_SHEET_SHARE"] = "robin@example.test, sam@example.test=writer"
    k.publish(quiet, snap(when=EVENING))
    first = list(g.perms)
    k.publish(quiet, snap(when=EVENING))
    second = g.perms[len(first):]
    os.environ["AGENTS_KPI_SHEET_SHARE"] += ",lee@example.test"
    k.publish(quiet, snap(when=EVENING))
    third = g.perms[len(first) + len(second):]
    check(len(first) == 2 and {(p[1]["emailAddress"], p[1]["role"]) for p in first}
          == {("robin@example.test", "reader"), ("sam@example.test", "writer")}
          and all(p[2].get("sendNotificationEmail") == ["false"] for p in first),
          "the sheet is shared once with each email in the setting, read only unless =writer, quietly")
    check(second == [] and len(third) == 1 and third[0][1]["emailAddress"] == "lee@example.test",
          "a later run shares nothing again, and an email added later is shared once")
    os.environ.pop("AGENTS_KPI_SHEET_SHARE")
    g.calls.clear()
    rw = k.publish(quiet, snap(when=EVENING, wide=30))
    batch = [r for c in g.calls if c["url"].endswith(":batchUpdate") for r in (c["json"] or {}).get("requests", [])]
    grown = [r for r in batch if r.get("updateSheetProperties", {}).get("properties", {}).get("gridProperties", {})
             .get("columnCount", 0) >= 30]
    check(grown and not [p for p in rw["problems"] if "Google" in p] and len(g.books[sid]["tabs"]["Scoreboard"]["rows"][3]) == 30,
          "a tab wider than Google's starting grid is given room before it is written")

    g.fail.append(("GET", lambda u: u.startswith(SHEETS + "/"),
                   (503, {"error": {"code": 503, "message": "The service is currently unavailable.", "status": "UNAVAILABLE"}})))
    ro = k.publish(quiet, snap(when=EVENING, dials=480))
    check(any(p.startswith("the Google Sheet could not be updated: Google answered 503 (The service is currently unavailable)")
              for p in ro["problems"]) and _csv(site / "scoreboard.csv")[4][1] == "480",
          "a Google outage is a problem, and the page is still written")
    k._TOKENS.clear()
    g.fail.append(("POST", lambda u: u == TOKEN, (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})))
    rx = k.publish(quiet, snap(when=EVENING))
    check(any(p.startswith("the Google sign-in has expired or was revoked") and "kpisinks oauth" in p for p in rx["problems"]),
          "an expired or revoked Google sign-in says how to sign in again")

    g2 = FakeGoogle()
    g2.make_book("made-by-hand", ["Sheet1"])
    k.GOOGLE = g2
    os.environ["AGENTS_KPI_SHEET_ID"] = "made-by-hand"
    rf = k.publish(quiet, snap(when=EVENING))
    tabs = g2.books["made-by-hand"]["tabs"]
    check(not [c for c in g2.calls if c["method"] == "POST" and c["url"] == SHEETS]
          and {"Scoreboard", "Callers", "How it is counted"} <= set(tabs) and tabs["Sheet1"]["rows"] == [["kept by hand"]]
          and rf["sheet"] == "https://docs.google.com/spreadsheets/d/made-by-hand/edit",
          "with AGENTS_KPI_SHEET_ID set, that sheet is used, the tabs go in beside what is there, and nothing is made")
    g2.books.pop("made-by-hand")
    rn = k.publish(quiet, snap(when=EVENING))
    check(any(p.startswith("the Google Sheet in AGENTS_KPI_SHEET_ID could not be opened (Google answered 404)")
              and "the Google account that signed in" in p for p in rn["problems"])
          and not [c for c in g2.calls if c["method"] == "POST" and c["url"] == SHEETS],
          "a configured sheet that cannot be opened says what to check, and is never replaced by a new one")
    for n in ("AGENTS_KPI_SHEET_ID", "AGENTS_GOOGLE_CLIENT_SECRET", "AGENTS_GOOGLE_REFRESH_TOKEN"):
        os.environ.pop(n)
    g3 = FakeGoogle()
    k.GOOGLE = g3
    rh = k.publish(quiet, snap(when=EVENING))
    check("the Google sign-in is incomplete: AGENTS_GOOGLE_CLIENT_SECRET and AGENTS_GOOGLE_REFRESH_TOKEN are not set"
          in rh["problems"] and g3.calls == [],
          "a half-set Google sign-in is a problem that names what is missing, and nothing is sent to Google")
    os.environ.pop("AGENTS_GOOGLE_CLIENT_ID")
    os.environ["AGENTS_GOOGLE_SA_JSON"] = json.dumps(sa)         # the key's own text this time, not base64
    (config.DATA_DIR / "kpi" / "sheet.json").unlink(missing_ok=True)
    k._TOKENS.clear()
    g3.fail.append(("POST", lambda u: u == SHEETS, (403, {"error": {
        "code": 403, "message": "The user's Drive storage quota has been exceeded.", "status": "PERMISSION_DENIED",
        "errors": [{"reason": "storageQuotaExceeded"}]}})))
    rq = k.publish(quiet, snap(when=EVENING))
    check(any("cannot own a new sheet" in p and SA_EMAIL in p and "AGENTS_KPI_SHEET_ID" in p for p in rq["problems"]),
          "a service account Google will not let own a new file gets the plain fix: make the sheet, share it, set its id")
    os.environ.pop("AGENTS_GOOGLE_SA_JSON")
    os.environ.pop("AGENTS_KPI_TITLE")

    # ------------------------------------------------------------------ 10. Slack: one message a day
    print("  slack: one message a day")
    k.GOOGLE, s = FakeGoogle(), FakeSlack()
    k.SLACK = s
    no_model = NoModel()
    voice.ASK, voice.CHECK, config.SLACK_VOICE = no_model, None, "ty"
    ctx = _Ctx()
    lines = ["KPI tracker refreshed Mon, Nov 23 at 7:45 AM.", "Friday final: 412 dials, 21 conversations, 3 new leads (Robin 2, Sam 1).",
             "Needs a person: call back 865-555-0177."]
    m1 = k.publish(ctx, snap(when="2026-11-23T12:45:00+00:00", lines=lines))
    posts = s.sent(channel="CPIPE")
    check(len(posts) == 1 and posts[0]["username"] == "Quinn" and posts[0]["unfurl_links"] is False and m1["slack"] == "posted",
          "the first run of the day posts one message in the pipeline channel, under Quinn's name, never unfurled")
    text = posts[0]["text"] if posts else ""
    check(text.split("\n")[0] == "KPI tracker refreshed Mon, Nov 23 at 7:45 AM. <%s|Open the tracker>" % m1["page"],
          "its first line links the tracker page")
    check(no_model.calls == 0 and text.split("\n")[1] == voice.clean(lines[1]),
          "the lines go out as written, cleaned the free way, with no model rewrite")
    check("865-555-0177" not in text and "call back [number]" in text, "a phone number in a Slack line becomes [number]")
    check(k.slack_text({"slack": {"lines": ["Today final at 7:45: 412 dials, 6 at 12:30."]}})
          == "Today final at 7:45: 412 dials, 6 at 12:30.",
          "a clock time followed by a colon survives the cleanup (voice.clean alone reads :45: as an emoji)")
    pipe = [ts for ch, ts in s.posted if ch == "CPIPE"]
    ts1 = pipe[0] if pipe else ""
    m2 = k.publish(ctx, snap(when="2026-11-23T17:30:00+00:00", dials=450,
                              lines=["KPI tracker refreshed Mon, Nov 23 at 12:30 PM.", "Today so far: 450 dials."]))
    ups = s.sent("chat.update")
    check(len(s.sent(channel="CPIPE")) == 1 and len(ups) == 1 and ups[0]["ts"] == ts1 and "450 dials" in s.msgs[("CPIPE", ts1)]
          and m2["slack"] == "updated",
          "a later run the same day edits that message in place instead of posting a new one")
    k.publish(ctx, snap(when="2026-11-24T00:30:00+00:00", lines=["KPI tracker refreshed Mon, Nov 23 at 7:30 PM.", "Today final."]))
    check(len(s.sent(channel="CPIPE")) == 1 and len(s.sent("chat.update")) == 2 and "Today final." in s.msgs[("CPIPE", ts1)],
          "the evening run, already past midnight in UTC, still edits the same business day's message")
    k.publish(ctx, snap(when="2026-11-24T12:45:00+00:00", lines=["KPI tracker refreshed Tue, Nov 24 at 7:45 AM."]))
    check(len(s.sent(channel="CPIPE")) == 2, "the next day starts a new message")
    ts2 = [ts for ch, ts in s.posted if ch == "CPIPE"][-1]
    s.msgs.pop(("CPIPE", ts2))                       # someone deleted Tuesday's message
    m5 = k.publish(ctx, snap(when="2026-11-24T17:30:00+00:00", lines=["KPI tracker refreshed Tue, Nov 24 at 12:30 PM."]))
    ts3 = [ts for ch, ts in s.posted if ch == "CPIPE"][-1]
    k.publish(ctx, snap(when="2026-11-24T18:00:00+00:00", lines=["KPI tracker refreshed Tue, Nov 24 at 1:00 PM."]))
    check(m5["slack"] == "posted again" and len(s.sent(channel="CPIPE")) == 3 and s.sent("chat.update")[-1]["ts"] == ts3,
          "a message someone deleted is posted again, and the new one is the one edited from then on")
    s.fail["chat.update"] = "ratelimited"
    m7 = k.publish(ctx, snap(when="2026-11-24T19:00:00+00:00"))
    check(len(s.sent(channel="CPIPE")) == 3 and m7["slack"] == "not updated"
          and "the KPI message in Slack could not be updated: Slack asked to slow down" in m7["problems"],
          "an edit Slack refuses for another reason is never posted a second time; it is a problem")

    check(len(s.sent(channel="CISSUE")) == 1 and "Slack asked to slow down" in s.sent(channel="CISSUE")[0]["text"],
          "and that problem reaches the issues channel once, in plain words")

    print("  slack: the issues post")
    base = len(s.sent(channel="CISSUE"))
    i1 = k.publish(ctx, snap(when="2026-11-25T12:45:00+00:00", failed=("Callers",), problems=["the dial log copy is 2 hours short"]))
    issues = s.sent(channel="CISSUE")[base:]
    said = issues[0]["text"] if issues else ""
    check(len(issues) == 1 and "Callers still shows the last good numbers." in said and "The dial log copy is 2 hours short." in said,
          "the issues channel hears which tabs still show their last good numbers, and why")
    check(not voice.first_today("kpi_tracker", i1["problems"][0])
          and not voice.first_today("kpi_tracker", "the dial log copy is 2 hours short"),
          "what it said is marked as said today, so the stage's own routing does not say it a second time")
    k.publish(ctx, snap(when="2026-11-25T17:30:00+00:00", failed=("Callers",)))
    check(len(s.sent(channel="CISSUE")) == base + 1, "and it is said once a day, not every run")
    k.publish(ctx, snap(when="2026-11-26T12:45:00+00:00", failed=("Callers",)))
    check(len(s.sent(channel="CISSUE")) == base + 2, "the next day it is said again while it lasts")
    check(all(chr(0x2014) not in p.get("text", "") and chr(0x2013) not in p.get("text", "") for _m, p in s.calls),
          "no em or en dash in anything posted to Slack")

    print("  slack: settings")
    before = len(s.calls)
    k.publish(_Ctx(publish={"slack": "off"}), snap(when="2026-11-27T12:45:00+00:00", failed=("Callers",)))
    check(len(s.calls) == before, "slack off sends nothing at all, not even the issues post")
    posts0, edits0 = len(s.sent(channel="CPIPE")), len(s.sent("chat.update"))
    k.publish(_Ctx(publish={"slack": "posts"}), snap(when="2026-11-28T12:45:00+00:00"))
    k.publish(_Ctx(publish={"slack": "posts"}), snap(when="2026-11-28T17:30:00+00:00"))
    check(len(s.sent(channel="CPIPE")) == posts0 + 2 and len(s.sent("chat.update")) == edits0,
          "posts mode posts a new message every run and edits none")
    os.environ["AGENTS_KPI_SLACK"] = "off"
    check(k.settings(_Stage({"publish": {"slack": "edit"}}))["slack"] == "off"
          and k.settings(_Stage({"publish": {"slack": "loud"}}))["slack"] == "off",
          "AGENTS_KPI_SLACK overrides the playbook, so the message can be stopped without a deploy")
    os.environ.pop("AGENTS_KPI_SLACK")
    check(k.settings(_Stage({"publish": {"slack": "loud"}}))["slack"] == "edit" and k.settings(None)["title"] == "KPI tracker",
          "an unknown Slack mode falls back to one message a day, and the title defaults to KPI tracker")
    check(k.block_problems({"publish": {"slack": "loud"}}) == ["publish.slack must be edit, posts or off"]
          and k.block_problems({"publish": {"colour": "x"}}) == ["publish has settings this module does not read: colour"]
          and k.block_problems({}) == [],
          "a mistyped publish block is refused when the playbook loads")
    config.SLACK_BOT_TOKEN = ""
    before = len(s.calls)
    m8 = k.publish(ctx, snap(when="2026-11-29T12:45:00+00:00", failed=("Callers",)))
    config.SLACK_BOT_TOKEN = "xoxb-test"
    check(len(s.calls) == before and m8["slack"] == "not sent: the Slack app is not set up"
          and not any("Slack" in p for p in m8["problems"]),
          "without the Slack app nothing is sent, and that is not a problem")
    url = k.consent_url("test-client-id", "http://127.0.0.1:8765/", "state-1")
    q = parse_qs(urlparse(url).query)
    check(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?") and q["access_type"] == ["offline"]
          and q["prompt"] == ["consent"] and q["redirect_uri"] == ["http://127.0.0.1:8765/"]
          and set(q["scope"][0].split()) == {"https://www.googleapis.com/auth/spreadsheets",
                                             "https://www.googleapis.com/auth/drive.file"},
          "the one-time sign-in asks for a refresh token and only the two scopes the sheet needs")

    check(wall.hits == [], "nothing in this section reached the network")
