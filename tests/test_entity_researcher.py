"""Unit tests for entity_researcher module.

Tests entity classification, name parsing, contact fallback, and filter exemption.
No API calls — all tests use synthetic data.

Usage:
    python tests/test_entity_researcher.py
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import NoticeData
from entity_researcher import _classify_entity, _try_parse_entity_name
from datasift_formatter import _get_contact_info, _build_tags, _is_entity_name

# ── Test Runner ─────────────────────────────────────────────────────────

passed = 0
failed = 0
errors = []


def check(name, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
    else:
        failed += 1
        errors.append(f"FAIL: {name}\n  expected: {expected!r}\n  got:      {actual!r}")


# ── Entity Classification Tests ─────────────────────────────────────────

print("=== Entity Classification ===")

check("LLC basic", _classify_entity("SUNRISE HOLDINGS LLC"), "llc")
check("L.L.C variant", _classify_entity("SMITH L.L.C."), "llc")
check("Corp INC", _classify_entity("DR HORTON INC"), "corp")
check("Corporation", _classify_entity("ACME CORPORATION"), "corp")
check("Trust", _classify_entity("SMITH FAMILY TRUST"), "trust")
check("Estate", _classify_entity("ESTATE OF JAMES BROWN"), "estate")
check("LP", _classify_entity("ABC PARTNERS LP"), "lp")
check("LLP", _classify_entity("JONES AND SMITH LLP"), "lp")
check("Other entity (PROPERTIES)", _classify_entity("SUNRISE PROPERTIES"), "other")
check("Other entity (INVESTMENTS)", _classify_entity("CAPITAL INVESTMENTS GROUP"), "other")
check("Individual name", _classify_entity("JOHN SMITH"), "")
check("Empty string", _classify_entity(""), "")
check("None-like", _classify_entity("   "), "")

# ── Name Parsing Tests ──────────────────────────────────────────────────

print("\n=== Name Parsing (Free Fast Path) ===")

# Trust parsing
result = _try_parse_entity_name("JOHN DOE REVOCABLE TRUST", "trust")
check("Trust - full name", result["person_name"], "John Doe")
check("Trust - role", result["role"], "trustee")
check("Trust - confidence", result["confidence"], "high")

result = _try_parse_entity_name("SMITH FAMILY TRUST", "trust")
check("Trust - family name only", result["person_name"], "Smith Family")
check("Trust - family confidence", result["confidence"], "high")

result = _try_parse_entity_name("JANE MARIE WILSON REVOCABLE LIVING TRUST", "trust")
check("Trust - 3-word name", result["person_name"], "Jane Marie Wilson")

result = _try_parse_entity_name("FIRST NATIONAL BANK TRUST", "trust")
check("Trust - business name returns None", result, None)

# Estate parsing
result = _try_parse_entity_name("ESTATE OF JAMES BROWN", "estate")
check("Estate - person name", result["person_name"], "James Brown")
check("Estate - role", result["role"], "executor")
check("Estate - confidence", result["confidence"], "high")

result = _try_parse_entity_name("THE ESTATE OF MARY ANN JOHNSON", "estate")
check("Estate - with THE prefix", result["person_name"], "Mary Ann Johnson")

# LLC parsing
result = _try_parse_entity_name("JOHNSON PROPERTIES LLC", "llc")
check("LLC - surname + generic", result["person_name"], "Johnson")
check("LLC - role", result["role"], "member")
check("LLC - confidence", result["confidence"], "low")

result = _try_parse_entity_name("FIRST PROPERTIES LLC", "llc")
check("LLC - non-name word rejected", result, None)

result = _try_parse_entity_name("ABC HOLDINGS LLC", "llc")
check("LLC - short acronym (3 chars)", result["person_name"], "Abc")

result = _try_parse_entity_name("QRT CAPITAL VENTURES LLC", "llc")
check("LLC - multi-word before generic", result, None)

result = _try_parse_entity_name("SUNSHINE HOMES LLC", "llc")
check("LLC - plausible surname", result["person_name"], "Sunshine")

# Corp parsing (no fast path for corps)
result = _try_parse_entity_name("ACME CORPORATION", "corp")
check("Corp - no fast path", result, None)

# ── Entity Filter Exemption Tests ────────────────────────────────────────

print("\n=== Entity Filter Exemption ===")

# Import the filter function
from enrichment_pipeline import _filter_entity_owners

# Entity without research → filtered out
n1 = NoticeData(owner_name="SUNRISE HOLDINGS LLC", address="100 Main St")
filtered = _filter_entity_owners([n1])
check("Entity without research → removed", len(filtered), 0)

# Entity with research → kept
n2 = NoticeData(owner_name="SUNRISE HOLDINGS LLC", address="100 Main St",
                entity_person_name="John Doe")
filtered = _filter_entity_owners([n2])
check("Entity with research → kept", len(filtered), 1)

# Personal trust → kept (exempt by name pattern)
n3 = NoticeData(owner_name="SMITH FAMILY TRUST", address="200 Oak Ave")
filtered = _filter_entity_owners([n3])
check("Personal trust → kept", len(filtered), 1)

# Estate → kept (exempt by name pattern)
n4 = NoticeData(owner_name="ESTATE OF JAMES BROWN", address="300 Pine Ln")
filtered = _filter_entity_owners([n4])
check("Estate → kept", len(filtered), 1)

# ── Contact Fallback Tests ──────────────────────────────────────────────

print("\n=== Contact Fallback (entity_person_name) ===")

# Entity with entity_person_name → uses it
n5 = NoticeData(
    owner_name="QRT PROPERTIES LLC",
    address="400 Elm St",
    city="Knoxville",
    state="TN",
    zip="37918",
    entity_person_name="John Smith",
    entity_person_role="registered_agent",
)
contact = _get_contact_info(n5)
check("Entity person → first name", contact["first"], "John")
check("Entity person → last name", contact["last"], "Smith")
check("Entity person → street fallback", contact["street"], "400 Elm St")

# Entity with entity_person_name AND tax_owner_name → entity_person takes priority
n6 = NoticeData(
    owner_name="SUNRISE HOLDINGS LLC",
    address="500 Cedar Ave",
    city="Maryville",
    state="TN",
    zip="37801",
    entity_person_name="Alice Carter",
    tax_owner_name="Bob Johnson",
)
contact = _get_contact_info(n6)
check("Entity person over tax owner → first", contact["first"], "Alice")
check("Entity person over tax owner → last", contact["last"], "Carter")

# Entity without entity_person_name falls back to tax_owner_name
n7 = NoticeData(
    owner_name="SUNRISE HOLDINGS LLC",
    address="600 Maple Dr",
    city="Alcoa",
    state="TN",
    zip="37701",
    tax_owner_name="Gerald Winslow",
)
contact = _get_contact_info(n7)
check("No entity person → tax owner first", contact["first"], "Gerald")
check("No entity person → tax owner last", contact["last"], "Winslow")

# ── Tags Tests ──────────────────────────────────────────────────────────

print("\n=== Entity Tags ===")

n8 = NoticeData(
    notice_type="foreclosure",
    county="Knox",
    date_added="2026-03-23",
    entity_type="llc",
    entity_person_name="John Smith",
)
tags = _build_tags(n8)
check("Entity tags include entity_owned", "entity_owned" in tags, True)
check("Entity tags include entity_researched", "entity_researched" in tags, True)

n9 = NoticeData(
    notice_type="tax_sale",
    county="Knox",
    date_added="2026-03-23",
    entity_type="corp",
)
tags = _build_tags(n9)
check("Unresearched entity has entity_owned", "entity_owned" in tags, True)
check("Unresearched entity no entity_researched", "entity_researched" not in tags, True)


# ── Summary ─────────────────────────────────────────────────────────────

print(f"\n{'='*60}")
print(f"Results: {passed} passed, {failed} failed")
if errors:
    print()
    for e in errors:
        print(e)
    sys.exit(1)
else:
    print("All tests passed!")
