"""Low-cost research engine for the deep prospect: two inexpensive models and one cross-check.

Measured 2026-11-12 on 11 houses with a hand-verified answer (6 of them hard cases): four low-cost
models confirmed 31 to 33 of 36 facts for 1 to 3 cents a record, against about $3.30 on the Claude
researcher. The one dangerous failure was a death that no web search finds. The county library's
obituary index plus the rule "never name a signer whose status is unverified" removed it. So:

    researcher A --+   same brief, same tools, working independently
    researcher B --+--> cross-check -- agree ----------------> shape -> source guard -> research JSON
                              +------- disagree --> referee --> shape -> ...

The tools are ours, not a model vendor's: Google search (Serper when it has credits, else the SerpApi
plan), a page opener (a plain request first because it is free and serves about 4 pages in 10, then
Firecrawl, then Scrapfly for protected pages) and the Knox County Public Library obituary index. One
web cache per record is shared by every model that works it and kept in the run folder, so a re-run
does not buy the same search twice.

Nothing here changes the default. The Claude researcher (researcher.py) stays the engine until
AGENTS_RESEARCH_ENGINE=open is set. Settings, all from the environment:

    AGENTS_RESEARCH_ENGINE    claude (default) | open
    AGENTS_RESEARCH_MODEL     first researcher           (deepseek/deepseek-v4.1-flash-1017)
    AGENTS_RESEARCH_SECOND    second opinion, "off" for none            (meta/muse-ember)
    AGENTS_RESEARCH_REFEREE   settles a disagreement, "off" for none    (anthropic/claude-sonnet-5.5)
    AGENTS_ANALYSIS_MODEL     analysis passes, "off" keeps Claude                   (off)
    AGENTS_RESEARCH_SEARCHES  searches each researcher may run                       (14)
    AGENTS_RESEARCH_PAGES     pages each researcher may open                         (12)

The output contract is researcher.research()'s: research/<slug>.json in RESEARCH_SCHEMA shape and
research/<slug>_urls.json. A sidecar, research/<slug>_open.json, records which models and hosts
ran, what each concluded, where they disagreed, and what every part cost.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import requests

from . import researcher

log = logging.getLogger(__name__)

DEFAULT_PRIMARY = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_SECOND = "meta/muse-ember"
DEFAULT_REFEREE = "anthropic/claude-sonnet-5.5"
MAX_STEPS = 40
PAGE_CHARS = 10000
PROTECTED_MAX = 8            # Scrapfly pages per record, across every model
REFEREE_SEARCHES, REFEREE_PAGES = 6, 8        # the referee is the costly model: it settles, it does not start over
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
INDEX_URL = "http://engagedpatrons.org/database/KnoxObits/Brief.cfm"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
# What one call costs us. Serper sells prepaid packs at about $1 per 1,000; the SerpApi plan works
# out to a penny a search; Firecrawl's 100,000-page plan to a tenth of a cent; a Scrapfly protected
# page uses about 30 of the plan's 200,000 credits.
SEARCH_USD = {"serper": 0.001, "serpapi": 0.01}
PAGE_USD = {"direct": 0.0, "firecrawl": 0.001, "scrapfly": 0.005}
CHALLENGE = ("just a moment", "complete the challenge", "verify you are human", "access denied",
             "cloudflare", "captcha", "enable javascript", "bot challenge", "unusual traffic")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr", "estate", "of", "the", "and"}
ENTITY_RX = re.compile(r"\b(llc|inc|corp|company|trust|trustee|lp|llp|properties|holdings|church|"
                       r"bank|association|partners|investments|group|estate)\b", re.I)
# A signer line that names a role or a group, not a person ("Successor trustee of the Doe Trust",
# "Jane's heirs", "A spouse, if any", "the executor of the estate of Jane Doe").
STRUCTURAL = re.compile(r"\b(trust|trustee|estate|heirs?|beneficiar\w*|successor|if any|unknown|guardian|"
                        r"personal representative|executor of|administrator of)\b", re.I)
VAGUE = re.compile(r"\b(before|after|about|around|circa|approx\w*|unknown|sometime|prior to|by)\b", re.I)
RULED_OUT = re.compile(r"ruled out|not this owner|not the owner|different person|unrelated|stranger|wrong person", re.I)

_dead = {"serper": False, "firecrawl": False}      # a vendor that ran out stays off for the process
_patch_lock = threading.Lock()


# ---------------------------------------------------------------- settings --

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _model(name: str, default: str) -> str:
    v = _env(name, default)
    return "" if v.lower() in ("off", "none", "0", "no") else v


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(_env(name, str(default))))
    except ValueError:
        return default


def settings() -> dict:
    return {"engine": _env("AGENTS_RESEARCH_ENGINE", "claude").lower(),
            "primary": _model("AGENTS_RESEARCH_MODEL", DEFAULT_PRIMARY) or DEFAULT_PRIMARY,
            "second": _model("AGENTS_RESEARCH_SECOND", DEFAULT_SECOND),
            "referee": _model("AGENTS_RESEARCH_REFEREE", DEFAULT_REFEREE),
            "analysis": _model("AGENTS_ANALYSIS_MODEL", "off"),
            "searches": _int("AGENTS_RESEARCH_SEARCHES", 14),
            "pages": _int("AGENTS_RESEARCH_PAGES", 12)}


def engine() -> SimpleNamespace:
    """What the deep prospect calls. The Claude researcher unless the open engine is switched on."""
    s = settings()
    if s["engine"] != "open":
        return SimpleNamespace(name="claude", research=researcher.research, analyze=researcher.analyze,
                               cap=3.0, label="research (Claude + web search)")
    pair = s["primary"].split("/")[-1] + (" + " + s["second"].split("/")[-1] if s["second"] else "")
    return SimpleNamespace(name="open", research=research, analyze=analyze, cap=0.75,
                           label="research (%s, cross-checked)" % pair)


# -------------------------------------------------------------------- names --

def _toks(name: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
            if len(t) > 1 and t not in SUFFIXES]


GENERATION = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii", "iv": "iv"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"), 1)}


def _generation(name: str) -> str:
    for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split():
        if t in GENERATION:
            return GENERATION[t]
    return ""


def same_person(a: str, b: str, strict: bool = False) -> bool:
    """Two spellings of one person: two shared name words. A middle initial or "Last, First"
    order does not split a person in two, but Sr. and Jr. are a father and a son, never one
    person. `strict` is for accusations (this dead person is that signer): there a suffix on
    only one of the two names is also treated as two people."""
    ga, gb = _generation(a), _generation(b)
    if ga != gb and ((ga and gb) or strict):
        return False
    fa, la, sa = _parts(a)
    fb, lb, sb = _parts(b)
    if not fa or not fb:
        return False
    if len(sa) == 1 or len(sb) == 1:
        return sa == sb
    if fa == lb and la == fb:                      # county order, "Last First", against "First Last"
        return True
    # The FIRST name has to agree (or be the name the other goes by). A shared middle name and
    # surname is a parent and a child, which is exactly the pair this must never merge.
    # An accusation needs the same first name outright: a son often carries his father's first
    # name as his middle name.
    first_ok = fa == fb or (not strict and (fa in sb - {lb} or fb in sa - {la}))
    last_ok = la == lb or la in sb or lb in sa     # a married name still carries the maiden one
    return first_ok and last_ok


def _parts(name: str) -> tuple[str, str, set]:
    """(first, last, every name word), reading "Last, First Middle" as well as "First Middle Last"."""
    s = re.sub(r"\(.*?\)", " ", name or "")
    if "," in s:
        last, rest = s.split(",", 1)
        toks = _toks(rest) + _toks(last)
    else:
        toks = _toks(s)
    return (toks[0], toks[-1], set(toks)) if toks else ("", "", set())


def _year(s: str) -> str:
    m = re.search(r"(19|20)\d\d", s or "")
    return m.group(0) if m else ""


def date_key(s: str) -> tuple | None:
    """(year, month, day) from the ways a date of death gets written, or None."""
    s = (s or "").strip().lower()
    m = re.search(r"\b((?:19|20)\d\d)-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b", s)
    if m:
        return int(m.group(3)), int(m.group(1)), int(m.group(2))
    m = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b", s)
    if m and m.group(1) in MONTHS:
        return int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?,?\s+((?:19|20)\d\d)\b", s)
    if m and m.group(2) in MONTHS:
        return int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))
    return None


# ---------------------------------------------------------------------- web --

def blocked(text: str) -> bool:
    t = (text or "").lower()
    return len(t) < 200 or (len(t) < 1500 and any(k in t for k in CHALLENGE))


def _strip_html(html: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", raw)).replace("&nbsp;", " ").strip()


class Web:
    """Search, page opens and the obituary index for ONE record, shared by every model on it."""

    def __init__(self, cache_path: Path | None = None, env: dict | None = None):
        self.env = env if env is not None else os.environ
        self.path = cache_path
        self.cache = {"search": {}, "page": {}, "index": {}}
        if cache_path and cache_path.exists():
            try:
                self.cache.update(json.loads(cache_path.read_text(encoding="utf-8")))
            except ValueError:
                pass
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.stats: Counter = Counter()
        self.usd = 0.0
        self.protected = 0
        self.log: list[dict] = []

    def save(self) -> None:
        if self.path:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.cache), encoding="utf-8")

    def _note(self, **kw) -> None:
        with self.lock:
            self.log.append(kw)

    def _count(self, key: str, usd: float = 0.0) -> None:
        with self.lock:
            self.stats[key] += 1
            self.usd += usd

    # -- search ------------------------------------------------------------
    def search(self, q: str) -> str:
        q = (q or "").strip()
        if not q:
            return "empty query"
        key = re.sub(r"\s+", " ", q.lower())
        hit = self.cache["search"].get(key)
        if hit is None:
            hit = self._search_live(q)
            if hit.get("results") is not None:
                with self.lock:
                    self.cache["search"][key] = hit
        else:
            self._count("search_cached")
        if hit.get("error"):
            return hit["error"]
        out = list(hit.get("boxes") or [])
        for o in hit["results"]:
            self.seen.add(o["link"])
            out.append("- %s\n  %s\n  %s %s" % (o["title"], o["link"], o.get("date") or "", o.get("snippet") or ""))
        return "\n".join(out) or "no results"

    def _search_live(self, q: str) -> dict:
        if self.env.get("SERPER_API_KEY") and not _dead["serper"]:
            try:
                r = requests.post("https://google.serper.dev/search", timeout=40,
                                  headers={"X-API-KEY": self.env["SERPER_API_KEY"]},
                                  json={"q": q, "num": 8, "gl": "us", "hl": "en"})
                j = r.json()
                if r.status_code == 200 and "organic" in j:
                    self._count("search_serper", SEARCH_USD["serper"])
                    self._note(search=q, via="serper", results=len(j["organic"]))
                    return {"results": [{"title": o.get("title"), "link": o.get("link") or "", "date": o.get("date"),
                                         "snippet": o.get("snippet")} for o in j["organic"][:8]],
                            "boxes": ["%s: %s" % (k, json.dumps(j[k])[:500]) for k in ("answerBox", "knowledgeGraph")
                                      if j.get(k)]}
                if "credit" in json.dumps(j).lower() or r.status_code in (400, 401, 402, 403):
                    _dead["serper"] = True
                    log.warning("Serper refused the search (%s); using the SerpApi plan", str(j)[:80])
            except Exception as exc:  # noqa: BLE001
                log.warning("Serper search failed: %s", str(exc)[:100])
        if not self.env.get("SERPAPI_KEY"):
            return {"error": "search is not available (no search key with credits)"}
        try:
            j = requests.get("https://serpapi.com/search.json", timeout=60,
                             params={"engine": "google", "q": q, "num": 8, "gl": "us", "hl": "en",
                                     "api_key": self.env["SERPAPI_KEY"]}).json()
        except Exception as exc:  # noqa: BLE001
            self._note(search=q, error=str(exc)[:80])
            return {"error": "search failed: %s" % str(exc)[:120]}
        if j.get("error") and not j.get("organic_results"):
            self._note(search=q, error=str(j.get("error"))[:80])
            if "hasn't returned any results" in str(j.get("error")):
                self._count("search_serpapi", SEARCH_USD["serpapi"])
                return {"results": [], "boxes": []}
            return {"error": "no results (%s)" % str(j.get("error"))[:80]}
        self._count("search_serpapi", SEARCH_USD["serpapi"])
        self._note(search=q, via="serpapi", results=len(j.get("organic_results") or []))
        return {"results": [{"title": o.get("title"), "link": o.get("link") or "", "date": o.get("date"),
                             "snippet": o.get("snippet")} for o in (j.get("organic_results") or [])[:8]],
                "boxes": ["%s: %s" % (k, json.dumps(j[k])[:500]) for k in ("answer_box", "knowledge_graph")
                          if j.get(k)]}

    # -- page opens --------------------------------------------------------
    def open(self, url: str) -> str:
        url = re.sub(r"[.,;:!?*`|]+$", "", (url or "").strip())
        if not url.lower().startswith("http"):
            return "That is not a web address."
        hit = self.cache["page"].get(url)
        if hit is None:
            text, how = self._open_live(url)
            ok = not blocked(text)
            hit = {"ok": ok, "text": text[:PAGE_CHARS * 2] if ok else "", "how": how}
            with self.lock:
                self.cache["page"][url] = hit
            self._count("page_" + (how.split()[-1] if ok else "failed"))
            self._note(fetch=url, ok=ok, chars=len(text), how=how)
        else:
            self._count("page_cached")
        if not hit["ok"]:
            return "Could not open this page (%s)." % hit["how"]
        self.seen.add(url)
        text = hit["text"]
        return text[:PAGE_CHARS] + ("\n[page truncated]" if len(text) > PAGE_CHARS else "")

    def _open_live(self, url: str) -> tuple[str, str]:
        """Free first, then the two paid openers. Returns (text, how); `how` ends with the tier
        that produced the text."""
        text, how = "", ""
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            kind = (r.headers.get("content-type") or "").lower()
            if r.status_code < 400 and "pdf" not in kind:
                text = _strip_html(r.text)
            how = "tried direct(%s)" % r.status_code
        except Exception as exc:  # noqa: BLE001
            how = "tried direct(%s)" % type(exc).__name__
        if not blocked(text):
            return text, how + " direct"
        if self.env.get("FIRECRAWL_API_KEY") and not _dead["firecrawl"]:
            try:
                r = requests.post("https://api.firecrawl.dev/v1/scrape", timeout=90,
                                  headers={"Authorization": "Bearer " + self.env["FIRECRAWL_API_KEY"]},
                                  json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
                if r.status_code in (402, 429) and "credit" in r.text.lower():
                    _dead["firecrawl"] = True
                    log.warning("Firecrawl is out of credits; protected pages go to Scrapfly")
                t2 = (((r.json() or {}).get("data") or {}).get("markdown") or "").strip() if r.status_code == 200 else ""
                how += " firecrawl(%s)" % r.status_code
                if r.status_code == 200:
                    with self.lock:
                        self.usd += PAGE_USD["firecrawl"]
                if not blocked(t2):
                    return t2, how + " firecrawl"
                text = t2 if len(t2) > len(text) else text
            except Exception as exc:  # noqa: BLE001
                how += " firecrawl(%s)" % type(exc).__name__
        if self.env.get("SCRAPFLY_KEY") and self.protected < PROTECTED_MAX:
            with self.lock:
                self.protected += 1
                self.usd += PAGE_USD["scrapfly"]
            try:
                import scrapfly_browser
                res = scrapfly_browser.ScrapflyBrowserClient().fetch(url)
                t3 = (getattr(res, "text", "") or "") if getattr(res, "ok", False) else ""
                how += " scrapfly(%s)" % ("ok" if t3 else (getattr(res, "error", "") or "no text"))[:40]
                if not blocked(t3):
                    return t3, how + " scrapfly"
            except Exception as exc:  # noqa: BLE001
                how += " scrapfly(%s)" % type(exc).__name__
        return text, how

    # -- obituary index ----------------------------------------------------
    def obituary_index(self, last: str, first: str = "") -> str:
        last, first = (last or "").strip(), (first or "").strip()
        if not last:
            return "A last name is required."
        key = "%s|%s" % (last.lower(), first.lower())
        hit = self.cache["index"].get(key)
        if hit is None:
            hit = lookup_obituary_index(last, first)
            if not hit.startswith("index lookup failed"):
                with self.lock:
                    self.cache["index"][key] = hit
            self._note(index="%s, %s" % (last, first), rows=hit.count("\n") + 1)
        self._count("index")
        self.seen.add(INDEX_URL)
        return hit + "\nCite this lookup as: " + INDEX_URL


def lookup_obituary_index(last: str, first: str = "") -> str:
    """Knox County Public Library index of Knoxville News Sentinel obituaries since 1991: free,
    and it lists same-name strangers as separate rows with their ages."""
    try:
        r = requests.get(INDEX_URL, timeout=40, headers={"User-Agent": UA},
                         params={"theStartRow": 1, "Browse": "", "GivenName": first, "SurName": last,
                                 "BeginMonth": "", "BeginDay": "", "BeginYear": "", "EndMonth": "",
                                 "EndDay": "", "EndYear": "", "Orderby": "Date"})
    except Exception as exc:  # noqa: BLE001
        return "index lookup failed: %s" % str(exc)[:100]
    return parse_index(r.text, last, first)


def parse_index(html: str, last: str, first: str = "") -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"\s+", " ", re.sub(r"\|[\s|]*", "| ", re.sub(r"(?s)<[^>]+>", "|", t))).replace("&nbsp;", " ")
    rows = re.findall(r"([^|]{0,60}%s,[^|]{0,80})\|\s*\(age:\s*([^)]*)\)\|\s*([^|]*)\|\s*([^|]*)\|"
                      % re.escape(last.strip().title()), t)
    if not rows:
        return ("No obituary in the index for %s %s. That is not proof the person is alive: the index covers "
                "the Knoxville News Sentinel only and can be 4 months behind." % (first, last)).replace("  ", " ")
    return "\n".join("%s | age %s | %s | obituary date %s" % tuple(x.strip() for x in row) for row in rows[:40])


# ------------------------------------------------------------------ models --

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search", "description": "Google search. Returns the top results with title, link and snippet.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "Open one web page and return its main text. Works on obituary and funeral home pages.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "obituary_index",
        "description": ("Knox County Public Library index of every Knoxville News Sentinel obituary since 1991: "
                        "name, age at death and obituary date. Free and complete, though it can run up to 4 months "
                        "behind. Search by last name and first name. Different people with the same name appear as "
                        "separate rows, so match on middle name and age."),
        "parameters": {"type": "object", "properties": {"last_name": {"type": "string"},
                                                        "first_name": {"type": "string"}},
                       "required": ["last_name"]}}},
]
TOOL_RULES = (
    "\n\nTools in this run: web_search, web_fetch and obituary_index only (web_fetch also opens protected "
    "obituary pages). You have at most %d searches and %d page opens, so spend them on what decides the case."
    "\n\nobituary_index is a free lookup of the county library's obituary index. Before you call anyone living "
    "or name anyone as a signer, look up the owner of record and every person you would name as a signer. A row "
    "that matches on name and on age is strong evidence of a death; open the obituary to confirm it. Do not name "
    "a person as a required signer while their status is unverified: say that it is unverified."
    "\n\nThe standard for Living, Deceased and Unknown. Nobody has a web page proving they are alive today, so "
    "do not ask for one. Call a person Living when BOTH hold: (1) the index lookup and your searches show no "
    "death that matches them on name AND on age or family, and (2) something recent places them in the present: "
    "a notice, filing, deed, mortgage or listing from the last two years that names them as a living party, or "
    "a skip trace subject record with a current address and no deceased flag. Same-name strangers in the index "
    "with the wrong age, the wrong middle name or a death before that recent record do not count against them: "
    "rule each one out in a line. Call a person Unknown only when a SPECIFIC candidate death could not be ruled "
    "out (a row or obituary that fits on name and plausibly on age), or when nothing recent places them at all; "
    "then name the candidate. Call a person Deceased only on an obituary, funeral home page or court record you "
    "opened, or an index row that matches on name and age.")
ASK = (
    "Research this property's owner of record and, if the owner has died, the heirs and who "
    "must sign. Use web search and web fetch: obituaries (legacy.com, funeral homes, local "
    "papers, findagrave), probate and Chancery filings, county records, and anything that "
    "proves identity. Finish with a plain research memo listing every finding with its URL and "
    "a verbatim quote, the decedents with their verbatim survivors lists, the living people and "
    "their relationship to the owner, red flags, and what you could not resolve.\n\n")
FACT_LINE = (
    "End the memo with a line that reads FACT_SHEET_JSON: followed by ONE JSON object with these keys: "
    "owner_status (Living, Deceased or Unknown), owners (list of {name, status}), decedents (list of "
    "{name, dod, obituary_url, survivors_verbatim}), must_sign (list of names), cited_urls (list).\n\n")
NUDGE = ("Now reply with ONLY the line FACT_SHEET_JSON: followed by the one JSON object "
         "(owner_status, owners, decedents, must_sign, cited_urls). If you have not written the "
         "research memo yet, write it first, briefly, then that line.")


def chat(model: str, messages: list, tools: list | None = None, max_tokens: int = 20000) -> dict:
    """One OpenRouter call. Only hosts that do not keep or train on what they are sent."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True},
            "provider": {"data_collection": "deny"}}
    if tools:
        body["tools"], body["tool_choice"] = tools, "auto"
        body["provider"]["require_parameters"] = True      # never a host that silently drops the tools
    last = ""
    for attempt in range(4):
        try:
            r = requests.post(OPENROUTER, timeout=300, headers={"Authorization": "Bearer " + key}, json=body)
        except requests.RequestException as exc:
            last = str(exc)[:200]
            time.sleep(6 * (attempt + 1))
            continue
        if r.status_code in (408, 429, 500, 502, 503, 504, 529):
            last = "HTTP %s" % r.status_code
            time.sleep(8 * (attempt + 1))
            continue
        try:
            j = r.json()
        except ValueError:
            last = "unreadable reply (HTTP %s)" % r.status_code
            time.sleep(6 * (attempt + 1))
            continue
        if j.get("error") and not j.get("choices"):
            code = (j["error"] or {}).get("code")
            if code in (408, 429, 500, 502, 503, 504, 529) and attempt < 3:
                last = "provider error %s" % code
                time.sleep(8 * (attempt + 1))
                continue
            raise RuntimeError("openrouter error: %s" % json.dumps(j["error"])[:300])
        return j
    raise RuntimeError("openrouter kept failing: %s" % last)


class Meter:
    def __init__(self):
        self.usd = 0.0
        self.prompt = self.completion = self.steps = 0
        self.hosts: Counter = Counter()

    def add(self, j: dict) -> None:
        u = j.get("usage") or {}
        self.usd += float(u.get("cost") or 0)
        self.prompt += u.get("prompt_tokens") or 0
        self.completion += u.get("completion_tokens") or 0
        self.steps += 1
        if j.get("provider"):
            self.hosts[str(j["provider"])] += 1

    def as_dict(self) -> dict:
        return {"usd": round(self.usd, 4), "prompt_tokens": self.prompt, "completion_tokens": self.completion,
                "steps": self.steps, "hosts": dict(self.hosts)}


def parse_fact(memo: str) -> dict:
    mm = re.search(r"FACT_SHEET_JSON:?\s*(?:```(?:json)?)?\s*(\{.*)", memo or "", re.S)
    if not mm:
        return {}
    try:
        fact, _ = json.JSONDecoder().raw_decode(mm.group(1))
        return fact if isinstance(fact, dict) else {}
    except ValueError:
        return {}


def investigate(model: str, system: str, ask: str, web: Web, searches: int, pages: int) -> dict:
    """One model's tool loop. Returns the memo, the fact sheet and what it cost; never raises."""
    meter, memo, err, nudged, t0 = Meter(), "", "", False, time.time()
    used = {"search": 0, "page": 0}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ask}]
    try:
        for _ in range(MAX_STEPS):
            spent_out = used["search"] >= searches and used["page"] >= pages
            j = chat(model, messages, tools=None if spent_out else TOOLS)
            meter.add(j)
            m = j["choices"][0]["message"]
            keep = {k: m[k] for k in ("role", "content", "tool_calls", "reasoning_details") if m.get(k) is not None}
            keep.setdefault("role", "assistant")
            keep.setdefault("content", "")
            messages.append(keep)
            calls = m.get("tool_calls") or []
            if not calls:
                memo = (memo + "\n\n" + (m.get("content") or "")).strip()
                if "FACT_SHEET_JSON" in memo or nudged:
                    break
                nudged = True
                messages.append({"role": "user", "content": NUDGE})
                continue
            for tc in calls:
                fn = tc.get("function") or {}
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    a = {}
                name = fn.get("name")
                if name == "web_search":
                    if used["search"] >= searches:
                        res = "LIMIT REACHED: no searches left. Write the final memo from what you have."
                    else:
                        used["search"] += 1
                        res = web.search(str(a.get("query") or ""))
                elif name == "web_fetch":
                    if used["page"] >= pages:
                        res = "LIMIT REACHED: no page opens left. Write the final memo from what you have."
                    else:
                        used["page"] += 1
                        res = web.open(str(a.get("url") or ""))
                elif name == "obituary_index":
                    res = web.obituary_index(str(a.get("last_name") or ""), str(a.get("first_name") or ""))
                else:
                    res = "unknown tool"
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": res})
    except Exception as exc:  # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        log.warning("research loop on %s stopped: %s", model, err)
    out = {"model": model, "memo": memo, "fact": parse_fact(memo), "error": err,
           "seconds": round(time.time() - t0), "searches": used["search"], "pages": used["page"]}
    out.update(meter.as_dict())
    return out


MIN_TOOL_CALLS = 3


def usable(r: dict | None, min_calls: int = MIN_TOOL_CALLS) -> bool:
    """A memo counts only when the model actually looked. Found live: a host that ignored the
    tools returned a confident 1,900-word memo in 12 seconds with zero searches, including a
    death it had no source for. Length alone proves nothing."""
    return (bool(r) and len((r.get("memo") or "").strip()) >= 400
            and (r.get("searches") or 0) + (r.get("pages") or 0) >= min_calls)


# -------------------------------------------------------------- cross-check --

def _status(f: dict) -> str:
    s = str((f or {}).get("owner_status") or "").strip().lower()
    return "Living" if s.startswith("liv") else "Deceased" if s.startswith("dec") else "Unknown"


def _decedents(f: dict) -> list[tuple[str, str]]:
    out = []
    for d in (f or {}).get("decedents") or []:
        name = str(d.get("name") or "").strip() if isinstance(d, dict) else ""
        # a same-name stranger the researcher listed in order to rule out is not a finding about this family
        if name and not RULED_OUT.search(name):
            out.append((name, str(d.get("dod") or "").strip()))
    return out


def _signers(f: dict) -> list[str]:
    """The PEOPLE a fact sheet names to sign. A line about a role or a group ("the executor of the
    estate of Jane Doe", "Jane's heirs") names nobody, so it never enters the comparison."""
    out = []
    for s in (f or {}).get("must_sign") or []:
        name = s.get("name") if isinstance(s, dict) else s
        name = re.sub(r"\(.*?\)", " ", str(name or "")).strip()
        if _toks(name) and not STRUCTURAL.search(name) and len(_toks(name)) <= 5:
            out.append(name)
    return out


def compare(a: dict, b: dict, absence_vouched: bool = False) -> dict:
    """Where two researchers' fact sheets disagree. `hard` points go to the referee: they change
    who gets called or who signs. `soft` points ride along as flags for the person reading the file.

    `absence_vouched` is true only when the obituary index covers this county AND holds no row for
    any owner's name. Then "Living" against "could not confirm" is a note, because the one source
    that would show a death shows none. Everywhere else a split on the owner's status is settled,
    never averaged: calling a dead owner living and hanging a stranger's obituary on a living owner
    are the two errors that hurt most."""
    hard, soft = [], []
    sa, sb = _status(a), _status(b)
    if sa != sb:
        line = "Owner status: one researcher says %s, the other says %s." % (sa, sb)
        (soft if absence_vouched and {sa, sb} == {"Living", "Unknown"} else hard).append(line)
    da, db, ma, mb = _decedents(a), _decedents(b), _signers(a), _signers(b)
    for dead, signers in ((da, mb), (db, ma)):
        for name, _ in dead:
            if any(same_person(name, s, strict=True) for s in signers):
                hard.append("%s: one researcher lists this person as deceased, the other names them as a signer." % name)
    owners = [str(x.get("name") or "") for f in (a, b) for x in (f or {}).get("owners") or [] if isinstance(x, dict)]
    for n1, d1 in da:
        twins = [d2 for n2, d2 in db if d2 and not VAGUE.search(d2) and same_person(n1, n2, strict=True)]
        if not d1 or VAGUE.search(d1) or not twins:
            continue
        if any(_year(d2) == _year(d1) and date_key(d2) == date_key(d1) for d2 in twins):
            continue                                  # the other researcher has this death on the same day
        d2 = twins[0]
        if any(m != n1 and same_person(n1, m, strict=True) and _year(dm) == _year(d2) for m, dm in da):
            continue                                  # two people of one name; this researcher has the other one too
        if _year(d1) and all(_year(x) and _year(x) != _year(d1) for x in twins):
            line = "%s: the two researchers give different years of death (%s and %s)." % (n1, d1, d2)
            # a date conflict about the title holder decides the case; about anyone else it is a note
            (hard if any(same_person(n1, ow) for ow in owners) or not owners else soft).append(line)
        elif date_key(d1) and date_key(d2) and date_key(d1) != date_key(d2):
            soft.append("%s: the two researchers give different dates of death (%s and %s)." % (n1, d1, d2))
    only_a = [s for s in ma if not any(same_person(s, t) for t in mb)]
    only_b = [s for s in mb if not any(same_person(s, t) for t in ma)]
    shared = [s for s in ma if any(same_person(s, t) for t in mb)]
    if ma and mb and not shared:
        hard.append("Who signs: the researchers name no one in common (%s against %s)."
                    % (", ".join(ma[:4]), ", ".join(mb[:4])))
    elif only_a or only_b:
        soft.append("Who signs: only one researcher names %s." % ", ".join((only_a + only_b)[:6]))
    seen, uniq = set(), {"hard": [], "soft": []}
    for kind, items in (("hard", hard), ("soft", soft)):
        for it in items:
            if it not in seen:
                seen.add(it)
                uniq[kind].append(it)
    return uniq


def pick_lead(first: dict, second: dict | None) -> dict:
    """The memo that goes forward when nothing is in dispute: the first researcher's, unless it
    failed or could not settle the owner's status and the second did."""
    if not usable(second):
        return first
    if not usable(first):
        return second
    if _status(first["fact"]) == "Unknown" and _status(second["fact"]) != "Unknown":
        return second
    return first


REFEREE_ASK = (
    "Two researchers worked this record independently and disagree. Settle ONLY the points in dispute, "
    "using the tools. Pages they already opened cost nothing to open again. Apply the standard for Living, "
    "Deceased and Unknown exactly as written above: a title holder named in a recent notice or filing, with no "
    "matching death in the index or in a search, is Living, and is named as a signer. Unknown is for a specific "
    "candidate death that could not be ruled out, and you must name that candidate. Then write the complete "
    "final research memo for this "
    "record: every finding with its URL and a verbatim quote, the decedents with their verbatim survivors "
    "lists, the living people and their relationship to the owner, red flags, and what could not be "
    "resolved, including each disputed point and how you settled it.\n\n")


# ------------------------------------------------------------------ shaping --

def shape(model: str, system: str, schema: dict, prompt: str, meter: Meter) -> dict:
    """A memo into the schema's JSON, on the low-cost model. Same wording the Claude path uses."""
    ask = (prompt + "\n\nReply with ONE JSON object and nothing else. It must follow this JSON "
           "Schema exactly (every key present; empty string, empty list or false when unknown):\n"
           + json.dumps(schema, separators=(",", ":")))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": ask}]
    err = ""
    for _ in range(3):
        j = chat(model, messages, max_tokens=32000)
        meter.add(j)
        text = (j["choices"][0]["message"].get("content") or "").strip()
        try:
            return researcher.conform(researcher._first_json(text), schema)
        except ValueError as exc:
            err = str(exc)
            messages += [{"role": "assistant", "content": text[:4000]},
                         {"role": "user", "content": "That was not one valid JSON object (%s). Reply with only "
                                                     "the JSON object." % err[:120]}]
    raise RuntimeError("the model did not return valid JSON three times: %s" % err[:160])


# ----------------------------------------------------------------- research --

def owner_names(run) -> list[tuple[str, str]]:
    """(first, last) for each person on the title we can name, for the obituary index."""
    out: list[tuple[str, str]] = []

    def add(first, last):
        first, last = (first or "").strip(), (last or "").strip()
        if first and last and not ENTITY_RX.search(first + " " + last) and (first.lower(), last.lower()) not in \
                {(f.lower(), l.lower()) for f, l in out}:
            out.append((first.split()[0], last))

    rec = ((run.load("records.json") or [{}])[0].get("record") or {})
    ow = rec.get("owner") or {}
    if not (ow.get("company") or "").strip():
        add(ow.get("first_name"), ow.get("last_name"))
    groups = run.load("people.json") or []
    for p in (groups[0]["people"] if groups else []):
        if p.get("is_subject"):
            add(p.get("first"), p.get("last"))
    return out[:3]


def index_covers(run) -> bool:
    """The obituary index is the Knoxville paper's. It can vouch that nobody of a name died only
    for a Knox County record; a Blount death may never have run there."""
    rec = ((run.load("records.json") or [{}])[0].get("record") or {})
    return "knox" in str((rec.get("address") or {}).get("county") or "").lower()


def research(run, row) -> tuple[dict, float]:
    """Web research for one record on the low-cost pair. Writes research/<slug>.json and returns
    (research, usd), the same contract as researcher.research()."""
    s = settings()
    slug = run.load("records.json")[0]["slug"]
    web = Web(run.p("research", slug + "_webcache.json"))
    brief = {"record": researcher.record_brief(run, row),
             "smartskip_relatives": researcher.people_brief(run, with_phones=False)}
    index_rows = [{"lookup": "%s, %s" % (last, first), "rows": web.obituary_index(last, first)}
                  for first, last in owner_names(run)]
    if index_rows:
        brief["obituary_index_for_owner"] = index_rows
    base = researcher._system()[0]["text"]
    system = base + TOOL_RULES % (s["searches"], s["pages"])
    ask = ASK + FACT_LINE + "RECORD AND RELATIVE GRAPH:\n" + json.dumps(brief, indent=1, default=str)

    def work(model: str) -> dict:
        r = investigate(model, system, ask, web, s["searches"], s["pages"])
        if usable(r):
            return r
        log.warning("%s produced no usable research (%s); one retry", model, r.get("error") or "no tool use")
        again = investigate(model, system, ask, web, s["searches"], s["pages"])
        again["usd"] = round(again.get("usd", 0) + r.get("usd", 0), 4)
        again["retried"] = True
        return again

    models = [s["primary"]] + ([s["second"]] if s["second"] else [])
    with ThreadPoolExecutor(len(models)) as ex:
        runs = list(ex.map(work, models))
    web.save()
    first, second = runs[0], (runs[1] if len(runs) > 1 else None)
    if not usable(first) and not usable(second):
        raise RuntimeError("no researcher produced a memo (%s)" % "; ".join(r.get("error") or "empty" for r in runs))

    both = usable(first) and usable(second)
    vouched = index_covers(run) and bool(index_rows) and all(x["rows"].startswith("No obituary in the index")
                                                             for x in index_rows)
    diff = compare(first["fact"], second["fact"], absence_vouched=vouched) if both else {"hard": [], "soft": []}
    if second is not None and not both:
        diff["soft"].append("Only one researcher finished (%s did not), so there was no cross-check."
                            % (second if usable(first) else first)["model"])
    referee, lead, disputed = None, pick_lead(first, second), False
    if diff["hard"] and s["referee"]:
        sys_ref = base + TOOL_RULES % (REFEREE_SEARCHES, REFEREE_PAGES)
        ask_ref = (REFEREE_ASK + FACT_LINE + "POINTS IN DISPUTE:\n- " + "\n- ".join(diff["hard"])
                   + "\n\nRESEARCHER A MEMO:\n" + first["memo"][:24000]
                   + "\n\nRESEARCHER B MEMO:\n" + second["memo"][:24000]
                   + "\n\nRECORD AND RELATIVE GRAPH:\n" + json.dumps(brief, indent=1, default=str))
        referee = investigate(s["referee"], sys_ref, ask_ref, web, REFEREE_SEARCHES, REFEREE_PAGES)
        web.save()
        if usable(referee, min_calls=1):
            lead = referee
        else:
            disputed = True
    elif diff["hard"]:
        disputed = True

    meter = Meter()
    seen = sorted(web.seen)
    shaped = shape(s["primary"], base, researcher.RESEARCH_SCHEMA, (
        "Shape this research memo into the research JSON for slug %r. Copy URLs and quotes exactly; "
        "add nothing the memo does not say. `searches_run` lists the searches and pages used.\n\n"
        "URLS VISITED IN THIS RUN:\n%s\n\nMEMO:\n%s" % (slug, "\n".join(seen), lead["memo"])), meter)
    shaped["slug"] = slug
    shaped = researcher.enforce_sources(shaped, set(seen))
    shaped = apply_cross_check(shaped, diff, disputed=disputed, refereed=bool(referee and usable(referee, min_calls=1)))

    usd = sum(r["usd"] for r in runs) + (referee["usd"] if referee else 0.0) + meter.usd + web.usd
    run.save("research/%s.json" % slug, shaped)
    run.save("research/%s_urls.json" % slug, seen)
    for tag, r in (("a", first), ("b", second), ("referee", referee)):
        if r and r.get("memo"):
            run.p("research", "%s_memo_%s.md" % (slug, tag)).write_text(r["memo"], encoding="utf-8")
    run.save("research/%s_open.json" % slug, {
        "engine": "open", "lead": lead["model"], "disputed": disputed, "cross_check": diff,
        "researchers": [{k: v for k, v in r.items() if k != "memo"} for r in runs],
        "referee": ({k: v for k, v in referee.items() if k != "memo"} if referee else None),
        "shaping": meter.as_dict(), "web": {"stats": dict(web.stats), "usd": round(web.usd, 4), "log": web.log},
        "usd": round(usd, 4)})
    return shaped, usd


def apply_cross_check(shaped: dict, diff: dict, *, disputed: bool, refereed: bool) -> dict:
    """Carry the cross-check into the research JSON, where the analysis and the reader see it.
    A dispute nobody settled never ships as a confident answer: the owner's status goes to Unknown."""
    flags = list(shaped.get("red_flags") or [])
    unresolved = list(shaped.get("unresolved") or [])
    if disputed:
        for d in diff["hard"]:
            flags.append("DISPUTED, needs a person to check before anyone is called: " + d)
        if any(d.startswith("Owner status") for d in diff["hard"]):
            shaped["owner_status"] = "Unknown"
            shaped["owner_status_confidence"] = "disputed between two researchers, not settled"
    elif refereed:
        for d in diff["hard"]:
            unresolved.append("Two researchers disagreed and a referee settled it: " + d)
    for d in diff["soft"]:
        unresolved.append("Cross-check note: " + d)
    shaped["red_flags"], shaped["unresolved"] = flags, unresolved
    return mark_unverified(shaped)


def mark_unverified(shaped: dict) -> dict:
    """The research schema stores `living` as true or false, so a person nobody could verify
    reads exactly like a dead one. Say which it is, in words, where the analysis will read it."""
    dead = [d.get("name") or "" for d in shaped.get("decedents") or [] if not RULED_OUT.search(d.get("name") or "")]
    for p in shaped.get("people") or []:
        rel = p.get("relationship_to_owner") or ""
        if p.get("living") is False and not any(same_person(p.get("name") or "", d) for d in dead) \
                and not re.search(r"deceased|died|dead|unverified", rel, re.I):
            p["relationship_to_owner"] = ("STATUS UNVERIFIED (not shown living, not shown dead). " + rel).strip()
    return shaped


# ----------------------------------------------------------------- analysis --

def analyze(run, row, *, final: bool) -> tuple[dict, float]:
    """The analysis pass. On Claude unless AGENTS_ANALYSIS_MODEL names a model, in which case the
    SAME prompt and the SAME guards (researcher.analyze, enforce_analysis) run on that model. Either
    way the signer guard below is applied."""
    model = settings()["analysis"]
    if not model:
        shaped, usd = researcher.analyze(run, row, final=final)
    else:
        meter = Meter()
        base = researcher._system()[0]["text"]

        def structured(_client, _usage, schema, prompt):
            return shape(model, base, schema, prompt, meter)

        with _patch_lock:
            keep = researcher._structured, researcher._client
            researcher._structured, researcher._client = structured, (lambda: None)
            try:
                shaped, _ = researcher.analyze(run, row, final=final)
            finally:
                researcher._structured, researcher._client = keep
        usd = meter.usd
    guarded = guard_signers(shaped)
    run.save("analysis/%s.json" % guarded["slug"], guarded)
    return guarded, usd


def guard_signers(a: dict) -> dict:
    """Nobody is presented as a signer unless the analysis itself calls them Living. A named
    person the analysis marks Deceased leaves the signer list; one whose status is Unknown stays,
    labelled UNVERIFIED, with a flag telling the caller to confirm first. A name that is both a
    dead parent and a living child (father and son) counts as the living one."""
    people = a.get("people") or []
    by = {state: [p.get("name") or "" for p in people if p.get("living") == state]
          for state in ("Living", "Deceased", "Unknown")}

    def state_of(name: str) -> str:
        if any(same_person(name, x) for x in by["Living"]):
            return "Living"
        if any(same_person(name, x, strict=True) for x in by["Deceased"]):
            return "Deceased"
        return "Unknown" if any(same_person(name, x) for x in by["Unknown"]) else ""

    keep, flags, items = [], list(a.get("signing_flags") or []), list(a.get("open_items") or [])
    for m in a.get("must_sign") or []:
        who = (m.get("who") or "").strip()
        named = [x.strip() for x in re.split(r",|;|&|\band\b", who) if _toks(x) and not STRUCTURAL.search(x)]
        states = {x: state_of(x) for x in named}
        dead = [x for x, st in states.items() if st == "Deceased"]
        unknown = [x for x, st in states.items() if st == "Unknown"]
        if dead and len(dead) == len([x for x in named if states[x]]):
            items.append("%s was listed as a signer but is recorded as deceased; removed from the signer list." % who)
            continue
        for x in dead:
            note = "%s is recorded as deceased and cannot sign." % x
            if note not in flags:
                flags.append(note)
        if unknown:
            if not str(m.get("status") or "").upper().startswith("UNVERIFIED"):
                m["status"] = ("UNVERIFIED: " + (m.get("status") or "")).strip()
            note = "%s: not verified as living. Confirm before treating as a signer." % ", ".join(unknown)
            if note not in flags:
                flags.append(note)
        keep.append(m)
    a["must_sign"], a["signing_flags"], a["open_items"] = keep, flags, items
    for p in people:
        if p.get("living") == "Deceased" and p.get("role") in ("signer", "decision maker"):
            p["role"] = "none"
    return a
