"""Build the two lookups the Knox lien loader needs from raw Register of Deeds rows.

`knox_ftm_pull.load_liens` reads `lien_releases.json` and `lien_amounts.json`,
and until 2026-11-11 nothing in this repo wrote either one: the August files
came out of a session scratchpad. This is that producer.

  lien_releases.json   {"released_instruments": [...], "released_names": [...]}
  lien_amounts.json    {instrument: dollars}

Where the facts live in a raw grid row (16 columns, see knox_rod.py):
  [8]  document type          [10] this row's own instrument number
  [13] "Marginal": on a RELEASE this is the instrument being released
       (several, space or comma separated, when one release clears many)
  [14] consideration: the recorded lien amount (state tax liens carry none)
  [4]/[5] party name, [6] party role

A release names the ORIGINAL instrument, so matching on [13] is exact. The name
set is only a weak corroborating flag: one release does not clear a person's
other liens.

    python src/knox_lien_index.py --scratch output/knox --merge output
"""
from __future__ import annotations

import argparse
import json
import os
import re

LIEN_TYPES = ("LEN", "STL", "FTL")
RELEASE_TYPES = ("REL", "RFTL", "RESL", "RSTL")

# Instrument numbers are 15 digits: YYYYMMDD + a 7-digit sequence.
_INSTRUMENT = re.compile(r"(?<!\d)(?:19|20)\d{13}(?!\d)")
_TAGS = re.compile(r"<[^>]+>")


def _txt(cell) -> str:
    return " ".join(_TAGS.sub(" ", str(cell or "")).split())


def _money(cell) -> float | None:
    raw = _txt(cell).replace("$", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _rows(scratch: str, code: str) -> list[list]:
    path = os.path.join(scratch, "rod2_%s.json" % code)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [r for r in (data.get("aaData") or []) if isinstance(r, list) and len(r) >= 15]


def released_from_rows(rows: list[list]) -> tuple[set, set]:
    """(instruments a release names, party names appearing on a release)."""
    instruments: set[str] = set()
    names: set[str] = set()
    for r in rows:
        own = _txt(r[10])
        for m in _INSTRUMENT.finditer(_txt(r[13])):
            if m.group(0) != own:
                instruments.add(m.group(0))
        name = (_txt(r[4]) + " " + _txt(r[5])).strip()
        if name:
            names.add(name)
    return instruments, names


def amounts_from_rows(rows: list[list]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        instrument, amount = _txt(r[10]), _money(r[14])
        if instrument and amount is not None:
            out[instrument] = amount
    return out


def _load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build(scratch: str, merge_dirs: list[str] | None = None) -> dict:
    """Write both lookups into `scratch`, folding in any earlier copies.

    Releases only ever accumulate (a released lien stays released), so earlier
    indexes found in `scratch` or `merge_dirs` are unioned in rather than
    replaced. That is what lets a short gap pull stand on a year of history.
    """
    released, names, amounts = set(), set(), {}
    for folder in [scratch] + list(merge_dirs or []):
        prior = _load_json(os.path.join(folder, "lien_releases.json")) or {}
        released.update(prior.get("released_instruments") or [])
        names.update(prior.get("released_names") or [])
        amounts.update(_load_json(os.path.join(folder, "lien_amounts.json")) or {})
    before_released, before_amounts = len(released), len(amounts)

    for code in RELEASE_TYPES:
        got_i, got_n = released_from_rows(_rows(scratch, code))
        released |= got_i
        names |= got_n
    for code in LIEN_TYPES:
        amounts.update(amounts_from_rows(_rows(scratch, code)))

    with open(os.path.join(scratch, "lien_releases.json"), "w", encoding="utf-8") as fh:
        json.dump({"released_instruments": sorted(released),
                   "released_names": sorted(names)}, fh)
    with open(os.path.join(scratch, "lien_amounts.json"), "w", encoding="utf-8") as fh:
        json.dump(amounts, fh)
    return {"released": len(released), "released_added": len(released) - before_released,
            "amounts": len(amounts), "amounts_added": len(amounts) - before_amounts}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--merge", default="", help="comma-separated folders holding earlier indexes")
    a = ap.parse_args()
    stats = build(a.scratch, [m for m in a.merge.split(",") if m.strip()])
    print("released instruments: %(released)d (+%(released_added)d new)" % stats)
    print("priced lien instruments: %(amounts)d (+%(amounts_added)d new)" % stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
