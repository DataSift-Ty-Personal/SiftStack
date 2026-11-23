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
   The page itself announces the sale ("Tax Sale 25 Information: Event Details: June 2nd, 2026"),
   and the results PDF does not print a date at all (measured on the Tax Sale 25 results,
   2026-11-20), so each archived document also keeps the date the page gave for ITS sale number
   (sale_date_page), set once and never overwritten by the page's next sale.

LOUD, NOT QUIET. The stage fails when the page cannot be read, when the page links no tax sale
document at all (the menu's own forms do not count: they are on every page), or when a document
that calls itself a list yields almost no parcel ids (a scanned image or a changed layout). A zero
on this feed is otherwise identical to "no sale is scheduled".

NOT DONE HERE, AND SAID SO. The results document (the Report of Sale) is archived and announced
but not parsed here: its leads are the FORMER owners, who are named only in the table this module
refuses to guess at. The brain's tax_sale_results stage (agents/autopilot/taxsale.py) reads that
table row by row, on its own copy of this archive, which it captures with this module's capture().

READ LIVE 2026-11-20 from a workstation: the page is a static site (Amazon S3 behind CloudFront, no
bot wall seen) and answered a plain request, the PDFs too. It also links the Trustee's site-wide
forms (Tangible Personal Property, Mailing Address Change), some on knoxcounty.org itself, which
answered the first-to-market box's datacenter address 403 the same day. Those forms are not about a
sale: they are not downloaded (site_form), and the first one was being read as a property list with
no parcel in it, which failed the feed. Every archived document is sorted again on each run, so a
form an older run filed as a list stops being one. A list must name a sale, a delinquency or an
auction: "property" alone names half the county's forms.

    KNOX_TAX_SALE_PAGE=https://trustee.knoxcounty.org/services/tax-sale
    KNOX_TAX_SALE_MAX_PARCELS=250     tax roll lookups in one run (the rest wait for tomorrow)
    KNOX_TAX_SALE_RECHECK_DAYS=7      how often a known link is downloaded again for new content
    KNOX_TAX_SALE_DATE=YYYY-MM-DD     the sale date, when the document's own cannot be read

    python src/knox_tax_sale.py --scratch output/knox                  # capture and prepare, uploads nothing
    python src/knox_tax_sale.py --scratch output/knox --capture-only   # archive the documents and stop
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
# The Trustee's site-wide forms, linked from the menu on every page, the tax sale page included.
# "Tangible Personal Property" read as a property list to the words below and failed the feed on the
# first-to-market box (live page, 2026-11-20). They are not about a sale, so they are not even downloaded.
_NOT_A_SALE = re.compile(r"tangible|mailing|address\s*change|bank\s*draft|relief|freeze|\bforms?\b|application", re.I)
# A property list must say what it is a list for; "property" alone names half the county's forms.
_SALE_LIST = re.compile(r"sale|delinquen|auction", re.I)
# A sale's number as a label or a file name writes it: "Tax Sale 25", "TaxSale25-...", "Tax Sale No. 25".
_SALE_NO = re.compile(r"\btax\s*sale\s*(?:no\.?\s*|number\s*|#\s*)?(\d{1,3})(?!\d)", re.I)
_SCRIPTS = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
PAGE_DATE_REACH = 120    # characters after a sale's name that its announced date may sit in


def page_url() -> str:
    return (os.environ.get("KNOX_TAX_SALE_PAGE") or DEFAULT_PAGE).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


# The page writes its labels with a real en dash ("Tax Sale 25 - Final Results", read 2026-11-20), and a
# label is repeated in run reports and Slack lines, which carry plain hyphens only.
_DASHES = {c: "-" for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)}


def _clean(s: str) -> str:
    import html
    return " ".join(html.unescape(_TAGS.sub(" ", s or "")).translate(_DASHES).split())


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


def _blob(label: str, url: str) -> str:
    return ("%s %s" % (label or "", (url or "").rsplit("/", 1)[-1])).lower().replace("_", " ").replace("-", " ")


def site_form(label: str, url: str) -> bool:
    """One of the Trustee's site-wide forms the menu links on every page (Tangible Personal Property, the
    Mailing Address Change Form): not a tax sale document, never downloaded."""
    return bool(_NOT_A_SALE.search(_blob(label, url)))


def classify(label: str, url: str) -> str:
    """"list" (properties going to a sale), "results" (after the sale) or "other". A list has to say
    it is for a sale ("Tax Sale 26 Property List", "Delinquent Properties"): a form that merely names
    property is not one."""
    blob = _blob(label, url)
    if re.search(r"result|report\s*of\s*sale|decree|confirm", blob):
        return "results"
    if re.search(r"terms|condition|instruction|faq|registration|bidder", blob) or _NOT_A_SALE.search(blob):
        return "other"
    if not _SALE_LIST.search(blob):
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


def sale_number(label: str, url: str = "") -> str:
    """The number of the sale a document belongs to, as its link or file name writes it ("Tax Sale 25 -
    Final Results", "TaxSale25-ReportofSale.pdf"), without leading zeros; '' when neither names one."""
    for text in (label or "", (url or "").rsplit("/", 1)[-1]):
        m = _SALE_NO.search(text.replace("_", " "))
        if m:
            return str(int(m.group(1)))
    return ""


def page_sales(html: str) -> dict:
    """{sale number: the date the page announces for it}, read off the page's own words ("Tax Sale 25
    Information: Event Details: June 2nd, 2026"). Only a date inside a short reach AFTER the sale's
    name counts, and the first such date wins; a sale named with no date after it is left out."""
    text = _clean(_SCRIPTS.sub(" ", html or ""))
    dates = _dates_in(text)
    out: dict = {}
    for m in _SALE_NO.finditer(text):
        n = str(int(m.group(1)))
        if n in out:
            continue
        after = [iso for pos, iso in dates if m.end() <= pos <= m.end() + PAGE_DATE_REACH]
        if after:
            out[n] = after[0]
    return out


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


def capture(scratch: str, http_get=None, today: str | None = None, page: str | None = None) -> dict:
    """Read the page, archive what is new. Returns {"new": [doc...], "docs": {...}, "problems": [...]}.

    A document is keyed by its content hash, so a link the county re-uses for a new file is kept
    as a second document instead of overwriting the first. `page` replaces KNOX_TAX_SALE_PAGE (the
    brain's stage passes its own setting). The Trustee's site-wide forms are skipped (site_form), and
    every archived document is sorted again by today's rules, so one an older run filed as a property
    list (the Tangible Personal Property form, 2026-11-20) stops being read as one, even on a day the
    page itself cannot be read."""
    if http_get is None:
        from knox_condemnations import http_get
    today = today or date.today().isoformat()
    url = (page or "").strip() or page_url()
    arc = os.path.join(scratch, "tax_sale_archive")
    os.makedirs(arc, exist_ok=True)
    docs = _load_docs(scratch)
    for d in docs.values():
        if isinstance(d, dict) and d.get("url"):
            d["kind"] = classify(d.get("label") or "", d["url"])
    problems, new = [], []
    try:
        html = http_get(url).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"new": [], "docs": docs, "problems": ["the Trustee's tax sale page could not be read (%s: %s)"
                                                      % (type(exc).__name__, str(exc)[:80])]}
    every = links_from_page(html, url)
    links = [x for x in every if not site_form(x["label"], x["url"])]
    if not every:
        return {"new": [], "docs": docs, "problems": [
            "the Trustee's tax sale page links no PDF at all. The page moved or its layout changed "
            "(KNOX_TAX_SALE_PAGE)."]}
    if not links:
        # The menu's forms are on every page, so a page holding nothing else has lost its tax sale documents.
        return {"new": [], "docs": docs, "problems": [
            "the Trustee's tax sale page links only the site's own forms, no tax sale document. The page moved "
            "or its layout changed (KNOX_TAX_SALE_PAGE)."]}
    announced = page_sales(html)
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
    # The date the page announces for each document's own sale number, kept on the document the first
    # time the page and the document name the same sale. Never overwritten: once the page moves on to
    # the next sale, it names another number and the older documents keep the date they were given.
    for d in docs.values():
        n = sale_number(d.get("label") or "", d.get("url") or "")
        if n and n in announced and not d.get("sale_date_page"):
            d["sale_date_page"] = announced[n]
    for d in new:
        d["sale_date_page"] = docs[d["sha"]].get("sale_date_page", "")
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
    ap.add_argument("--capture-only", action="store_true",
                    help="archive the page's documents and stop: no parcel lookups, nothing staged (the copy a box "
                         "that cannot reach the Trustee's site reads)")
    a = ap.parse_args()
    if a.capture_only:
        os.makedirs(a.scratch, exist_ok=True)
        got = capture(a.scratch)
        print("%d new document(s), %d in the archive at %s" % (len(got["new"]), len(got["docs"]), a.scratch))
        for d in got["new"]:
            print("  new: %s (%s)" % (d["label"][:80], d["kind"]))
        for p in got["problems"]:
            print("  problem:", p)
        return 1 if got["problems"] else 0
    stats = prepare(a.scratch)
    print(describe(stats))
    for p in stats["problems"]:
        print("  problem:", p)
    return 1 if stats["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
