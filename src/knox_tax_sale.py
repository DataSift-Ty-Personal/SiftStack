"""The Knox County Trustee's tax sale documents (build plan 1.10). Knox only.

The Trustee posts each tax sale as a handful of PDFs on one public page: the list of properties
going to the sale, and afterwards the results. An owner on that list is facing a dated sale, so
these records ride the hot auction lane by the sale date. Nothing here needs a login.

THREE JOBS, in the order they are trusted:

1. CAPTURE, which does not depend on any layout. Every PDF the page links is archived by content
   hash the first day it is seen, and each NEW document is named in the run report. A list that
   is noticed two days late is not first to market, and the capture stage for condemnations
   taught that an overwritten county file cannot be fetched again later.

2. PARCEL IDS, not table rows. A tax sale list prints the address and the owner run together
   with no delimiter (a street can be named "Drive E"), and a table parse that goes wrong goes
   wrong quietly. So this reads only the PARCEL IDS out of the text and asks the county tax roll
   for each one's official address and owner of record. Every id is verified by that lookup, so a
   stray number that only looks like a parcel dies there instead of becoming a record.

3. THE SALE DATE, best effort and flagged. The date is read from the words around "sale" at the
   top of the document. When it cannot be read the rows are still staged, with no sale date, and
   the run report says so, because a tax sale record without its date cannot ride the hot lane.

LOUD, NOT QUIET. The stage fails when the page cannot be read, when the page links no PDF at
all, or when a document that calls itself a list yields almost no parcel ids (a scanned image
or a changed layout). A zero on this feed is otherwise identical to "no sale is scheduled".

NOT DONE HERE, AND SAID SO. The results document (the Report of Sale) is archived and announced
but not parsed: its leads are the FORMER owners, who are named only in the table this module
refuses to guess at. Its winning bidders are a cash buyer list for the buyer side, not for this
pipeline.

UNVERIFIED. Written from the county's public page address and one earlier sale's file names; the
page itself was not fetched while this was built. The address is a setting.

    KNOX_TAX_SALE_PAGE=https://trustee.knoxcounty.org/services/tax-sale
    KNOX_TAX_SALE_MAX_PARCELS=250     tax roll lookups in one run (the rest wait for tomorrow)
    KNOX_TAX_SALE_RECHECK_DAYS=7      how often a known link is downloaded again for new content
    KNOX_TAX_SALE_DATE=YYYY-MM-DD     the sale date, when the document's own cannot be read

    python src/knox_tax_sale.py --scratch output/knox     # capture and prepare, uploads nothing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ftm_staging  # noqa: E402

FEED = "tax_sale_report"
FEED_KEY = "knox_tax_sale_report"
DEFAULT_PAGE = "https://trustee.knoxcounty.org/services/tax-sale"
MIN_PARCELS = 5          # a "list" with fewer ids than this was not really read
DOCS = "tax_sale_docs.json"

_LINK = re.compile(r'<a\b[^>]*href="([^"]+?\.pdf[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
# Knox parcel ids: a 3 digit map, one or two group letters, a 3 digit parcel, sometimes ".NN".
_PARCEL_LETTERED = re.compile(r"(?<![A-Za-z0-9])(\d{3})\s?([A-Z]{1,2})\s?-?\s?(\d{3})(\.\d{2})?(?![0-9])")
# No group letters (rural maps). Only with a dash: two bare numbers side by side are everywhere.
_PARCEL_PLAIN = re.compile(r"(?<![A-Za-z0-9.-])(\d{3})\s?-\s?(\d{3})(\.\d{2})?(?![0-9-])")
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december")
_DATE_WORDS = re.compile(r"\b(%s)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b" % _MONTHS, re.I)
_DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
_SALE_WORD = re.compile(r"\b(sale|auction|sold)\b", re.I)


def page_url() -> str:
    return (os.environ.get("KNOX_TAX_SALE_PAGE") or DEFAULT_PAGE).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _clean(s: str) -> str:
    return " ".join(_TAGS.sub(" ", s or "").split())


def links_from_page(html: str, base: str) -> list[dict]:
    """Every PDF the page links, with the words of the link. Order kept, duplicates dropped."""
    out, seen = [], set()
    for href, label in _LINK.findall(html or ""):
        url = urljoin(base, href.replace("&amp;", "&"))
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "label": _clean(label) or url.rsplit("/", 1)[-1]})
    return out


def classify(label: str, url: str) -> str:
    """"list" (properties going to a sale), "results" (after the sale) or "other"."""
    blob = ("%s %s" % (label, url.rsplit("/", 1)[-1])).lower().replace("_", " ").replace("-", " ")
    if re.search(r"result|report\s*of\s*sale|decree|confirm", blob):
        return "results"
    if re.search(r"terms|condition|instruction|faq|registration|bidder", blob):
        return "other"
    if re.search(r"propert|parcel|list", blob):
        return "list"
    return "other"


def parcel_ids(text: str) -> list[str]:
    """Parcel ids in reading order, each once, in the roll's own form (map, group, parcel)."""
    found: dict[int, str] = {}
    for m in _PARCEL_LETTERED.finditer(text or ""):
        found[m.start()] = "%s%s%s%s" % (m.group(1), m.group(2), m.group(3), m.group(4) or "")
    for m in _PARCEL_PLAIN.finditer(text or ""):
        found.setdefault(m.start(), "%s-%s%s" % (m.group(1), m.group(2), m.group(3) or ""))
    out, seen = [], set()
    for pos in sorted(found):
        pin = found[pos]
        if pin not in seen:
            seen.add(pin)
            out.append(pin)
    return out


def _dates_in(text: str) -> list[tuple[int, str]]:
    months = _MONTHS.split("|")
    out = []
    for m in _DATE_WORDS.finditer(text):
        try:
            out.append((m.start(), date(int(m.group(3)), months.index(m.group(1).lower()) + 1, int(m.group(2))).isoformat()))
        except ValueError:
            continue
    for m in _DATE_SLASH.finditer(text):
        try:
            out.append((m.start(), date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()))
        except ValueError:
            continue
    return sorted(out)


def sale_date(text: str, label: str = "") -> str:
    """The sale date a document states, or "" when none sits near the word sale.

    Only the head of the document is read, and only a date within a short reach of "sale" or
    "auction": the body is full of other dates (delinquency years, deed dates) that are not it."""
    head = ("%s\n%s" % (label or "", (text or "")[:4000]))
    dates = _dates_in(head)
    if not dates:
        return ""
    words = [m.start() for m in _SALE_WORD.finditer(head)]
    best = None
    for pos, iso in dates:
        gap = min((abs(pos - w) for w in words), default=None)
        if gap is not None and gap <= 160 and (best is None or gap < best[0]):
            best = (gap, iso)
    return best[1] if best else ""


def _pdf_text(path: str) -> str:
    from pdfminer.high_level import extract_text
    return extract_text(path) or ""


def _load_docs(scratch: str) -> dict:
    try:
        with open(os.path.join(scratch, DOCS), encoding="utf-8") as fh:
            got = json.load(fh)
            return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_docs(scratch: str, docs: dict) -> None:
    path = os.path.join(scratch, DOCS)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(docs, fh, indent=1)
    os.replace(tmp, path)


def capture(scratch: str, http_get=None, today: str | None = None) -> dict:
    """Read the page, archive what is new. Returns {"new": [doc...], "docs": {...}, "problems": [...]}.

    A document is keyed by its content hash, so a link the county re-uses for a new file is kept
    as a second document instead of overwriting the first."""
    if http_get is None:
        from knox_condemnations import http_get
    today = today or date.today().isoformat()
    arc = os.path.join(scratch, "tax_sale_archive")
    os.makedirs(arc, exist_ok=True)
    docs = _load_docs(scratch)
    problems, new = [], []
    try:
        html = http_get(page_url()).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"new": [], "docs": docs, "problems": ["the Trustee's tax sale page could not be read (%s: %s)"
                                                      % (type(exc).__name__, str(exc)[:80])]}
    links = links_from_page(html, page_url())
    if not links:
        return {"new": [], "docs": docs, "problems": [
            "the Trustee's tax sale page links no PDF at all. The page moved or its layout changed "
            "(KNOX_TAX_SALE_PAGE)."]}
    recheck = _env_int("KNOX_TAX_SALE_RECHECK_DAYS", 7)
    last_by_url: dict[str, str] = {}
    for d in docs.values():
        last_by_url[d["url"]] = max(last_by_url.get(d["url"], ""), d.get("checked") or d.get("first_seen") or "")
    for link in links:
        last = last_by_url.get(link["url"], "")
        if last and (date.fromisoformat(today) - date.fromisoformat(last)).days < recheck:
            continue
        try:
            content = http_get(link["url"])
        except Exception as exc:  # noqa: BLE001
            problems.append("could not download %s (%s)" % (link["label"][:60], type(exc).__name__))
            continue
        if not content.startswith(b"%PDF-"):
            problems.append("%s is not a PDF (%d bytes)" % (link["label"][:60], len(content)))
            continue
        sha = hashlib.sha256(content).hexdigest()[:12]
        if sha in docs:
            docs[sha]["checked"] = today
            continue
        path = os.path.join(arc, "%s.pdf" % sha)
        with open(path, "wb") as fh:
            fh.write(content)
        docs[sha] = {"url": link["url"], "label": link["label"], "kind": classify(link["label"], link["url"]),
                     "first_seen": today, "checked": today, "path": path}
        new.append(dict(docs[sha], sha=sha))
    _save_docs(scratch, docs)
    return {"new": new, "docs": docs, "problems": problems}


def _has_house_number(address: str) -> bool:
    m = re.match(r"^\s*(\d+)\s+\S", address or "")
    return bool(m) and int(m.group(1)) > 0


def prepare(scratch: str, http_get=None, pdf_text=None, parcel_lookup=None, today: str | None = None) -> dict:
    """Capture the page, then stage the parcels of every list whose sale has not been held."""
    today = today or date.today().isoformat()
    os.makedirs(scratch, exist_ok=True)
    got = capture(scratch, http_get, today)
    docs, problems = got["docs"], list(got["problems"])
    if parcel_lookup is None:
        from tax_enricher import _knox_parcel_address as parcel_lookup
    pdf_text = pdf_text or _pdf_text
    cap = _env_int("KNOX_TAX_SALE_MAX_PARCELS", 250)
    forced_date = (os.environ.get("KNOX_TAX_SALE_DATE") or "").strip()

    rows, keys = [], []
    looked = 0
    counts = {"lists": 0, "parcels_read": 0, "staged": 0, "not_on_roll": 0, "no_house_number": 0,
              "sale_held": 0, "left_for_tomorrow": 0, "no_sale_date": 0}
    changed = False
    for sha, doc in docs.items():
        if doc.get("kind") != "list":
            continue
        if "parcels" not in doc:
            try:
                text = pdf_text(doc["path"])
            except Exception as exc:  # noqa: BLE001
                problems.append("%s could not be read as text (%s)" % (doc["label"][:60], type(exc).__name__))
                continue
            doc["parcels"] = parcel_ids(text)
            doc["sale_date"] = sale_date(text, doc["label"])
            changed = True
        pins = doc.get("parcels") or []
        if len(pins) < MIN_PARCELS:
            problems.append("%s reads as a property list but only %d parcel id(s) were found in it. "
                            "It may be a scanned image, or the layout changed." % (doc["label"][:60], len(pins)))
            continue
        when = forced_date or doc.get("sale_date") or ""
        if when and when < today:
            counts["sale_held"] += 1
            continue
        counts["lists"] += 1
        counts["parcels_read"] += len(pins)
        if not when:
            counts["no_sale_date"] += 1
        for pin in pins:
            key = "%s:%s" % (sha, pin)
            if not ftm_staging.is_new(scratch, FEED, key):
                continue
            if looked >= cap:
                counts["left_for_tomorrow"] += 1
                continue
            looked += 1
            try:
                found = parcel_lookup(pin)
            except Exception:  # noqa: BLE001  (left open: tomorrow's run asks again)
                counts["left_for_tomorrow"] += 1
                continue
            keys.append(key)
            if not found or not found.get("address"):
                counts["not_on_roll"] += 1
                continue
            if not _has_house_number(found["address"]):
                counts["no_house_number"] += 1
                continue
            counts["staged"] += 1
            rows.append({
                "source": "knox_tax_sale", "feed": FEED_KEY, "notice_type": "tax_sale",
                "address": found["address"].title(), "owner_raw": (found.get("owner") or "").title(),
                "case_number": pin, "tax_account": pin, "sale_date": when,
                "event_date": when, "filed_date": doc.get("first_seen") or today,
                "notes": ("On the Knox County Trustee's tax sale list (%s), first posted %s. %s Parcel %s."
                          % (doc["label"][:80], doc.get("first_seen") or today,
                             ("Sale date %s." % when) if when else "The sale date could not be read from the list.",
                             pin)),
                "source_url": doc["url"],
            })
    if changed:
        _save_docs(scratch, docs)
    ftm_staging.stage(scratch, FEED, rows, keys)
    return {"new_documents": [d["label"] for d in got["new"]], "problems": problems, "lookups": looked, **counts}


def describe(stats: dict) -> str:
    """One plain sentence for the run report."""
    bits = []
    if stats["new_documents"]:
        shown = "; ".join(stats["new_documents"][:3])
        bits.append("%d new document(s) on the Trustee's tax sale page: %s" % (len(stats["new_documents"]), shown))
    if stats["lists"]:
        bits.append("%d parcel(s) staged from %d open list(s) of %d read"
                    % (stats["staged"], stats["lists"], stats["parcels_read"]))
        for key, said in (("not_on_roll", "not on the tax roll"), ("no_house_number", "with no house number"),
                          ("left_for_tomorrow", "left for tomorrow")):
            if stats[key]:
                bits.append("%d %s" % (stats[key], said))
        if stats["no_sale_date"]:
            bits.append("the sale date could not be read on %d list(s)" % stats["no_sale_date"])
    elif not stats["new_documents"]:
        bits.append("no open tax sale list on the Trustee's page"
                    + ((" (%d past sale(s) skipped)" % stats["sale_held"]) if stats["sale_held"] else ""))
    return "; ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    a = ap.parse_args()
    stats = prepare(a.scratch)
    print(describe(stats))
    for p in stats["problems"]:
        print("  problem:", p)
    return 1 if stats["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
