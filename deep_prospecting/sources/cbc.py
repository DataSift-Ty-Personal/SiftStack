"""CyberBackgroundChecks (CBC) people-search source.

The original L3 spec named TPS / FPS / CBC as the three skip-trace sources.
TruePeopleSearch (TPS) is hard-blocked at every transport tried (direct
HTTP 403, Firecrawl returns 'Please enable JS'), FastPeopleSearch (FPS) is
Cloudflare-blocked. CBC is the only free site that renders through
Firecrawl successfully.

Two-stage fetch:
  1. Listing page:  /people/{first}-{last}/{city}-{state}
     - Multiple candidates with name/age/addresses
     - Each candidate has a VIEW DETAILS link → /detail/{slug}/{pid}
  2. Detail page:   /detail/{slug}/{pid}
     - Phones, emails, relatives, associates for one specific person

We accept a candidate from the listing page when its primary name token
matches (case-insensitive); when multiple match we pick the one whose
address state matches the requested state. The detail page is then
fetched and parsed for phones/relatives/associates.

URL discovery uses direct URL construction first (faster, deterministic)
and falls back to Serper (`site:cyberbackgroundchecks.com`) if direct
hit returns empty. Both stages route through Firecrawl via the bridge.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

import requests

from deep_prospecting._siftstack_bridge import firecrawl_fetch_full

logger = logging.getLogger(__name__)


_CBC_DOMAIN = "cyberbackgroundchecks.com"


@dataclass
class CBCPerson:
    name: str
    source_url: str
    age: int | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    relatives: list[str] = field(default_factory=list)
    associates: list[str] = field(default_factory=list)
    detail_url: str | None = None


# ── URL construction + fallback discovery ───────────────────────────────


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _construct_listing_url(name: str, city: str, state_code: str) -> str | None:
    parts = (name or "").strip().split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    name_slug = f"{_slug(first)}-{_slug(last)}"
    if city and state_code:
        return f"https://www.{_CBC_DOMAIN}/people/{name_slug}/{_slug(city)}-{_slug(state_code)}"
    return f"https://www.{_CBC_DOMAIN}/people/{name_slug}"


def _serper_fallback_url(name: str, city: str, state_code: str) -> str | None:
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        return None
    parts = (name or "").strip().split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    query = f'"{first} {last}" {city} {state_code} site:{_CBC_DOMAIN}'.strip()
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("Serper CBC fallback failed for %s: %s", name, e)
        return None
    for it in data.get("organic", []):
        url = it.get("link", "") or ""
        if _CBC_DOMAIN in url and "/people/" in url:
            return url
    return None


# ── Listing-page parser ─────────────────────────────────────────────────


# Block boundary on CBC listing pages: each candidate starts with `## NAME ...`
# and may include "Lives at", "Used to live", and "[VIEW DETAILS](...)".
_PERSON_HEADING_RE = re.compile(r"^##\s+(.+?)(?:\s+Age:\s*(\d+))?\s*$", re.M)
# Capture detail URL only — strip the markdown link title (the trailing
# `"View full address history..."` segment Firecrawl emits after the URL).
_VIEW_DETAILS_RE = re.compile(
    rf"\[VIEW DETAILS\]\((https?://(?:www\.)?{re.escape(_CBC_DOMAIN)}/detail/\S+?)(?:\s+\"[^\"]*\")?\)"
)
_ADDR_LINE_RE = re.compile(r"\[(.+?,\s*[A-Z]{2}\s+\d{5}[^\]]*)\]", re.M)


def _parse_listing_page(text: str) -> list[dict]:
    """Return list of candidate dicts: {name, age, addresses[], detail_url}.

    One entry per `## NAME` heading on the listing page. Caller picks the
    best match by name + state. Empty list when the page has no people
    blocks (Cloudflare challenge, empty results, or a completely different
    structure).
    """
    if not text:
        return []
    # Slice the markdown into per-person sections.
    headings = list(_PERSON_HEADING_RE.finditer(text))
    candidates: list[dict] = []
    for i, m in enumerate(headings):
        name_raw = m.group(1).split("  ")[0].split(" goes by")[0].strip()
        age = int(m.group(2)) if m.group(2) else None
        start = m.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block = text[start:end]
        addrs = [
            a.strip() for a in _ADDR_LINE_RE.findall(block)
            if "," in a and re.search(r"[A-Z]{2}\s+\d{5}", a)
        ]
        det = _VIEW_DETAILS_RE.search(block)
        candidates.append({
            "name": name_raw,
            "age": age,
            "addresses": addrs,
            "detail_url": det.group(1) if det else None,
        })

    return candidates


def _pick_candidate(
    candidates: list[dict],
    *,
    name_hint: str,
    state_code: str,
) -> dict | None:
    """Pick the candidate whose name + state best matches the hint."""
    if not candidates:
        return None
    hint_tokens = [t.lower() for t in re.findall(r"[A-Za-z]+", name_hint or "")]
    if not hint_tokens:
        return candidates[0]
    state_code = (state_code or "").upper()

    # First pass — both first AND last name tokens in candidate name AND
    # state appears in any address.
    for c in candidates:
        cand_lower = c["name"].lower()
        if hint_tokens[0] in cand_lower and hint_tokens[-1] in cand_lower:
            if state_code and any(
                f", {state_code} " in a or a.endswith(f", {state_code}")
                for a in c.get("addresses", [])
            ):
                return c
    # Second pass — name match alone (ignore state).
    for c in candidates:
        cand_lower = c["name"].lower()
        if hint_tokens[0] in cand_lower and hint_tokens[-1] in cand_lower:
            return c
    return None


# ── Detail-page parser ──────────────────────────────────────────────────


_PHONE_RE = re.compile(r"\(?(\d{3})\)?[\s\-\.]?(\d{3})[\s\-\.]?(\d{4})")
_NAME_LINE_RE = re.compile(r"^\s*[\-\*]?\s*([A-Z][a-zA-Z' .\-]+\s+[A-Z][a-zA-Z' .\-]+)\s*$")


def _parse_detail_page(text: str, *, name_hint: str) -> CBCPerson | None:
    if not text:
        return None

    # Strip Google Maps tile noise — Firecrawl renders them as huge
    # markdown image links with pb=! query strings that contain dozens
    # of 4-5 digit numbers per line. They concatenate into bogus phone
    # matches. Drop any `![...](https://www.google.com/maps/vt?...)` runs
    # before phone extraction.
    cleaned = re.sub(
        r"!\[\]\(https://www\.google\.com/maps/[^)]+\)\s*",
        "",
        text,
    )

    # Top-of-page heading — `# Catherine M Geczik  Age: 64`. The pattern
    # tolerates the double-space CBC inserts before "Age:".
    name_m = re.search(
        r"^#\s+([A-Z][A-Za-z][^\n]+?)(?:\s{2,}Age:\s*(\d+))?\s*$",
        cleaned,
        re.M,
    )
    person = CBCPerson(
        name=name_m.group(1).strip() if name_m else (name_hint or "Unknown"),
        source_url="",
    )
    if name_m and name_m.group(2):
        try:
            person.age = int(name_m.group(2))
        except ValueError:
            pass

    # Section-extractor: grab the body between a `## HEADER` and the
    # next `## ` (any other level-2 header). CBC uses `## Phone Numbers`,
    # `## Email Addresses`, `## Possible Relatives`, `## Possible
    # Associates`, `## Address History`. Each section has the entries as
    # `### [text](url)` sub-headings.
    def _section(label_re: str) -> str:
        m = re.search(
            rf"^##\s+{label_re}\s*\n(.+?)(?=^##\s|\Z)",
            cleaned,
            re.S | re.M | re.I,
        )
        return m.group(1) if m else ""

    # Phones — scoped to "## Phone Numbers". Each phone is a `### [(NXX)
    # NXX-XXXX](...)` heading.
    phone_blob = _section(r"Phone\s+Numbers?")
    seen_phones: set[str] = set()
    for pm in _PHONE_RE.finditer(phone_blob):
        digits = "".join(pm.groups())
        if (
            digits
            and digits not in seen_phones
            and not digits.startswith(("800", "888", "877", "866", "855", "844"))
        ):
            seen_phones.add(digits)
            person.phones.append(digits)

    # Emails — `## Email Addresses` with `### [foo@bar.com](...)` entries.
    email_blob = _section(r"Email\s+Addresses?")
    for em in re.finditer(r"\[([A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\]", email_blob):
        addr = em.group(1)
        if addr not in person.emails:
            person.emails.append(addr)

    # Addresses — pull from Current Address + Address History sections.
    # Each is `### [<addr>, City, ST ZZZZZ]`. The full page also has
    # noisy in-block addresses, so scope to those two sections.
    addr_blobs = _section(r"Current\s+Address") + "\n" + _section(r"Address\s+History")
    for a in _ADDR_LINE_RE.findall(addr_blobs):
        a = a.strip()
        if "," in a and re.search(r"[A-Z]{2}\s+\d{5}", a) and a not in person.addresses:
            person.addresses.append(a)

    # Relatives + Associates — `## Possible Relatives` / `## Possible
    # Associates`. Each entry is a `### [Full Name](url)` heading.
    def _names_under(label_re: str) -> list[str]:
        blob = _section(label_re)
        if not blob:
            return []
        out: list[str] = []
        for m in re.finditer(r"^###\s+\[([^\]]+)\]", blob, re.M):
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            if name and name not in out:
                out.append(name)
        return out

    person.relatives = _names_under(r"Possible\s+Relatives")
    person.associates = _names_under(r"Possible\s+Associates")

    return person


# ── Public entry point ─────────────────────────────────────────────────


async def cbc_fetch_person(
    name: str,
    city: str,
    state_code: str,
) -> tuple[CBCPerson | None, str]:
    """Fetch + parse a CBC person page. Returns (person, status).

    status ∈ {"HIT", "EMPTY", "BLOCKED", "ERROR", "SKIPPED"}.

    Two-stage flow:
      stage 1: listing page (addresses + detail-url discovery)
      stage 2: detail page (phones + relatives + associates)
    """
    if not name:
        return None, "SKIPPED"

    listing_url = _construct_listing_url(name, city, state_code)
    if not listing_url:
        return None, "SKIPPED"

    listing_text = await asyncio.to_thread(firecrawl_fetch_full, listing_url)
    if not listing_text:
        # Direct URL fetch failed entirely — try Serper-discovered URL.
        fallback = await asyncio.to_thread(
            _serper_fallback_url, name, city, state_code,
        )
        if not fallback:
            return None, "BLOCKED"
        listing_url = fallback
        listing_text = await asyncio.to_thread(firecrawl_fetch_full, listing_url)
        if not listing_text:
            return None, "BLOCKED"

    candidates = _parse_listing_page(listing_text)
    chosen = _pick_candidate(candidates, name_hint=name, state_code=state_code)
    if chosen is None:
        return None, "EMPTY"

    # Stage 2 — fetch the detail page for phones/relatives/associates.
    if not chosen.get("detail_url"):
        # No detail link but we still have addresses from the listing.
        person = CBCPerson(
            name=chosen["name"],
            age=chosen.get("age"),
            addresses=list(chosen.get("addresses") or []),
            source_url=listing_url,
        )
        return person, "HIT"

    detail_text = await asyncio.to_thread(
        firecrawl_fetch_full, chosen["detail_url"],
    )
    person = _parse_detail_page(detail_text, name_hint=name)
    if person is None:
        # Detail page blocked — fall back to listing-page addresses.
        person = CBCPerson(
            name=chosen["name"],
            age=chosen.get("age"),
            addresses=list(chosen.get("addresses") or []),
            source_url=listing_url,
            detail_url=chosen["detail_url"],
        )
        return person, "HIT"

    # Merge listing addresses into detail-page addresses (listing often
    # has the most recent address; detail tail-loads history).
    for a in chosen.get("addresses") or []:
        if a not in person.addresses:
            person.addresses.insert(0, a)
    person.source_url = chosen["detail_url"]
    person.detail_url = chosen["detail_url"]
    return person, "HIT"
