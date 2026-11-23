"""The property check on a new lead (rules C9.16, L14.115, L14.117, L14.122, L10.34).

Why it exists. The lead manager asks every new lead about the house, and a seller describes their
own house kindly: the course says believe "good shape" only when the old listing photos confirm it
(C9.16). It also asks whether the house was listed with an agent in the last six months (L14.115):
an agent may still hold a listing agreement and be owed money, and a listing that expired or was
pulled is a strong reason to sell. A house on the market with an agent right now is not a target at
all (L10.34). None of it was looked up: on 2026-11-15 nothing in src/agents read a lead's own listing
history or its photos (grepped "listing photo|street view|priceHistory": no hits), so the first call
started from what the seller said.

Once per record, when it ENTERS a lead status (a move between two lead statuses is not a new lead):
  1. the record's status changes are read from the brain's copy of the account log, from a
     watermark, so a run that stops early resumes where it left off;
  2. the record is gated (the research channel) and a company or trust is left alone (people only);
  3. ONE Zillow property details call through OpenWebNinja (the call property_enricher.py makes):
     the price history gives every listing with its price and how it ended, and the last sale; the
     listing's attribution names the agent;
  4. when that answer carries listing photos, up to `max_photos` of them go to Claude Haiku 5.5 with
     thinking off and effort low (voice._ask_claude's way of calling it) for a one-phrase condition
     (excellent, needs a little updating, needs a lot of work) and what it saw (L14.117);
  5. ONE note, "Property check", is written on the record. A house with no photos says so, and the
     note asks the seller to text some (L14.122).

Plan mode reads and gates, estimates what a live run would spend, and spends nothing: no Zillow call,
no photo read, no note. A missing key is a problem said in words, never a crash. A result that was
paid for is kept before its note is written, so a note that fails is written on the next run without
paying again. SiftMap's images route is never used: DataSift's acceptable use prohibits it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
import model_registry
from . import activity, budget, lanes, ledger
from .stages import Ctx, addr

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

# The same endpoint property_enricher.py, sms_agent.sc_inbound and mls_offers.py call.
DETAILS_URL = "https://api.openwebninja.com/realtime-zillow-data/property-details-address"
ACTION = "leadprep"                 # ap_events: one row per paid check, with what it cost
MARK = "leadprep"                   # ap_facts on a status event: what a live run decided about it
PLAN_MARK = "leadprep_plan"         # the same for plan runs, kept apart so a plan never blocks a live run
CHECKED = "leadprep_checked"        # ap_facts on a record: when it was last checked and what was found
RESULT = "leadprep_result"          # ap_facts on a record: a paid result kept until its note is written
DONE = ("checked", "skipped")
CONDITIONS = ("excellent", "needs a little updating", "needs a lot of work")
UNSURE = "cannot tell"
PHOTO_TOKENS = 1600                 # a 1,024 pixel photo is about 1,400 input tokens: the estimate runs high
TEXT_TOKENS = 900
OUT_TOKENS = 250
MAX_TRIES = 3                       # a lead whose Zillow call keeps failing is closed after this many runs
NOTE_MAX = 3900                     # the add-notes route keeps 4,000 characters
MONTH_DAYS = 30.44
_DASHES = (chr(0x2014), chr(0x2013))
_TEN_DIGITS = re.compile(r"\d{10}")

# Tests swap these for fakes. Nothing in a selftest reaches Zillow, Anthropic or a photo host.
ZILLOW = None     # (address, key) -> {"code": ok | none | refused | error, "data": dict, "calls": int, "detail": str}
ASK = None        # (system, content blocks, model, max_tokens) -> (reply text, usage dict, stop reason)
DOWNLOAD = None   # (url, max_bytes) -> (bytes, media type) or None

DEFAULTS = {
    "statuses": list(lanes.LEAD_STATUS_VALUES),   # a record entering any of these is a new lead
    "recheck_statuses": [],      # entering one of these checks the house again even from another lead status
    "recheck_days": 90,          # a house checked this recently is not checked again
    "per_run": 10,               # checks a run (plan and live alike, so the plan says what live would do)
    "per_day": 25,               # paid checks a business day
    "max_reads": 40,             # record reads a run
    "first_days": 3,             # how far back the first run looks
    "months_listed": 6,          # "listed in the last six months" (L14.115)
    "max_photos": 4,
    "photo_width": 768,          # the listing photo size asked for: about 500 input tokens a photo
    "model": model_registry.model("reading_images"),
    "zillow_call_usd": None,     # None: AGENTS_ZILLOW_CALL_USD, the price quick_screen books, else $0.01
    "sync": True,
    "sync_calls": 60,            # log pages read to bring the status copy up to date first
}

SYSTEM = (
    "You rate a house's condition from its listing photos for a real estate investor who buys houses as "
    "they sit. Answer with JSON only and no other words: {\"condition\": \"excellent\" or \"needs a little "
    "updating\" or \"needs a lot of work\" or \"cannot tell\", \"saw\": \"one plain sentence of at most 25 "
    "words naming what the photos show of the kitchen, the baths, the floors, the roof and the outside\"}. "
    "Say cannot tell when the photos show no room and no outside of the house (a map, an aerial view, a "
    "bare lot, a street). Judge only what the photos show and never guess at what they do not.")
USER = "These are %d photos from this house's Zillow listing. They may be several years old. Rate the condition."


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with the stage's block, for the playbook checker: a typo is refused when the
    playbook loads instead of being read as a default."""
    out = []
    for k in ("statuses", "recheck_statuses"):
        v = st.get(k)
        if v is not None and (not isinstance(v, list) or [x for x in v if not str(x or "").strip()]):
            out.append("stage %s: %s lists statuses exactly as the account stores them" % (st.id, k))
    for k, lo, hi in (("recheck_days", 0, 3650), ("per_run", 0, 500), ("per_day", 0, 2000), ("max_reads", 1, 2000),
                      ("first_days", 1, 60), ("months_listed", 1, 60), ("max_photos", 0, 10), ("photo_width", 192, 2048),
                      ("sync_calls", 1, 2000)):
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            out.append("stage %s: %s must be a whole number from %d to %d" % (st.id, k, lo, hi))
    if st.get("model") is not None and not str(st.get("model") or "").strip():
        out.append("stage %s: model may not be empty" % st.id)
    v = st.get("zillow_call_usd")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
        out.append("stage %s: zillow_call_usd is what one Zillow details call costs, in dollars" % st.id)
    return out


# ---------------------------------------------------------------------- money
def zillow_price(st: dict) -> float:
    """What one details call costs: the stage's own figure, else the one quick_screen books."""
    v = st.get("zillow_call_usd")
    if v is None:
        try:
            v = float(os.environ.get("AGENTS_ZILLOW_CALL_USD") or 0.01)
        except ValueError:
            v = 0.01
    return max(0.0, float(v))


def model_price(model: str) -> tuple[float, float]:
    """Input and output dollars per million tokens, from the one table the brain books with."""
    from .. import voice
    return voice.PRICES.get(model, voice.DEFAULT_PRICE)


def vision_estimate(model: str, photos: int) -> float:
    if photos <= 0:
        return 0.0
    pin, pout = model_price(model)
    return round(((photos * PHOTO_TOKENS + TEXT_TOKENS) * pin + OUT_TOKENS * pout) / 1_000_000, 6)


def usage_usd(usage: dict, model: str) -> float:
    pin, pout = model_price(model)
    tin = sum(int((usage or {}).get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                        "cache_read_input_tokens"))
    return round((tin * pin + int((usage or {}).get("output_tokens") or 0) * pout) / 1_000_000, 6)


# ---------------------------------------------------------------------- small helpers
def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _money(v) -> int | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f)) if f > 0 else None


def _usd(v: int | None) -> str:
    return "$%s" % format(int(v), ",") if v else ""


def _nice(d: date | None) -> str:
    return d.strftime("%b %d, %Y").replace(" 0", " ") if d else ""


def _month(d: date | None) -> str:
    return d.strftime("%b %Y") if d else ""


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _when(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def clean(text: str) -> str:
    """Model words as a note may carry them: one line, no em or en dash (house rule)."""
    t = re.sub(r"\s*%s\s*" % _DASHES[0], ", ", str(text or ""))
    t = re.sub(r"\s*%s\s*" % _DASHES[1], "-", t)
    return " ".join(t.split()).strip(" ,")


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


def _mark(subject: str, live: bool) -> dict:
    v = ledger.fact(subject, MARK if live else PLAN_MARK)
    return v if isinstance(v, dict) else {}


def _set_mark(subject: str, live: bool, status: str, now: datetime, **detail) -> None:
    ledger.set_fact(subject, MARK if live else PLAN_MARK, dict(detail, status=status, at=now.isoformat(timespec="seconds")))


# ---------------------------------------------------------------------- the Zillow call
def fetch_details(where: str, key: str, tries: int = 3) -> dict:
    """One house's Zillow details. Every answer says what happened, because a refused key read as
    "Zillow has no record" would look exactly like a house with no history (property_enricher logs
    the error and returns nothing). `calls` is how many requests were made: each one is billed."""
    if ZILLOW is not None:
        return ZILLOW(where, key)
    import requests
    out = {"code": "error", "data": {}, "calls": 0, "detail": ""}
    for attempt in range(tries):
        out["calls"] += 1
        try:
            r = requests.get(DETAILS_URL, params={"address": where}, headers={"x-api-key": key}, timeout=30)
        except requests.RequestException as exc:
            out["detail"] = str(exc)[:120]
            continue
        if r.status_code == 404:
            return dict(out, code="none")
        if r.status_code in (401, 403):
            return dict(out, code="refused", detail="HTTP %d" % r.status_code)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
            out["detail"] = "HTTP %d" % r.status_code
            time.sleep(3 + 4 * attempt)
            continue
        if r.status_code != 200:
            return dict(out, detail="HTTP %d" % r.status_code)
        try:
            body = r.json() or {}
        except ValueError:
            return dict(out, detail="the answer was not JSON")
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(body, dict) and str(body.get("status") or "").upper() == "OK" and isinstance(data, dict) and data:
            return dict(out, code="ok", data=data)
        return dict(out, code="none")
    return out


# ---------------------------------------------------------------------- the listing history
def _day_of(entry: dict) -> date | None:
    v = entry.get("date")
    if v:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            pass
    t = entry.get("time")
    if isinstance(t, (int, float)) and t > 0:
        try:
            return datetime.fromtimestamp(t / 1000 if t > 10 ** 11 else t, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def event_kind(entry: dict) -> str:
    """listed, price, pending, sold, removed, rent_listed, rent_removed or other, from Zillow's
    event words. Zillow writes "Listing removed" whether the listing expired or was pulled, so the
    two cannot be told apart and the note says "expired or pulled"."""
    e = str(entry.get("event") or "").strip().lower()
    gone = any(w in e for w in ("removed", "expired", "withdrawn", "cancel", "delist", "off market"))
    if entry.get("postingIsRental") or "rent" in e:
        return "rent_removed" if gone else "rent_listed"
    if "sold" in e:
        return "sold"
    if any(w in e for w in ("pending", "contingent", "under contract")):
        return "pending"
    if gone:
        return "removed"
    if "price" in e:
        return "price"
    if any(w in e for w in ("listed", "relisted", "back on market", "coming soon")):
        return "listed"
    return "other"


def listing_facts(data: dict, today: date, months: int) -> dict:
    """What the details answer says about the house's listings, in plain fields (dates as ISO text,
    so the result can be kept as a fact)."""
    rows = []
    for e in data.get("priceHistory") or []:
        if isinstance(e, dict):
            d = _day_of(e)
            if d is not None:
                rows.append((d, event_kind(e), _money(e.get("price"))))
    rows.sort(key=lambda r: r[0])
    sale = rent = None
    sales, rents = [], []
    sold_on, sold_price = None, None
    for d, kind, price in rows:
        if kind == "listed":
            if sale is None:
                sale = {"start": d, "price": price, "last": price, "end": None, "how": "", "pending": None}
                sales.append(sale)
            elif price:
                sale["last"] = price
        elif kind == "price":
            if sale is not None and price:
                sale["last"] = price
        elif kind == "pending":
            if sale is not None:
                sale["pending"] = d
        elif kind == "sold":
            if sale is not None:
                sale.update(end=d, how="sold")
                sale = None
            sold_on, sold_price = d, price or sold_price
        elif kind == "removed":
            # One "Listing removed" ends a sale or a rental listing alike: it ends the one opened last.
            if sale is not None and (rent is None or sale["start"] >= rent["start"]):
                sale.update(end=d, how="removed")
                sale = None
            elif rent is not None:
                rent["end"] = d
                rent = None
        elif kind == "rent_listed":
            if rent is None:
                rent = {"start": d, "price": price, "end": None}
                rents.append(rent)
        elif kind == "rent_removed":
            if rent is not None:
                rent["end"] = d
                rent = None
    if sold_on is None:
        ms = data.get("dateSold")
        if isinstance(ms, (int, float)) and ms > 0:
            sold_on = _day_of({"time": ms})
        sold_price = sold_price or _money(data.get("lastSoldPrice"))
    raw = str(data.get("keystoneHomeStatus") or data.get("homeStatus") or "").replace("_", "").lower()
    sub = data.get("listingSubType") or data.get("listing_sub_type") or {}
    sub = sub if isinstance(sub, dict) else {}
    auction = bool(sub.get("isForAuction") or sub.get("isForeclosure") or sub.get("is_forAuction") or sub.get("is_foreclosure"))
    status = {"forsale": "for sale", "comingsoon": "coming soon", "pending": "pending", "undercontract": "pending",
              "forrent": "for rent", "recentlysold": "sold", "sold": "sold"}.get(raw, "off market" if raw else "")
    if auction and status in ("for sale", "coming soon", "pending"):
        status = "auction"
    on_market = status in ("for sale", "coming soon", "pending")
    by_owner = bool(sub.get("isFSBO") or sub.get("is_FSBO"))
    last = sales[-1] if sales else None
    window = timedelta(days=round(months * MONTH_DAYS))
    recent = on_market or bool(last and today - (last["end"] or last["start"]) <= window)
    days = None
    if last and last["end"]:
        days = (last["end"] - last["start"]).days
    elif last and on_market:
        days = (today - last["start"]).days
    a = data.get("attributionInfo") or {}
    a = a if isinstance(a, dict) else {}
    rent_last = rents[-1] if rents else None
    dz = data.get("daysOnZillow")
    return {
        "status": status, "on_market": on_market, "by_owner": by_owner,
        "price_now": _money(data.get("price")) if on_market else None,
        "days_on_zillow": int(dz) if isinstance(dz, (int, float)) and on_market else None,
        "listed": last["start"].isoformat() if last else None, "list_price": last["price"] if last else None,
        "last_price": last["last"] if last else None,
        "ended": last["end"].isoformat() if last and last["end"] else None,
        "how": (last["how"] or ("" if on_market else "off market")) if last else "",
        "pending": last["pending"].isoformat() if last and last["pending"] else None,
        "days": days, "recent": recent,
        "agent": clean(a.get("agentName") or "") if recent else "",
        "broker": clean(a.get("brokerName") or "") if recent else "",
        "sold": sold_on.isoformat() if sold_on else None, "sold_price": sold_price,
        "rent_listed": rent_last["start"].isoformat() if rent_last else None,
        "rent_price": rent_last["price"] if rent_last else None,
        "rent_recent": bool(rent_last and today - (rent_last["end"] or rent_last["start"]) <= window),
    }


# ---------------------------------------------------------------------- the photos
PHOTO_LISTS = ("originalPhotos", "responsivePhotos", "photos", "hugePhotos", "big_photos", "images")
PHOTO_SINGLES = ("hiResImageLink", "desktopWebHdpImageLink", "imgSrc")


def _source(p, width: int) -> str:
    """One photo's address at about `width` pixels, from whichever of Zillow's shapes it comes in."""
    if isinstance(p, str):
        return p
    if not isinstance(p, dict):
        return ""
    ms = p.get("mixedSources") or {}
    for fmt in ("jpeg", "webp"):
        opts = [o for o in (ms.get(fmt) or []) if isinstance(o, dict) and isinstance(o.get("url"), str)]
        if opts:
            return min(opts, key=lambda o: abs(int(o.get("width") or 0) - width))["url"]
    for k in ("url", "src", "href", "imageUrl"):
        if isinstance(p.get(k), str):
            return p[k]
    return ""


def spread(urls: list[str], limit: int) -> list[str]:
    """The first photo (most often the front of the house) and the rest chosen evenly across the set,
    so a few photos see more than the first few rooms."""
    if limit <= 0:
        return []
    if len(urls) <= limit:
        return list(urls)
    if limit == 1:
        return [urls[0]]
    step = (len(urls) - 1) / (limit - 1)
    return [urls[i] for i in sorted({round(i * step) for i in range(limit)})]


def photo_urls(data: dict, limit: int, width: int = 768) -> tuple[list[str], int, str]:
    """(the photos to read, how many the answer holds, the field they came from). Whether the details
    call returns photos at all is not proven, so every shape seen in Zillow's own pages is read and
    the field is recorded: the first live runs say which one it is."""
    for key in PHOTO_LISTS:
        items = data.get(key)
        if isinstance(items, list) and items:
            urls = []
            for p in items:
                u = _source(p, width)
                if u.startswith(("http://", "https://")) and u not in urls:
                    urls.append(u)
            if urls:
                return spread(urls, limit), len(urls), key
    for key in PHOTO_SINGLES:
        u = data.get(key)
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return ([u] if limit > 0 else []), 1, key
    return [], 0, ""


class PhotoFetchError(Exception):
    """The model could not open a photo by its address. The photos are then downloaded and sent as data."""


def _about_photos(exc) -> bool:
    words = str(getattr(exc, "message", "") or exc).lower()
    return any(w in words for w in ("image", "url", "download", "fetch", "media"))


def _ask(system: str, content: list, model: str, max_tokens: int) -> tuple[str, dict, str]:
    """One read, the way voice._ask_claude makes it: thinking off (Haiku 5.5 takes that at effort high
    or below) and effort low, because naming what a photo shows needs no reasoning. A model that
    refuses "disabled" is asked once more without it; a photo the API could not open is raised."""
    if ASK is not None:
        return ASK(system, content, model, max_tokens)
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    base = dict(model=model, system=system, output_config={"effort": "low"},
                messages=[{"role": "user", "content": content}])
    try:
        msg = client.messages.create(max_tokens=max_tokens, thinking={"type": "disabled"}, **base)
    except anthropic.BadRequestError as exc:
        if _about_photos(exc):
            raise PhotoFetchError(str(exc)[:200]) from exc
        msg = client.messages.create(max_tokens=max_tokens + 4000, **base)
    u = getattr(msg, "usage", None)
    usage = {k: int(getattr(u, k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                      "cache_creation_input_tokens")}
    text = "".join(getattr(b, "text", "") or "" for b in (msg.content or []) if getattr(b, "type", "") == "text")
    return text, usage, str(getattr(msg, "stop_reason", "") or "")


def _download(url: str, max_bytes: int = 3_500_000):
    if DOWNLOAD is not None:
        return DOWNLOAD(url, max_bytes)
    import requests
    try:
        r = requests.get(url, timeout=20)
    except requests.RequestException:
        return None
    kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if r.status_code != 200 or kind not in ("image/jpeg", "image/png", "image/webp", "image/gif") or len(r.content) > max_bytes:
        return None
    return r.content, kind


def parse_reply(text: str) -> tuple[str, str]:
    """(condition, what was seen) from the model's JSON. A condition outside the three phrases (or
    cannot tell) is dropped, never passed on: the note says only what the scale allows."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return "", ""
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    cond = " ".join(str(d.get("condition") or "").lower().split())
    if cond not in CONDITIONS and cond != UNSURE:
        cond = ""
    return cond, clean(str(d.get("saw") or ""))[:220]


def read_condition(urls: list[str], model: str, max_tokens: int = 300) -> dict:
    """{"condition", "saw", "usage", "why"}: one read of the photos. A photo the API cannot fetch by
    its address is downloaded here and sent as data, once."""
    blocks = [{"type": "image", "source": {"type": "url", "url": u}} for u in urls]
    blocks.append({"type": "text", "text": USER % len(urls)})
    usage: dict = {}
    try:
        text, usage, stop = _ask(SYSTEM, blocks, model, max_tokens)
    except PhotoFetchError:
        data_blocks = []
        for u in urls:
            got = _download(u)
            if got:
                data_blocks.append({"type": "image", "source": {"type": "base64", "media_type": got[1],
                                                                "data": base64.standard_b64encode(got[0]).decode("ascii")}})
        if not data_blocks:
            return {"condition": "", "saw": "", "usage": {}, "why": "the photos could not be opened"}
        data_blocks.append({"type": "text", "text": USER % len(data_blocks)})
        text, usage, stop = _ask(SYSTEM, data_blocks, model, max_tokens)
    if stop in ("refusal", "max_tokens"):
        return {"condition": "", "saw": "", "usage": usage, "why": "the photo read came back cut off or refused"}
    cond, saw = parse_reply(text)
    if not cond:
        return {"condition": "", "saw": "", "usage": usage, "why": "the photo read did not give one of the three phrases"}
    return {"condition": cond, "saw": saw, "usage": usage, "why": ""}


# ---------------------------------------------------------------------- the note
def listing_sentence(f: dict, months: int) -> str:
    listed, ended = _d(f.get("listed")), _d(f.get("ended"))
    who = ""
    if f.get("agent") or f.get("broker"):
        who = " with %s" % " of ".join(x for x in (f.get("agent"), f.get("broker")) if x)
    if f.get("status") == "auction":
        return "Zillow shows it listed for auction right now."
    if f.get("on_market"):
        if f.get("status") == "pending":
            if not listed:
                return "pending sale right now%s." % who
            price = " at %s" % _usd(f["list_price"]) if f.get("list_price") else ""
            return "pending sale right now (listed %s%s%s)." % (_nice(listed), price, who)
        if f.get("by_owner"):
            return "for sale by the owner right now%s, not with an agent." % (" at %s" % _usd(f.get("price_now"))
                                                                            if f.get("price_now") else "")
        bits = "for sale right now"
        if f.get("price_now"):
            bits += " at %s" % _usd(f["price_now"])
        bits += who
        if f.get("days_on_zillow") is not None:
            bits += ", %s on Zillow" % _plural(int(f["days_on_zillow"]), "day")
        return bits + "."
    if not listed:
        return "no sale listing on Zillow."
    price = _usd(f.get("list_price"))
    cut = f.get("last_price") and f.get("list_price") and f["last_price"] < f["list_price"]
    first = "listed for sale %s%s%s%s" % (_nice(listed), " at %s" % price if price else "",
                                          ", cut to %s" % _usd(f["last_price"]) if cut else "", who)
    if not f.get("recent"):
        return "not listed in the last %d months (last %s)." % (months, first)
    days = (" after %s" % _plural(int(f["days"]), "day")) if f.get("days") is not None else ""
    if f.get("how") == "sold":
        return "%s, sold %s%s." % (first, _nice(ended), days)
    if f.get("how") == "removed":
        return "%s, taken off the market %s%s (expired or pulled)." % (first, _nice(ended), days)
    return "%s, off the market now with no end date on Zillow." % first


def listing_advice(f: dict) -> str:
    """The one thing the lead manager should do about the listing, or ""."""
    if f.get("status") == "pending" and f.get("on_market"):
        return "Someone is under contract on it: ask the seller what happened with that buyer."
    if f.get("on_market") and not f.get("by_owner"):
        return "A house listed with an agent is not a target until the listing ends."
    if f.get("recent") and f.get("how") in ("removed", "off market"):
        return ("An agent may still hold a listing agreement and be owed: ask who they listed with and whether it "
                "has ended.")
    return ""


def short_listing(res: dict) -> str:
    """The listing in a few words, for the line a person reads in Slack."""
    if res.get("code") == "none":
        return "no Zillow record"
    f = res.get("listing") or {}
    if f.get("status") == "auction":
        return "listed for auction now"
    if f.get("on_market"):
        if f.get("status") == "pending":
            return "pending sale now"
        return "for sale by the owner now" if f.get("by_owner") else "for sale now with an agent"
    listed = _d(f.get("listed"))
    if not listed:
        return "never listed for sale"
    if not f.get("recent"):
        return "not listed in %d months" % int(res.get("months") or 6)
    days = (" after %s" % _plural(int(f["days"]), "day")) if f.get("days") is not None else ""
    if f.get("how") == "sold":
        return "listed %s, sold%s" % (_month(listed), days)
    return "listed %s, taken off%s" % (_month(listed), days)


def note_text(res: dict, today: date) -> str:
    months = int(res.get("months") or 6)
    out = ["Property check (Zillow, read %s)" % _nice(today)]
    if res.get("code") == "none":
        out.append("Zillow has no record of this address, so there is no listing history or photo to check.")
        out.append("Ask the seller to text a few photos, or walk the house.")
        return "\n".join(out)[:NOTE_MAX]
    f = res.get("listing") or {}
    out.append("Listing: " + listing_sentence(f, months))
    advice = listing_advice(f)
    if advice:
        out.append(advice)
    if f.get("rent_recent"):
        rent = _usd(f.get("rent_price"))
        out.append("Rental: listed for rent %s%s, so a tenant may live there." % (
            _nice(_d(f.get("rent_listed"))), " at %s a month" % rent if rent else ""))
    sold = _d(f.get("sold"))
    out.append("Last sold: %s." % ("%s%s" % (_nice(sold), " for %s" % _usd(f["sold_price"]) if f.get("sold_price") else "")
                                   if sold else "no sale on Zillow"))
    total, read = int(res.get("photos") or 0), int(res.get("read") or 0)
    if not total:
        out.append("Photos: Zillow has none for this house. Ask the seller to text a few, or walk it.")
        return "\n".join(out)[:NOTE_MAX]
    if res.get("condition"):
        line = "Condition from %s: %s." % (_plural(read, "listing photo"), res["condition"])
        if res.get("saw"):
            line += " Seen: %s" % res["saw"].rstrip(".") + "."
        out.append(line)
        when = _month(_d(f.get("listed")))
        out.append("The photos are Zillow's%s, so they may be out of date. Ask the seller whether they still show "
                   "the house as it is today." % (" from the %s listing" % when if when else ""))
    else:
        out.append("Photos: Zillow has %d, not read this time (%s). Ask the seller how the house looks today." % (
            total, res.get("photo_why") or "the read did not finish"))
    return "\n".join(out)[:NOTE_MAX]


def _alert(street: str, res: dict) -> str:
    """News the lead manager acts on now: the new lead's house is for sale or pending with someone
    else. "" when it is not. A street that looks like a phone number is left out of the post."""
    f = res.get("listing") or {}
    if not f.get("on_market") or f.get("by_owner"):
        return ""
    where = street if not _TEN_DIGITS.search(street.replace("-", "")) else "A new lead's house"
    if f.get("status") == "pending":
        return "%s, a new lead, shows a pending sale on Zillow right now: ask the seller about that buyer before an offer." % where
    price = " at %s" % _usd(f.get("price_now")) if f.get("price_now") else ""
    return "%s, a new lead, is for sale on Zillow right now%s with an agent. It is not a target until the listing ends." % (
        where, price)


# ---------------------------------------------------------------------- one paid check
def check_house(ctx: Ctx, rec: dict, st: dict, key: str, subject: str) -> dict | None:
    """The paid part, live only: the Zillow call and, when it has photos and a key, the photo read.
    Returns the result to note, or None when nothing could be learned this run (the problem is said
    and the lead waits for the next run). "stop" in the result means no later lead can be checked
    this run either (the key was refused)."""
    a = rec.get("address") or {}
    where = " ".join(str(x) for x in (a.get("street"), a.get("city"), a.get("state"), str(a.get("postal_code") or "")[:5]) if x)
    price = zillow_price(st)
    got = fetch_details(where, key) if a.get("street") else {"code": "none", "data": {}, "calls": 0}
    cost = round(price * int(got.get("calls") or 0), 6)
    res = {"entry": subject, "at": ctx.now.isoformat(timespec="seconds"), "code": got.get("code") or "error",
           "months": int(st["months_listed"]), "photos": 0, "read": 0, "field": "", "condition": "", "saw": "",
           "photo_why": "", "usd": cost}
    if res["code"] == "refused":
        res["stop"] = True
        return res
    if res["code"] == "error":
        res["detail"] = str(got.get("detail") or "no answer")[:80]
        return res
    if res["code"] == "ok":
        data = got.get("data") or {}
        res["listing"] = listing_facts(data, ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date(), int(st["months_listed"]))
        urls, total, field = photo_urls(data, int(st["max_photos"]), int(st["photo_width"]))
        res.update(photos=total, field=field)
        if urls and not (ASK is not None or str(config.ANTHROPIC_API_KEY or "").strip()):
            res["photo_why"] = "no Anthropic key on this box"
        elif urls:
            try:
                r = read_condition(urls, str(st["model"]))
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001  (the listing is still worth its note)
                r = {"condition": "", "saw": "", "usage": {}, "why": "the photo read failed (%s)" % str(exc)[:60]}
            res.update(condition=r["condition"], saw=r["saw"], photo_why=r["why"], read=len(urls))
            res["usd"] = round(cost + usage_usd(r.get("usage") or {}, str(st["model"])), 6)
    return res


def _checks_today(ctx: Ctx) -> int:
    zone = ZoneInfo(ctx.policy.tz)
    local = ctx.now.astimezone(zone)
    midnight = datetime(local.year, local.month, local.day, tzinfo=zone)
    return len(ledger.events(action=ACTION, stage=ctx.stage.id, since=midnight.astimezone(timezone.utc)))


# ---------------------------------------------------------------------- the stage
def run_leadprep(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    out = ctx.out
    live = ctx.live
    want = {str(s).strip().lower() for s in st["statuses"] if str(s).strip()}
    again = {str(s).strip().lower() for s in st["recheck_statuses"] if str(s).strip()}
    key = (os.environ.get("OPENWEBNINJA_API_KEY") or "").strip()
    if not key and ZILLOW is None:
        out.problem("the Zillow key (OPENWEBNINJA_API_KEY) is not on this box, so new leads get no property check")
        if live:
            # Said before any read: reading leads that cannot be checked spends requests for nothing.
            out.head = "No property check ran: the Zillow key is missing."
            return
    if not (ASK is not None or str(config.ANTHROPIC_API_KEY or "").strip()):
        out.problem("the Anthropic key is not on this box, so the listing photos of new leads are not read")
    if st["sync"]:
        try:
            activity.sync(ctx.crm, "state", now=ctx.now, max_calls=int(st["sync_calls"]))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out.problem("could not read the change log (%s), so new leads may be checked late" % str(exc)[:100])
    kv = "autopilot:leadprep:%s:%s" % (ctx.stage.id, "live" if live else "plan")
    start = _when(store.kv_get(kv)) or (ctx.now - timedelta(days=float(st["first_days"])))
    newest: dict[str, dict] = {}
    rechecks: set[str] = set()      # events into a recheck status: checked again whatever the recheck window
    for e in activity.events("property.status.updated", since=start - activity.OVERLAP, until=ctx.now):
        d = e.get("data") or {}
        to, frm = str(d.get("to") or "").strip().lower(), str(d.get("from") or "").strip().lower()
        if to in again:
            rechecks.add(str(e.get("id")))
        if to in again or (to in want and frm not in lanes.LEAD_STATUSES):
            u = str(e.get("resource") or "")
            if u:
                older = newest.get(u)
                if older is not None and _mark("leadprep:%s" % older["id"], live).get("status") not in DONE:
                    _set_mark("leadprep:%s" % older["id"], live, "skipped", ctx.now, why="a later entry of the same record")
                newest[u] = e
    est_one = zillow_price(st) + vision_estimate(str(st["model"]), int(st["max_photos"]))
    checks = reads = 0
    today_paid = _checks_today(ctx) if live else 0
    open_at: list[datetime] = []
    waiting: Counter = Counter()
    would, did, alerts = [], [], 0
    spent = 0.0
    stopped = False
    for u, e in sorted(newest.items(), key=lambda kv_: kv_[1]["when"]):
        subject = "leadprep:%s" % e["id"]
        m = _mark(subject, live)
        if m.get("status") in DONE:
            continue
        if stopped:
            open_at.append(e["when"])
            continue
        if checks >= int(st["per_run"]):
            waiting["the run's cap of %d checks" % int(st["per_run"])] += 1
            open_at.append(e["when"])
            continue
        if live and today_paid + checks >= int(st["per_day"]):
            waiting["today's cap of %d checks" % int(st["per_day"])] += 1
            open_at.append(e["when"])
            continue
        last = ledger.fact(u, CHECKED)
        last_at = _when(last.get("at")) if isinstance(last, dict) else None
        if last_at and ctx.now - last_at < timedelta(days=float(st["recheck_days"])) and last.get("entry") != subject \
                and str(e.get("id")) not in rechecks:
            out.tally["checked within the last %d days" % int(st["recheck_days"])] += 1
            _set_mark(subject, live, "skipped", ctx.now, why="checked %s" % last_at.date().isoformat())
            continue
        if reads >= int(st["max_reads"]):
            waiting["the run's read cap"] += 1
            open_at.append(e["when"])
            continue
        reads += 1
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out.problem("could not read a new lead's record (%s)" % str(exc)[:80])
            open_at.append(e["when"])
            continue
        if not rec or not rec.get("uuid"):
            _set_mark(subject, live, "skipped", ctx.now, why="the record is gone")
            continue
        if lanes.status_of(rec) not in want | again:
            out.tally["no longer a lead"] += 1
            _set_mark(subject, live, "skipped", ctx.now, why="no longer a lead")
            continue
        if _is_entity(rec.get("owner") or {}):
            out.tally["a company or trust owns it (people only)"] += 1
            _set_mark(subject, live, "skipped", ctx.now, why="a company or trust owns it")
            continue
        if not ctx.passes(rec, "research", allow=("lead",)):
            _set_mark(subject, live, "skipped", ctx.now, why="held back by the gate")
            continue
        if not str((rec.get("address") or {}).get("street") or "").strip():
            out.tally["no street on the record"] += 1
            _set_mark(subject, live, "skipped", ctx.now, why="no street on the record")
            continue
        street = addr(rec)
        kept = ledger.fact(u, RESULT) if live else None
        res = kept if isinstance(kept, dict) and kept.get("entry") == subject else None
        if res is None:
            # The record is not named: its lifetime cap is for traces and scoring. The day caps apply.
            ok, why = ctx.budget(est_one)
            if not ok:
                waiting[why] += 1
                open_at.append(e["when"])
                stopped = True
                continue
            ctx.reserve(est_one)
            if not live:
                checks += 1
                would.append(street)
                _set_mark(subject, live, "checked", ctx.now, record=u)
                continue
            res = check_house(ctx, rec, st, key, subject)
            ctx.settle(est_one, float(res.get("usd") or 0), "property check")
            spent += float(res.get("usd") or 0)
            checks += 1
            ledger.event(ctx.run_id, ctx.stage.id, u, ACTION,
                         {"code": res.get("code"), "photos": res.get("photos"), "read": res.get("read"),
                          "field": res.get("field"), "condition": res.get("condition")},
                         usd=float(res.get("usd") or 0), now=ctx.now, tz=ctx.policy.tz)
            if res.get("stop"):
                out.problem("Zillow refused the key (OPENWEBNINJA_API_KEY), so new leads get no property check")
                open_at.append(e["when"])
                stopped = True
                continue
            if res.get("code") == "error":
                tries = int(m.get("tries") or 0) + 1
                if tries >= MAX_TRIES:
                    out.tally["closed: the Zillow call failed %d times" % MAX_TRIES] += 1
                    _set_mark(subject, live, "skipped", ctx.now, why="the Zillow call failed %d times" % tries)
                else:
                    out.problem("the Zillow details call failed (%s); the lead is tried again on the next run" % res.get("detail"))
                    _set_mark(subject, live, "open", ctx.now, tries=tries)
                    open_at.append(e["when"])
                continue
            ledger.set_fact(u, RESULT, res)         # paid for: kept so a failed note is not paid for twice
        note = note_text(res, ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date())
        try:
            ctx.write("property check note", ctx.crm.message, u, note)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (the paid result is kept: the next run writes the note for free)
            out.problem("could not write a property check note on %s (%s); it is written on the next run" % (street, str(exc)[:80]))
            open_at.append(e["when"])
            continue
        ledger.set_fact(u, CHECKED, {"at": ctx.now.isoformat(timespec="seconds"), "entry": subject,
                                     "listed_recently": bool((res.get("listing") or {}).get("recent")),
                                     "on_market": bool((res.get("listing") or {}).get("on_market")),
                                     "condition": res.get("condition") or ""})
        ledger.set_fact(u, RESULT, {})
        _set_mark(subject, live, "checked", ctx.now, record=u)
        did.append((street, res))
        text = _alert(street, res)
        if text:
            out.alert(text)
            alerts += 1
    store.kv_set(kv, (min(open_at) if open_at else ctx.now).isoformat(timespec="seconds"))
    _report(ctx, st, would, did, waiting, spent)


def _report(ctx: Ctx, st: dict, would: list, did: list, waiting: Counter, spent: float) -> None:
    out = ctx.out
    months = int(st["months_listed"])
    for why, n in waiting.most_common(3):
        out.tally["waiting: %s" % why] += n
    if not ctx.live:
        out.found = len(would)
        out.acted = len(would)
        if would:
            out.head = ("Would check %s on Zillow (about $%.2f): the listing history, the agent, and the condition from "
                        "the listing photos, written in one note on each." % (_plural(len(would), "new lead"),
                                                                             max(out.would_usd, 0.01)))
            for street in would[:10]:
                out.line(street)
        if waiting:
            out.line("Waiting for a later run: %s." % ", ".join("%d (%s)" % (n, w) for w, n in waiting.most_common(3)))
        return
    out.found = len(did)
    out.acted = len(did)
    if not did:
        if waiting:
            out.head = "No property check this run: %s." % ", ".join("%d waiting (%s)" % (n, w) for w, n in waiting.most_common(2))
        return
    recent = sum(1 for _, r in did if (r.get("listing") or {}).get("recent"))
    market = sum(1 for _, r in did if (r.get("listing") or {}).get("on_market"))
    photos = sum(1 for _, r in did if r.get("condition"))
    out.head = "Checked %s on Zillow: %d listed in the last %d months, %d on the market now, %d with the photos read ($%.2f)." % (
        _plural(len(did), "new lead"), recent, months, market, photos, spent)
    for street, r in did[:10]:
        cond = r.get("condition") or ("no photos" if not r.get("photos") else "photos not read")
        out.line("%s: %s; %s" % (street, short_listing(r), cond))
    fields = sorted({r.get("field") for _, r in did if r.get("field")})
    if fields:
        out.tally["photos came from the field %s" % ", ".join(fields)] += 1
    if waiting:
        out.line("Waiting for a later run: %s." % ", ".join("%d (%s)" % (n, w) for w, n in waiting.most_common(3)))
