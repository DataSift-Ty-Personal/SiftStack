"""Fixture-backed tests for nj_middlesex_probate.py.

Verifies the Middlesex surrogate probate parser against 5 saved detail-page
HTML fixtures captured via scripts/capture_middlesex_probate_fixture.py.

Run:
    python tests/test_middlesex_probate_parser.py
    pytest tests/test_middlesex_probate_parser.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nj_middlesex_probate import (  # noqa: E402
    MIDDLESEX,
    _build_notice,
    _parse_detail_fields,
    _parse_parties,
    _pick_executor,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURE_DIR / "middlesex_probate_manifest.json").read_text())

# Expected executor per fixture — hand-verified from the raw HTML.
# Format: fixture index → (executor_name, executor_type, executor_relation, executor_status)
EXPECTED = {
    1: ("Patrick Christopher Pakruda", "Applicant",     "Child",  "Accept"),
    2: ("Ruth M. Kennedy",             "Executor",      "Cousin", "Accept"),
    3: ("Marlene T. Stiso",            "Executor",      "Spouse", "Accept"),
    4: ("Edward C. Benkert",           "Executor",      "Child",  "Accept"),
    5: ("Jahad Booker",                "Administrator", "Child",  "Accept"),
}

# Expected decedent-side data per fixture — hand-verified from HTML label values.
EXPECTED_CASE = {
    1: {"decedent": "Christopher R. Pakruda", "address": "162 Devoe Avenue",
        "city": "Spotswood",   "zip": "08884", "date_filed": "2026-08-04", "dod": "2026-07-26"},
    # 2-5 are validated via cross-fixture invariants (all filed and dod are ISO dates,
    # every case has a non-empty address, city, and zip)
}


def _load(idx: int) -> str:
    return (FIXTURE_DIR / f"middlesex_probate_detail_{idx}.html").read_text()


# ── case-tab parser ─────────────────────────────────────────────────────


def test_parse_detail_fields_extracts_labeled_inputs():
    """_parse_detail_fields must extract every case-tab field with a value=."""
    fields = _parse_detail_fields(_load(1))
    assert fields.get("Last Name")      == "Pakruda"
    assert fields.get("First Name")     == "Christopher"
    assert fields.get("Address")        == "162 Devoe Avenue"
    assert fields.get("City")           == "Spotswood"
    assert fields.get("State")          == "NJ"
    assert fields.get("Zip")            == "08884"
    assert fields.get("Date Filed")     == "8/4/2026"
    assert fields.get("Date of Death")  == "7/26/2026"
    assert fields.get("Docket #")       == "296496"


def test_parse_detail_fields_all_fixtures_have_address():
    """Every fixture must yield a populated street + city + zip on the case tab."""
    for i in range(1, 6):
        fields = _parse_detail_fields(_load(i))
        assert fields.get("Address"), f"fixture {i}: missing Address"
        assert fields.get("City"),    f"fixture {i}: missing City"
        assert fields.get("Zip"),     f"fixture {i}: missing Zip"


# ── parties grid ────────────────────────────────────────────────────────


def test_parse_parties_returns_four_columns_per_row():
    """The public parties grid is 4-column (Name/Type/Relation/Status) — every
    row parsed must expose exactly those four keys, no more no less."""
    for i in range(1, 6):
        parties = _parse_parties(_load(i))
        assert parties, f"fixture {i}: no party rows parsed"
        for p in parties:
            assert set(p.keys()) == {"name", "type", "relation", "status"}, (
                f"fixture {i}: unexpected keys — {list(p.keys())}"
            )


def test_parse_parties_row_counts_match_ground_truth():
    """Row counts hand-verified from the raw HTML."""
    expected_counts = {1: 1, 2: 10, 3: 7, 4: 3, 5: 2}
    for i, n in expected_counts.items():
        assert len(_parse_parties(_load(i))) == n, f"fixture {i}: expected {n} parties"


# ── executor picker ────────────────────────────────────────────────────


def test_pick_executor_handles_all_role_types():
    """_pick_executor must resolve across Applicant, Executor (with & without
    a duplicate Legatee row for the same person), Administrator."""
    for i, (name, ptype, relation, status) in EXPECTED.items():
        parties = _parse_parties(_load(i))
        exe = _pick_executor(parties)
        assert exe is not None, f"fixture {i}: no executor picked"
        assert exe["name"]     == name,     f"fixture {i}: got {exe['name']!r}"
        assert exe["type"]     == ptype,    f"fixture {i}: got type {exe['type']!r}"
        assert exe["relation"] == relation, f"fixture {i}: got relation {exe['relation']!r}"
        assert exe["status"]   == status,   f"fixture {i}: got status {exe['status']!r}"


# ── build_notice integration ────────────────────────────────────────────


def _make_grid_row(entry: dict) -> dict:
    """Synthesize a grid_row dict the way _parse_grid_rows would."""
    return {
        "pk_id":         str(entry["pk_id"]),
        "detail_url":    entry["detail_url"],
        "row_idx":       0,
        "decedent_name": entry.get("grid_row_decedent", ""),
        "docket":        "",
        "case_desc":     "Probate",
        "town":          "",
        "filed":         "",
        "issued":        "",
        "dod":           "",
        "dob":           "",
    }


def test_build_notice_populates_date_filed_separately_from_date_added():
    """date_filed must be the court's filing date (ISO). date_added must be
    the scrape timestamp (today) — NOT the filing date."""
    entry = MANIFEST["details"][0]  # detail_1 Pakruda
    html = _load(1)
    fields = _parse_detail_fields(html)
    parties = _parse_parties(html)
    row = _make_grid_row(entry)

    notice = _build_notice(row, fields, parties, MIDDLESEX)
    assert notice is not None
    assert notice.date_filed == "2026-08-04", \
        f"date_filed should be court filing date; got {notice.date_filed!r}"
    assert notice.date_added == datetime.now().strftime("%Y-%m-%d"), \
        f"date_added should be scrape timestamp; got {notice.date_added!r}"
    assert notice.date_added != notice.date_filed, \
        "date_added and date_filed must be independent"


def test_build_notice_sets_decision_maker_fields_when_executor_present():
    """decision_maker_name/relationship/source/confidence must be populated
    from the picked executor for every case that has one (all 5 fixtures)."""
    for entry, (exec_name, _ptype, exec_relation, _status) in zip(
        MANIFEST["details"], EXPECTED.values()
    ):
        idx = entry["index"]
        html = _load(idx)
        row = _make_grid_row(entry)
        notice = _build_notice(row, _parse_detail_fields(html), _parse_parties(html), MIDDLESEX)
        assert notice is not None, f"fixture {idx}: build_notice returned None"
        assert notice.decision_maker_name         == exec_name,      f"fixture {idx}: DM name"
        assert notice.decision_maker_relationship == exec_relation,  f"fixture {idx}: DM relation"
        assert notice.decision_maker_source       == "court_record", f"fixture {idx}: DM source"
        assert notice.dm_confidence               == "high",         f"fixture {idx}: DM conf"


def test_build_notice_leaves_decision_maker_address_empty():
    """Executor address is NOT on the portal — verified via full-page grep on
    all 5 fixtures (see the audit report). The parser must not silently
    populate decision_maker_street from the decedent's address (which would
    lock every record into the S/occupant classification)."""
    for entry in MANIFEST["details"]:
        idx = entry["index"]
        html = _load(idx)
        notice = _build_notice(
            _make_grid_row(entry),
            _parse_detail_fields(html),
            _parse_parties(html),
            MIDDLESEX,
        )
        assert notice is not None, f"fixture {idx}"
        # These must stay empty at build time — enrichment_pipeline populates
        # them via the address-lookup waterfall (NJ MOD-IV → people search →
        # Tracerfy) once skip_dm_address=False.
        assert notice.decision_maker_street == "", f"fixture {idx}: DM street leaked"
        assert notice.decision_maker_city   == "", f"fixture {idx}: DM city leaked"
        assert notice.decision_maker_state  == "", f"fixture {idx}: DM state leaked"
        assert notice.decision_maker_zip    == "", f"fixture {idx}: DM zip leaked"


def test_build_notice_property_address_is_decedents_address():
    """The property address on the record IS the decedent's mailing address —
    that's the target-property model. Sanity-check the wiring."""
    entry = MANIFEST["details"][0]  # Pakruda
    html = _load(1)
    notice = _build_notice(
        _make_grid_row(entry), _parse_detail_fields(html), _parse_parties(html), MIDDLESEX,
    )
    assert notice.address == "162 Devoe Avenue"
    assert notice.city    == "Spotswood"
    assert notice.state   == "NJ"
    assert notice.zip     == "08884"


# ── Applicant & Affiant coverage (empty-DM investigation, week 32) ──────
#
# The 5 initial fixtures cover Executor + Administrator + one Applicant.
# The three fixtures below cover the two role types that were driving the
# empty-DM tail on the 2026-08-12 HELD_Probate CSV (11/49 records had no
# recognized fiduciary type). All three are single-purpose regression
# guards for _pick_executor's priority list.
#
# Provenance: fetched via plain curl against the stable GET URLs (no
# ViewState needed) — the parties grid is server-rendered.
#   pk=5406105  Randall Kolb           — 1 Applicant / Accept
#   pk=5406116  Luis R. Soto           — 1 Affiant   / Accept
#   pk=5405329  Dollie L. Somers       — 6 Affiants  (1 Accept, 2 Consent, 3 Deceased)


def test_pick_executor_applicant_only_accept():
    """Kolb: single Applicant/Accept row must be picked."""
    html = (FIXTURE_DIR / "middlesex_probate_detail_applicant_kolb.html").read_text()
    exe = _pick_executor(_parse_parties(html))
    assert exe is not None, "expected an Applicant to be picked"
    assert exe["name"]     == "Anne Marie Kolb"
    assert exe["type"]     == "Applicant"
    assert exe["relation"] == "Spouse"
    assert exe["status"]   == "Accept"


def test_pick_executor_affiant_only_accept():
    """Soto: single Affiant/Accept row must be picked (small-estate path)."""
    html = (FIXTURE_DIR / "middlesex_probate_detail_affiant_soto.html").read_text()
    exe = _pick_executor(_parse_parties(html))
    assert exe is not None, "expected an Affiant to be picked"
    assert exe["name"]     == "Mirna Yanes Gonzalez"
    assert exe["type"]     == "Affiant"
    assert exe["relation"] == "Spouse"
    assert exe["status"]   == "Accept"


def test_pick_executor_affiant_multirow_prefers_accept():
    """Somers has 6 Affiants: 1 Accept, 2 Consent, 3 Deceased. The picker
    must return the Accept row, not the first row unconditionally."""
    html = (FIXTURE_DIR / "middlesex_probate_detail_affiant_multirow_somers.html").read_text()
    parties = _parse_parties(html)
    assert len(parties) == 6, f"expected 6 parties, got {len(parties)}"
    exe = _pick_executor(parties)
    assert exe is not None
    assert exe["name"]   == "Chervilla A. Somers-Oglesby"
    assert exe["type"]   == "Affiant"
    assert exe["status"] == "Accept"


def test_pick_executor_priority_executor_beats_applicant_beats_affiant():
    """Synthetic mixed-type parties list — priority must be
    Executor > Applicant > Affiant even when they all have status=Accept."""
    mixed = [
        {"name": "Affiant Person",    "type": "Affiant",   "relation": "Child",  "status": "Accept"},
        {"name": "Applicant Person",  "type": "Applicant", "relation": "Spouse", "status": "Accept"},
        {"name": "Executor Person",   "type": "Executor",  "relation": "Spouse", "status": "Accept"},
    ]
    assert _pick_executor(mixed)["name"] == "Executor Person"

    # Drop Executor — Applicant must win over Affiant
    assert _pick_executor(mixed[:2])["name"] == "Applicant Person"

    # Drop Applicant — Affiant is the last resort but must still be picked
    assert _pick_executor([mixed[0]])["name"] == "Affiant Person"


def test_pick_executor_returns_none_when_no_fiduciary_types():
    """Parties with only non-fiduciary types (Legatee/Next of Kin/None) must
    yield None — the caller then falls back to owner_name = decedent_name."""
    non_fiduciary = [
        {"name": "Someone",       "type": "Legatee",     "relation": "Cousin", "status": "N/A"},
        {"name": "Someone Else",  "type": "Next of Kin", "relation": "Sister", "status": "Deceased"},
        {"name": "Yet Another",   "type": "None",        "relation": "Child",  "status": "N/A"},
    ]
    assert _pick_executor(non_fiduciary) is None


def test_build_notice_populates_dm_fields_from_affiant():
    """End-to-end on the Soto fixture: an Affiant must land in
    decision_maker_name/relationship/source/confidence exactly like an
    Executor would — Affiant is the small-estate equivalent for our DM
    purposes and must not be silently discarded."""
    html = (FIXTURE_DIR / "middlesex_probate_detail_affiant_soto.html").read_text()
    fields = _parse_detail_fields(html)
    parties = _parse_parties(html)
    row = {
        "pk_id": "5406116", "detail_url":
            "https://surrogatesearch.co.middlesex.nj.us/SurrogateSearch/"
            "WebPages/web_case_detail_middlesex.aspx?Q_PK_ID=5406116",
        "row_idx": 0, "decedent_name": "Soto Luis", "docket": "",
        "case_desc": "Probate", "town": "", "filed": "", "issued": "",
        "dod": "", "dob": "",
    }
    notice = _build_notice(row, fields, parties, MIDDLESEX)
    assert notice is not None, "Affiant record must not be dropped"
    assert notice.decision_maker_name         == "Mirna Yanes Gonzalez"
    assert notice.decision_maker_relationship == "Spouse"
    assert notice.decision_maker_source       == "court_record"
    assert notice.dm_confidence               == "high"
    # owner_name (→ full_name in CSV) is the Affiant, per docstring line 31
    assert notice.owner_name    == "Mirna Yanes Gonzalez"
    # decedent stays the decedent
    assert notice.decedent_name == "Luis R. Soto"


# ── CLI runner ────────────────────────────────────────────────────────


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(0 if failures == 0 else 1)
