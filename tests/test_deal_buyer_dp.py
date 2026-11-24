"""deal_buyer_dp's dial rows: one person running two companies keeps their checked numbers on both. Zero network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import deal_buyer_dp as dp  # noqa: E402


def _entry(rank, buyer):
    return {"rank": rank, "buyer": buyer, "phones": [{"number": "8655550101", "sources": ["registry"]}],
            "decision_makers": [{"name": "Pat Example", "confidence": "high", "source": "research", "seen": []}],
            "crm_owner": ""}


def test_a_person_running_two_companies_keeps_the_number_on_both():
    dossier = [_entry(1, "Sample Homes LLC"), _entry(2, "Sample Homes II LLC")]
    # the check is cached once per (name, number), under whichever company asked first
    cache = {"Pat Example|8655550101": {"rank": 1, "name": "Pat Example", "number": "8655550101",
                                       "phone.name_match": True, "phone.activity_score": 95,
                                       "phone.linetype": "Mobile", "phone.is_valid": True}}
    rows = dp._phone_rows(dossier, {}, cache)
    assert sorted(r["rank"] for r in rows) == [1, 2]
    assert all(r["dm_match"] == "Yes" and r["tier"] == "Dial First" for r in rows)
