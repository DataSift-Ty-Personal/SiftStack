"""The default outputs of the D4D Dream 100 build and the obituary ranking, pinned.

The team's idle_records workflow reuses both engines, and that work moved code inside them (the Dream
100 ranking into rank_records, the workbook's words into a parameter, the obituary reader onto the Open
API key, a capped pull). The command lines must produce exactly what they produced before. This test
runs both on invented records with the date frozen, reads every cell of both workbooks plus the ranked
JSON and the CSV, and compares a hash of all of it to the hash the code produced before that work
(commit 232638a, computed the same way). Zero network.

    python tests/test_wf_data_defaults.py
"""
import hashlib
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

# The hash of the snapshot below, from dream100_build, d4d_call_list and obituary_opportunity as they
# stood at commit 232638a (before the idle_records work), run by snapshot() exactly as here.
PINNED = "9559862ac8cc4114b7298505bdb5f9c6acca839b1bd7603babc16af4a06a7e0f"
FROZEN = date(2026, 6, 30)


class _Frozen(date):
    @classmethod
    def today(cls):
        return cls(FROZEN.year, FROZEN.month, FROZEN.day)


def _rec(u, street, value, eq, owner_uuid, first, last, lists, phones, status=None):
    return {"uuid": u, "dataflik_id": int(u[1:]) + 1000, "status": status, "lists": lists, "tags": ["D4D"],
            "address": {"street": street, "city": "Exampleton", "postal_code": "37999", "state": "TN",
                        "county": "Alder", "vacant": False},
            "owner": {"uuid": owner_uuid, "type": "person", "first_name": first, "last_name": last, "age": 70,
                      "address": {"street": "5 Mail Rd", "city": "Exampleton", "state": "TN", "postal_code": "37998"},
                      "phones": phones, "emails": []},
            "estimate_value": str(value), "equity_percent": str(eq), "last_sale_price": "40000",
            "last_sold": "2001-05-01", "rental_value": "1200", "structure_type": "Single Family Residential",
            "mls": "Off Market", "investor_score": 70, "realtor_score": 30, "on_market_score": 40}


def _ph(n, tier, status="UNKNOWN", kind="MOBILE"):
    return {"number": n, "type": kind, "tags": [tier] if tier else [], "status": status}


def _dream_fixture():
    full = {
        "u1": _rec("u1", "1 Oak Ln", 250000, 100, "o1", "Ann", "Example", ["D4D Top 3000", "Vacant"], [_ph("8655550101", "Dial First")]),
        "u2": _rec("u2", "2 Oak Ln", 180000, 60, "o1", "Ann", "Example", ["D4D Top 3000", "Tax Delinquent"], [_ph("8655550102", "")]),
        "u3": _rec("u3", "3 Pine Ct", 400000, 20, "o2", "Bo", "Sample", ["D4D Top 3000"], [_ph("8655550103", "Dial Second")]),
        "u4": _rec("u4", "4 Elm Way", 90000, 100, "o3", "Cy", "Demo", ["D4D Top 3000", "Probate"], [], status="sold"),
        "u5": _rec("u5", "5 Ash Dr", 900000, 50, "o4", "Di", "Fake", ["D4D Top 3000"], [_ph("8655550105", "Dial Third")]),
        "u6": _rec("u6", "6 Birch St", 150000, 80, "o5", "Ed", "Test", ["D4D Top 3000", "Tired Landlord"],
                   [_ph("8655550106", "", status="WRONG"), _ph("8655550107", "", kind="LANDLINE")]),
    }
    cohort = [{"saved_uuid": u, "rank": i, "score": 100 - i * 4, "county": "Alder", "neighborhood": "Nbhd %d" % i,
               "known_distress": ""} for i, u in enumerate(full, 1)]
    return full, cohort, {"8655550107": {"tier": "Dial Fourth"}}


def _obit_entry(u, street, value, obit, status=None, cf=None, lists=("Obituary",)):
    r = {"uuid": u, "address": {"street": street, "city": "Exampleton", "state": "TN", "postal_code": "37999",
                                "county": "Alder", "vacant": False},
         "owner": {"first_name": "Gus", "last_name": "Example", "type": "person",
                   "address": {"street": "9 Mail Rd", "postal_code": "37998", "city": "Exampleton", "state": "TN"},
                   "phones": [{"number": "8655550201", "tags": ["Dial First"]}], "emails": []},
         "status": status, "lists": list(lists), "tags": [], "estimate_value": value, "equity_percent": 100,
         "last_obituary_date": obit, "structure_type": "Single Family Residential", "mls": "",
         "investor_score": 80, "realtor_score": 20, "year": 1955, "sqft": 1400}
    return {"uuid": u, "thin": {"uuid": u}, "record": r,
            "custom_fields": [{"custom_field": {"label": k}, "value": v} for k, v in (cf or {}).items()], "error": None}


def _cells(path):
    from openpyxl import load_workbook
    wb = load_workbook(path)
    return {ws.title: [[(v.isoformat() if hasattr(v, "isoformat") else v) for v in row]
                       for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets}


def snapshot(b, d4d, oo, tmp: Path) -> dict:
    """Both engines' default outputs on the invented records, the date frozen in both modules."""
    keep = (b.date, oo.date, d4d.COHORT_PATH, d4d.SCORES_PATH, b.OUT, b.FULL_PATH)
    b.date = oo.date = _Frozen
    full, cohort, scores = _dream_fixture()
    (tmp / "d4d").mkdir()
    (tmp / "d4d" / "cohort.json").write_text(json.dumps(cohort), encoding="utf-8")
    (tmp / "d4d" / "phone_scores.json").write_text(json.dumps(scores), encoding="utf-8")
    out = tmp / "dream"
    out.mkdir()
    (out / "full_records.json").write_text(json.dumps(full), encoding="utf-8")
    d4d.COHORT_PATH, d4d.SCORES_PATH = tmp / "d4d" / "cohort.json", tmp / "d4d" / "phone_scores.json"
    b.OUT, b.FULL_PATH = out, out / "full_records.json"
    try:
        b.phase_build(SimpleNamespace(top=3))
        dream = {"book": _cells(sorted(out.glob("Dream100_*.xlsx"))[-1]),
                 "csv": sorted(out.glob("Dream100_*.csv"))[-1].read_text(encoding="utf-8"),
                 "ranked": json.loads((out / "dream100_ranked.json").read_text(encoding="utf-8"))}
        cache = tmp / "obit.json"
        cache.write_text(json.dumps({"pulled": "2026-06-01", "account": "datasift-apikey", "option_labels": {}, "records": [
            _obit_entry("b1", "11 Cedar Rd", 210000, "2025-01-15", cf={"Tax delinquency amount": "900"}),
            _obit_entry("b2", "12 Cedar Rd", 160000, "2025-06-01", lists=("Obituary", "Vacant")),
            _obit_entry("b3", "13 Cedar Rd", 300000, "2026-05-30"),
            _obit_entry("b4", "14 Cedar Rd", 120000, "2024-03-01", status="not_interested"),
            _obit_entry("b5", "15 Cedar Rd", 950000, "2024-03-01"),
            {"uuid": "b6", "thin": {"uuid": "b6", "address": {"street": "16 Cedar Rd"}}, "record": None,
             "custom_fields": None, "error": "HTTP 500"}]}), encoding="utf-8")
        q, x, meta = oo.assemble(cache, 3, FROZEN)
        book = oo.build_workbook(q, x, meta, tmp / "Obit.xlsx", 3, FROZEN, 0.75, 6, 50)
        obit = {"book": _cells(book), "qualified": [r["uuid"] for r in q], "scores": [r["score"] for r in q],
                "excluded": [[r.get("uuid"), r.get("reason")] for r in x], "meta": meta}
    finally:
        b.date, oo.date, d4d.COHORT_PATH, d4d.SCORES_PATH, b.OUT, b.FULL_PATH = keep
    return {"dream": dream, "obit": obit}


def digest(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import d4d_call_list as d4d
    import dream100_build as b
    import obituary_opportunity as oo
    with tempfile.TemporaryDirectory(prefix="wf_defaults_") as tmp:
        got = digest(snapshot(b, d4d, oo, Path(tmp)))
    assert got == PINNED, "the Dream 100 or obituary default output changed: %s, pinned %s" % (got, PINNED)
    print("test_wf_data_defaults: the Dream 100 and obituary default outputs match the pinned snapshot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
