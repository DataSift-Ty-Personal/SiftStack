"""Tests for pdf_importer regex parser and validation.

Run: python tests/test_pdf_importer.py
  or: python -m pytest tests/test_pdf_importer.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pdf_importer import parse_page_regex, validate_row, PARCEL_RE, ROW_RE


# ── ROW_RE tests ─────────────────────────────────────────────────────

class TestRowRegex:
    """Test the full row regex against realistic OCR lines."""

    def test_standard_row(self):
        line = "1 003-05827   8428 Example Rd   Ogletree Burl & Nona"
        m = ROW_RE.match(line)
        assert m, f"Failed to match: {line}"
        assert m.group(1) == "003-05827"
        assert m.group(2).strip() == "8428 Example Rd"
        assert m.group(3).strip() == "Ogletree Burl & Nona"

    def test_row_with_letter_suffix(self):
        line = "005LB-00801   1234 Oak Ave   Smith John"
        m = ROW_RE.match(line)
        assert m, f"Failed to match: {line}"
        assert m.group(1) == "005LB-00801"

    def test_row_no_number(self):
        """Row without leading row number still matches."""
        line = "018AA-022   5100 Maple Dr   County Trust"
        m = ROW_RE.match(line)
        assert m, f"Failed to match: {line}"
        assert m.group(1) == "018AA-022"
        assert m.group(2).strip() == "5100 Maple Dr"

    def test_highway_route_uses_fallback(self):
        """Highway+route (e.g. 'Old Hwy 33') doesn't match ROW_RE — handled by fallback."""
        line = "018AA-022   5100 Old Hwy 33   County Trust"
        m = ROW_RE.match(line)
        # ROW_RE can't handle route numbers after suffix — fallback picks it up
        assert m is None

    def test_row_with_unit(self):
        line = "42 069-123   4500 Main St #5   Jones Robert"
        m = ROW_RE.match(line)
        assert m, f"Failed to match: {line}"
        assert "#5" in m.group(2) or "Main St" in m.group(2)

    def test_row_drive_suffix(self):
        line = "100-00718   4221 Sample Dr   Hatfield Doreen"
        m = ROW_RE.match(line)
        assert m, f"Failed to match: {line}"
        assert m.group(2).strip() == "4221 Sample Dr"
        assert m.group(3).strip() == "Hatfield Doreen"


# ── PARCEL_RE tests ──────────────────────────────────────────────────

class TestParcelRegex:
    """Test parcel ID regex against various formats."""

    def test_simple_parcel(self):
        assert PARCEL_RE.search("003-05827")

    def test_parcel_with_letters(self):
        assert PARCEL_RE.search("005LB-00801")

    def test_short_parcel(self):
        assert PARCEL_RE.search("018AA-022")

    def test_parcel_trailing_letter(self):
        m = PARCEL_RE.search("049-123A")
        assert m
        assert m.group(1) == "049-123A"

    def test_no_parcel(self):
        assert not PARCEL_RE.search("hello world")

    def test_parcel_in_noisy_line(self):
        m = PARCEL_RE.search("| 003-05827 8428 Example ~*")
        assert m
        assert m.group(1) == "003-05827"


# ── parse_page_regex tests ───────────────────────────────────────────

class TestParsePageRegex:
    """Test the full regex parser against OCR-like text blocks."""

    def test_standard_block(self):
        text = """1 003-05827   8428 Example Rd   Ogletree Burl & Nona
2 005LB-00801   1234 Oak Ave   Smith John
3 100-00718   4221 Sample Dr   Hatfield Doreen"""
        rows = parse_page_regex(text)
        assert len(rows) == 3
        assert rows[0]["parcel_id"] == "003-05827"
        assert rows[0]["address"] == "8428 Example Rd"
        assert rows[0]["owner_name"] == "Ogletree Burl & Nona"
        assert rows[2]["parcel_id"] == "100-00718"

    def test_ocr_artifacts_cleaned(self):
        text = "| 003-05827   8428 Example Rd   Ogletree Burl |"
        rows = parse_page_regex(text)
        assert len(rows) >= 1
        assert rows[0]["parcel_id"] == "003-05827"

    def test_short_lines_skipped(self):
        text = """short
x
003-05827   8428 Example Rd   Ogletree Burl & Nona"""
        rows = parse_page_regex(text)
        assert len(rows) == 1

    def test_empty_text(self):
        assert parse_page_regex("") == []

    def test_no_matches(self):
        text = "This is just a page header\nWith no property data at all"
        assert parse_page_regex(text) == []

    def test_fallback_parcel_split(self):
        """Lines with a parcel but no suffix match use fallback splitting."""
        text = "003-05827    8428 Example    Ogletree Burl"
        rows = parse_page_regex(text)
        # Fallback should find parcel + split at 3+ spaces
        assert len(rows) >= 1
        assert rows[0]["parcel_id"] == "003-05827"

    def test_multiple_pages_combined(self):
        page1 = "1 003-05827   8428 Example Rd   Ogletree Burl"
        page2 = "1 100-00718   4221 Sample Dr   Hatfield Doreen"
        rows1 = parse_page_regex(page1)
        rows2 = parse_page_regex(page2)
        assert len(rows1) == 1
        assert len(rows2) == 1
        assert rows1[0]["parcel_id"] != rows2[0]["parcel_id"]


# ── validate_row tests ───────────────────────────────────────────────

class TestValidateRow:
    """Test row validation logic."""

    def test_valid_row(self):
        row = {"parcel_id": "003-05827", "address": "8428 Example Rd", "owner_name": "Smith John"}
        assert validate_row(row) is True

    def test_valid_row_no_owner(self):
        """Owner can be empty — we still have the property."""
        row = {"parcel_id": "003-05827", "address": "8428 Example Rd", "owner_name": ""}
        assert validate_row(row) is True

    def test_invalid_no_parcel(self):
        row = {"parcel_id": "", "address": "8428 Example Rd", "owner_name": "Smith"}
        assert validate_row(row) is False

    def test_invalid_no_dash(self):
        """Parcel without dash is rejected."""
        row = {"parcel_id": "00305827", "address": "8428 Example Rd", "owner_name": "Smith"}
        assert validate_row(row) is False

    def test_invalid_no_address(self):
        row = {"parcel_id": "003-05827", "address": "", "owner_name": "Smith"}
        assert validate_row(row) is False

    def test_whitespace_only_parcel(self):
        row = {"parcel_id": "  ", "address": "123 Main St", "owner_name": "Smith"}
        assert validate_row(row) is False

    def test_missing_keys(self):
        assert validate_row({}) is False
        assert validate_row({"parcel_id": "003-05827"}) is False

    def test_zero_address_valid(self):
        """'0 Street' is valid for vacant land."""
        row = {"parcel_id": "003-001", "address": "0 Old Highway 33", "owner_name": "County"}
        assert validate_row(row) is True


# ── Run with custom runner (matching project convention) ─────────────

if __name__ == "__main__":
    import traceback

    test_classes = [TestRowRegex, TestParcelRegex, TestParsePageRegex, TestValidateRow]
    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in sorted(methods):
            total += 1
            test_fn = getattr(instance, method_name)
            try:
                test_fn()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except Exception as e:
                failed += 1
                errors.append((cls.__name__, method_name, e))
                print(f"  FAIL  {cls.__name__}.{method_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print(f"\nFailures:")
        for cls_name, method_name, exc in errors:
            print(f"  {cls_name}.{method_name}:")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
    print()
    sys.exit(1 if failed else 0)
