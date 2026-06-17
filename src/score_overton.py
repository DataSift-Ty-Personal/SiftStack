"""Trestle-score the deduped phones for 1437 Overton Ln (forward flow Step E).

Scores each unique phone for activity + line type + litigator risk, assigns a
dial tier, and prints a master dial sheet (best score first). Carries the
Tracerfy DNC flag + carrier + rank through. Reusable: add heir phones to PHONES
as they are resolved, then re-run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from phone_validator import call_trestle, assign_tier, DEFAULT_TIERS

BAR = "=" * 92

# (number, owner_label, tracerfy_type, dnc, rank, carrier)
PHONES = [
    ("8653848638", "Nicholas Chessher (owner)", "Mobile",   False, 1, "POWERTEL/T-Mobile"),
    ("8656610687", "Nicholas Chessher (owner)", "Mobile",   False, 2, "Verizon Wireless TN"),
    ("8656809399", "Nicholas Chessher (owner)", "Mobile",   True,  3, "AT&T/Cingular"),
    ("8656901299", "Nicholas Chessher (owner)", "Landline", True,  4, "BellSouth/South Central Bell"),
    ("8656881281", "Nicholas Chessher (owner)", "Landline", True,  5, "BellSouth/South Central Bell"),
]


def main():
    print(BAR)
    print("MASTER DIAL SHEET -- 1437 Overton Ln, Knoxville TN 37923 -- Nicholas Chessher (owner)")
    print("Trestle phone_intel (activity score + line type + litigator) | Tracerfy DNC carried through")
    print(BAR)

    scored = []
    for (num, owner, ttype, dnc, rank, carrier) in PHONES:
        data = call_trestle(num, cfg.TRESTLE_API_KEY, add_litigator=True)
        if data.get("error") and not data.get("is_valid"):
            scored.append({"phone": num, "owner": owner, "score": None, "tier": "ERROR",
                           "line": ttype, "valid": None, "dnc": dnc, "lit": None,
                           "carrier": carrier, "prepaid": None, "rank": rank,
                           "err": f"{data.get('error')} {data.get('detail','')}"})
            continue
        score = data.get("activity_score")
        addons = data.get("add_ons") or {}
        lit = (addons.get("litigator_checks") or {}).get("phone.is_litigator_risk")
        scored.append({
            "phone": num, "owner": owner, "score": score,
            "tier": assign_tier(score, DEFAULT_TIERS) if score is not None else "Unknown",
            "line": data.get("line_type") or ttype, "valid": data.get("is_valid"),
            "dnc": dnc, "lit": lit, "carrier": data.get("carrier") or carrier,
            "prepaid": data.get("is_prepaid"), "rank": rank, "err": None,
        })

    scored.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))

    print(f"\n{'PHONE':<12}{'SCORE':>6}  {'TIER':<12}{'LINE':<10}{'VALID':<6}{'DNC':<6}{'LIT':<6}OWNER")
    print("-" * 92)
    for r in scored:
        print(f"{r['phone']:<12}{str(r['score']):>6}  {r['tier']:<12}{str(r['line']):<10}"
              f"{str(r['valid']):<6}{str(r['dnc']):<6}{str(r['lit']):<6}{r['owner']}")
        if r["err"]:
            print(f"             !! {r['err']}")

    n = len(scored)
    print("\n" + BAR)
    print(f"Scored {n} unique phones (~${n*0.015:.2f} Trestle). Lead with highest-score, "
          f"DNC-clear mobile. Drop any litigator-risk number.")
    print(BAR)


if __name__ == "__main__":
    main()
