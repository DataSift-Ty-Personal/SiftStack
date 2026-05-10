"""Round-trip-safe CSV writer for DataSift exports.

DataSift's CSV export has a 229-column fixed schema (validated against
test_data/sample_export.csv). This writer:

  1. Reads a DataSift CSV preserving every column and value.
  2. Overlays deep-prospecting results onto matched rows:
       - new phones into the next-empty Phone N slot
       - new emails into the next-empty Email N slot
       - tags appended to the row's Tags column
       - markdown research-pack block written to Notes
         (column injected if missing — required for the operator
          workflow even though the export shape may omit it)
  3. Writes the CSV back preserving column order + non-touched cells.

Round-trip contract: read CSV → write CSV with no overlay → output ==
input (byte-for-byte except for the trailing newline / line endings,
which we normalize to Unix `\\n`). Validated by `validate_round_trip()`.

Phone slot rules:
  - Per-row scan from Phone 1 → Phone 30; first empty slot is the write
    point for our highest-confidence new phone.
  - Never overwrite an existing phone (operator-verified data is sacred).
  - If all 30 slots are full, we drop the lowest-confidence new phones
    to fit and append `Deep Prospect Phones Truncated` to the row's Tags.

Phone Tag format (deterministic order):
    {SubjectRole},{stars},{Source Flag}

Examples:
    Subject,*,Verified 2+ Sites
    Heir,**,Found via TPS
    Family Pivot,***,Found via CBC

Dial First/Second/Third labels are intentionally NOT emitted here —
those come from Trestle phone-scoring downstream of the CLI, which has
the line-type + carrier reputation data needed to rank dial priority.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Iterable

from deep_prospecting.models import (
    Phone, ResearchPack, SourceID,
)

logger = logging.getLogger(__name__)

# Column count is fixed at 229 in the export. Don't hardcode the names —
# we read them from the input header to stay tolerant of minor schema
# drift (DataSift adds/removes property metadata occasionally).
PHONE_SLOTS = 30
EMAIL_SLOTS = 10

# Tag-cell separators
PHONE_TAG_SEP = ","
ROW_TAG_SEP = ","

# Where we inject Notes if missing — between Tags and Email 1, mirroring
# SiftStack's existing datasift_formatter.py column order.
NOTES_INJECT_AFTER = "Tags"
NOTES_COL = "Notes"


# ── Tag derivation ────────────────────────────────────────────────────


def _stars_for_subject_role(subject_role: str) -> str:
    """Map subject role → person-identifier asterisk count.

    These asterisks identify WHICH PERSON a phone belongs to. Dial-rank
    labeling is Trestle's job — not emitted by this CLI.

    Sample data confirmed: Sally Baksh's phones all carry `*` because
    all of them are her, the Subject. Family-member phones get `**` to
    distinguish them as a different person.

    Slice 1 mapping (single DM only):
        SUBJECT       → "*"   (DM is the original record owner, alive)
        HEIR          → "**"  (DM is heir of deceased Subject)
        EXECUTOR      → "**"  (DM is court-named executor)
        FAMILY_PIVOT  → "**"  (DM is an associate; rare in Slice 1)

    Slice 2+ adds backup heirs at "***", secondary contacts at "****".
    The People & Star Markers block in Notes resolves these to names.
    """
    return "*" if subject_role == "SUBJECT" else "**"


def _source_flag(sources: list[SourceID]) -> str:
    """Phone Tag source flag:
        2+ sources  → "Verified 2+ Sites"
        1 source    → "Found via TPS" / "Found via FPS" / "Found via CBC"
    """
    if len(sources) >= 2:
        return "Verified 2+ Sites"
    if not sources:
        return ""
    src = sources[0]
    label = {"tps": "TPS", "fps": "FPS", "cbc": "CBC"}.get(src, src.upper())
    return f"Found via {label}"


def _subject_role_label(subject_role: str) -> str:
    """SubjectRole literal → operator-friendly label for the tag cell."""
    return subject_role.replace("_", " ").title()


def derive_phone_tag_cell(phone: Phone, *, subject_role: str) -> str:
    """Build the Phone Tags N cell content.

    Stable order: {role label}, {person-stars}, {source flag}.

    Dial First/Second/Third labels are NOT emitted here — those come
    from Trestle phone-scoring downstream of this CLI, which has the
    line-type + carrier reputation data needed to rank dial priority.
    Phase skip-trace produces phones; the CLI surfaces them as a
    person-keyed set; Trestle adds the dial ordering later.

    Person-stars derive from `subject_role` (which person owns this
    phone). The People & Star Markers block at the top of the Notes
    field resolves the stars back to named people.
    """
    parts = [
        _subject_role_label(subject_role),
        _stars_for_subject_role(subject_role),
    ]
    src = _source_flag(phone.sources)
    if src:
        parts.append(src)
    return PHONE_TAG_SEP.join(parts)


# ── Row matching ──────────────────────────────────────────────────────


def _norm_addr(s: str) -> str:
    """Uppercase + collapse whitespace + drop city/state/ZIP tail.

    Pack inputs typically carry the full address ("8 Phyllis Pl, Milltown,
    NJ 08850") while DataSift's `Property address` column holds just
    the street ("8 Phyllis Pl"). Splitting at the first comma normalizes
    both forms to "8 PHYLLIS PL" for matching.
    """
    if not s:
        return ""
    street = s.split(",", 1)[0]
    return " ".join(street.upper().split())


def find_row_index(rows: list[dict], pack: ResearchPack) -> int | None:
    """Match a research pack to its row by Property address.

    Returns the row index in `rows`, or None if no match. The caller
    decides what to do with unmatched packs (we currently flag them in
    a tag and append as new rows).
    """
    target = _norm_addr(pack.input.address or "")
    if not target:
        return None
    for i, row in enumerate(rows):
        # DataSift's exact column name (lowercase 'a'). Fall back to
        # title-case in case some exports differ.
        addr = row.get("Property address") or row.get("Property Address") or ""
        if _norm_addr(addr) == target:
            return i
    return None


# ── Slot allocation ───────────────────────────────────────────────────


def _next_empty_phone_slot(row: dict) -> int | None:
    """Return the 1-based phone slot index that's empty, or None if all full."""
    for n in range(1, PHONE_SLOTS + 1):
        if not (row.get(f"Phone {n}") or "").strip():
            return n
    return None


def _next_empty_email_slot(row: dict) -> int | None:
    for n in range(1, EMAIL_SLOTS + 1):
        if not (row.get(f"Email {n}") or "").strip():
            return n
    return None


# ── Tags column helpers ───────────────────────────────────────────────


def _split_row_tags(cell: str) -> list[str]:
    if not cell:
        return []
    return [t.strip() for t in cell.split(ROW_TAG_SEP) if t.strip()]


def _join_row_tags(tags: Iterable[str]) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = t.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return ROW_TAG_SEP.join(out)


def _outcome_tags_for(pack: ResearchPack, phones_added: int, *, truncated: bool) -> list[str]:
    today = date.today().isoformat()
    tags: list[str] = []

    if pack.heir_map and pack.heir_map.escalation_needed:
        tags.append("Deep Prospecting Complete - L4 Escalate")
    elif phones_added > 0:
        tags.append("Deep Prospecting Complete - NUMBERS ADDED")
    else:
        tags.append("Deep Prospecting Complete - NO NUMBERS ADDED")

    tags.append(f"Deep Prospected via CLI {today}")

    if truncated:
        tags.append("Deep Prospect Phones Truncated")

    if pack.heir_map and any(h.status == "LIVING" for h in pack.heir_map.heirs):
        tags.append("Verified Living Heir Found")
    if pack.decision_maker and pack.decision_maker.subject_role == "EXECUTOR":
        tags.append("Executor Confirmed")
    # LLC marker: the input was an LLC notice and we resolved a DM.
    if (pack.input.notice_type and "llc" in (pack.input.owner or "").lower()
            and pack.decision_maker is not None):
        tags.append("LLC Owner Resolved")

    return tags


# ── CSV I/O ───────────────────────────────────────────────────────────


def read_csv(path: Path) -> tuple[list[str], list[dict], int]:
    """Read a DataSift CSV. Returns (headers, rows, data_field_count).

    `data_field_count` is the number of comma-separated fields each data
    row actually contains. It can be < len(headers) when the export has
    a "footer-style" trailing column that's a signature, not a real data
    field — DataSift's `exported from REISift.io` is the canonical case:
    229 header fields, 228 data fields. We replay this asymmetry on
    write so the round-trip is byte-identical.

    If data rows aren't uniform, we use the first data row's count and
    emit a warning — non-uniform rows would be a malformed source CSV.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        # Two passes: one with csv.reader to learn the data field count,
        # one with DictReader to load row dicts. Keeps the field-count
        # detection robust against quoted fields with embedded commas.
        f.seek(0)
        raw = csv.reader(f)
        header_row = next(raw, None)
        if header_row is None:
            raise ValueError(f"{path}: CSV has no header row")
        first_data_row = next(raw, None)
        data_field_count = len(first_data_row) if first_data_row else len(header_row)
        # Verify uniformity for diagnostics; not fatal.
        for i, r in enumerate(raw, start=2):
            if len(r) != data_field_count:
                logger.warning(
                    "%s: data row %d has %d fields (expected %d)",
                    path.name, i, len(r), data_field_count,
                )
                break

        f.seek(0)
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: CSV has no header row")
        headers = list(reader.fieldnames)
        rows = [dict(r) for r in reader]
    logger.info(
        "read %d rows × %d header cols (%d data fields) from %s",
        len(rows), len(headers), data_field_count, path,
    )
    return headers, rows, data_field_count


def write_csv(
    headers: list[str], rows: list[dict], path: Path,
    *, data_field_count: int | None = None,
) -> None:
    """Write rows back preserving column order. Round-trip safe.

    Ensures every row has every header column (missing keys → empty
    string). Any row keys not in headers are silently dropped — the
    header is the contract.

    `data_field_count` controls how many comma-separated fields each
    DATA row gets. When it's None or equals len(headers), every row is
    emitted with all header fields (standard CSV). When it's less than
    len(headers), data rows are truncated to that count — the trailing
    header columns are emitted in the header line only. This preserves
    DataSift's footer-column convention.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if data_field_count is None or data_field_count >= len(headers):
        # Symmetric case — DictWriter does the right thing.
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({h: (row.get(h) or "") for h in headers})
    else:
        # Asymmetric case — emit header fully, then write data rows with
        # only the first `data_field_count` columns. Use raw csv.writer
        # so we control field-count per row exactly.
        data_cols = headers[:data_field_count]
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([(row.get(h) or "") for h in data_cols])
    logger.info("wrote %d rows × %d cols to %s",
                len(rows), len(headers), path)


def ensure_notes_column(headers: list[str]) -> list[str]:
    """Inject `Notes` column (after `Tags`) if absent. Returns new headers list.

    DataSift's auto-match accepts new columns on upload, so this is safe
    for round-trip: input → output adds a column → re-uploading to
    DataSift auto-creates the field.
    """
    if NOTES_COL in headers:
        return headers
    if NOTES_INJECT_AFTER not in headers:
        # Tags missing too — append at end as a fallback.
        return headers + [NOTES_COL]
    idx = headers.index(NOTES_INJECT_AFTER)
    return headers[: idx + 1] + [NOTES_COL] + headers[idx + 1:]


# ── Overlay logic ─────────────────────────────────────────────────────


def overlay_pack_onto_row(row: dict, pack: ResearchPack) -> tuple[int, bool]:
    """Mutate `row` in place with the pack's data.

    Returns (phones_added, truncated) so the caller can compute outcome
    tags. `row` MUST have all DataSift columns (caller ensures this).
    """
    phones_added = 0
    truncated = False

    # Sort phones by confidence (HIGH first), preserving stable order
    # within the same confidence so source ordering is deterministic.
    phones = []
    if pack.skip_trace:
        confidence_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        phones = sorted(
            pack.skip_trace.phones,
            key=lambda p: confidence_rank.get(p.confidence, 3),
        )

    dm = pack.decision_maker
    subject_role = dm.subject_role if dm else "SUBJECT"

    # Existing phones in the row — skip writing a duplicate number into
    # a fresh slot. Common when a row was previously enriched by the
    # operator or by an earlier CLI run. Compare on the 10-digit canonical
    # form (Phone.csv_value matches the column format).
    existing_phones = {
        (row.get(f"Phone {n}") or "").strip()
        for n in range(1, PHONE_SLOTS + 1)
    }
    existing_phones.discard("")

    # Walk phones, write into next-empty slot, never overwrite. Dial
    # rank labels are intentionally NOT emitted here — Trestle adds
    # those downstream when it scores the phones for line-type and
    # carrier reputation.
    for p in phones:
        if p.csv_value in existing_phones:
            logger.debug(
                "row already has phone %s — skipping duplicate write",
                p.csv_value,
            )
            continue
        slot = _next_empty_phone_slot(row)
        if slot is None:
            truncated = True
            logger.warning(
                "row exhausted Phone 1..30 — dropping %s (confidence=%s)",
                p.csv_value, p.confidence,
            )
            break
        row[f"Phone {slot}"] = p.csv_value
        row[f"Phone Type {slot}"] = p.type
        row[f"Phone Status {slot}"] = "UNKNOWN"
        row[f"Phone Tags {slot}"] = derive_phone_tag_cell(
            p, subject_role=subject_role,
        )
        # Phone Is Connected stays blank (untested).
        row[f"Phone Is Connected {slot}"] = ""
        existing_phones.add(p.csv_value)
        phones_added += 1

    # Emails — same first-empty pattern, also deduped against existing
    # column values (case-insensitive: "Foo@Bar.com" ≡ "foo@bar.com").
    if pack.skip_trace:
        existing_emails = {
            (row.get(f"Email {n}") or "").strip().lower()
            for n in range(1, EMAIL_SLOTS + 1)
        }
        existing_emails.discard("")
        for e in pack.skip_trace.emails:
            addr_norm = e.address.strip().lower()
            if addr_norm in existing_emails:
                logger.debug("row already has email %s — skipping", e.address)
                continue
            slot = _next_empty_email_slot(row)
            if slot is None:
                logger.warning("row exhausted Email 1..10 — dropping %s", e.address)
                break
            row[f"Email {slot}"] = e.address
            existing_emails.add(addr_norm)

    return phones_added, truncated


def append_outcome_tags(row: dict, pack: ResearchPack, *,
                        phones_added: int, truncated: bool) -> None:
    """Merge outcome tags into the row's Tags column (dedup-safe)."""
    existing = _split_row_tags(row.get("Tags", ""))
    new = _outcome_tags_for(pack, phones_added, truncated=truncated)
    row["Tags"] = _join_row_tags(existing + new)


def append_notes_block(row: dict, pack: ResearchPack) -> None:
    """Append the FULL 9-section research-pack markdown to row Notes,
    wrapped in `=== DEEP PROSPECTING ===` delimiters.

    The on-disk research_pack.md is the canonical artifact; this Notes
    embed is the operator-convenience copy so they can read the entire
    breakdown inside DataSift without opening another file.

    The People & Star Markers lookup table sits above the markdown so
    the operator sees who `*` / `**` / `***` resolve to before reading
    the rest. Existing Notes content (e.g., SiftStack's
    `=== DECEASED OWNER ===` block) is preserved with `\\n\\n` separator.
    """
    from deep_prospecting.output import render, render_people_block

    people = render_people_block(pack)
    body = render(pack)

    block_parts = ["=== DEEP PROSPECTING ==="]
    if people:
        block_parts.append(people)
        block_parts.append("---")
    block_parts.append(body)
    block_parts.append("=== END DEEP PROSPECTING ===")
    block = "\n\n".join(block_parts)

    existing = (row.get(NOTES_COL) or "").rstrip()
    row[NOTES_COL] = f"{existing}\n\n{block}" if existing else block


# ── Public API ────────────────────────────────────────────────────────


def overlay(
    in_csv: Path, out_csv: Path, packs: list[ResearchPack],
) -> dict:
    """Overlay one or more research packs onto a DataSift CSV.

    Args:
        in_csv: source DataSift export.
        out_csv: where to write the merged result.
        packs: research packs to overlay. Each is matched to a row by
            Property address. Packs with no matching row are appended
            as new rows tagged `Deep Prospecting - No Match in Input`.

    Returns: dict with `matched`, `unmatched`, `phones_added`,
        `truncated_rows`, `out_csv`.
    """
    headers, rows, data_field_count = read_csv(in_csv)
    new_headers = ensure_notes_column(headers)
    # If we injected a column, the data row count grows by one too —
    # otherwise the new column would be silently dropped on write.
    if len(new_headers) > len(headers):
        data_field_count += len(new_headers) - len(headers)
    headers = new_headers

    # Ensure every row has the new Notes key (Python defaults to empty).
    for r in rows:
        r.setdefault(NOTES_COL, "")

    matched = unmatched = total_phones = truncated_rows = 0

    for pack in packs:
        idx = find_row_index(rows, pack)
        if idx is None:
            # Unmatched — append a new row.
            new_row: dict = {h: "" for h in headers}
            new_row["Property address"] = pack.input.address or ""
            new_row["First Name"] = (pack.input.owner or "").split()[0] if pack.input.owner else ""
            last = (pack.input.owner or "").split()
            new_row["Last Name"] = last[-1] if len(last) > 1 else ""
            existing_tags = _split_row_tags(new_row.get("Tags", ""))
            new_row["Tags"] = _join_row_tags(existing_tags + ["Deep Prospecting - No Match in Input"])
            phones_added, truncated = overlay_pack_onto_row(new_row, pack)
            append_outcome_tags(new_row, pack, phones_added=phones_added, truncated=truncated)
            append_notes_block(new_row, pack)
            rows.append(new_row)
            unmatched += 1
            total_phones += phones_added
            if truncated:
                truncated_rows += 1
            continue

        row = rows[idx]
        phones_added, truncated = overlay_pack_onto_row(row, pack)
        append_outcome_tags(row, pack, phones_added=phones_added, truncated=truncated)
        append_notes_block(row, pack)
        matched += 1
        total_phones += phones_added
        if truncated:
            truncated_rows += 1

    write_csv(headers, rows, out_csv, data_field_count=data_field_count)
    return {
        "matched": matched,
        "unmatched": unmatched,
        "phones_added": total_phones,
        "truncated_rows": truncated_rows,
        "out_csv": str(out_csv),
    }


# ── Round-trip validation ─────────────────────────────────────────────


def validate_round_trip(in_csv: Path, *, out_csv: Path | None = None) -> dict:
    """Read → write back unchanged → assert byte-equal (after EOL norm).

    No overlay applied. Used as a CI check: any drift means the writer
    is silently mutating data and round-trip safety is broken.

    Returns dict with `ok`, `differences`. `ok=True` means safe.
    """
    headers, rows, data_field_count = read_csv(in_csv)
    if out_csv is None:
        out_csv = in_csv.with_name(in_csv.stem + "_roundtrip.csv")
    write_csv(headers, rows, out_csv, data_field_count=data_field_count)

    src_text = Path(in_csv).read_bytes().decode("utf-8-sig")
    dst_text = Path(out_csv).read_bytes().decode("utf-8")

    # Normalize line endings — csv module always writes \r\n on Windows
    # but we forced newline="" + Unix \n via DictWriter default. Compare
    # without the trailing newline since DataSift exports may or may not
    # have one.
    src_norm = src_text.replace("\r\n", "\n").rstrip("\n")
    dst_norm = dst_text.replace("\r\n", "\n").rstrip("\n")

    if src_norm == dst_norm:
        return {"ok": True, "differences": []}

    # Compute line-by-line diff for diagnosis.
    src_lines = src_norm.split("\n")
    dst_lines = dst_norm.split("\n")
    diffs: list[str] = []
    if len(src_lines) != len(dst_lines):
        diffs.append(f"line count: {len(src_lines)} vs {len(dst_lines)}")
    for i, (a, b) in enumerate(zip(src_lines, dst_lines)):
        if a != b:
            diffs.append(f"line {i+1}: differs")
            if len(diffs) >= 5:
                diffs.append("... (more diffs suppressed)")
                break
    return {"ok": False, "differences": diffs}
