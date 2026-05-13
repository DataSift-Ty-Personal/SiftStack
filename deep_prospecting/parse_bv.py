"""BV paste-and-parse helper — Slice 5b Piece 1.

The operator deep-dives a record in BeenVerified when the automated CLI
can't resolve it (Nerissa / Allan dry-miss pattern; the title_owner-alive
case where Tracerfy / CBC won't surface the right person; or younger-
generation heirs invisible to address-record vendors). They paste the
BV output here — HTML, structured markdown, or raw text — and the
parser folds the contact data into the existing research pack.

CLI:
    python -m deep_prospecting parse-bv --case <slug-or-address-or-name> \\
        --input <path>

Flow:
  1. Read the input file. Format detection picks one of:
       - HTML: strip tags via stdlib html.parser before extraction
       - Markdown / plain text: pass through
  2. Send the cleaned text to Haiku for structured extraction:
       {persons: [{name, age, phones[], emails[], relatives[]}]}
  3. Locate the existing pack by --case (substring match on slug
     across deep_prospecting/outputs/*/<slug>/)
  4. Merge BV-derived phones + emails into pack.skip_trace.phones /
     .emails, source=["bv_manual"]
  5. Re-derive star markers via datasift_csv_writer
  6. Re-run Phase Trestle on the newly added phones (activity_score +
     dial-rank labels)
  7. Write back results.json + research_pack.md, re-overlay the CSV
     row, append a `BV Manual Pass YYYY-MM-DD` outcome tag

Cost (Haiku + Trestle): typically ~$0.01-0.02 per invocation. No Tracerfy
or CBC calls — the parser TRUSTS the BV paste as a primary source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

# Ensure SiftStack `src/` is importable before the bridge call.
from deep_prospecting import _siftstack_bridge  # noqa: F401
from deep_prospecting import models as _models
from deep_prospecting import datasift_csv_writer as _writer
from deep_prospecting.phases import phase_trestle as _phase_trestle
from deep_prospecting._utils import slug as _slug

import llm_client as _llm_client  # provided by _siftstack_bridge sys.path push


logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

_HAIKU_MAX_TOKENS = 2048
_BV_SOURCE: _models.SourceID = "bv_manual"


# ── Input format handling ──────────────────────────────────────────────


class _TagStripper(HTMLParser):
    """Minimal HTML → text. Preserves line breaks for block-level tags so
    BV's "Phones" / "Emails" / "Relatives" sections stay separable in
    the extracted text. Skips <script>/<style> contents.
    """

    _BLOCK_TAGS = {
        "br", "p", "div", "li", "ul", "ol", "tr", "table", "thead",
        "tbody", "h1", "h2", "h3", "h4", "h5", "h6", "section",
        "article",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        return re.sub(r"\n{2,}", "\n\n", raw).strip()


def _detect_format(raw: str) -> str:
    """Heuristic format detection. Order matters — HTML check first
    because BV's HTML can also contain markdown-y `*` characters in
    body text."""
    head = raw[:2048].lower()
    if "<html" in head or "<body" in head or "<table" in head or "<div" in head:
        return "html"
    # Markdown is detected by structured headers / bullets in the body.
    if re.search(r"^\s*#{1,6}\s", raw, re.M) or re.search(r"^\s*[-*]\s", raw, re.M):
        return "markdown"
    return "text"


def _to_plain_text(raw: str, fmt: str) -> str:
    if fmt == "html":
        s = _TagStripper()
        try:
            s.feed(raw)
            s.close()
        except Exception:
            logger.warning("html parse failed — falling back to raw text")
            return raw
        return s.text()
    return raw


# ── Haiku extraction ───────────────────────────────────────────────────


_EXTRACTION_PROMPT = """\
You are extracting contact data from a BeenVerified-style record that an \
operator manually pasted. The record may describe one PRIMARY subject and \
several relatives. Return STRICT JSON only (no prose, no markdown) matching:

{{
  "decedent": null | {{
    "name": "First Last",
    "date_of_death": "YYYY-MM-DD" | null,
    "age_at_death": <int or null>
  }},
  "persons": [
    {{
      "name": "First Last",
      "is_primary_subject": true | false,
      "age": <int or null>,
      "relationship_to_primary": "self" | "spouse" | "ex_spouse" | "son" | "daughter" | "father" | "mother" | "brother" | "sister" | "stepchild" | "stepparent" | "cousin" | "uncle" | "aunt" | "associate" | "unknown",
      "city": "City, ST" | null,
      "phones": [
        {{ "number": "9085551234", "type": "MOBILE" | "LANDLINE" | "VOIP" | "UNKNOWN", "dnc": true | false }}
      ],
      "emails": ["name@example.com"]
    }}
  ]
}}

The `decedent` field captures probate / deceased-owner records. Fill it \
when the record explicitly states someone is deceased (DOD, "predeceased", \
"† deceased", obituary references, probate filings, etc.). Set to null \
when no death is referenced.

IMPORTANT: A decedent often has legacy phones still in service (the \
household landline, a number that may forward to the executor, etc.). \
If the record lists phones or emails for the decedent — even labeled \
"legacy" / "may still ring through" / "for reference only" — INCLUDE \
the decedent as a regular `persons[]` entry with their full contact data. \
The `decedent` block captures name + DOD; the `persons[]` entry captures \
their phones/emails. Both are needed.

Rules:
- Output ONLY the JSON. No commentary.
- Normalize phones to bare 10-digit (no punctuation, no country code).
- Drop a phone ONLY if it is structurally invalid (fewer than 10 digits, \
all zeros, repeated digits like 5555555555) OR explicitly labeled \
"disconnected / fax / business / wrong number" in the source narrative. \
A phone whose TYPE column is "UNKNOWN" is still a real phone — type=UNKNOWN \
means line-type wasn't identified, NOT that the phone is bad. KEEP these. \
Use "UNKNOWN" as the JSON "type" value when in doubt.
- Include phones flagged "low value / legacy / shared with another person" \
— the dialer needs them as backup contacts even if not primary.
- Skip phones explicitly tagged "different family / ruled out / not related" \
in the source — those are negative findings, not contact data.
- Skip persons explicitly tagged "different family / ruled out / not related" \
— do NOT emit them in the output even with empty contact lists.
- The primary subject is whoever the record is centered on — typically \
the first person profiled or the one named in section headings. Set \
"is_primary_subject": true on EXACTLY one person.
- If relationship is ambiguous, prefer "associate" over guessing.
- If a relative's name is mentioned but no phones AND no emails are listed \
for them, OMIT them from the output. We only want persons with at least \
one contact channel.
- Per-email confidence hint: if a person's email list is split into \
"Best Match" and "lower confidence" groups, ONLY emit the Best Match \
emails. Skip the lower-confidence noise — operator already filtered.

Record:
{record_text}
"""


def _extract_with_haiku(text: str, api_key: str) -> dict:
    """Call Haiku, return the parsed JSON dict, or {"persons": []} on
    failure. Trims the input to ~32KB to keep one call's cost bounded.
    """
    bounded = text[:32_000]
    payload = _llm_client.chat_json(
        _EXTRACTION_PROMPT.format(record_text=bounded),
        system="You are a precise data extractor. Output strict JSON only.",
        max_tokens=_HAIKU_MAX_TOKENS,
        api_key=api_key,
    )
    if not isinstance(payload, dict):
        logger.warning("Haiku returned non-dict for BV extraction")
        return {"persons": []}
    if "persons" not in payload or not isinstance(payload["persons"], list):
        return {"persons": []}
    return payload


# ── Pack lookup ────────────────────────────────────────────────────────


def _find_pack_dir(case_query: str, root: Path | None = None) -> Path | None:
    """Locate the per-record output dir by substring match on slug.

    Search order: most-recent date folder first, then older. Returns the
    first match. Caller responsible for disambiguating when multiple
    cases match the same query — we surface a warning.
    """
    root = root or (Path(__file__).resolve().parent / "outputs")
    if not root.exists():
        return None
    needle = _slug(case_query).lower()
    if not needle:
        return None

    date_dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        reverse=True,
    )
    matches: list[Path] = []
    for date_dir in date_dirs:
        for case_dir in date_dir.iterdir():
            if not case_dir.is_dir():
                continue
            slug = case_dir.name.lower()
            if needle in slug or slug in needle:
                matches.append(case_dir)

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "multiple pack matches for '%s' — picking most recent: %s",
            case_query, matches[0],
        )
    return matches[0]


# ── Merge ──────────────────────────────────────────────────────────────


_PHONE_DIGITS_RE = re.compile(r"\D+")


def _e164(raw: str) -> str:
    """10-digit US → '+1XXXXXXXXXX'. Falls back to digit-only when not
    NANP-shaped."""
    digits = _PHONE_DIGITS_RE.sub("", raw or "")
    if not digits:
        return ""
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _normalize_phone_type(raw: str) -> _models.PhoneType:
    t = (raw or "").strip().upper()
    if t in ("MOBILE", "LANDLINE", "VOIP", "UNKNOWN"):
        return t  # type: ignore[return-value]
    return "UNKNOWN"


def _apply_decedent_to_heir_map(
    pack_dict: dict, decedent: dict | None,
) -> bool:
    """When the BV record names a decedent (probate / deceased-owner
    case), populate pack.heir_map.decedent_name + decedent_dod so the
    star map correctly puts `*` on the deceased person.

    Conservative: only writes when the pack has no existing decedent_dod
    (the automated CLI's obit found has precedence — it has primary
    source observability). Returns True when the heir_map was updated.
    """
    if not decedent:
        return False
    name = (decedent.get("name") or "").strip()
    if not name:
        return False
    # heir_map may be explicitly null in results.json (L1 pack), so
    # setdefault doesn't help — read-then-replace pattern instead.
    hm = pack_dict.get("heir_map") or {}
    pack_dict["heir_map"] = hm
    # Don't clobber a CLI-found decedent that already has a parseable DOD.
    if hm.get("decedent_dod"):
        return False

    hm["decedent_name"] = name
    dod = (decedent.get("date_of_death") or "").strip()
    if dod:
        hm["decedent_dod"] = dod
        hm["decedent_dod_text"] = dod
    # Heir list stays whatever it was (probably empty). BV-surfaced
    # children + grandchildren are folded into skip_trace.phones for
    # star derivation; promoting them to typed Heir entries would
    # require more structural work.
    hm.setdefault("heirs", [])
    hm.setdefault("generations_searched", 0)
    return True


def _merge_bv_into_pack(
    pack_dict: dict,
    extracted: dict,
) -> tuple[int, int, list[str], bool]:
    """Mutate `pack_dict` in place with BV persons. Returns
    (phones_added, emails_added, person_names_seen, decedent_applied).

    Dedup: by E.164 phone / lowercased email against existing entries.
    New phones / emails carry sources=["bv_manual"] and the
    person_name field for star derivation. DNC flag rides through to
    the Phone entry so downstream Trestle scoring + dial-rank labeling
    respects the operator's data.
    """
    persons = extracted.get("persons") or []
    decedent_applied = _apply_decedent_to_heir_map(
        pack_dict, extracted.get("decedent"),
    )

    skip = pack_dict.setdefault("skip_trace", {})
    phones = skip.setdefault("phones", [])
    emails = skip.setdefault("emails", [])

    existing_phones = {(p.get("number") or "").strip() for p in phones}
    existing_phones.discard("")
    existing_emails = {
        (e.get("address") or e.get("email") or "").strip().lower()
        for e in emails
    }
    existing_emails.discard("")

    phones_added = 0
    emails_added = 0
    persons_seen: list[str] = []

    for person in persons:
        name = (person.get("name") or "").strip()
        if not name:
            continue
        persons_seen.append(name)

        for ph in person.get("phones") or []:
            if not isinstance(ph, dict):
                ph = {"number": ph}
            raw_number = ph.get("number")
            e164 = _e164(str(raw_number or ""))
            if not e164 or e164 in existing_phones:
                continue
            ptype = _normalize_phone_type(ph.get("type") or "UNKNOWN")
            phones.append({
                "number": e164,
                "type": ptype,
                "sources": [_BV_SOURCE],
                "confidence": "MEDIUM",  # operator-curated, not auto-verified
                "person_name": name,
                "activity_score": None,
                # DNC is a load-bearing operator signal — if BV / the
                # operator flagged a phone as DNC, downstream tooling
                # (Trestle scoring, dial-rank labeling) must respect it.
                "dnc": bool(ph.get("dnc", False)),
                "carrier": "",
                "is_litigator": False,
            })
            existing_phones.add(e164)
            phones_added += 1

        for em in person.get("emails") or []:
            em_str = (em or "").strip()
            if not em_str or em_str.lower() in existing_emails:
                continue
            # Email model only has (address, sources) — no person_name field.
            # Person attribution is preserved via the matching phone's
            # person_name, which is sufficient for the star map.
            emails.append({
                "address": em_str,
                "sources": [_BV_SOURCE],
            })
            existing_emails.add(em_str.lower())
            emails_added += 1

    return phones_added, emails_added, persons_seen, decedent_applied


# ── Overlay row update ──────────────────────────────────────────────────


def _reoverlay_csv_row(
    pack: _models.ResearchPack,
    overlay_csv: Path,
) -> dict:
    """Re-overlay the pack onto its CSV row. Phones already in the row
    are skipped by the writer's dedup; new BV phones drop into the next
    empty slot with the refreshed star map.
    """
    return _writer.overlay(overlay_csv, overlay_csv, [pack])


# ── Outputs ─────────────────────────────────────────────────────────────


def _bv_outcome_tag() -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return f"BV Manual Pass {today}"


def _write_back(pack_dir: Path, pack_dict: dict) -> None:
    (pack_dir / "results.json").write_text(
        json.dumps(pack_dict, indent=2, default=str), encoding="utf-8"
    )


def _rerun_trestle_on_new_phones(pack: _models.ResearchPack) -> tuple[int, float]:
    """Score the unscored BV phones via Trestle Phone Intel. Returns
    (phones_scored, cost_usd). Quiet no-op when TRESTLE_API_KEY is
    missing or all new phones are DNC-flagged.

    DNC-flagged phones are NOT scored: the operator's DNC signal
    overrides automated dial-rank derivation, and there's no point
    spending $0.015/phone on a number the operator can't dial.
    """
    if not pack.skip_trace:
        return 0, 0.0
    new_phones = [
        p for p in pack.skip_trace.phones
        if _BV_SOURCE in p.sources
        and p.activity_score is None
        and not p.dnc  # skip DNC-flagged numbers
    ]
    if not new_phones:
        return 0, 0.0

    # Reuse Phase Trestle's per-phone scoring loop. It accepts a stub
    # pack and rescores phones in place. Cleanest path: call its run()
    # which already handles missing keys + retries.
    pt = asyncio.run(_phase_trestle.run(pack))
    if pt is None:
        return 0, 0.0
    updated_pack, _checks, cost = pt
    # Replace the pack's skip_trace with the rescored one so the caller
    # writes back the updated activity_scores.
    if updated_pack and updated_pack.skip_trace:
        pack.skip_trace.phones = updated_pack.skip_trace.phones
    return len(new_phones), float(cost.trestle or 0.0)


# ── Public entry ───────────────────────────────────────────────────────


def run(case: str, input_path: Path) -> dict:
    """Parse + merge BV record. Returns a result summary dict."""
    api_key = __import__("os").environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}

    raw = input_path.read_text(encoding="utf-8", errors="replace")
    fmt = _detect_format(raw)
    text = _to_plain_text(raw, fmt)
    logger.info("parse-bv: detected format=%s, %d chars", fmt, len(text))

    extracted = _extract_with_haiku(text, api_key)
    if not extracted.get("persons"):
        return {
            "ok": False,
            "error": "Haiku extraction returned no persons",
            "format": fmt,
            "input_length": len(text),
        }

    pack_dir = _find_pack_dir(case)
    if pack_dir is None:
        return {
            "ok": False,
            "error": f"no pack found matching '{case}' under deep_prospecting/outputs/",
            "format": fmt,
            "extracted_persons": [p.get("name") for p in extracted["persons"]],
        }

    results_path = pack_dir / "results.json"
    if not results_path.exists():
        return {
            "ok": False,
            "error": f"pack dir found but results.json missing: {pack_dir}",
        }
    pack_dict = json.loads(results_path.read_text(encoding="utf-8"))

    phones_added, emails_added, persons_seen, decedent_applied = (
        _merge_bv_into_pack(pack_dict, extracted)
    )

    # Re-hydrate into a typed ResearchPack so the writer + Trestle can
    # consume it. Pydantic v2 model_validate handles the dict round-trip.
    pack = _models.ResearchPack.model_validate(pack_dict)

    phones_scored, trestle_cost = _rerun_trestle_on_new_phones(pack)

    # Persist back as a dict (pydantic → JSON) so future re-runs see
    # the merged state.
    _write_back(pack_dir, json.loads(pack.model_dump_json()))

    # Re-overlay the CSV row if the overlay file exists in the pack's
    # date folder.
    overlay_csv = pack_dir.parent / "sample_export_overlay.csv"
    overlay_result = {}
    if overlay_csv.exists():
        overlay_result = _reoverlay_csv_row(pack, overlay_csv)

    return {
        "ok": True,
        "format": fmt,
        "pack_dir": str(pack_dir),
        "persons_seen": persons_seen,
        "phones_added": phones_added,
        "emails_added": emails_added,
        "decedent_applied": decedent_applied,
        "phones_scored_by_trestle": phones_scored,
        "trestle_cost_usd": trestle_cost,
        "overlay_result": overlay_result,
        "bv_outcome_tag": _bv_outcome_tag(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deep_prospecting parse-bv")
    parser.add_argument("--case", required=True,
                        help="Slug, address, or owner name to match against an existing pack")
    parser.add_argument("--input", required=True, type=Path,
                        help="Path to BV record (HTML / markdown / text)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=(logging.DEBUG if args.verbose else logging.INFO),
        format="%(levelname)s %(name)s %(message)s",
    )

    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        return 2

    result = run(args.case, args.input)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
