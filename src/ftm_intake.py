"""The intake screen for notices (build plan 1.11) and the notice text research reads (3.3).

A county record has always passed a buy box before it was uploaded (knox_ftm_pull): single
family, a value band, the debt weighed against the equity. A NOTICE never did. A foreclosure on a
warehouse, a probate on a mobile home lot and a house worth two million all went to a caller, and
none of them carried the figures a caller needs to judge the lead.

What this adds to a notice at upload, each part behind its own switch and all of them OFF until
asked for:

  FTM_INTAKE_SCREEN=1
      Single family and value band, read off SiftMap. A record is screened OUT only on a POSITIVE
      verdict (SiftMap names another property type, or a value outside the band). "Not found" and
      "lookup failed" are KEPT: the Smarty lapse taught that a vendor outage must never empty a
      feed, and a missed match is not a finding about the house. Every record left out is written
      to a rejects file with its reason, never dropped silently.
      Also the figures: estimated equity, total delinquency (county tax), debt, and the listing
      status. And a foreclosure with no sale date gets a tag so someone looks the date up, because
      research skips a foreclosure that has none.

  FTM_NOTICE_TEXT=field | notes
      The full notice text goes on the record. As a custom field by preference: research already
      reads every custom field with a value, nothing else parses fields, and a republished notice
      overwrites the text instead of stacking another copy. The notes are parsed by the sold
      guard, the research gates and the lien release check and are shown to callers, so thousands
      of characters of legal text do not belong there. "notes" exists for an account that has no
      such field yet.

  FTM_TAG_APPEARED_AGAIN=1
      A record that already existed when a NEW filing arrives is tagged Appeared Again (the
      teaching: keep the record, keep its history, mark that it showed up again). "New filing" is
      the hard part. The county stage re-sends the same condemnation rows every morning and a
      foreclosure republishes three weeks running, so "the record exists" alone would tag
      everything within a week. The ledger here remembers every (property, list) pair this
      pipeline has sent and when; a pair counts as appearing again only when it has not been sent
      for FTM_APPEARED_AGAIN_GAP_DAYS (180), and the ledger is seeded from the upload files
      already on the volume so day one does not tag everything we uploaded before it began.

Tuning (read only when the screen is on):
  FTM_INTAKE_VALUE_MIN=1  FTM_INTAKE_VALUE_MAX=700000   the value band ($1 floor on purpose: condemned
                                                         and tax-distressed houses fall under $100K)
  FTM_INTAKE_TYPES=single_family_residence              SiftMap property types that pass
  FTM_INTAKE_DROP_UPSIDE_DOWN=0                         1 leaves out a record whose debt swallows its equity
  FTM_INTAKE_MAX_LOOKUPS=200                            SiftMap lookups in one run
  FTM_INTAKE_NO_SALE_DATE_TAG="Sale Date Needed"        "" disables the tag
  FTM_INTAKE_LISTED_TAG="Listed"                        "" disables the tag
  FTM_NOTICE_TEXT_MAX=6000                              characters of notice text kept
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftm_intake")

_TRUE = ("1", "true", "yes", "on")
SFR = "single_family_residence"

# Columns this module can add to an upload row. The uploader maps each to a custom field by
# label (see datasift_api_upload.field_map); a label the account does not have is skipped with a
# warning, never guessed.
EXTRA_COLUMNS = [
    "Est Equity", "Total Delinquency", "Debt vs Equity Pct", "Upside Down", "Upside Down Reason",
    "Mortgage Debt", "Listing Status", "Notice Text",
]

_LISTED = ("active", "listed", "pending", "contingent", "under contract", "coming soon", "for sale")
_PRINCIPAL = re.compile(
    r"(?:original\s+)?principal\s+(?:amount|balance|sum|indebtedness)\s+of\s+\$\s*([\d,]+(?:\.\d\d)?)", re.I)


def _on(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUE


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def enabled() -> bool:
    return _on("FTM_INTAKE_SCREEN")


def notice_text_mode() -> str:
    mode = (os.environ.get("FTM_NOTICE_TEXT") or "").strip().lower()
    return mode if mode in ("field", "notes") else "off"


def appeared_again_on() -> bool:
    return _on("FTM_TAG_APPEARED_AGAIN")


def appeared_again_tag() -> str:
    return (os.environ.get("FTM_APPEARED_AGAIN_TAG") or "Appeared Again").strip()


def allowed_types() -> set[str]:
    raw = os.environ.get("FTM_INTAKE_TYPES") or SFR
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def _tag_setting(name: str, default: str) -> str:
    got = os.environ.get(name)
    return default if got is None else got.strip()


# ── the notice's own facts (no network) ───────────────────────────────


def notice_text(notice, limit: int | None = None) -> str:
    """The notice body as one clean block, cut to the configured length.

    Whitespace is collapsed per line and blank runs are squeezed, so a PDF's narrow column does
    not spend the budget on line breaks. Cut on a word boundary and marked when cut, so nobody
    reads a truncated notice as the whole one."""
    limit = int(limit or _num("FTM_NOTICE_TEXT_MAX", 6000))
    lines = [" ".join(ln.split()) for ln in (getattr(notice, "raw_text", "") or "").splitlines()]
    text = "\n".join(ln for ln in lines if ln).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    i = cut.rfind(" ")
    return (cut[:i] if i > limit * 0.8 else cut).rstrip() + " [cut at %d characters]" % limit


def stated_principal(text: str) -> float | None:
    """The original loan amount a foreclosure notice states in words, when it states one."""
    m = _PRINCIPAL.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value >= 1000 else None


def needs_sale_date(notice) -> bool:
    """A foreclosure with no sale date parsed. Research skips it ("no sale date on record") and
    the hot auction lane cannot see it, so it needs a person to read the date off the notice."""
    return (getattr(notice, "notice_type", "") == "foreclosure"
            and not (getattr(notice, "auction_date", "") or "").strip())


def listing_status(notice, detail: dict | None) -> str:
    """"listed", "pending", "off market" or "" (not stated).

    An MLS feed is the first-party source for a listing, so the Zillow read on the notice wins
    when the enrichment ran; SiftMap's own status key is the fallback. Nothing is inferred from a
    sale history."""
    raw = (getattr(notice, "mls_status", "") or "").strip().lower()
    if not raw and isinstance(detail, dict):
        for key in ("mls_status", "listing_status", "mls"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                raw = value.strip().lower().replace("_", " ")
                break
    if not raw:
        return ""
    if "pending" in raw or "contingent" in raw or "under contract" in raw:
        return "pending"
    if raw in ("not listed", "off market", "sold", "closed", "expired", "withdrawn", "cancelled", "canceled"):
        return "off market"
    if any(word in raw for word in _LISTED):
        return "listed"
    return ""


# ── SiftMap lookup ────────────────────────────────────────────────────


def _pick(hits: list, address: str, county: str) -> dict | None:
    """The autocomplete hit that is provably this street in this county. The first hit is NOT
    taken on trust: the search returns the same street name in another town without blinking.

    The test itself is address_fallback.pick_hit, the one the ZIP fallback already runs on every
    probate record, asked one hit at a time because this caller needs the hit (it carries the
    property id) and that function returns only the parsed address."""
    from address_fallback import pick_hit

    for hit in hits or []:
        if pick_hit([hit], address, county):
            return hit
    return None


def lookup(client, notice) -> tuple[dict | None, str]:
    """(SiftMap detail, "") for one notice, or (None, why there is none)."""
    import ftm_budget
    from knox_ftm_pull import normalize_address

    address = (getattr(notice, "address", "") or "").strip()
    if not address:
        return None, "no address"
    query = "%s, %s, TN" % (normalize_address(address), (getattr(notice, "city", "") or "").strip())
    try:
        ftm_budget.acquire("siftmap")
        hit = _pick(client.autocomplete(query, limit=5), address, getattr(notice, "county", ""))
        if not hit or not (hit.get("id") or hit.get("dataflik_id")):
            return None, "not found on SiftMap"
        ftm_budget.acquire("siftmap")
        detail = client.get_detail(hit.get("id") or hit.get("dataflik_id"))
    except ftm_budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (a lookup that fails keeps the record, it never drops it)
        return None, "lookup failed (%s)" % type(exc).__name__
    return (detail, "") if isinstance(detail, dict) and detail else (None, "not found on SiftMap")


def verdict(detail: dict | None, types: set[str], vmin: float, vmax: float) -> str:
    """"" when the record may be uploaded, else the positive reason it is outside the buy box.

    None, a missing type and a missing value all pass: unknown is not a reason to lose a lead."""
    if not isinstance(detail, dict):
        return ""
    ptype = str(detail.get("property_type") or "").strip().lower()
    if ptype and ptype not in types:
        return "not single family (%s)" % ptype.replace("_", " ")
    value = detail.get("total_market_value")
    try:
        value = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        value = None
    if value is not None and not (vmin <= value <= vmax):
        return "value outside the buy box"
    return ""


def figures(notice, detail: dict | None) -> dict:
    """The money a caller judges a lead by, as upload columns. Blank means not known.

    A foreclosure is the one record where "no mortgage on file" cannot mean free and clear: the
    notice exists because a deed of trust is in default. So when SiftMap shows no loan on a
    foreclosure, equity is left blank instead of being stated as the whole value."""
    out: dict = {}
    text = getattr(notice, "raw_text", "") or ""
    debt, debt_source = None, ""
    if isinstance(detail, dict):
        from knox_ftm_pull import _equity_block, _mortgage_on_parcel

        try:
            tax = float(getattr(notice, "tax_delinquent_amount", "") or 0) or None
        except ValueError:
            tax = None
        block = _equity_block({"sm": detail, "tax_due": tax, "lien_total": None})
        assumed_clear = block.pop("_assumed_free_clear", False)
        foreclosure = getattr(notice, "notice_type", "") == "foreclosure"
        if foreclosure and assumed_clear:
            block.update({"Est Equity": "", "Debt vs Equity Pct": "", "Upside Down": "", "Upside Down Reason": ""})
        else:
            mortgage = _mortgage_on_parcel(detail)
            if mortgage and mortgage > 0:
                debt, debt_source = float(mortgage), "the loan SiftMap has on record"
        out.update({k: v for k, v in block.items() if v not in ("", None)})
    if debt is None:
        stated = stated_principal(text)
        if stated:
            debt, debt_source = stated, "the original principal the notice states"
    if debt is not None:
        out["Mortgage Debt"] = round(debt, 2)
        out["_debt_source"] = debt_source
    return out


def screen(notices: list, force: bool = False, client=None, today: str | None = None) -> dict:
    """Screen a run's notices and build their extra columns.

    Returns {"kept": [...], "extras": {id(notice): {column: value, "_tags": [...]}},
             "dropped": {reason: count}, "rejected": [(notice, reason)], "warnings": [...],
             "looked_up": n, "not_verified": n}.

    Does nothing (everything kept, no extras) unless the screen is on or `force` is set."""
    out = {"kept": list(notices), "extras": {}, "dropped": {}, "rejected": [], "warnings": [],
           "looked_up": 0, "not_verified": 0}
    if not (force or enabled()) or not notices:
        return out
    if client is None:
        try:
            from siftmap_standalone import SiftMapClient
            client = SiftMapClient()
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append("Intake screen did not run (SiftMap client unavailable: %s). Nothing was screened."
                                   % type(exc).__name__)
            return out

    import ftm_budget

    types, vmin, vmax = allowed_types(), _num("FTM_INTAKE_VALUE_MIN", 1.0), _num("FTM_INTAKE_VALUE_MAX", 700000.0)
    drop_upside = _on("FTM_INTAKE_DROP_UPSIDE_DOWN")
    cap = int(_num("FTM_INTAKE_MAX_LOOKUPS", 200))
    sale_tag = _tag_setting("FTM_INTAKE_NO_SALE_DATE_TAG", "Sale Date Needed")
    listed_tag = _tag_setting("FTM_INTAKE_LISTED_TAG", "Listed")
    kept: list = []
    no_sale_date = 0
    for n in notices:
        detail, why_none = None, "the day's lookup cap was reached"
        if out["looked_up"] < cap:
            try:
                detail, why_none = lookup(client, n)
            except ftm_budget.BudgetExhausted as exc:
                out["warnings"].append("Intake screen stopped early: %s. The rest were kept unscreened." % exc)
                cap = 0
                detail, why_none = None, "the request budget ran out"
            out["looked_up"] += 1
        why = verdict(detail, types, vmin, vmax)
        extra = figures(n, detail)
        if not why and drop_upside and extra.get("Upside Down") == "yes":
            why = "debt is larger than the equity"
        if why:
            out["dropped"][why] = out["dropped"].get(why, 0) + 1
            out["rejected"].append((n, why))
            continue
        if detail is None:
            out["not_verified"] += 1
        tags: list[str] = []
        status = listing_status(n, detail)
        if status:
            extra["Listing Status"] = status
            if status in ("listed", "pending") and listed_tag:
                tags.append(listed_tag)
        if needs_sale_date(n):
            no_sale_date += 1
            if sale_tag:
                tags.append(sale_tag)
        if tags:
            extra["_tags"] = tags
        extra["_note"] = intake_note(n, detail, extra, why_none)
        out["extras"][id(n)] = extra
        kept.append(n)
    out["kept"] = kept
    if no_sale_date:
        out["warnings"].append("%d foreclosure(s) carry no sale date and were tagged for a look-up." % no_sale_date)
    if out["not_verified"] and out["not_verified"] == len(notices) and len(notices) >= 5:
        out["warnings"].append("Intake screen verified none of %d records on SiftMap, so nothing was screened out. "
                               "Check the SiftMap key before trusting today's upload." % len(notices))
    return out


def intake_note(notice, detail: dict | None, extra: dict, why_none: str) -> str:
    """One line for the record's notes saying what intake found. Kept to one line with no bare
    date on it, because other code reads dates out of the notes."""
    bits = []
    if isinstance(detail, dict):
        ptype = str(detail.get("property_type") or "").replace("_", " ")
        if ptype:
            bits.append(ptype)
        value = detail.get("total_market_value")
        if value not in (None, ""):
            try:
                bits.append("value $%s" % format(float(value), ",.0f"))
            except (TypeError, ValueError):
                pass
    else:
        bits.append("type and value not verified (%s)" % why_none)
    if extra.get("Est Equity") not in ("", None):
        bits.append("est. equity $%s" % format(float(extra["Est Equity"]), ",.0f"))
    if extra.get("Mortgage Debt"):
        bits.append("debt $%s (%s)" % (format(float(extra["Mortgage Debt"]), ",.0f"), extra.get("_debt_source", "")))
    if extra.get("Listing Status"):
        bits.append("listing: %s" % extra["Listing Status"])
    if needs_sale_date(notice):
        bits.append("no sale date in the notice")
    return "Intake check: " + ", ".join(bits) + "." if bits else ""


def text_extras(notices: list, extras: dict | None = None) -> dict:
    """Add the notice text to each notice's extras when FTM_NOTICE_TEXT is on. Returns extras."""
    extras = extras if extras is not None else {}
    if notice_text_mode() == "off":
        return extras
    for n in notices:
        text = notice_text(n)
        if text:
            extras.setdefault(id(n), {})["Notice Text"] = text
    return extras


def apply_extras(row: dict, extra: dict | None) -> dict:
    """Fold one notice's extras into its upload row: columns set, tags appended, the note put FIRST.

    Keys that start with an underscore are the screen's own working values and never become columns.
    """
    if not extra:
        return row
    for key, value in extra.items():
        if not key.startswith("_"):
            row[key] = value
    tags = [t for t in (extra.get("_tags") or []) if t]
    if tags:
        have = [t.strip() for t in str(row.get("Tags") or "").split(",") if t.strip()]
        row["Tags"] = ",".join(have + [t for t in tags if t not in have])
    note = extra.get("_note")
    if note:
        # Leads the notes so the uploader's length cap can never cut it off.
        rest = str(row.get("Notes") or "").strip()
        row["Notes"] = note + ((" | " + rest) if rest else "")
    return row


def write_csv(notices: list, extras: dict, filename: str, out_dir: Path | None = None) -> Path:
    """The core notice upload file with intake columns. Same rows as write_datasift_csv, plus the
    extras; used only when an intake switch is on, so the default file is untouched."""
    import config
    from datasift_formatter import DATASIFT_COLUMNS, _build_row

    path = (out_dir or config.OUTPUT_DIR) / filename
    columns = DATASIFT_COLUMNS + [c for c in EXTRA_COLUMNS if c not in DATASIFT_COLUMNS]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for n in notices:
            w.writerow(apply_extras(_build_row(n), extras.get(id(n))))
    return path


def write_rejects(rejected: list, path: Path) -> int:
    """Every record the screen left out, with its reason. A shrinking list is never silent."""
    if not rejected:
        return 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["notice_type", "county", "address", "city", "zip", "source_url", "reason"])
        for n, why in rejected:
            w.writerow([getattr(n, "notice_type", ""), getattr(n, "county", ""), getattr(n, "address", ""),
                        getattr(n, "city", ""), getattr(n, "zip", ""), getattr(n, "source_url", ""), why])
    return len(rejected)


# ── appeared again ────────────────────────────────────────────────────


class AppearedLedger:
    """Every (property, list) pair this pipeline has sent, and the day it last sent it.

    should_tag() answers one question for a record DataSift says already exists: is this filing
    new to us? It is when we have never sent this property to this list, or have not for `gap`
    days. A daily re-send and a weekly republication keep refreshing the pair, so they never tag."""

    FILE = "ftm_appeared_ledger.json"
    PREFIXES = ("ftm_notices", "knox_ftm_pull", "ftm_feed")

    def __init__(self, state_dir: Path | None = None, out_dir: Path | None = None, today: str | None = None):
        if state_dir is None or out_dir is None:
            import config
            state_dir, out_dir = state_dir or config.STATE_DIR, out_dir or config.OUTPUT_DIR
        self.path = Path(state_dir) / self.FILE
        self.out_dir = Path(out_dir)
        self.today = today or date.today().isoformat()
        try:
            self.gap = max(1, int(os.environ.get("FTM_APPEARED_AGAIN_GAP_DAYS") or 180))
        except ValueError:
            self.gap = 180
        self.pairs: dict = {}
        self.seeded = ""
        self._dirty = False
        self._load()

    @staticmethod
    def key(street: str, zipcode: str, list_name: str) -> str:
        """One property on one list. "Street" and "St" are the same house: DataSift standardizes
        the street on ingest, so two spellings of one address reach the same record."""
        s = ""
        try:
            from address_fallback import street_key
            number, tokens = street_key(street or "")
            if number:
                s = "%s %s" % (number, " ".join(sorted(tokens)))
        except Exception:  # noqa: BLE001
            s = ""
        if not s:
            s = " ".join(re.sub(r"[^A-Za-z0-9 ]", " ", street or "").lower().split())
        return "%s|%s|%s" % (s, (zipcode or "").strip()[:5], (list_name or "").strip().lower())

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.pairs = dict(data.get("pairs") or {})
            self.seeded = str(data.get("seeded") or "")
        except (OSError, ValueError):
            self.pairs, self.seeded = {}, ""
        if not self.seeded:
            self.seed()

    def seed(self) -> int:
        """First use only: absorb every upload file already on the volume, dated by the file, so
        a record we created last month is not announced as appearing again today."""
        n = 0
        for prefix in self.PREFIXES:
            for p in sorted(glob.glob(str(self.out_dir / (prefix + "_*.csv")))):
                if p.endswith(("_upside_down.csv", "_rejected.csv")):
                    continue
                m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(p))
                if not m or m.group(1) >= self.today:
                    continue
                try:
                    with open(p, encoding="utf-8-sig", newline="") as fh:
                        for row in csv.DictReader(fh):
                            for lst in str(row.get("Lists") or "").split(","):
                                k = self.key(row.get("Property Street Address", ""), row.get("Property ZIP Code", ""), lst)
                                if k.startswith("|") or not lst.strip():
                                    continue
                                if self.pairs.get(k, "") < m.group(1):
                                    self.pairs[k] = m.group(1)
                                    n += 1
                except OSError:
                    continue
        self.seeded = datetime.now().isoformat(timespec="seconds")
        self._dirty = True
        return n

    def should_tag(self, street: str, zipcode: str, lists) -> bool:
        """True when at least one of the row's lists is new for this property (or has been silent
        for the gap). Call only for a record DataSift reported as already existing."""
        cutoff = (date.fromisoformat(self.today) - timedelta(days=self.gap)).isoformat()
        for lst in lists or []:
            k = self.key(street, zipcode, lst)
            if k.startswith("|") or not str(lst).strip():
                continue
            last = self.pairs.get(k, "")
            if not last or last < cutoff:
                return True
        return False

    def record(self, street: str, zipcode: str, lists) -> None:
        for lst in lists or []:
            k = self.key(street, zipcode, lst)
            if k.startswith("|") or not str(lst).strip():
                continue
            if self.pairs.get(k) != self.today:
                self.pairs[k] = self.today
                self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        tmp = Path(str(self.path) + ".tmp")
        tmp.write_text(json.dumps({"pairs": self.pairs, "seeded": self.seeded}), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False
