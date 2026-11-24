"""An owner matched to an obituary index row: the name first, then the age at death against the owner's age today,
and the obituary date against any date the record carries. Several people share most names, so a name alone is
never a death; the age has to fit.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from . import common as C

# One index row as open_research.lookup_obituary_index writes it:
#   "SAMPLE, JOHN Q | age 84 | News Sentinel | obituary date Mar 03, 2021"
ROW_RX = re.compile(r"^([^,|]+),\s*([^|]*)\|\s*age\s*([^|]*)\|\s*([^|]*)\|\s*obituary date\s*(.+)$")


def _pday(s) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            continue
    return None


def index_rows(txt) -> list[dict]:
    out = []
    for line in str(txt or "").splitlines():
        m = ROW_RX.match(line.strip())
        if not m:
            continue
        age = re.sub(r"[^\d]", "", m.group(3))
        out.append({"last": m.group(1).strip(), "given": m.group(2).strip(), "age": int(age) if age else None,
                    "day": _pday(m.group(5)), "text": line.strip()})
    return out


def match_person(name, rows: list[dict], owner_age, obit_day: date | None, last_sale: date | None,
                 today: date | None = None) -> tuple[str, dict | None]:
    """(verdict, row): confirmed, likely, name only, name only (unconfirmed), ambiguous, not found, no name."""
    today = today or C.today()
    t = C.name_toks(name)
    if len(t) < 2:
        return "no name", None
    first, mids = t[0], t[1:-1]
    own_gen = C.gens(name)
    named = []
    for r in rows:
        g = C.name_toks(r["given"])
        if not g or not C.first_ok(first, g[0]):
            continue
        row_gen = C.gens(r["given"])
        if own_gen and row_gen and not (own_gen & row_gen):
            continue          # Jr and Sr are two people: a son owns the house his father's obituary names
        full_mid = False
        if mids and len(g) > 1:
            if mids[0][0] != g[1][0]:
                continue
            if len(mids[0]) > 1 and len(g[1]) > 1:
                if not C.first_ok(mids[0], g[1]) and mids[0] not in g[1:]:
                    continue  # both spell the middle name out and they differ: two people
                full_mid = True
        if r["day"] and last_sale and r["day"] < last_sale:
            continue          # died before this owner bought the house: a namesake
        named.append((r, full_mid, len(g) > 1))
    if not named:
        return "not found", None
    cands = []
    for r, full_mid, row_mid in named:
        age_ok = date_ok = weak = False
        if owner_age and r["age"] and r["day"]:
            years_ago = (today - r["day"]).days / 365.25
            implied = owner_age - years_ago
            counting = abs(r["age"] - implied) <= 2.5
            # Some data freezes an owner's age at death. Allowed only where it cannot be a namesake: a name the
            # index holds once, or a full middle name that agrees.
            frozen = abs(r["age"] - owner_age) <= 1.5 and (len(named) == 1 or full_mid)
            age_ok = counting or frozen
            # a frozen-age match on an old death with no full middle name can be the father: kept as likely only
            weak = age_ok and not counting and years_ago > 5 and not full_mid
            # an old death where the owner carries a middle initial the obituary does not: never checked
            weak = weak or (age_ok and years_ago > 10 and bool(mids) and not row_mid)
        if obit_day and r["day"]:
            date_ok = abs((r["day"] - obit_day).days) <= 75
        cands.append((age_ok or date_ok, age_ok and date_ok, r, date_ok, weak and not date_ok))
    strong = [c for c in cands if c[0]]
    if strong:
        best = sorted(strong, key=lambda c: (c[1], not c[4], c[2]["day"] or date(1900, 1, 1)), reverse=True)[0]
        if (not best[3] and len(cands) >= 2) or best[4]:
            return "likely", best[2]
        return "confirmed", best[2]
    if len(cands) == 1 and not owner_age:
        return "name only", cands[0][2]
    return ("name only (unconfirmed)" if len(cands) == 1 else "ambiguous"), cands[0][2]
