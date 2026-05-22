"""Phase 2d — equity estimator → **lien/encumbrance sweep** for KY probate.

Computes ``estimated_equity`` + ``equity_percent`` + ``lien_flags`` from the
Jefferson PVA assessed value (Phase 2a) and a FULL-history deed/lien scan of
Jefferson County Deeds (Phase 2b helpers).

Why a sweep, not a subtraction (128-case review, pattern #4): binary
"free-and-clear" is wrong ~36% of the time — ≈46/128 cases carried distress
liens a single most-recent-mortgage check missed:
  * HECM / reverse mortgages — due-and-payable on death, negative-amortizing;
    a straight-line balance estimate is wrong (Wheatley, Herflicker).
  * State / judgment / credit-card liens + lis pendens (Presley, Logsdon).
  * Tax-certificate / code-violation liens — can exceed value on a low-end
    home → NEGATIVE equity on a "mortgage-free" house (Walker, Thompson-Hale).
  * Releases hidden past the first page — a mortgage with no release in the
    first window was wrongly assumed open (Murphy, Mudd-Francis, Perrin). The
    sweep walks the ENTIRE deed history so a late release is netted.
  * Medicaid / MERP estate-recovery risk — proxied for free via a DMS noticed
    party (Duckworth) or an elder-law / Medicaid-specialist estate attorney
    (Jenkins-Ruley, Underwood, Duckworth-Bullock).

Each encumbrance type sets its own discrete flag in ``lien_flags``
(``open_mortgage;hecm;judgment;lis_pendens;tax_cert;medicaid``). INVARIANT
(locked): a record with any flag set can NEVER report 100% free-and-clear.

Policy carried over from the pre-sweep estimator:
  * Only runs for KY records. TN records already get equity from
    ``property_enricher`` via Zillow — don't overwrite.
  * Skips records where equity is already populated (idempotent).
  * Gated on ``property_owner_status`` — equity only for properties still
    held by the decedent, estate, trust, or a recent-transfer heir.
  * Fallback: when there is NO mortgage signal AND the scan found NO liens
    AND no flags, fall to an ``assessed × 0.85`` floor — but NEVER apply the
    floor (or full equity) when any flag is set.

Resilience: ``scan_liens`` is the ONLY network-bearing function — it is
try/except guarded and returns ``[]`` on any failure so equity still computes
from existing fields when JCD is down. The pure helpers (``_classify_lien``,
``_has_medicaid_signal``, ``_net_encumbrances``) do no I/O and are unit-tested
via the injectable ``records=`` path of ``estimate_equity`` (no network).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

# Reuse the existing deed-history pipeline rather than re-implementing it.
# DeedRecord/_classify/_parse_money/_amortized_balance/_choose_active_mortgage
# are the same primitives Step 3d uses; the network helpers drive the live scan.
from jefferson_deeds_scraper import (
    DeedRecord,
    _accept_disclaimer,
    _amortized_balance,
    _choose_active_mortgage,
    _classify,
    _fetch_deed_list,
    _fetch_pdetail,
    _make_opener,
    _parse_deed_list,
    _parse_money,
    _pick_best_name_match,
    _search_names_unique,
)

if TYPE_CHECKING:
    from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Lien-type classification regexes ──────────────────────────────────────
# ReDoS-safe (T-03-01): every pattern is a flat alternation of literal tokens
# anchored on \b with fixed \s+ between words — no nested quantifiers, no `.*`
# between tokens, no backtracking ambiguity. Applied to untrusted scraped
# doc_type/grantor/grantee strings, so boundedness matters.

# HECM / reverse mortgage. Matched against a mortgage record's doc_type +
# lender (grantee). HECM is due-and-payable on death and negative-amortizing —
# it is flagged but NEVER straight-line amortized to a balance.
HECM_RE = re.compile(
    r"\b(HECM|REVERSE\s+MORTGAGE|HOME\s+EQUITY\s+CONVERSION|HUD\s+MORTGAGE"
    r"|FINANCIAL\s+FREEDOM|AMERICAN\s+ADVISORS\s+GROUP|REVERSE)\b",
    re.IGNORECASE,
)

# State / judgment / credit-card / collection liens.
JUDGMENT_RE = re.compile(
    r"\b(JUDGMENT|STATE\s+LIEN|TAX\s+LIEN\s+STATE|CHILD\s+SUPPORT|UCC"
    r"|CREDIT|COLLECTION)\b",
    re.IGNORECASE,
)

# Lis pendens / notice of pending litigation.
LIS_PENDENS_RE = re.compile(
    r"\b(LIS\s+PENDENS|NOTICE\s+OF\s+(?:PENDING\s+)?(?:ACTION|SUIT)"
    r"|PENDING\s+LITIGATION)\b",
    re.IGNORECASE,
)

# Tax-certificate / certificate-of-delinquency / code-enforcement / municipal
# / demolition liens. These can exceed value on a low-end home.
TAX_CERT_RE = re.compile(
    r"\b(TAX\s+CERTIFICATE|CERTIFICATE\s+OF\s+DELINQUENCY"
    r"|CODE\s+(?:ENFORCEMENT|VIOLATION)|MUNICIPAL\s+LIEN|DEMOLITION\s+LIEN)\b",
    re.IGNORECASE,
)

# Elder-law / Medicaid-specialist estate-attorney heuristic. Duckworth's
# attorney was Linda Bullock at KY Elder Law — BULLOCK + the firm token are
# included. Literal/extensible; a heuristic flag, not a paid lookup.
ELDER_LAW_ATTORNEY_RE = re.compile(
    r"\b(ELDER\s+LAW|MEDICAID|KY\s+ELDER\s+LAW|BULLOCK|ELDER\s+CARE)\b",
    re.IGNORECASE,
)

# Stable flag-emission order (Phase 4 reads lien_flags positionally-agnostic,
# but a deterministic order keeps tests + CSVs stable).
_FLAG_ORDER = ["open_mortgage", "hecm", "judgment", "lis_pendens", "tax_cert", "medicaid"]

# Conservative non-zero haircut for a real lien with no parseable dollar
# figure. We must never net $0 for a known lien (that would leave equity
# untouched and defeat the flag). Depresses equity below 100% so the flag has
# teeth even when the amount is unknown.
_UNKNOWN_LIEN_HAIRCUT = 1

# Equity ceiling when a flag is set but the netting produced no dollar haircut
# (medicaid-only, or an HECM whose original amount is unknown). Enforces the
# never-100%-when-flagged invariant while still signalling "mostly equity,
# but encumbered — verify".
_FLAGGED_NO_DOLLAR_CEILING = 90.0


def _classify_lien(doc_type: str, counterparty: str) -> str:
    """Bucket a record by lien type. Returns one of
    ``"hecm"`` / ``"judgment"`` / ``"lis_pendens"`` / ``"tax_cert"`` / ``""``.

    Tests the regexes (HECM first) against the joined
    ``doc_type + " " + counterparty`` string. Used to flag a mortgage as HECM
    and to bucket non-mortgage / non-deed / non-release lien records. Pure —
    no I/O.
    """
    blob = f"{doc_type or ''} {counterparty or ''}"
    if HECM_RE.search(blob):
        return "hecm"
    if LIS_PENDENS_RE.search(blob):
        return "lis_pendens"
    if TAX_CERT_RE.search(blob):
        return "tax_cert"
    if JUDGMENT_RE.search(blob):
        return "judgment"
    return ""


def _has_medicaid_signal(notice: "NoticeData") -> bool:
    """True if the case carries a free Medicaid/MERP risk signal.

    Two free signals (locked decision 4 — no paid lookup):
      1. ``"DMS"`` appears as a token in the pipe-separated
         ``courtnet_party_types`` (the Cabinet for Health & Family Services /
         Dept for Medicaid Services noticed as a party — Duckworth).
      2. ``estate_attorney_name`` matches the elder-law heuristic
         (Jenkins-Ruley, Underwood, Duckworth-Bullock).
    Pure — no I/O. Wrapped in try/except returning False on any odd input.
    """
    try:
        party_types = getattr(notice, "courtnet_party_types", "") or ""
        tokens = {t.strip().upper() for t in party_types.split("|") if t.strip()}
        if "DMS" in tokens:
            return True
        attorney = getattr(notice, "estate_attorney_name", "") or ""
        if attorney and ELDER_LAW_ATTORNEY_RE.search(attorney):
            return True
    except Exception:  # noqa: BLE001 — defensive, never let a bad string crash equity
        return False
    return False


def _record_amount(rec: DeedRecord, amounts: dict[str, int] | None) -> int:
    """Best-effort dollar figure for a deed/lien record.

    Resolution order:
      1. A live-scan ``amounts`` map keyed by ``instnum`` (filled by
         ``scan_liens`` from ``_fetch_pdetail``).
      2. An optional ``amount`` attribute on the record (tests set this so the
         suite needs no network and no pdetail fetch).
      3. ``_parse_money`` over the record's free-text fields (book_page /
         legal_desc sometimes embed a dollar figure).
    Returns 0 when nothing parseable is found (caller decides the placeholder).
    """
    if amounts and rec.instnum in amounts:
        return amounts[rec.instnum]
    attr = getattr(rec, "amount", None)
    if attr is not None:
        return _parse_money(str(attr))
    for field_val in (rec.book_page, rec.legal_desc, rec.doc_type):
        parsed = _parse_money(field_val or "")
        if parsed > 0:
            return parsed
    return 0


def _net_encumbrances(
    notice: "NoticeData",
    records: list[DeedRecord],
    assessed: float,
    amounts: dict[str, int] | None = None,
) -> tuple[int, list[str]]:
    """Core sweep. Walks the FULL record set, classifies + nets each
    encumbrance, and returns ``(total_haircut_dollars, flags)``.

    Pure (no network/file I/O) — the live ``amounts`` map and any per-record
    ``amount`` attribute carry the dollar figures so this stays unit-testable
    via injected fixtures. ``records`` is the ENTIRE deed history (not the
    first page) so a release sitting past the first mortgage is found and the
    mortgage is treated as released (Murphy, Mudd-Francis, Perrin).
    """
    haircut = 0
    flags: set[str] = set()

    if records is None:
        records = []

    # Mortgages: only the UNRELEASED set is netted. _choose_active_mortgage
    # encapsulates both release signals (explicit release xref + implicit
    # later-year xref) and walks all records, so a late release is honored.
    active_mtg = None
    try:
        active_mtg = _choose_active_mortgage(records)
    except Exception as exc:  # noqa: BLE001
        logger.warning("  [Equity] active-mortgage selection failed: %s", exc)
        active_mtg = None

    if active_mtg is not None:
        if _classify_lien(active_mtg.doc_type, active_mtg.grantee) == "hecm":
            # HECM: flag it but do NOT straight-line a balance (it is
            # negative-amortizing and due-on-death). If we know the original
            # amount, treat the FULL amount as the haircut (worst case for a
            # reverse mortgage that has been accruing); otherwise leave the
            # dollar haircut at 0 and let the never-100% ceiling depress equity.
            flags.add("hecm")
            original = _record_amount(active_mtg, amounts)
            if original > 0:
                haircut += original
                logger.info(
                    "  [Equity] HECM/reverse mortgage %s — haircut full original ~$%s (estimate, NOT amortized)",
                    active_mtg.instnum, f"{original:,}",
                )
            else:
                logger.info(
                    "  [Equity] HECM/reverse mortgage %s — balance unknown, not straight-lined (flag only)",
                    active_mtg.instnum,
                )
        else:
            # Conventional open mortgage with no matching release → estimate a
            # straight-line balance and net it (mark as estimate in the log).
            flags.add("open_mortgage")
            original = _record_amount(active_mtg, amounts)
            balance = _amortized_balance(original, active_mtg.filed_date) if original > 0 else 0
            if balance > 0:
                haircut += balance
                logger.info(
                    "  [Equity] open mortgage %s — balance ~$%s (estimate, straight-line)",
                    active_mtg.instnum, f"{balance:,}",
                )
            else:
                logger.info(
                    "  [Equity] open mortgage %s — balance unknown, flag only",
                    active_mtg.instnum,
                )

    # Lien / other records: hecm / judgment / lis_pendens / tax_cert. Skip the
    # rows already accounted for as mortgages or releases. A stray HECM record
    # whose doc_type didn't classify as a "mortgage" (e.g. doc_type=="HECM")
    # is caught here as a flag-only encumbrance — still NOT straight-lined.
    active_instnum = active_mtg.instnum if active_mtg is not None else None
    for rec in records:
        if active_instnum and rec.instnum == active_instnum:
            continue  # already handled on the mortgage path
        try:
            group = _classify(rec.doc_type)
        except Exception:  # noqa: BLE001
            group = "other"
        if group in ("mortgage", "release", "deed"):
            continue
        bucket = _classify_lien(rec.doc_type, f"{rec.grantor or ''} {rec.grantee or ''}")
        if bucket == "hecm":
            # HECM-as-non-mortgage row: flag only, never amortize.
            flags.add("hecm")
            amount = _record_amount(rec, amounts)
            if amount > 0:
                haircut += amount
                logger.info(
                    "  [Equity] HECM record %s — haircut full original ~$%s (NOT amortized)",
                    rec.instnum, f"{amount:,}",
                )
            else:
                logger.info(
                    "  [Equity] HECM record %s — balance unknown, flag only", rec.instnum,
                )
            continue
        if bucket not in ("judgment", "lis_pendens", "tax_cert"):
            continue
        flags.add(bucket)
        amount = _record_amount(rec, amounts)
        if amount > 0:
            haircut += amount
            logger.info(
                "  [Equity] %s lien %s — net $%s",
                bucket, rec.instnum, f"{amount:,}",
            )
        else:
            # Never net $0 for a real lien — depress equity by a conservative
            # placeholder so the flag still has effect when the amount is
            # unparseable.
            haircut += _UNKNOWN_LIEN_HAIRCUT
            logger.info(
                "  [Equity] %s lien %s — amount unknown, conservative haircut",
                bucket, rec.instnum,
            )

    # Medicaid/MERP — a risk FLAG (no dollar haircut), but it still trips the
    # never-100%-when-flagged invariant downstream.
    if _has_medicaid_signal(notice):
        flags.add("medicaid")
        logger.info("  [Equity] Medicaid/MERP risk flagged (DMS party or elder-law attorney)")

    ordered = [f for f in _FLAG_ORDER if f in flags]
    return haircut, ordered


def scan_liens(
    notice: "NoticeData",
    opener=None,
    records: list[DeedRecord] | None = None,
) -> list[DeedRecord]:
    """Full-history deed/lien scan for a notice. THE ONLY network function.

    Reuses the Step-3d deed pipeline (p3.php name search → dlist.php record
    list) but walks the ENTIRE result set across the matching owner row — it
    does NOT stop at the first active mortgage and does NOT slice the records
    (the hidden-release cases live past the first mortgage row).

    Resilient by contract: any failure (JCD down, no match, parse error) logs
    a warning and returns ``[]`` so equity still computes from existing fields.
    Injectable: pass ``records=`` to skip the network entirely (unit tests).
    """
    if records is not None:
        return records

    try:
        from kentucky_name_resolver import SUFFIX_RE  # local import — optional dep

        query = (notice.decedent_name or "").strip() or (notice.owner_name or "").strip()
        if not query:
            return []
        # Normalize KCOJ-style "LAST, FIRST MIDDLE" → "LAST FIRST MIDDLE" and
        # strip suffixes, matching the Step 3d search normalization.
        query = SUFFIX_RE.sub("", query).strip()
        query = query.replace(",", " ")
        query = re.sub(r"\s+", " ", query).strip()
        if not query:
            return []

        own = opener is None
        if own:
            opener = _make_opener()
            _accept_disclaimer(opener)

        rows = _search_names_unique(opener, query)
        best = _pick_best_name_match(query, rows)
        if not best:
            logger.info("  [Equity] no JCD deed-list match for %r", query)
            return []
        _display, checkbox_value, _count = best
        html = _fetch_deed_list(opener, checkbox_value)
        if not html:
            return []
        all_records = _parse_deed_list(html)
        logger.debug("  [Equity] scanned %d deed/lien records for %r", len(all_records), query)
        return all_records
    except Exception as exc:  # noqa: BLE001 — network is best-effort
        logger.warning("  [Equity] lien scan failed (degrading to existing fields): %s", exc)
        return []


def estimate_equity(
    notice: "NoticeData",
    records: list[DeedRecord] | None = None,
    opener=None,
) -> bool:
    """Populate ``estimated_equity`` / ``equity_percent`` / ``lien_flags`` on a
    KY notice by netting a full-history lien sweep against the PVA assessed
    value.

    Gates (preserved from the pre-sweep estimator):
      * KY-only (TN gets equity from Zillow via property_enricher).
      * Idempotent — skips if equity is already populated.
      * ``property_owner_status`` in {direct, estate, trust, heir_recent}.

    ``records`` is injectable for unit tests (no network). When omitted,
    ``scan_liens`` performs the (resilient) live scan. Returns True if any
    field was written, False otherwise.
    """
    if notice.state.upper() != "KY":
        return False
    if notice.estimated_equity.strip() and notice.equity_percent.strip():
        return False
    if notice.property_owner_status not in ("direct", "estate", "trust", "heir_recent"):
        # No confirmed current-ownership signal — refuse to compute equity
        # even if we have an estimated_value. Accepted statuses:
        #   direct       — decedent is the named PVA owner
        #   estate       — PVA shows "ESTATE OF <decedent>"
        #   trust        — title held by a trust the decedent created
        #   heir_recent  — title transferred to an heir within 24 months
        return False

    try:
        assessed = float(notice.estimated_value or "0")
    except ValueError:
        assessed = 0.0
    if assessed <= 0:
        return False

    # ── Lien sweep (guarded — falls back to the prior simple equity) ──────
    try:
        records = scan_liens(notice, opener=opener, records=records)
        haircut, flags = _net_encumbrances(notice, records, assessed)

        notice.lien_flags = ";".join(flags)

        # Negative equity is intentional when tax/code/judgment liens exceed
        # value on a "mortgage-free" home (Walker, Thompson-Hale). Do NOT
        # clamp to 0 on the flagged path.
        equity = assessed - haircut
        percent = (equity / assessed) * 100 if assessed > 0 else 0.0

        if not flags:
            # No mortgage signal AND no liens AND no flags. Two sub-cases:
            #   * A confirmed "$0 mortgage, no liens" → genuine free-and-clear
            #     (100%). We treat the absence of any active mortgage in the
            #     scanned records as that confirmation when records were
            #     actually scanned.
            #   * Unknown (nothing found / scan empty) → conservative 0.85
            #     floor rather than a fabricated full 100%.
            had_mortgage_records = any(
                _classify(r.doc_type) == "mortgage" for r in (records or [])
            )
            if records and (had_mortgage_records or not _scan_was_empty(records)):
                # Records present, all mortgages released or none open, no
                # liens → genuine free-and-clear.
                equity = assessed
                percent = 100.0
            else:
                # No usable scan signal at all → 0.85 floor (clean unknown).
                equity = assessed * 0.85
                percent = 85.0
        else:
            # INVARIANT (locked): a flagged record can never read 100%. If the
            # netting produced no dollar haircut (medicaid-only / unknown HECM
            # balance), cap below 100 with a conservative ceiling. The >= guard
            # also catches a tiny float that would round to "100.0".
            if percent >= 100.0 or f"{percent:.1f}" == "100.0":
                percent = _FLAGGED_NO_DOLLAR_CEILING
                equity = assessed * (percent / 100.0)

        notice.estimated_equity = str(int(round(equity)))
        notice.equity_percent = f"{percent:.1f}"

        logger.info(
            "  [Equity] %s -> $%s (%.1f%%) assessed $%s - haircut $%s flags=[%s]",
            notice.case_number or notice.address or notice.decedent_name,
            f"{int(round(equity)):,}",
            percent,
            f"{int(assessed):,}",
            f"{int(haircut):,}",
            notice.lien_flags,
        )
        return True

    except Exception as exc:  # noqa: BLE001 — never let the sweep crash equity
        logger.warning(
            "  [Equity] lien sweep failed (%s) — falling back to simple equity", exc
        )
        return _simple_equity_fallback(notice, assessed)


def _scan_was_empty(records: list[DeedRecord] | None) -> bool:
    """A scan is 'empty' (no usable signal) when there are no records at all."""
    return not records


def _simple_equity_fallback(notice: "NoticeData", assessed: float) -> bool:
    """Prior pre-sweep equity: assessed - mortgage_balance_estimate.

    Used only when the lien sweep raises unexpectedly. Mirrors the original
    estimator's required-mortgage-signal contract. Leaves ``lien_flags`` empty.
    """
    if not notice.mortgage_balance_estimate.strip():
        return False
    try:
        mortgage = float(notice.mortgage_balance_estimate)
    except ValueError:
        return False

    equity = max(0.0, assessed - mortgage)
    percent = (equity / assessed) * 100 if assessed > 0 else 0.0
    notice.estimated_equity = str(int(round(equity)))
    notice.equity_percent = f"{percent:.1f}"
    logger.info(
        "  [Equity-fallback] %s -> $%s (%.1f%%) from assessed $%s - mortgage $%s",
        notice.case_number or notice.address or notice.decedent_name,
        f"{int(round(equity)):,}", percent,
        f"{int(assessed):,}", f"{int(mortgage):,}",
    )
    return True


def enrich_equity(notices: list["NoticeData"]) -> int:
    """Batch entry point. Jefferson-gated, idempotent. Returns count enriched."""
    if not notices:
        return 0

    enriched = 0
    for n in notices:
        if n.county.lower() != "jefferson":
            continue
        if estimate_equity(n):
            enriched += 1
    if enriched:
        logger.info("  [Equity] KY equity enrichment: %d record(s)", enriched)
    return enriched
