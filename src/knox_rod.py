"""Knox Register of Deeds bulk pull (paxsub.knoxrod.org).

Bulk pulling is PERMITTED here: Ty cleared it with the county 2026-07-27. Their
concern is server load, so this uses large monthly windows and few large pages
rather than many small ones. Note the login page still carries a generic warning
about automated access; the clearance is a relationship, not a change to their
written terms, so keep the request count low and do not parallelise.

THE CONTRACT, captured live 2026-08-19.

  * Do NOT drive the DataTables grid. It returns 10 rows and reports one page.
  * The grid is fed by `POST /api/v2Search`, form-encoded.
  * The body CANNOT be hand-built. Several doc types return 0 rows for a
    hand-made body while the browser returns data for the same search. So log
    in with Playwright once, capture the page's OWN request, and replay it with
    only a few fields mutated. Everything else, including SessionId,
    SessionGuid and SessionTicket, is carried through untouched.
  * Fields to mutate: `CategoryDocumentTypes` (doc type), `RecordedDate1` and
    `RecordedDate2` (the window), `FirstRecordNum` / `LastRecordNum` (paging).
  * Doc type is a BARE INT. The jstree node id is `doc5`; the API wants `5`.
    Sending `doc5` errors on nvarchar to int conversion.
  * The response body is DOUBLE ENCODED: a JSON string that itself contains the
    JSON payload. `resp.json()` hands back a str, so parse twice.
  * Hard cap 5,000 results per search. Over that it returns a correct
    `recordsTotal` with an EMPTY `aaData`, which reads exactly like a quiet
    month. Chunk monthly and assert the row count against recordsTotal.
  * Each instrument appears twice, once per party role, so collapse on the
    instrument number.

Doc types on this account (jstree id -> API int):
    LEN  5    general lien        ~12,600/yr
    STL  6    state tax lien       ~1,500/yr
    RFTL 14   release of FTL
    REL  13   release
    RESL 15   release of lien
    RSTL 16   release of state tax lien
    JDG  56   judgment

    python src/knox_rod.py --months 1 --types LEN
    python src/knox_rod.py --months 12 --types LEN,STL,FTL --out output/knox/rod.json

VERIFY BEFORE A YEAR-LONG PULL. Run one month first and check the row count
matches recordsTotal; that is the only thing that distinguishes a real result
from the 5,000-cap silent truncation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

BASE = "https://paxsub.knoxrod.org"
SEARCH = BASE + "/api/v2Search"
PAGE = 2000          # large pages on purpose: fewer requests, county asked for that
CAP = 5000           # server-side hard cap per search

# jstree label -> API integer. Read off the live tree, not guessed.
# Read off the live tree (output/knox/rod_doctypes.json), never guessed. The
# labels are codes, not words, and the numbering is alphabetical WITHIN a
# category, so LEN=5 sits in the lien category while TRS=50 sits in the deed
# category. FTL was missing here until 2026-08-25, silently dropping federal
# tax liens from every pull.
DOC_TYPES = {"FTL": 4, "LEN": 5, "STL": 6, "REL": 13, "RFTL": 14, "RESL": 15,
             "RSTL": 16, "JDG": 56,
             # Starred twins carry the same label plus "*" in the tree.
             "FTL*": 104, "LEN*": 105, "STL*": 106, "REL*": 113,
             "RFTL*": 114, "RSTL*": 116}


SUBSCRIPTION_WARN_DAYS = 14
# What the last login saw: {"state": "active" | "expired" | "unknown", "days": int | None}.
# Read by the daily lien stage so the days-left figure can ride in the run report.
SUBSCRIPTION: dict = {}

# The doc-type tree as the last login read it off the live page: {label: API integer}.
# DOC_TYPES above stays the source for the types the lien pull has always used. Any OTHER type
# (the substitute trustee appointment, the deed of trust it points at) is resolved here by its
# LABEL, never by a number typed into the code: the numbering is the county's, and a renumbered
# tree would otherwise pull a different kind of document without a single error.
DOC_TREE: dict = {}

# The last captured search request, so a second stage in the same process (the appointment pull
# straight after the lien pull) replays the session that is already open instead of logging in
# again. The county's one concern is load, and a login is the heaviest thing this module does.
SESSION: dict = {}
SESSION_MAX_AGE = 900     # seconds a captured session is reused before a fresh login


def doc_code(label: str) -> int | None:
    """The API integer for a doc-type label, or None when neither table knows it."""
    key = (label or "").strip().upper()
    if key in DOC_TYPES:
        return DOC_TYPES[key]
    return DOC_TREE.get(key)


def remember_tree(pairs) -> int:
    """Fill DOC_TREE from the live tree's [id, label] pairs. Returns how many labels it holds."""
    DOC_TREE.clear()
    for code, label in pairs or []:
        label = str(label or "").strip().upper()
        try:
            if label:
                DOC_TREE.setdefault(label, int(str(code).strip()))
        except ValueError:
            continue
    return len(DOC_TREE)


def capture_cached(max_age: float = SESSION_MAX_AGE, capture=None, clock=time.time) -> tuple[dict, dict]:
    """(fields, headers) from this process's open session, logging in only when there is none
    or it is older than max_age seconds."""
    if SESSION.get("fields") and clock() - float(SESSION.get("at") or 0) <= max_age:
        return SESSION["fields"], SESSION["headers"]
    if capture is None:
        import asyncio
        fields, headers = asyncio.run(capture_request())
    else:
        fields, headers = capture()
    SESSION.update(fields=fields, headers=headers, at=clock())
    return fields, headers


class RodSubscriptionExpired(RuntimeError):
    """The paid Register of Deeds subscription has lapsed.

    Login still works on a lapsed account and the search page still renders,
    but the search button fires no request at all: the site answers with a
    browser alert instead. For 11 weeks (2026-08-26 on) that read as "no
    v2Search request was captured; the page layout changed". No retry or code
    change fixes it; the account has to be renewed on the Subscription tab.
    """


def subscription_state(timer_text: str) -> tuple[str, int | None]:
    """Read the header timer ("DAYS UNTIL SUBSCRIPTION EXPIRES: 23").

    Returns ("expired", None), ("active", days) or ("unknown", None). Unknown
    is not treated as expired: the search itself is the second check.
    """
    tail = (timer_text or "").split(":")[-1].strip().lower()
    if "expired" in tail:
        return "expired", None
    digits = "".join(c for c in tail if c.isdigit())
    if digits:
        return "active", int(digits)
    return "unknown", None


def _month_windows(months: int, end: date | None = None) -> list[tuple[str, str]]:
    """Calendar months, oldest first, as (MM/DD/YYYY, MM/DD/YYYY)."""
    end = end or date.today()
    out, y, m = [], end.year, end.month
    for _ in range(max(1, months)):
        first = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        out.append((first.strftime("%m/%d/%Y"), (nxt - timedelta(days=1)).strftime("%m/%d/%Y")))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


async def capture_request() -> tuple[dict, dict]:
    """Log in, run a throwaway search, return (body_fields, headers).

    This is the only step that needs a browser. Everything after it is plain
    HTTP replay against the captured session.
    """
    from playwright.async_api import async_playwright

    import asyncio

    caps: list[dict] = []
    tree: list = []
    alerts: list[str] = []

    def _on_dialog(d) -> None:
        # The site reports a lapsed subscription ONLY through a browser alert.
        # Unhandled, Playwright dismisses it silently and nothing is left to see.
        alerts.append(d.message or "")
        asyncio.ensure_future(d.accept())

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await (await b.new_context()).new_page()
        page.on("dialog", _on_dialog)
        page.on("request", lambda r: caps.append(
            {"headers": dict(r.headers), "post": r.post_data})
            if "v2Search" in r.url else None)

        await page.goto(BASE + "/Default", wait_until="domcontentloaded")
        await page.fill("#txtUsername", config.PAXSUB_USERNAME)
        await page.fill("#txtPassword", config.PAXSUB_PASSWORD)
        await page.click("#btnLogin")
        await page.wait_for_timeout(5000)

        body_txt = await page.evaluate("() => document.body.innerText")
        if "Log off" not in body_txt and "Log Off" not in body_txt:
            await b.close()
            raise RuntimeError(
                "paxsub login failed. A logged-out page still renders a plausible "
                "grid, so this asserts on the Log off link rather than trusting the URL.")

        timer = await page.evaluate(
            "() => { const e = document.getElementById('subscriptionTimer');"
            " return e ? (e.value || e.innerText || '') : ''; }")
        state, days = subscription_state(timer)
        SUBSCRIPTION.clear()
        SUBSCRIPTION.update(state=state, days=days)
        if state == "expired":
            await b.close()
            raise RodSubscriptionExpired(
                "Register of Deeds subscription has EXPIRED. Renew it at "
                + BASE + "/views/subscription (Renew Subscription); nothing "
                "else will make the search return rows.")
        if state == "active":
            print("  Register of Deeds subscription: %d day(s) left" % days)
            if days <= SUBSCRIPTION_WARN_DAYS:
                print("  WARNING: the Register of Deeds subscription expires in "
                      "%d day(s). Renew it before the next pull." % days)

        await page.goto(BASE + "/views/search", wait_until="domcontentloaded")
        await page.wait_for_timeout(4500)

        # Expand the type tree and dump it. The labels are CODES (LEN, STL), not
        # words, so a selector searching for "LIEN" matches nothing; and the map
        # below has to be read off the live tree rather than guessed. FTL was
        # missing from DOC_TYPES for exactly that reason.
        await page.evaluate(
            "() => document.querySelectorAll('#jstreeCatDocTypes .jstree-ocl')"
            ".forEach(e => e.click())")
        await page.wait_for_timeout(2500)
        tree = await page.evaluate("""() => {
            const o = [];
            document.querySelectorAll('#jstreeCatDocTypes li').forEach(li => {
                const a = li.querySelector('a');
                if (a && li.id && li.id.startsWith('doc'))
                    o.push([li.id.replace('doc', ''), (a.textContent || '').trim().slice(0, 40)]);
            });
            return o;
        }""")
        # Do NOT click #btnCriteria: the accordion is open on load and toggling
        # it hides every search button.
        await page.evaluate("""() => {
            const set = (id, v) => {
                const e = document.getElementById(id);
                if (e) { e.value = v; e.dispatchEvent(new Event('change', {bubbles: true})); }
            };
            set('dtFrom', '07/01/2026');
            set('dtTo', '07/31/2026');
        }""")
        # JS click: the button reports not-visible to Playwright even when shown.
        await page.evaluate("() => document.getElementById('btnSummarySearch').click()")
        await page.wait_for_timeout(9000)
        await b.close()

    if tree:
        remember_tree(tree)
        try:
            os.makedirs("output/knox", exist_ok=True)
            with open("output/knox/rod_doctypes.json", "w", encoding="utf-8") as fh:
                json.dump(dict(tree), fh, indent=1)
        except OSError:
            pass
        print("  live doc-type tree (%d types) -> output/knox/rod_doctypes.json" % len(tree))

    if not caps:
        said = "; ".join(a for a in alerts if a)
        if "expired" in said.lower() or "subscription" in said.lower():
            raise RodSubscriptionExpired("Register of Deeds refused the search: " + said)
        raise RuntimeError(
            "no v2Search request was captured"
            + ((". The site said: " + said) if said else "; the page layout changed"))
    c = caps[0]
    fields = dict(urllib.parse.parse_qsl(c["post"], keep_blank_values=True))
    headers = {k: v for k, v in c["headers"].items()
               if k.lower() not in ("content-length", "host", ":authority")}
    SESSION.update(fields=fields, headers=headers, at=time.time())
    return fields, headers


def _post(fields: dict, headers: dict, timeout: int = 90) -> dict:
    import requests

    r = requests.post(SEARCH, data=fields, headers=headers, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    # Double encoded: the JSON body is itself a JSON string.
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def fetch_type(fields: dict, headers: dict, doc: str,
               d_from: str, d_to: str, sleep: float = 1.5) -> list[list]:
    """All rows for one doc type in one window, paged."""
    code = doc_code(doc)
    if code is None:
        raise ValueError(f"unknown doc type {doc!r}; known: {sorted(set(DOC_TYPES) | set(DOC_TREE))}")

    base = dict(fields)
    base["CategoryDocumentTypes"] = str(code)
    base["RecordedDate1"] = d_from
    base["RecordedDate2"] = d_to
    base["FirstRecordNum"] = "1"
    base["LastRecordNum"] = str(PAGE)

    first = _post(base, headers)
    total = int(first.get("recordsTotal") or 0)
    rows = list(first.get("aaData") or [])

    if total >= CAP and not rows:
        # The documented failure mode: a correct total with no data.
        raise RuntimeError(
            f"{doc} {d_from}..{d_to} exceeded the {CAP} result cap "
            f"(recordsTotal={total}, rows=0). Narrow the window.")

    got = len(rows)
    while got < total and got < CAP:
        time.sleep(sleep)
        nxt = dict(base)
        nxt["FirstRecordNum"] = str(got + 1)
        nxt["LastRecordNum"] = str(got + PAGE)
        page = _post(nxt, headers)
        chunk = page.get("aaData") or []
        if not chunk:
            break
        rows.extend(chunk)
        got = len(rows)

    if total and got < total:
        print(f"    WARN {doc} {d_from}: got {got} of {total} rows")
    return rows


def collapse(rows: list[list], doc: str) -> list[dict]:
    """One record per instrument.

    Every instrument appears twice, once for each party role, so a raw row count
    is roughly double the real document count.
    """
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, list) or len(r) < 15:
            continue
        instrument = str(r[10]).strip()
        if not instrument:
            continue
        rec = out.setdefault(instrument, {
            "source": "register_of_deeds", "doc_type": doc,
            "instrument": instrument, "names": [], "roles": [],
            "legal": str(r[9]).strip(), "consideration": str(r[14]).strip(),
            "detail": str(r[3])[:400],
        })
        name = str(r[4]).strip()
        if name and name not in rec["names"]:
            rec["names"].append(name)
        role = str(r[6]).strip()
        if role and role not in rec["roles"]:
            rec["roles"].append(role)
    return list(out.values())


def main() -> int:
    import asyncio

    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=1)
    ap.add_argument("--types", default="LEN,STL")
    ap.add_argument("--out", default="")
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--sleep", type=float, default=2.0)
    a = ap.parse_args()

    if not config.PAXSUB_USERNAME or not config.PAXSUB_PASSWORD:
        print("PAXSUB_USERNAME / PAXSUB_PASSWORD are not set")
        return 2

    os.makedirs(a.scratch, exist_ok=True)
    types = [t.strip().upper() for t in a.types.split(",") if t.strip()]
    windows = _month_windows(a.months)
    print(f"Knox ROD: {types} over {len(windows)} month(s) "
          f"({windows[0][0]} .. {windows[-1][1]})")

    print("capturing a live search request (browser, once)...")
    try:
        fields, headers = asyncio.run(capture_request())
    except RodSubscriptionExpired as exc:
        # Exit 4, distinct from "no rows" (1) and "no credentials" (2): a lapsed
        # subscription is not a quiet month and no retry fixes it.
        print("SUBSCRIPTION EXPIRED: %s" % exc)
        return 4
    print(f"  session {fields.get('SessionId','')[:8]} captured")

    allrec, counts = [], {}
    raw: dict[str, list] = {}
    for d_from, d_to in windows:
        for doc in types:
            try:
                rows = fetch_type(fields, headers, doc, d_from, d_to, sleep=a.sleep)
            except Exception as exc:
                print(f"  {doc} {d_from}: {type(exc).__name__}: {exc}")
                continue
            # Keep the RAW grid rows as well. knox_lien_resolve reads
            # rod2_<CODE>.json and needs the row-level grantor/grantee split to
            # tell the debtor from the creditor; the collapsed record merges
            # both parties into one name list and loses that distinction.
            raw.setdefault(doc, []).extend(rows)
            recs = collapse(rows, doc)
            allrec.extend(recs)
            counts[doc] = counts.get(doc, 0) + len(recs)
            print(f"  {doc:5} {d_from[:7]}  {len(rows):>5} rows -> {len(recs):>5} instruments")
            time.sleep(a.sleep)

    for doc, rows in raw.items():
        rp = os.path.join(a.scratch, "rod2_%s.json" % doc)
        with open(rp, "w", encoding="utf-8") as fh:
            json.dump({"aaData": rows}, fh)
        print(f"  raw {doc:5} {len(rows):>6} rows -> {rp}")

    out = a.out or os.path.join(a.scratch, "rod_raw.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(allrec, fh, indent=1)
    print(f"\ninstruments by type: {counts}")
    print(f"total {len(allrec)} -> {out}")
    return 0 if allrec else 1


if __name__ == "__main__":
    raise SystemExit(main())
