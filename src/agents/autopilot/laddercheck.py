"""The monthly ladder check (build plan 5.1 and 5.3): are the lists ranked on true numbers, did
the deals come off them, and do the feeders mirror the ladder.

Once a month, on the night list, this stage answers four questions and writes nothing:

  1. THE WINDOW. The published doors-per-deal data is read through the window guard
     (shard_guard). A month the file cannot stand behind is named, with the reason.

  2. THE DEALS. For each county, every investor purchase of the last six recorded months is
     read from SiftMap's sold data, one month at a time. A row carries whether the house was
     already a record in this account, since when, and which lists it was on. From that:
       * what share of the deals was on each list (the lesson: validate a list in Sold
         Properties before trusting it);
       * the account's own doors per deal for the list, which is how a small-sample rung is
         proven as it is worked;
       * how long a record sat in the account before it sold (probate runs four to seven months).
     A record counts only when it was in the account BEFORE the month it sold. The buyer
     feeders add every investor purchase after the sale, and counting those would make a list
     of buyers look like the best seller list there is.

  3. THE REACH. A calling pass reaches 10 to 20% of a list, so a doors-per-deal figure is a
     ceiling. With a saved preset that holds reached records (the playbook names it), each
     list's reached count is read, and reached / doors per deal is the deals that reach
     should produce in six months.

  4. THE FEEDERS. The Priority 1 rungs of the ladder against the SiftMap feeders
     (feeder_audit.ladder_check): which rungs have no feeder adding records, and which
     feeders lack the base filters. Named only. Creating a feeder is a person's decision.

Deeds reach the data four to eight weeks after a closing, so the newest month is never
counted (lag_months), and the stage's own monthly counts go through the same guard as the
published file: a month reading zero or far below the months around it is left out and said.

Every count is proven before it is used. SiftMap ignores a filter key it does not know and
answers as if it were not there, so the six months are also asked for in one request and the
two totals must agree; when they do not, the county's numbers are not reported at all.

The requests: SiftMap searches (its own request counter, paced here, capped per run), the
account's list index and a few counts through the budgeted client, the feeder listing, and
two public files from the learning site. A search reads. Nothing here can add a record:
the only SiftMap path this module can post to is the search.
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import shard_guard  # noqa: E402

from .. import store  # noqa: E402
from . import feeder_audit  # noqa: E402
from .stages import Ctx  # noqa: E402

log = logging.getLogger(__name__)

MAP = "https://map.reisift.io"
SEARCH_PATH = "/properties/search/"            # the one SiftMap path this module posts to
LADDER_URLS = {"capture": "https://learn.datasift.ai/capture-data/%s.json",
               "county": "https://learn.datasift.ai/county-data/%s.json"}
SEARCH_GAP_S = 1.5
INVESTOR_TYPES = ["pending", "wholesale", "wholetail", "rental", "flip"]
K_TYPE = "extra_last_sale_investor_transaction_type"
# The whole-world viewport: a search intersects its addresses with the map view.
WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852}, {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103}, {"lon": -163.30518783395442, "lat": 72.08452694723852}]
_STD = {"accept": "application/json", "content-type": "application/json", "origin": "https://beta.reisift.io",
        "referer": "https://beta.reisift.io/"}

# Tests swap these for fakes. In production they reach SiftMap, the learning site and the feeder list.
search = None          # (crm, body) -> the search response
fetch_json = None      # (url) -> the parsed file
read_feeders = None    # (crm) -> the raw filter rows
_last_search = [0.0]


class LadderError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------- transport
def _auth(crm) -> str:
    api = getattr(crm, "api", None)
    key = getattr(api, "api_key", "") or ""
    return ("Api-Key " + key) if key else ("Bearer " + (getattr(api, "token", "") or ""))


def _map_search(crm, body: dict) -> dict:
    """One SiftMap search. The path is fixed: this function cannot reach the route that adds
    records to the account, whatever it is handed."""
    if search is not None:
        return search(crm, body)
    wait = SEARCH_GAP_S - (time.time() - _last_search[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(MAP + SEARCH_PATH, json.dumps(body).encode("utf-8"),
                                 headers=dict(_STD, authorization=_auth(crm)), method="POST")
    for attempt in range(4):
        _last_search[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:200]
            if e.code == 429 and attempt < 3:
                try:
                    hold = float((e.headers.get("Retry-After") if e.headers else "") or "")
                except ValueError:
                    hold = 10.0 * (attempt + 1)
                time.sleep(min(hold, 90))
                continue
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(3 + 4 * attempt)
                continue
            raise LadderError("HTTP %s from the SiftMap search: %s" % (e.code, text))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(3 + 4 * attempt)
                continue
            raise LadderError("could not reach SiftMap: %s" % e)
    raise LadderError("SiftMap kept refusing")


def _get_json(url: str):
    """A published ladder file. Only the learning site is read, and nothing is sent to it."""
    if fetch_json is not None:
        return fetch_json(url)
    if not url.startswith("https://learn.datasift.ai/"):
        raise LadderError("refusing to read %s" % url.split("?")[0])
    with urllib.request.urlopen(urllib.request.Request(url, headers={"accept": "*/*"}, method="GET"), timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _feeder_rows(crm) -> list:
    if read_feeders is not None:
        return read_feeders(crm)
    key = getattr(getattr(crm, "api", None), "api_key", "") or ""
    if not key:
        raise LadderError("the feeder listing needs the Open API key")
    return feeder_audit.fetch_filters(key)["rows"]


# ---------------------------------------------------------------------------------------- months
def recorded_months(today: date, months: int, lag: int) -> list[tuple[str, date, date]]:
    """The last `months` whole calendar months, skipping the newest `lag` (their deeds are still
    being recorded). Each is (label 'YYYY-MM', first day, last day)."""
    y, m = today.year, today.month
    for _ in range(lag + 1):                 # step back past the current month and the lag
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    out = []
    for _ in range(months):
        first = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        out.append(("%04d-%02d" % (y, m), first, nxt - timedelta(days=1)))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def county_address(c: dict) -> dict:
    """The county in the shape the search wants. `search` is the bare county name: the full
    'Name County, ST' is accepted and answers zero, which reads exactly like an empty county."""
    title = "%s County, %s" % (c["name"], c["state"])
    return {"state": c["state"], "title": title, "value": title, "county": c["name"], "searchType": "county",
            "counties": [{"fips": str(c["fips"]), "county_name": c["name"]}], "search": c["name"], "type": "county"}


def _body(c: dict, first: date, last: date, offset: int = 0, extra: dict | None = None) -> dict:
    filters = {"type_single_family": True, K_TYPE: list(INVESTOR_TYPES), "extra_last_sale_value_min": 1000,
               "extra_is_last_sale_interfamily": False, "extra_last_sale_date_min": first.isoformat(),
               "extra_last_sale_date_max": last.isoformat()}
    filters.update(extra or {})
    return {"result_index": 1 + offset, "with_boundaries": False, "filters": filters,       # a row offset, not a page number
            "addresses": [county_address(c)], "polygon": WORLD}


def _when(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


class _Searches:
    """The run's search allowance. When it is used the stage stops reading and says so."""

    def __init__(self, crm, cap: int):
        self.crm, self.cap, self.n = crm, cap, 0

    def __call__(self, body: dict) -> dict:
        if self.n >= self.cap:
            raise LadderError("the run's limit of %d SiftMap searches is used" % self.cap)
        self.n += 1
        r = _map_search(self.crm, body)
        if not isinstance(r, dict):
            raise LadderError("the search answered something that is not a result")
        return r


# ---------------------------------------------------------------------------------------- the deals
def county_deals(ask: _Searches, c: dict, months: list, max_rows: int) -> dict:
    """Every investor purchase in the county for each month, with the account fields its row
    carries. Raises when the monthly counts and the whole-window count disagree: that is what an
    ignored date filter looks like, and numbers built on it would be wrong without saying so."""
    counts, rows, short = [], [], 0
    for label, first, last in months:
        got, total, offset = [], None, 0
        while True:
            r = ask(_body(c, first, last, offset))
            if total is None:
                total = int(r.get("total_results") or 0)
            page = r.get("data") or []
            got += page
            offset += len(page)               # the next page starts after the rows read, whatever size a page is
            if not page or len(got) >= total or len(rows) + len(got) >= max_rows:
                break
        counts.append(total)
        short += max(0, total - len(got))
        rows += [(label, first, row) for row in got]
    window = int(ask(_body(c, months[0][1], months[-1][2])).get("total_results") or 0)
    # A deed recorded between two requests may move a count by one. An ignored date filter moves it
    # by whole months, so a small slack does not hide it.
    if abs(window - sum(counts)) > max(2, 0.01 * window):
        raise LadderError("the months add to %d sales and the same window asked at once holds %d, so the date filter "
                          "cannot be trusted" % (sum(counts), window))
    return {"counts": counts, "rows": rows, "rows_not_read": short}


def list_index(crm) -> dict:
    """uuid -> {title, size, kept_current}. Two listings: the account's own lists and the lists
    DataSift keeps current. The second kind follows today's owner, who after a sale is the buyer."""
    out = {}
    for path, system in (("/api/internal/list/?offset=0&limit=1000", False), ("/api/internal/system-list/?offset=0&limit=1000", True)):
        try:
            r = crm.call(path)
        except Exception as exc:  # noqa: BLE001  (an account with no system lists route still has its own lists)
            if not system:
                raise
            log.info("system lists not read: %s", str(exc)[:100])
            continue
        for row in (r.get("results") if isinstance(r, dict) else r) or []:
            size = row.get("properties_count")
            try:
                size = int(size) if size not in (None, "") else None
            except (TypeError, ValueError):
                size = None
            if row.get("uuid"):
                out[row["uuid"]] = {"title": str(row.get("title") or "").strip(), "size": size, "kept_current": system}
    return out


def read_deals(rows: list, kept: set, lists: dict, skip: tuple) -> dict:
    """What the kept months' deals say about the account's lists."""
    total = in_before = in_late = 0
    on, waits, unknown = Counter(), defaultdict(list), 0
    account_fields = lists_given = False
    for label, first, row in rows:
        if label not in kept:
            continue
        total += 1
        if "saved" in row or "saved_uuid" in row:
            account_fields = True
        if not (row.get("saved") or row.get("saved_uuid")):
            continue
        saved = _when(row.get("saved_date"))
        if saved is None or saved >= first:
            in_late += 1                      # added the month it sold, or after: it was not a lead first
            continue
        in_before += 1
        if isinstance(row.get("saved_lists"), list):
            lists_given = True                # an empty list is "on no list"; a missing one is "not told"
        mid = first + timedelta(days=14)
        titles = set()
        for u in row.get("saved_lists") or []:
            u = u.get("uuid") if isinstance(u, dict) else u
            info = lists.get(u)
            if info is None:
                unknown += 1
                continue
            titles.add(u)
        for u in titles:
            if any(lists[u]["title"].lower().startswith(p.lower()) for p in skip):
                continue
            on[u] += 1
            waits[u].append((mid - saved).days / 30.44)
    return {"deals": total, "in_account_before": in_before, "added_after": in_late, "on": on, "waits": waits,
            "lists_gone": unknown, "account_fields": account_fields, "lists_given": lists_given}


def _pct(n: int, d: int) -> str:
    return "%.1f%%" % (100.0 * n / d) if d else "0%"


def _n(v) -> str:
    return format(int(round(v)), ",")


# ---------------------------------------------------------------------------------------- the stage
def run_laddercheck(ctx: Ctx) -> None:
    st = ctx.stage
    counties = [dict(c) for c in (st.get("counties") or []) if isinstance(c, dict) and c.get("fips") and c.get("name") and c.get("state")]
    if not counties:
        ctx.out.problem("no county is named in the playbook (counties: [{fips, name, state}])")
        ctx.out.head = "The ladder check has no county to read."
        return
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    months = recorded_months(today, int(st.get("months", 6)), int(st.get("lag_months", 1)))
    ask = _Searches(ctx.crm, int(st.get("max_searches", 60)))
    skip = tuple(str(x) for x in (st.get("skip_lists") or ["Dispo - "]))
    result: dict = {"month": today.isoformat()[:7], "months": [m[0] for m in months], "counties": {}}
    found = acts = 0

    # ---- 1. the published window, through the guard
    shards: dict = {}
    if st.get("window", True) or st.get("feeders", True):
        for c in counties:
            for family in ("county", "capture"):
                key = (family, str(c["fips"])[:2])
                if key in shards or (family == "county" and not st.get("window", True)) or (family == "capture" and not st.get("feeders", True)):
                    continue
                try:
                    shards[key] = _get_json(LADDER_URLS[family] % key[1])
                except Exception as exc:  # noqa: BLE001  (a file that cannot be read is said, and the rest still runs)
                    shards[key] = None
                    ctx.out.problem("could not read the published %s file for state %s (%s)" % (
                        "doors-per-deal" if family == "county" else "ladder", key[1], str(exc)[:100]))
    if st.get("window", True):
        for c in counties:
            shard = shards.get(("county", str(c["fips"])[:2]))
            g = (shard_guard.county_from(shard, str(c["fips"])) or {}).get("guard") if shard else None
            if g and g.get("left_out"):
                acts += 1
                ctx.out.line("%s, published doors per deal: %s. Read every figure at %s of its size." % (
                    c["name"], g["line"], ("%.2f" % (1 / g["six_month_factor"])).rstrip("0").rstrip(".")
                    if g.get("usable") and g["six_month_factor"] else "its published size, with care"))
                result["counties"].setdefault(c["name"], {})["published_window"] = g["line"]

    # ---- 2 and 3. the deals, and the reach
    lists = None
    if st.get("validate", True):
        try:
            lists = list_index(ctx.crm)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read the account's lists (%s); the share of deals per list was not measured" % str(exc)[:100])
    reached_must = _reached_filter(ctx)
    for c in counties if lists is not None else []:
        try:
            got = county_deals(ask, c, months, int(st.get("max_rows", 5000)))
        except Exception as exc:  # noqa: BLE001  (one county failing must not hide the other)
            ctx.out.problem("%s: %s. Nothing is reported for the county" % (c["name"], str(exc)[:220]))
            continue
        if not sum(got["counts"]):
            # A county name the search does not resolve answers zero, exactly like a county where
            # nothing sold. Six months with no investor purchase is said as a question, not a finding.
            ctx.out.problem("%s: no investor purchase was found in %d months. Check the county's name and state in the "
                            "playbook before reading that as a market with no buyers" % (c["name"], len(months)))
            continue
        verdicts = shard_guard.judge(got["counts"], [m[0] for m in months])
        kept = {v["label"] for v in verdicts if v["kept"]}
        window = shard_guard.window_line(verdicts)
        facts = read_deals(got["rows"], kept, lists, skip)
        found += facts["deals"]
        cres = result["counties"].setdefault(c["name"], {})
        cres.update(window=window, deals=facts["deals"], counts=dict(zip([m[0] for m in months], got["counts"])),
                    in_account_before=facts["in_account_before"], added_after=facts["added_after"])
        if got["rows_not_read"]:
            ctx.out.problem("%s: %d sale(s) were past the row limit and were not read" % (c["name"], got["rows_not_read"]))
        if len(kept) < shard_guard.MIN_MONTHS:
            ctx.out.problem("%s: %s. Too few months to read a share from" % (c["name"], window))
            continue
        if facts["deals"] and not facts["account_fields"]:
            ctx.out.problem("%s: the sold rows carried no account fields, so the share of deals per list could not be "
                            "measured" % c["name"])
            continue
        ctx.out.line("%s: %s investor deal(s) in %s. %s (%s) were records here before they sold; %s were added after." % (
            c["name"], _n(facts["deals"]), window, _n(facts["in_account_before"]),
            _pct(facts["in_account_before"], facts["deals"]), _n(facts["added_after"])))
        if facts["in_account_before"] and not facts["lists_given"]:
            # "On no list" and "the row did not say" must not read the same.
            ctx.out.problem("%s: the sold rows did not say which lists each record is on, so the share of deals per list "
                            "could not be measured" % c["name"])
            continue
        cres["lists"] = _per_list(ctx, facts, lists, len(kept), reached_must, c["name"])
    if lists is not None:
        _remember_sizes(ctx, lists, result)

    # ---- 4. the feeders against the ladder
    if st.get("feeders", True):
        acts += _feeders(ctx, counties, shards, result)

    # ---- the standing layered count, when the playbook names layers
    _layers(ctx, ask, counties, result)

    store.kv_set("autopilot:ladder:%s" % result["month"], json.dumps(result, default=str))      # the autopilot's own record
    ctx.out.found, ctx.out.acted = found, acts
    ctx.out.tally["SiftMap searches"] = ask.n
    span = "%s to %s" % (shard_guard.month_name(months[0][0]), shard_guard.month_name(months[-1][0], with_year=True))
    names = [c["name"] for c in counties]
    where = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    ctx.out.head = "Ladder check for %s, %s: %s investor deal(s) read, %d thing(s) to look at." % (
        where, span, _n(found), acts + len(ctx.out.problems))


def _reached_filter(ctx: Ctx) -> dict | None:
    """The saved preset that holds reached records, as a search filter. A preset that names lists
    of its own cannot be crossed with each list, so it is refused and said."""
    title = str(ctx.stage.get("reached_preset") or "").strip()
    if not title:
        return None
    try:
        must = dict(ctx.crm.preset_must(title))
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("the reached preset %r could not be read (%s); reach was not measured" % (title, str(exc)[:80]))
        return None
    if "any_lists" in must or "all_lists" in must:
        ctx.out.problem("the reached preset %r names lists of its own, so it cannot be counted list by list" % title)
        return None
    return must


def _per_list(ctx: Ctx, facts: dict, lists: dict, kept_months: int, reached_must: dict | None, county: str) -> list:
    st = ctx.stage
    min_deals = int(st.get("min_deals", 5))
    out, narrowed = [], False
    for u, n in facts["on"].most_common(int(st.get("max_lists", 10))):
        info = lists[u]
        size = info["size"]
        # A list DataSift keeps current is not one of the account's own lists, and the records
        # search is only proven for those. Its size comes from its own listing or not at all.
        if size is None and not info["kept_current"]:
            size = ctx.crm.count({"any_lists": [u]})
        row = {"list": info["title"], "deals": n, "share": round(100.0 * n / facts["deals"], 1) if facts["deals"] else 0.0,
               "size": size, "kept_current": info["kept_current"]}
        at_six = n * 6.0 / kept_months
        if n >= min_deals and size:
            row["doors_per_deal"] = round(size / at_six, 1)
        if len(facts["waits"][u]) >= min_deals:
            row["months_to_sale"] = round(statistics.median(facts["waits"][u]), 1)
        if reached_must is not None and size and not info["kept_current"]:
            reached = ctx.crm.count(dict(reached_must, any_lists=[u]))
            row["reached"] = reached
            narrowed = narrowed or reached < size
            if row.get("doors_per_deal"):
                row["deals_that_reach_should_produce"] = round(reached / row["doors_per_deal"], 1)
        out.append(row)
    if reached_must is not None and out and not narrowed:
        ctx.out.problem("%s: the reached preset did not narrow any list, so it is not filtering; reach is not reported" % county)
        for row in out:
            row.pop("reached", None)
            row.pop("deals_that_reach_should_produce", None)
    if out:
        ctx.out.line("%s, share of deals by list: %s." % (county, "; ".join(
            "%s %s%% (%d)%s" % (r["list"], ("%.1f" % r["share"]).rstrip("0").rstrip("."), r["deals"],
                               " [follows today's owner]" if r["kept_current"] else "") for r in out[:4])))
        proven = [r for r in out if r.get("doors_per_deal")][:3]
        if proven:
            ctx.out.line("%s, this account's own doors per deal: %s." % (county, "; ".join(
                "%s 1 in %s%s" % (r["list"], _n(r["doors_per_deal"]),
                                  (", %s months to a sale" % ("%g" % r["months_to_sale"])) if r.get("months_to_sale") else "")
                for r in proven)))
        forecast = [r for r in out if r.get("deals_that_reach_should_produce") is not None][:3]
        if forecast:
            ctx.out.line("%s, reach: %s." % (county, "; ".join(
                "%s %s of %s reached (%s), about %s deal(s) in six months" % (
                    r["list"], _n(r["reached"]), _n(r["size"]), _pct(r["reached"], r["size"]),
                    ("%g" % r["deals_that_reach_should_produce"])) for r in forecast)))
        thin = [r["list"] for r in out if r["deals"] < min_deals]
        if thin:
            ctx.out.tally["lists with too few sales to read a rate from"] += len(thin)
    elif facts["deals"]:
        ctx.out.line("%s: none of the deals was on a list in this account before it sold." % county)
    if facts["lists_gone"]:
        ctx.out.tally["list marks on sold records that no longer match a list"] += facts["lists_gone"]
    return out


def _remember_sizes(ctx: Ctx, lists: dict, result: dict) -> None:
    """Each list's size, kept month over month, so turnover is a number and not a guess (about a
    tenth of a list is new each month in the lessons)."""
    now = {v["title"]: v["size"] for v in lists.values() if v["size"] is not None and not v["kept_current"]}
    try:
        before = json.loads(store.kv_get("autopilot:ladder:sizes") or "{}")
    except ValueError:
        before = {}
    month = result["month"]
    if before.get("month") and before["month"] != month:
        moved = []
        for title, size in now.items():
            was = (before.get("sizes") or {}).get(title)
            if was and size and abs(size - was) / was >= float(ctx.stage.get("turnover_note", 0.05)):
                moved.append((abs(size - was), "%s %s%s (%s%%)" % (title, "+" if size > was else "-", _n(abs(size - was)),
                                                                  ("%.0f" % (100.0 * abs(size - was) / was)))))
        if moved:
            ctx.out.line("List sizes since %s: %s." % (before["month"], "; ".join(t for _n_, t in sorted(moved, reverse=True)[:4])))
        result["sizes_before"] = before.get("month")
    if before.get("month") != month:
        store.kv_set("autopilot:ladder:sizes", json.dumps({"month": month, "sizes": now}))
    result["list_sizes"] = now


def _feeders(ctx: Ctx, counties: list, shards: dict, result: dict) -> int:
    """The Priority 1 rungs against the feeders. Returns how many things it found to look at."""
    rungs = []
    for c in counties:
        shard = shards.get(("capture", str(c["fips"])[:2]))
        got = feeder_audit.ladder_rungs(shard, str(c["fips"])) if shard else []
        if not got:
            ctx.out.problem("%s: the published ladder has no rungs for the county, so its feeders were not checked" % c["name"])
        top = int(ctx.stage.get("rungs", 0))
        if top:
            keep = {r["n"] for r in [r for r in got if r.get("priority") == feeder_audit.LADDER_PRIORITY][:top]}
            got = [r for r in got if r.get("priority") != feeder_audit.LADDER_PRIORITY or r["n"] in keep]
        rungs += got
    if not rungs:
        return 0
    try:
        rows = _feeder_rows(ctx.crm)
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the feeders (%s); the ladder was not compared with them" % str(exc)[:120])
        return 0
    feeders = [f for f in (feeder_audit.facts(r) for r in rows) if f["account_filter"]]
    for f in feeders:
        f["kind"] = feeder_audit.kind_of(f)
    check = feeder_audit.ladder_check(feeders, rungs)
    result["feeders"] = {"summary": check["summary"], "rungs": check["rungs"], "notes": check["notes"]}
    s = check["summary"]
    said = feeder_audit.ladder_lines(check, limit=int(ctx.stage.get("max_feeder_lines", 3)))
    ctx.out.line(said[0])
    if s["missing"]:
        ctx.out.problem("%d Priority 1 rung(s) have no feeder: %s" % (s["missing"], "; ".join(
            "%s%s" % (r["label"], (" (%s)" % r["county"]) if len(counties) > 1 and r["county"] else "")
            for r in check["rungs"] if r["status"] == "missing")[:300]))
    for ln in said[1:]:
        if not ln.startswith("No feeder for rung"):
            ctx.out.line(ln)
    for k in ("covered", "missing", "saved_not_adding", "covered_with_gaps"):
        ctx.out.tally["Priority 1 rungs: %s" % k.replace("_", " ")] = s[k]
    return s["missing"] + s["covered_with_gaps"] + s["saved_not_adding"] + len(check["notes"])


def _layers(ctx: Ctx, ask: _Searches, counties: list, result: dict) -> None:
    """The standing layered count (lesson 3-01): county, base criteria, top ZIPs and so on, each a
    named filter in the playbook, counted once a month and kept. The filters live in the playbook
    so each key can be proven there by a count that moves; none is invented here."""
    layers = [x for x in (ctx.stage.get("layers") or []) if isinstance(x, dict) and x.get("name")]
    for c in counties if layers else []:
        got, last = [], None
        for layer in layers:
            only = layer.get("county")
            if only and str(only).lower() != c["name"].lower():
                continue
            try:
                body = {"result_index": 1, "with_boundaries": False, "filters": dict(layer.get("filters") or {}),
                        "addresses": [county_address(c)], "polygon": WORLD}
                n = int(ask(body).get("total_results") or 0)
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("%s, layer %s: %s" % (c["name"], layer["name"], str(exc)[:120]))
                break
            if last is not None and n == last and layer.get("filters"):
                ctx.out.problem("%s: the layer %r counts the same as the one above it (%s), so its filter may be ignored"
                                % (c["name"], layer["name"], _n(n)))
            got.append((str(layer["name"]), n))
            last = n
        if got:
            ctx.out.line("%s, records by layer: %s." % (c["name"], ", ".join("%s %s" % (k, _n(v)) for k, v in got)))
            result["counties"].setdefault(c["name"], {})["layers"] = dict(got)
