"""buyer_contacts: the buyer search's buyers, rebuilt offline, and the guards on who decides and which number counts.
Zero network: buyer_search.fakes and invented names."""
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import buyer_contacts as bc  # noqa: E402
from buyer_search import client, fakes, open_api  # noqa: E402

TODAY = date(2026, 11, 23)


@pytest.fixture()
def no_registry(tmp_path, monkeypatch):
    import deal_buyer_match as dbm
    monkeypatch.setattr(dbm, "REG_DIR", tmp_path / "no_registry")
    return tmp_path


def test_rebuild_from_kept_deeds_matches_the_live_search(no_registry):
    work = no_registry / "work"
    live = client.annotate(open_api.find_buyers(dict(fakes.SIFTMAP_SUBJECT), client=fakes.sample_siftmap(),
                                                work=work, today=TODAY))
    again = bc.rebuild(work, dict(fakes.SIFTMAP_SUBJECT), today=TODAY)
    assert [(b["buyer_full_name"], b["score"]) for b in again["buyers"]] == \
        [(b["buyer_full_name"], b["score"]) for b in live["buyers"]]
    assert again["pricing"]["usual_price"] == 125000 and again["buyers"][0].get("target_rank") == 1


@pytest.mark.parametrize("buyer, want", [
    ({"buyer_full_name": "Dana Sample", "is_buyer_corporation": False}, "Dana Sample"),
    ({"buyer_full_name": "SAMPLE KAYLA", "is_buyer_corporation": False}, "Kayla Sample"),        # deed order
    ({"buyer_full_name": "Revive Sample Real Estate Investments", "is_buyer_corporation": False}, None),
    ({"buyer_full_name": "Larry V Example Revocable Living Trust", "is_buyer_corporation": True}, "Larry V Example"),
    ({"buyer_full_name": "Example Family Revocable Trust", "is_buyer_corporation": True}, None),
    ({"buyer_full_name": "Sample Homes LLC", "is_buyer_corporation": True}, None),
])
def test_who_decides_for_a_person_or_a_named_trust(buyer, want):
    dm = bc.own_decision_maker(dict(buyer, buyer_address="100 Example St", buyer_city="Sampleton",
                                    buyer_state="TN", buyer_zip="37999"))
    assert (dm or {}).get("name") == want
    if dm:
        assert dm["address_or_city"] == "100 Example St, Sampleton, TN 37999" and dm["from"] == "deed"


def test_a_record_counts_only_when_its_owner_is_the_buyer():
    rec = lambda **ow: {"owner": ow}  # noqa: E731
    assert bc.owner_is_buyer(rec(company="SAMPLE HOMES LLC"), "Sample Homes Llc")
    assert bc.owner_is_buyer(rec(first_name="Dana", last_name="Sample"), "Dana Sample")
    assert bc.owner_is_buyer(rec(first_name="Sample", last_name="Dana"), "Dana Sample")       # either order
    assert not bc.owner_is_buyer(rec(first_name="Pat", last_name="Seller"), "Sample Homes Llc")
    # a person who runs the company counts once research or the registry names them
    assert bc.owner_is_buyer(rec(first_name="Stacy", last_name="Example"), "Sample Homes Llc", ["Stacy Example"])
    assert not bc.owner_is_buyer(rec(first_name="Pat", last_name="Seller"), "Sample Homes Llc", ["Stacy Example"])


def _web(pages=None, search=None):
    return SimpleNamespace(cache={"page": {u: {"ok": True, "text": t} for u, t in (pages or {}).items()},
                                  "search": search or {}})


def test_a_person_counts_only_when_a_page_names_both_them_and_the_company():
    web = _web({"https://example.com/about": "Sample Homes LLC was founded by Pat Example in Sampleton."},
               {"q": {"results": [{"link": "https://example.org/x", "title": "Sample Homes",
                                   "snippet": "Organizer: Jordan Placeholder"}]}})
    dm = lambda name, url: {"name": name, "evidence": [{"url": url, "quote": "x"}]}  # noqa: E731
    assert bc.valid_person(web, "Sample Homes LLC", dm("Pat Example", "https://example.com/about"))
    assert bc.valid_person(web, "Sample Homes LLC", dm("Jordan Placeholder", "https://example.org/x"))  # a snippet
    assert not bc.valid_person(web, "Sample Homes LLC", dm("Casey Mockup", "https://example.com/about"))
    assert not bc.valid_person(web, "Sample Homes LLC", dm("Pat Example", "https://example.net/never-read"))
    other = _web({"https://example.com/p": "Pat Example sells insurance in Sampleton."})
    assert not bc.valid_person(other, "Sample Homes LLC", dm("Pat Example", "https://example.com/p"))


def test_a_number_or_an_email_counts_only_when_its_page_shows_it():
    web = _web({"https://example.com/contact": "Call (865) 555-0142 or write info@example.com"})
    assert bc.valid_contact(web, {"type": "phone", "value": "865-555-0142", "url": "https://example.com/contact"}) == \
        "8655550142"
    assert bc.valid_contact(web, {"type": "phone", "value": "865-555-0199", "url": "https://example.com/contact"}) == ""
    assert bc.valid_contact(web, {"type": "email", "value": "info@example.com", "url": "https://example.com/contact"})
    assert bc.valid_contact(web, {"type": "email", "value": "ceo@example.com", "url": "https://example.com/contact"}) == ""


def test_trim_keeps_the_best_numbers_and_no_foil_that_is_the_buyer(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "REG_DIR", tmp_path)
    (tmp_path / "phone_scores.json").write_text(json.dumps({"6155550001": {"tier": "Dial First"},
                                                            "8655550002": {"tier": "Dial Fourth"}}), encoding="utf-8")
    phones = [{"number": "8655550002", "type": "Mobile"}, {"number": "6155550001", "type": "Landline"},
              {"number": "8655550003", "type": "Mobile", "status": "WRONG"},
              {"number": "8655550004", "type": "Mobile", "status": "CORRECT"},
              {"number": "8655550005", "type": "Mobile", "dnc": True},
              {"number": "8655550006", "type": "Mobile"}, {"number": "8655550007", "type": "Mobile"}]
    dms = [{"name": "Dana Sample", "confidence": "high"}, {"name": "Pat Example", "confidence": "medium"},
           {"name": "Jo Third", "confidence": "medium"}]
    (tmp_path / "dossier.json").write_text(json.dumps([{"rank": 1, "buyer": "Dana Sample", "phones": phones,
                                                        "decision_makers": dms, "crm_owner": "Dana Sample"}]),
                                           encoding="utf-8")
    bc.trim(tmp_path)
    d = json.loads((tmp_path / "dossier.json").read_text(encoding="utf-8"))[0]
    nums = [p["number"] for p in d["phones"]]
    assert nums[0] == "8655550004" and nums[1] == "6155550001" and len(nums) == bc.MAX_NUMBERS
    assert "8655550003" not in nums and "8655550005" not in nums           # marked wrong, do not call
    assert [x["confidence"] for x in d["decision_makers"]] == ["high", "medium", "low"]
    assert d["crm_owner"] == "" and d["phones_all"] == 7


def test_a_record_with_no_buyer_name_is_set_aside():
    assert client.not_a_target("Record Owner") == "no buyer name on the record"
    assert client.not_a_target("UNKNOWN") == "no buyer name on the record"
    assert client.not_a_target("Record Owner Holdings LLC") == ""
