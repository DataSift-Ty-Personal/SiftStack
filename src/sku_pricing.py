"""Locked master-material-list pricing for the rehab engine.

Reads data/master_materials_locked_37914.json (written ONLY by
`python src/material_list.py --master --zip 37914 --lock`) and prices the
MATERIAL side of each rehab-engine category from real, Knox-local Home Depot
SKUs. Labor never comes from here; the engine keeps its labor tables and the
regional multiplier applies to labor only. Locked prices are already local to
zip 37914, so they must NEVER be multiplied by the 0.88/0.86 regional factor
(that would double-discount materials ~12%).

Grade map: engine tier 1/2/3 -> Budget/Standard/Upgrade. Tier 4
(Premium/Custom) is off-list by definition and stays on the legacy tables.
A category whose basket hits a missing SKU falls back to the engine table for
that whole category, loudly (the "outstanding random issue" clause).
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).parent.parent / "data" / "master_materials_locked_37914.json"
LOCKED_ZIP = "37914"
SKU_REGIONS = ("knoxville", "blount")
GRADE_BY_TIER = {1: "budget", 2: "standard", 3: "upgrade"}

_cache: dict = {"loaded": False, "data": None}


class _MissingSKU(Exception):
    pass


def load_locked() -> dict | None:
    """Load + validate the locked list once per process. None = engine fallback."""
    if _cache["loaded"]:
        return _cache["data"]
    _cache["loaded"] = True
    if not DATA_PATH.exists():
        logger.warning("Locked master material list missing (%s); "
                       "rehab materials fall back to engine tables", DATA_PATH)
        return None
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Locked master material list unreadable (%s); engine fallback", exc)
        return None
    if not data.get("locked") or data.get("zip") != LOCKED_ZIP:
        logger.warning("Locked list failed validation (locked=%s zip=%s); engine fallback",
                       data.get("locked"), data.get("zip"))
        return None
    data["_rows"] = {r["key"]: r for r in data.get("rows", [])}
    data["_allow"] = {a["key"]: a for a in data.get("allowances", [])}
    data["_stamp"] = f"locked_sku {data['zip']} pulled {data.get('pulled', '?')}"
    _cache["data"] = data
    return data


def reset_cache() -> None:
    """Test hook: forget the loaded file."""
    _cache["loaded"], _cache["data"] = False, None


def _price(locked: dict, key: str, tier: int) -> float:
    """Unit price for a row at the tier's grade; missing grade falls back to
    standard (fixed/commodity rows only carry standard)."""
    row = locked["_rows"].get(key)
    if row is None:
        raise _MissingSKU(key)
    grade = row["grades"].get(GRADE_BY_TIER.get(tier, "standard"))
    if grade is None or grade.get("quote"):
        grade = row["grades"].get("standard")
    if grade is None or grade.get("quote"):
        raise _MissingSKU(key)
    return float(grade["unit_price"])


def _allow(locked: dict, key: str) -> float:
    a = locked["_allow"].get(key)
    if a is None:
        raise _MissingSKU(f"allowance:{key}")
    return float(a["amount"])


# ── Category baskets ─────────────────────────────────────────────────
# Each returns {group_label: cost} for ONE unit of the category, using the
# quantity drivers from the MASTER catalog / material_list build_lines().

def _kitchen(locked: dict, ctx: dict, tier: int) -> dict:
    cabinets = (_price(locked, "base_cabinets_24_in", tier) * 6
                + _price(locked, "wall_cabinets_30_in", tier) * 6
                + _price(locked, "cab_pulls", tier))
    if tier >= 3:
        countertops = _allow(locked, "fabricated_quartz_countertop") * 27  # ~27 sf, quote basis
    else:
        countertops = _price(locked, "countertops", tier) * 2  # 2 sections per kitchen
    appliances = (_price(locked, "range", tier) + _price(locked, "otr_micro", tier)
                  + _price(locked, "dishwasher", tier) + _price(locked, "fridge", tier))
    fixtures = _price(locked, "kitchen_sink", tier) + _price(locked, "kitchen_faucet", tier)
    groups = {"cabinets": cabinets, "countertops": countertops,
              "appliances": appliances, "fixtures": fixtures}
    if tier >= 2:
        groups["backsplash"] = _price(locked, "backsplash", tier) * 35  # sf per kitchen
    return groups


def _bath(locked: dict, ctx: dict, tier: int, master: bool) -> dict:
    vanity = _price(locked, "vanity36" if master else "vanity24", tier)
    tub_shower = _price(locked, "bathtub", tier) + _price(locked, "shower_valve", tier)
    if tier >= 3:
        tub_shower += _price(locked, "tile", tier) * 60  # tiled walls replace the surround
    else:
        tub_shower += _price(locked, "tub_surround", tier)
    tile = _price(locked, "tile", tier) * 45  # bath floor
    fixtures = (_price(locked, "bath_faucet", tier) + _price(locked, "bath_fan", tier)
                + _price(locked, "towel_set", tier))
    if master or tier >= 2:
        fixtures += _price(locked, "medicine_cab", tier)
    return {"vanity": vanity, "toilet": _price(locked, "toilet", tier),
            "tub_shower": tub_shower, "tile": tile, "fixtures": fixtures}


def _flooring(locked: dict, ctx: dict, tier: int) -> dict:
    return {"lvp": _price(locked, "lvp", tier) * ctx["sqft"] * 1.1}


def _paint_interior(locked: dict, ctx: dict, tier: int) -> dict:
    buckets = math.ceil(ctx["sqft"] / 350)
    primer = math.ceil(ctx["sqft"] / 1400)
    return {"paint": _price(locked, "paint_int_5gal", tier) * buckets
            + _price(locked, "primer_5gal", tier) * primer}


def _windows(locked: dict, ctx: dict, tier: int) -> dict:
    return {"windows": _price(locked, "window", tier) * ctx["window_count"]}


def _roof(locked: dict, ctx: dict, tier: int) -> dict:
    roof_sqft = ctx["roof_sqft"]
    squares = roof_sqft / 100
    perimeter = 4 * math.sqrt(ctx["sqft"]) * 1.15
    bundles = math.ceil(roof_sqft * 3 / 100) + 3  # +1 square waste
    total = (_price(locked, "shingles", tier) * bundles
             + _price(locked, "underlayment", tier) * math.ceil(squares / 10)
             + _price(locked, "drip_edge", tier) * math.ceil(perimeter / 10)
             + _price(locked, "ridge_cap", tier) * max(1, math.ceil(math.sqrt(ctx["sqft"]) * 1.15 / 25))
             + _price(locked, "osb", tier) * 9
             + _price(locked, "roof_nails", tier) * max(1, math.ceil(squares / 15))
             + _price(locked, "pipe_boot", tier) * 2)
    return {"roof": total}


def _hvac(locked: dict, ctx: dict, tier: int) -> dict:
    return {"hvac": _price(locked, "heat_pump", tier) + _price(locked, "thermostat", tier)
            + _allow(locked, "heat_pump_scope_kit")}


def _electrical(locked: dict, ctx: dict, tier: int) -> dict:
    beds = ctx["beds"]
    devices = (_price(locked, "outlets", tier) + _price(locked, "switches", tier)
               + _price(locked, "gfci", tier) * 3
               + _price(locked, "smoke", tier) * (beds + 1)
               + _allow(locked, "misc_devices_plates_low_volt"))
    if tier == 1:
        return {"electrical": devices}
    panel = _price(locked, "panel_200", tier) + _allow(locked, "breaker_set")
    if tier == 2:
        total = (devices + panel + _price(locked, "romex_12_2", tier)
                 + _price(locked, "romex_14_2", tier)
                 + _price(locked, "flush_mount", tier) * 3
                 + _price(locked, "vanity_light", tier) * ctx["full_baths"])
        return {"electrical": total}
    total = (devices + panel
             + _price(locked, "romex_12_2", tier) * 3
             + _price(locked, "romex_14_2", tier) * 4
             + _price(locked, "gfci", tier) * 3          # rewire doubles the GFCI count
             + _price(locked, "outlets", tier)            # second pack for the rewire
             + _price(locked, "co_det", tier)
             + _price(locked, "flush_mount", tier) * 3
             + _price(locked, "ceiling_fan", tier) * (beds + 1)
             + _price(locked, "vanity_light", tier) * ctx["full_baths"])
    return {"electrical": total}


def _plumbing(locked: dict, ctx: dict, tier: int) -> dict:
    fittings = _allow(locked, "pex_fittings_valves_manifold")
    if tier == 1:
        return {"plumbing": fittings}  # repair allowance only; no repipe at tier 1
    return {"plumbing": fittings + _price(locked, "water_heater", tier)
            + _price(locked, "pex_half", tier) * 4 + _price(locked, "pex_34", tier) * 2}


def _exterior(locked: dict, ctx: dict, tier: int) -> dict:
    buckets = {1: 2, 2: 3, 3: 4}.get(tier, 3)
    return {"paint": _price(locked, "paint_ext_5gal", tier) * buckets,
            "landscaping": _allow(locked, "landscaping_and_curb")
            + _price(locked, "mulch", tier) * 20}


_BUILDERS = {
    "kitchen": _kitchen,
    "master_bath": lambda lk, ctx, t: _bath(lk, ctx, t, master=True),
    "secondary_bath": lambda lk, ctx, t: _bath(lk, ctx, t, master=False),
    "flooring": _flooring,
    "paint_interior": _paint_interior,
    "windows": _windows,
    "roof": _roof,
    "hvac": _hvac,
    "electrical": _electrical,
    "plumbing": _plumbing,
    "exterior": _exterior,
}


def category_materials(category: str, ctx: dict, tier: int, locked: dict) -> dict | None:
    """Basket for one category -> {group: cost}, or None (engine fallback).

    None means an item the basket needs is off the locked list; the caller
    keeps the whole category on the legacy engine table so the estimate never
    silently mixes half-SKU numbers.
    """
    builder = _BUILDERS.get(category)
    if builder is None or locked is None:
        return None
    try:
        return builder(locked, ctx, tier)
    except _MissingSKU as exc:
        logger.warning("SKU basket for %s missing '%s' in the locked list; "
                       "category falls back to engine tables", category, exc)
        return None
