"""mls_offers: every active MLS listing in a market screened, priced and offered on, on the install's own key.

Roma's workflow (team/roster.yaml). The engine is `src/mls_offers_api.py`; this module is the team's
door to it: it reads the ask, decides what this box may spend, runs the engine into the task's run
folder, links the workbook and the agent CSV in Slack, and says what it did not do and why.

What runs on every ask, dry or live (it spends nothing and writes nothing):
  * SiftMap search over the market: every active single-family listing, its days on market, the
    distress flags, the equity, and the investor purchases nearby that price the cash offer. Search
    is the route DataSift's acceptable use allows in volume.
  * Full details (the listing agent, the listing history, the seller's loans) only for listings that
    are ALREADY records in the account, which acceptable use allows; the loans are modelled from
    public data (HMDA, Freddie Mac, the Census geocoder).

What waits for a yes:
  * Adding the shortlist to the account (it uses the record allowance) happens only when the box is
    live for this task (config.task_live), the ask said add, and AGENTS_MLS_ADD_MAX is above 0 on
    this box. The shortlist is then capped at that number, so no ask adds more records than the box
    allows. The engine quotes the add on every run (a quote stores and charges nothing), so the
    reply always says how many records adding the rest of the shortlist would use.
  * Zillow (the listing remarks and a freshness check, paid per call) only when live, asked, the
    OPENWEBNINJA_API_KEY is set, and the call budget fits under the run and day caps. Capped at
    AGENTS_MLS_ZILLOW_MAX calls.

It sends nothing to an agent or a homeowner: the workbook holds the first text to each listing agent
as a draft for a person to send. The Slack reply carries no phone number at all; the agent's office
line stays in the workbook.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from pathlib import Path

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

NAME = "mls_offers"
SHORTLIST_MAX = 150                      # the hardest a person can ask for in one run

# Tests swap these for fakes.
ENGINE = None        # the mls_offers_api module
CLIENT = None        # the SiftMap client handed to the engine

PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
ZIPS_RX = re.compile(r"^\d{5}(?:[\s,]+\d{5})*$")
FIPS_RX = re.compile(r"^(?i:fips)[:\s]*(\d{5}(?:[\s,]+\d{5})*)$")
YES = ("1", "true", "yes", "y", "on", "add", "go")


# --------------------------------------------------------------------------- plumbing

def setting(name: str, default):
    """One setting from the environment (Fly secrets on the box), typed like its default."""
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    try:
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


def _engine():
    if ENGINE is not None:
        return ENGINE
    import mls_offers_api  # noqa: PLC0415  (src/ is on the path; imported on first use)
    return mls_offers_api


class _BudgetedSiftMap:
    """SiftMap's documented routes on the Open API key, each request counted against the app's shared
    request budget like every DataSift read (the list_pull pattern). The engine calls the client's
    search and get_detail, which both go through `_request`."""

    def __init__(self):
        from siftmap_standalone import SiftMapClient  # noqa: PLC0415
        key = (os.environ.get("REISIFT_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("REISIFT_API_KEY is not set on this box, so SiftMap cannot be read")

        class Budgeted(SiftMapClient):
            def _request(self, path, method="GET", body=None, auth=True, timeout=40.0):
                from ..autopilot import budget  # noqa: PLC0415
                budget.acquire()
                return super()._request(path, method=method, body=body, auth=auth, timeout=timeout)

        self.client = Budgeted(key, min_interval=0.5)

    def __getattr__(self, name):
        return getattr(self.client, name)


def _client():
    if CLIENT is not None:
        return CLIENT
    return _BudgetedSiftMap().client


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task.get("id") or 0)))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def scrub(text) -> str:
    """Anything shaped like a phone number comes out of text that goes to Slack."""
    return " ".join(PHONE_RX.sub("", str(text or "")).split())


def yes(v) -> bool:
    return str(v or "").strip().lower() in YES


def money(v) -> str:
    try:
        return "$%s" % format(int(round(float(v))), ",")
    except (TypeError, ValueError):
        return ""


def num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- the ask

def parse_market(text: str) -> tuple[str, str, str]:
    """(counties, fips, zips) from the ask. Counties are separated by semicolons because a county reads
    "Knox, TN"; a run of five-digit numbers is ZIP codes; "fips 47093" names a county by its code."""
    counties, fips, zips = [], [], []
    for part in re.split(r"[;\n]+", str(text or "")):
        p = part.strip().strip(".")
        if not p:
            continue
        hit = FIPS_RX.match(p)
        if hit:
            fips += re.findall(r"\d{5}", hit.group(1))
        elif ZIPS_RX.match(p):
            zips += re.findall(r"\d{5}", p)
        else:
            counties.append(p)
    return "; ".join(counties), ",".join(fips), ",".join(zips)


def resolve(eng, text: str):
    """(market dict, problem). One call with every county; when that fails on a list of counties,
    each is resolved alone and the markets merged, so one engine that reads a single county still
    takes "Knox, TN; Blount, TN"."""
    counties, fips, zips = parse_market(text)
    if not (counties or fips or zips):
        return None, "name the market: a county like Knox, TN (several separated by semicolons) or ZIP codes"
    try:
        return eng.resolve_market(county=counties, fips=fips, zips=zips), ""
    except ValueError as exc:
        many = [c for c in counties.split("; ") if c]
        if len(many) < 2:
            return None, str(exc).rstrip(".")
    merged, why = None, ""
    for i, c in enumerate(many):
        try:
            m = eng.resolve_market(county=c, fips=fips if i == 0 else "", zips=zips if i == 0 else "")
        except ValueError as exc:
            return None, str(exc).rstrip(".")
        if merged is None:
            merged = dict(m)
            continue
        for k, v in m.items():
            if isinstance(v, list):
                merged[k] = list(merged.get(k) or []) + [x for x in v if x not in (merged.get(k) or [])]
        merged["name"] = "%s-%s" % (merged.get("name") or "market", m.get("name") or "market")
    return merged, why


def plan(task: dict) -> dict:
    """What this ask may do on this box, before anything runs."""
    inputs = store.payload_of(task).get("inputs") or {}
    live = config.task_live(task)
    want = num(inputs.get("shortlist")) if str(inputs.get("shortlist") or "").strip() else int(setting("AGENTS_MLS_SHORTLIST", 60))
    shortlist = max(1, min(SHORTLIST_MAX, want or int(setting("AGENTS_MLS_SHORTLIST", 60))))
    add_asked, zillow_asked = yes(inputs.get("add")), yes(inputs.get("zillow"))
    add_max = max(0, int(setting("AGENTS_MLS_ADD_MAX", 0)))
    out = {"live": live, "shortlist": shortlist, "asked_shortlist": want, "add_asked": add_asked, "add_max": add_max,
           "commit": False, "add": add_asked, "zillow": False, "zillow_limit": 0, "zillow_asked": zillow_asked,
           "not_done": [], "months": int(setting("AGENTS_MLS_MONTHS", 24)),
           "list_name": str(setting("AGENTS_MLS_LIST", "MLS Offers")), "tag": str(setting("AGENTS_MLS_TAG", "MLS Listing"))}
    if want > SHORTLIST_MAX:
        out["not_done"].append("The shortlist is capped at %d a run, so %d were read instead of %d." % (SHORTLIST_MAX, SHORTLIST_MAX, want))
    if add_asked:
        if not live:
            out["not_done"].append("Nothing was added to the account: the brain is in dry mode for this ask.")
        elif add_max <= 0:
            out["not_done"].append("Nothing was added to the account: AGENTS_MLS_ADD_MAX is 0 on this box, so the team "
                                   "never adds records on its own.")
        else:
            out["commit"] = True
            if shortlist > add_max:
                out["not_done"].append("The shortlist was cut to %d, the most records one ask may add here "
                                       "(AGENTS_MLS_ADD_MAX)." % add_max)
                out["shortlist"] = add_max
    if zillow_asked:
        key = (os.environ.get("OPENWEBNINJA_API_KEY") or "").strip()
        cap = max(0, int(setting("AGENTS_MLS_ZILLOW_MAX", 60)))
        price = float(setting("AGENTS_ZILLOW_CALL_USD", 0.01))
        most = round(price * cap, 2)
        run_cap = float(setting("AGENTS_MLS_RUN_CAP_USD", 2.0))
        day_used = store.spent(day=store.today(), research=False)
        if not live:
            out["not_done"].append("Zillow was not read: the brain is in dry mode for this ask.")
        elif not key:
            out["not_done"].append("Zillow was not read: OPENWEBNINJA_API_KEY is not set on this box.")
        elif cap <= 0:
            out["not_done"].append("Zillow was not read: AGENTS_MLS_ZILLOW_MAX is 0 on this box.")
        elif most > run_cap or day_used + most > config.DAILY_CAP_USD:
            out["not_done"].append("Zillow was not read: about $%.2f of calls would pass the cap (run $%.2f, day $%.0f, "
                                   "$%.2f used today)." % (most, run_cap, config.DAILY_CAP_USD, day_used))
        else:
            out.update(zillow=True, zillow_limit=cap, zillow_price=price)
    return out


# --------------------------------------------------------------------------- run

def run(task: dict) -> dict:
    inputs = store.payload_of(task).get("inputs") or {}
    folder = _folder(task)
    base = {"ok": False, "run_dir": str(folder), "files": [], "market_asked": str(inputs.get("market") or "").strip()}
    try:
        eng = _engine()
    except Exception as exc:  # noqa: BLE001  (an install without the engine answers in a sentence)
        return dict(base, problem="the MLS offer engine could not be loaded (%s)" % str(exc)[:160])
    market, why = resolve(eng, base["market_asked"])
    if market is None:
        return dict(base, problem=why)
    p = plan(task)
    base.update(market=market.get("name") or "", plan=p)
    try:
        client = _client()
    except RuntimeError as exc:
        return dict(base, problem=str(exc).rstrip("."))
    lines = []
    # The engine names the workbook after its folder (<Folder>_MLS_Offers_<date>.xlsx), so the folder is the
    # market's own slug, exactly as on the command line. It sits beside files/, never in it: only the
    # workbook and the CSV are copied into the folder the signed links serve.
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(market.get("name") or "")).strip("-.")[:60] or "market"
    if slug in ("files", "packs"):
        slug = "market-" + slug
    try:
        # sender: who the agent drafts are signed by, the same MLS_OFFERS_SENDER the command line reads
        # (the engine's run() takes it as given; unset, the drafts name nobody).
        res = eng.run(market, out=folder / slug, shortlist=p["shortlist"], add=p["add"], commit=p["commit"],
                      zillow=p["zillow"], zillow_limit=p["zillow_limit"], months=p["months"], list_name=p["list_name"],
                      tag=p["tag"], client=client, log=lambda m: lines.append(str(m)),
                      sender=(os.environ.get("MLS_OFFERS_SENDER") or "").strip())
    except (ValueError, RuntimeError, SystemExit) as exc:
        # SystemExit: the engine is also a command line, and a download it cannot finish (the Freddie
        # Mac series, say) stops it that way. Said here as a sentence like every other refusal.
        return dict(base, problem=str(exc).rstrip("."))
    res = res or {}
    if p["zillow"]:
        counts = res.get("counts") or {}
        calls = counts.get("zillow_calls", counts.get("zillow"))
        price = p.get("zillow_price", 0.01)
        usd = round((num(calls) if calls is not None else p["zillow_limit"]) * price, 2)
        if usd > 0:
            store.add_spend(int(task.get("id") or 0), usd, "MLS offers: Zillow (%s calls)" % (
                calls if calls is not None else "up to %d" % p["zillow_limit"]))
        base["zillow_usd"] = usd
    files = []
    for key in ("workbook", "csv"):
        src = res.get(key)
        if src and Path(src).is_file():
            dst = folder / "files" / Path(src).name
            if Path(src).resolve() != dst.resolve():
                shutil.copy2(src, dst)
            files.append(dst.name)
    top = []
    for t in (res.get("top") or [])[:10]:
        # The agent's phone is kept out of the result: it stays in the workbook only.
        top.append({k: t.get(k) for k in ("address", "city", "list_price", "cash_offer", "lane", "creative_price",
                                          "agent", "score", "why")})
    return dict(base, ok=True, problem="", files=files, counts=res.get("counts") or {}, quote=res.get("quote"),
                notes=[n for n in (slack_note(scrub(x)) for x in (res.get("notes") or [])) if n][:6], top=top,
                log_tail=[scrub(x) for x in lines[-5:]])


# --------------------------------------------------------------------------- render

# The engine is also a command line, so a few of its notes name flags and phases that mean nothing
# in Slack. Its wording is pinned by selftest_mls_offers, which fails when these phrases leave it.
NOTE_SAID_HERE = "not in your account"   # the reply's own line counts these and says how to add them
NOTE_WORDS = (("run the detail phase again later", "ask again later"),
              ("run the detail phase again", "ask again"))


def slack_note(note: str) -> str:
    """An engine note in Slack's words, or "" when the reply already says it its own way."""
    n = str(note or "").strip()
    if NOTE_SAID_HERE in n:
        return ""
    for cli, ask in NOTE_WORDS:
        n = n.replace(cli, ask)
    return n


def _offer_line(i: int, t: dict) -> str:
    where = ", ".join(x for x in (t.get("address"), t.get("city")) if x) or "a listing"
    bits = ["list %s" % money(t.get("list_price"))] if t.get("list_price") else []
    cash = money(t.get("cash_offer"))
    lane = str(t.get("lane") or "").strip()
    if lane.lower() == "cash":
        # The engine names a cash-only row's lane "Cash"; a creative_price on such a row belongs to a
        # creative lane it dropped (one that did not cash flow), so it is not an offer to quote.
        lane = ""
    if cash and lane:
        bits.append("cash %s or %s at %s" % (cash, lane.lower(), money(t.get("creative_price")) or "list"))
    elif cash:
        bits.append("cash %s" % cash)
    elif lane:
        bits.append("%s at %s" % (lane.lower(), money(t.get("creative_price")) or "list"))
    if t.get("agent"):
        bits.append("agent %s" % t["agent"])
    return "%d. %s: %s." % (i, where, "; ".join(bits) or "no offer fits")


def render(task: dict, result: dict) -> str:
    if not result.get("ok"):
        return scrub("MLS offers did not run: %s." % str(result.get("problem") or "no reason given").rstrip("."))
    c, p = result.get("counts") or {}, result.get("plan") or {}
    lines = ["MLS offers for %s: %d active listings screened, %d shortlisted, %d read in full (the ones in the account%s)." % (
        result.get("market") or "the market", num(c.get("listings")), num(c.get("shortlisted")), num(c.get("detailed")),
        ", %d of them added now" % num(c.get("added")) if num(c.get("added")) else "")]
    lines.append("%d offers: %d cash, %d subject-to, %d seller finance; %d on the price watch; %d investor buys priced them." % (
        num(c.get("offers")), num(c.get("cash")), num(c.get("subject_to")), num(c.get("seller_finance")),
        num(c.get("price_watch")), num(c.get("investor_sales"))))
    top = result.get("top") or []
    if top:
        lines.append("Top offers:")
        lines += [_offer_line(i, t) for i, t in enumerate(top[:5], 1)]
    q = result.get("quote") or {}
    not_done = list(p.get("not_done") or [])
    waiting = num(c.get("shortlisted")) - num(c.get("detailed"))
    if waiting > 0 and not p.get("commit"):
        # Not read in full = not a record yet (most) or a read that failed (its own note says so). Each
        # keeps a preliminary cash number from search on the More listings sheet, with no agent or loan.
        line = ("%d shortlisted listings were not read in full, so each has only a preliminary cash number, on "
                "the More listings sheet." % waiting)
        if q:
            line += (" %d of them are not records yet; adding them would use %d records of the allowance (the "
                     "quote stores and charges nothing)." % (num(q.get("new", q.get("total"))),
                                                            num(q.get("billable", q.get("new")))))
        if not p.get("add_asked"):
            line += " " + ("Ask again with add=yes to add them." if p.get("live") and num(p.get("add_max")) > 0 else
                           "Adding them needs AGENTS_MLS_ADD_MAX above 0 on this box, then an ask with add=yes."
                           if p.get("live") else
                           "Adding them needs a live box, AGENTS_MLS_ADD_MAX above 0 and an ask with add=yes.")
        not_done.append(line)
    if result.get("zillow_usd"):
        not_done.append("Zillow: about $%.2f." % result["zillow_usd"])
    lines += [n for n in not_done]
    lines += [n for n in (result.get("notes") or [])[:3]]
    lines.append("Nothing was sent: the first text to each listing agent is a draft in the workbook for a person to send.")
    lines = [scrub(x) for x in lines]
    for name in result.get("files") or []:
        # The signed link is added after the scrub: its signature is hex and must reach Slack unchanged.
        url = slack.file_url(int(task.get("id") or 0), name)
        lines.append("%s: %s" % (name, url) if url else name)
    return "\n".join(lines)
