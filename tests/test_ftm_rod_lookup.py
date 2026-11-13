"""rod_lookup and plan_limits.

rod_lookup: what research reads first (the deed of trust a notice cites, the tax roll's owner).
plan_limits: the check a blueprint apply makes before it creates presets.

Zero network. The recorder, the tax roll and the DataSift API are fakes. Every instrument
number, name and address is invented.

Run: python -m pytest tests/test_ftm_rod_lookup.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import plan_limits as P  # noqa: E402
import rod_lookup as L  # noqa: E402

DEED = "201903150000444"         # a deed of trust recorded 2019-03-15
VEST = "201102020000123"         # the vesting deed, recorded 2011-02-02
LATER = "202106300000999"

NOTICE = ("SUBSTITUTE TRUSTEE'S NOTICE OF SALE. WHEREAS, default has occurred in the Deed of Trust dated "
          "March 12, 2019, executed by MARLOWE QUILLFEATHER, of record in Instrument No. %s, in the Register's "
          "Office for Knox County, Tennessee. Being the same property conveyed by deed of record in Instrument "
          "No. %s. Property address: 412 Sample Hollow Rd." % (DEED, VEST))


def row(instrument, name, role, recorded, legal="", amount="", doc="TD"):
    r = [""] * 16
    r[3] = "Recorded: %s" % recorded
    r[4], r[6], r[8], r[9], r[10], r[14] = name, role, doc, legal, instrument, amount
    return r


class Recorder:
    def __init__(self, by_label_day):
        self.data, self.asked = by_label_day, []

    def __call__(self, label, d_from, d_to):
        self.asked.append((label, d_from))
        return list(self.data.get((label, d_from), []))


def recorder():
    return Recorder({
        ("TD", "03/15/2019"): [row(DEED, "QUILLFEATHER MARLOWE", "Grantor", "03/15/2019", "PrpId:081JA018", "$185,000.00"),
                               row(DEED, "FIRST EXAMPLE BANK", "Grantee", "03/15/2019", "PrpId:081JA018", "$185,000.00")],
        ("WD", "02/02/2011"): [row(VEST, "THORNBURY ESME", "Grantor", "02/02/2011", doc="WD", amount="$120,000.00"),
                               row(VEST, "QUILLFEATHER MARLOWE", "Grantee", "02/02/2011", doc="WD", amount="$120,000.00")],
    })


ROLL = {"owner": "QUILLFEATHER MARLOWE", "account": "081JA-018", "address": "412 SAMPLE HOLLOW RD",
        "delinquent_amount": 1840.5, "delinquent_years": ["2024", "2025"]}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name in ("ROD_LOOKUP_MAX_FETCHES", "ROD_DEED_LABELS", "KNOX_ROD_DOT_LABEL", "DATASIFT_PLAN_LIMITS"):
        monkeypatch.delenv(name, raising=False)


def releases(tmp_path, instruments):
    (tmp_path / "lien_releases.json").write_text(json.dumps({"released_instruments": instruments}), encoding="utf-8")
    return str(tmp_path)


# ── what the notice cites ─────────────────────────────────────────────


def test_the_instruments_a_notice_cites_are_read_in_order():
    assert L.cited_instruments(NOTICE) == [DEED, VEST]
    assert L.cited_instruments("Book 1234 Page 56, parcel 081JA018, phone 8655550100") == []
    assert L.cited_instruments(NOTICE + " again " + DEED) == [DEED, VEST]


def test_the_debt_comes_from_the_deed_of_trust_the_notice_cites(tmp_path):
    out = L.lookup("412 Sample Hollow Rd", "Knox", "Marlowe Quillfeather", NOTICE, releases(tmp_path, []),
                   fetch_rows=recorder(), roll=ROLL)
    d = out["deeds_of_trust"][0]
    assert d["instrument"] == DEED and d["amount"] == 185000.0 and d["recorded"] == "2019-03-15"
    assert d["borrowers"] == ["QUILLFEATHER MARLOWE"] and d["lenders"] == ["FIRST EXAMPLE BANK"]
    assert d["released"] is False and d["parcel_ids"] == ["081JA018"]
    assert out["debt_total"] == 185000.0


def test_the_vesting_deed_gives_one_link_of_the_chain(tmp_path):
    out = L.lookup("412 Sample Hollow Rd", "Knox", "Marlowe Quillfeather", NOTICE, releases(tmp_path, []),
                   fetch_rows=recorder(), roll=ROLL)
    assert out["chain"] == [{"instrument": VEST, "doc_type": "WD", "recorded": "2011-02-02",
                             "from": ["THORNBURY ESME"], "to": ["QUILLFEATHER MARLOWE"], "amount": 120000.0}]


def test_each_instrument_is_fetched_for_its_own_day_only(tmp_path):
    rec = recorder()
    L.lookup("412 Sample Hollow Rd", "Knox", "", NOTICE, str(tmp_path), fetch_rows=rec, roll=ROLL)
    assert rec.asked == [("TD", "03/15/2019"), ("TD", "02/02/2011"), ("WD", "02/02/2011")]


def test_a_released_loan_is_not_counted_as_debt(tmp_path):
    out = L.lookup("412 Sample Hollow Rd", "Knox", "", NOTICE, releases(tmp_path, [DEED]),
                   fetch_rows=recorder(), roll=ROLL)
    assert out["deeds_of_trust"][0]["released"] is True and out["debt_total"] is None


def test_with_no_release_index_a_paid_off_loan_cannot_be_told_and_the_answer_says_so(tmp_path):
    out = L.lookup("412 Sample Hollow Rd", "Knox", "", NOTICE, str(tmp_path), fetch_rows=recorder(), roll=ROLL)
    assert out["deeds_of_trust"][0]["released"] is None
    assert any("no release index" in x for x in out["not_checked"])


def test_a_second_loan_is_reported_as_not_checked_never_as_none(tmp_path):
    """The recorder can only be read by instrument here. Silence about a second loan would read
    as "there is none"."""
    out = L.lookup("412 Sample Hollow Rd", "Knox", "", NOTICE, str(tmp_path), fetch_rows=recorder(), roll=ROLL)
    assert any("second loan" in x for x in out["not_checked"])


def test_a_cited_instrument_that_is_not_found_is_named(tmp_path):
    out = L.lookup("412 Sample Hollow Rd", "Knox", "", NOTICE + " and Instrument No. " + LATER, str(tmp_path),
                   fetch_rows=recorder(), roll=ROLL)
    assert any(LATER in x for x in out["not_checked"])


def test_the_fetch_limit_bounds_one_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("ROD_LOOKUP_MAX_FETCHES", "1")
    rec = recorder()
    out = L.lookup("412 Sample Hollow Rd", "Knox", "", NOTICE, str(tmp_path), fetch_rows=rec, roll=ROLL)
    assert len(rec.asked) == 1 and len(out["deeds_of_trust"]) == 1 and out["chain"] == []


def test_a_notice_that_cites_no_instrument_never_touches_the_recorder(tmp_path):
    rec = recorder()
    out = L.lookup("412 Sample Hollow Rd", "Knox", "", "NOTICE TO CREDITORS, Estate of Marlowe Quillfeather",
                   str(tmp_path), fetch_rows=rec, roll=ROLL)
    assert rec.asked == [] and any("cites no instrument number" in x for x in out["not_checked"])


# ── the owner of record, and Blount ───────────────────────────────────


def test_still_owns_is_yes_when_the_roll_carries_the_name():
    assert L.still_owns("Marlowe Quillfeather", ROLL)[0] is True
    assert L.still_owns("QUILLFEATHER MARLOWE J", ROLL)[0] is True


def test_a_different_owner_is_reported_with_the_caveat_not_as_a_wrong_house():
    verdict, why = L.still_owns("Marlowe Quillfeather", dict(ROLL, owner="THORNBURY ESME"))
    assert verdict is False and "THORNBURY ESME" in why and "surviving spouse" in why


def test_no_roll_row_or_no_name_is_unknown_not_no():
    assert L.still_owns("Marlowe Quillfeather", None)[0] is None
    assert L.still_owns("", ROLL)[0] is None


def test_blount_gets_the_tax_roll_and_is_told_the_deeds_were_not_read(tmp_path):
    rec = recorder()
    out = L.lookup("77 Invented Ave", "Blount", "Marlowe Quillfeather", NOTICE, str(tmp_path), fetch_rows=rec,
                   roll=dict(ROLL, address="77 INVENTED AVE"))
    assert rec.asked == [], "Blount has no online recorder"
    assert out["still_owns"] is True and out["roll"]["delinquent_amount"] == 1840.5
    assert out["deeds_of_trust"] == [] and any("no online recorder" in x for x in out["not_checked"])


def test_the_roll_search_takes_only_the_parcel_at_this_street():
    """The county search is fuzzy; its first row is often the house next door."""
    parcels = [{"account_number": "A1", "owner": "NEIGHBOR NED", "parcel_address": "414 SAMPLE HOLLOW RD"},
               {"account_number": "A2", "owner": "QUILLFEATHER MARLOWE", "parcel_address": "412 SAMPLE HOLLOW RD"}]
    got = L.roll_owner("412 Sample Hollow Road", "Knox", search=lambda q: parcels,
                       bills=lambda acct: {"delinquent_amount": 0.0, "delinquent_year_list": []})
    assert got["owner"] == "QUILLFEATHER MARLOWE" and got["account"] == "A2"
    assert L.roll_owner("999 Sample Hollow Rd", "Knox", search=lambda q: parcels, bills=lambda a: {}) is None
    assert L.roll_owner("412 Sample Hollow Rd", "Sevier", search=lambda q: parcels, bills=lambda a: {}) is None


def test_the_summary_is_one_note_with_the_facts_and_the_gaps(tmp_path):
    out = L.lookup("412 Sample Hollow Rd", "Knox", "Marlowe Quillfeather", NOTICE, releases(tmp_path, []),
                   fetch_rows=recorder(), roll=ROLL)
    note = L.summary(out)
    assert note.startswith("Deed records check: Owner of record on the Knox County tax roll: QUILLFEATHER MARLOWE.")
    assert "original amount $185,000.00" in note and "no release in our index" in note
    assert "Delinquent county tax $1,840.50." in note and "Not checked:" in note
    assert "\n" not in note, "one paragraph, so no bare date can sit on a line of its own"


# ── the daily stage ───────────────────────────────────────────────────

UUID = "11111111-2222-3333-4444-555555555555"


class Api:
    def __init__(self):
        self.calls = []

    def call(self, path, method="GET", body=None, headers=None):
        self.calls.append((method, path, body))
        if headers and headers.get("x-http-method-override") == "GET":
            return {"results": [{"uuid": UUID, "address": {"street": "412 Sample Hollow Rd", "postal_code": "37918"}}]}
        if path.startswith("/api/internal/custom-fields/"):
            return {"results": [{"label": "Mortgage Debt", "uuid": "f-debt", "field_type": "money"}]}
        return {}


def upload_row(**kw):
    base = {"Property Street Address": "412 Sample Hollow Rd", "Property ZIP Code": "37918", "County": "Knox",
            "Notice Type": "foreclosure", "Owner First Name": "Marlowe", "Owner Last Name": "Quillfeather",
            "Source URL": "https://example.invalid/Details.aspx?ID=1001", "Notice Text": NOTICE}
    base.update(kw)
    return base


def annotate(tmp_path, rows, commit=True, api=None, **kw):
    api = api or Api()
    stats = L.annotate(rows, str(tmp_path), api=api, commit=commit, fetch_rows=recorder(),
                       roll_for=lambda r: ROLL, sleep=lambda s: None, **kw)
    return stats, api


def test_a_knox_foreclosure_gets_a_note_and_the_debt_field(tmp_path):
    stats, api = annotate(tmp_path, [upload_row()])
    assert stats["written"] == 1 and stats["with_debt"] == 1
    notes = [c[2]["notes"] for c in api.calls if c[1].endswith("/add-notes/")]
    assert len(notes) == 1 and notes[0].startswith("Deed records check:")
    fields = [c[2] for c in api.calls if c[1].endswith("/custom-field/update-values/")]
    assert fields == [[{"field_uuid": "f-debt", "value": "185000.0"}]]


def test_a_dry_run_writes_nothing_and_settles_nothing(tmp_path):
    stats, api = annotate(tmp_path, [upload_row()], commit=False)
    assert stats["looked_up"] == 1 and stats["written"] == 0 and api.calls == []
    again, _ = annotate(tmp_path, [upload_row()], commit=False)
    assert again["candidates"] == 1, "a dry run does not mark the row done"


def test_a_row_is_looked_up_once(tmp_path):
    annotate(tmp_path, [upload_row()])
    stats, api = annotate(tmp_path, [upload_row()])
    assert stats["candidates"] == 0 and api.calls == []


def test_only_knox_foreclosures_are_looked_up(tmp_path):
    rows = [upload_row(County="Blount"), upload_row(**{"Notice Type": "probate"})]
    stats, api = annotate(tmp_path, rows)
    assert stats["candidates"] == 0 and api.calls == []


def test_a_row_with_no_notice_text_is_counted_not_guessed(tmp_path):
    stats, _ = annotate(tmp_path, [upload_row(**{"Notice Text": ""})])
    assert stats["no_text"] == 1 and stats["looked_up"] == 0


def test_the_days_limit_leaves_the_rest(tmp_path):
    rows = [upload_row(**{"Source URL": "https://example.invalid/Details.aspx?ID=%d" % i}) for i in range(5)]
    stats, _ = annotate(tmp_path, rows, limit=2)
    assert stats["looked_up"] == 2 and stats["left_for_tomorrow"] == 3


# ── plan limits ───────────────────────────────────────────────────────


def blueprint(n_presets=98, boards=("Acquisitions", "Deep Prospecting")):
    return {"preset_folders": [{"title": "05. FTM", "numbered": True,
                                "presets": [{"title": "Preset %d" % i} for i in range(n_presets)]},
                               {"title": "default", "numbered": False, "presets": [{"title": "Loose"}]}],
            "boards": [{"title": b} for b in boards]}


class Account:
    def __init__(self, plan):
        self.plan, self.calls = plan, []

    def call(self, path, method="GET", body=None):
        self.calls.append(path)
        return {"plan_name": self.plan, "email": "someone@example.invalid"}


def test_a_professional_plan_cannot_take_the_full_preset_system():
    out = P.check("professional", {"presets": 98}, {"presets": 0})
    assert out["ok"] is False
    assert out["problems"] == ["the professional plan allows 8 filter presets. This account has 0 and the "
                               "blueprint would add 98."]


def test_presets_that_already_exist_do_not_count_against_the_plan():
    bp = blueprint(8)
    have = {"Preset %d" % i for i in range(8)}
    assert P.blueprint_needs(bp, have)["presets"] == 0
    assert P.check_blueprint(Account("professional"), bp, have)["ok"] is True


def test_what_fits_is_allowed_and_one_more_is_not():
    assert P.check("professional", {"presets": 3}, {"presets": 5})["ok"] is True
    assert P.check("professional", {"presets": 4}, {"presets": 5})["ok"] is False


def test_an_unknown_limit_is_reported_and_never_blocks():
    out = P.check("business", {"presets": 98}, {"presets": 0})
    assert out["ok"] is True and out["problems"] == []
    assert "limit on filter presets is not known" in out["unknown"][0]


def test_an_unknown_plan_is_reported_and_never_blocks():
    out = P.check("enterprise_custom", {"presets": 98})
    assert out["ok"] is True and "is not one this check knows" in out["unknown"][0]
    assert P.check("", {"presets": 98})["ok"] is True


def test_the_expert_plan_has_no_preset_limit():
    out = P.check("expert", {"presets": 500, "boards": 40})
    assert out["ok"] is True and out["unknown"] == []


def test_boards_are_checked_too():
    out = P.check("professional", {"boards": 2}, {"boards": 2})
    assert out["ok"] is False and "3 boards" in out["problems"][0]


def test_the_plan_is_read_from_the_accounts_own_user_route():
    acct = Account("Business")
    assert P.read_plan(acct) == "business" and acct.calls == ["/api/internal/user/"]


def test_a_failed_read_means_no_plan_not_a_crash():
    class Broken:
        def call(self, path, method="GET", body=None):
            raise RuntimeError("401")
    assert P.read_plan(Broken()) == ""
    assert P.check_blueprint(Broken(), blueprint())["ok"] is True


def test_only_the_folders_the_apply_would_create_are_counted():
    bp = blueprint(10)
    assert P.blueprint_needs(bp)["presets"] == 10
    assert P.blueprint_needs(bp, folders="all")["presets"] == 11
    assert P.blueprint_needs(bp, existing_boards={"Acquisitions"})["boards"] == 1


def test_a_published_limit_can_be_filled_in_by_a_setting(monkeypatch):
    monkeypatch.setenv("DATASIFT_PLAN_LIMITS", '{"business": {"presets": 60}}')
    assert P.check("business", {"presets": 98})["ok"] is False
    monkeypatch.setenv("DATASIFT_PLAN_LIMITS", "not json")
    assert P.check("business", {"presets": 98})["ok"] is True, "a bad override is ignored, not obeyed"


def test_the_whole_check_refuses_the_ty2_blueprint_on_a_professional_account():
    out = P.check_blueprint(Account("professional"), blueprint(98))
    assert out["ok"] is False and out["plan"] == "professional"
    assert out["checked"]["presets"] == {"limit": 8, "have": 0, "would_add": 98}


def test_the_modules_hold_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/rod_lookup.py", "src/plan_limits.py", "tests/test_ftm_rod_lookup.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
