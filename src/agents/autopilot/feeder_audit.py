"""Feeder audit (build plan 0.8): every SiftMap feeder, what it adds, and what should change.

A feeder is a saved SiftMap filter with auto-add switched on. Every day it adds each new match to
the account with the tags and lists it names, and every add spends the record allowance. Nobody
looks at a feeder once it runs, so they drift: the Tier 2 program was retired in August and its ten
feeders still add records daily, the Priority feeders still stamp a Tier 2 tag on what they add, the
same free and clear filter was saved three times, and the obituary feeders pull any house at any
price, listed or not, with no tag to find them by.

This tool reads every filter on the account and writes two things for the owner: one row per
feeder (what it is, whether it adds, what it stamps, the value, equity, year and listing filters it
carries) and the list of proposed changes with the reason for each.

It changes nothing and cannot: the one function that touches the network builds a GET and has no
argument that would make it anything else. Applying the plan is a separate, approved step (the
route the sold guard used to switch off the nightly tagging).

The three filters that handed out the "recently sold" tag every night are listed as off on purpose.
They were switched off on 2026-11-12 because the rule behind them marked 25,715 records sold on the
owner's own purchase; switching one back on re-tags every restored lead the same night.

THE LADDER (build plan 5.3). Handed a county's doors-per-deal ladder, the audit also says how the
Priority 1 feeders line up with it: which rungs have no feeder adding records, and which feeders
lack the base filters (built 2010 or older, owned seven years), the doors-per-deal figure in their
description, or the second feeder for the records the seven-year filter drops. This part only ever
NAMES things. It lives outside the change list on purpose: `feeder_apply` sends the change list,
and a feeder that starts adding records spends the record allowance the same night, so a missing
rung is something a person creates after reading this, never something a tool does.

    python src/agents/autopilot/feeder_audit.py plan
    python src/agents/autopilot/feeder_audit.py plan --date 2026-11-13 --cache <file>
    python src/agents/autopilot/feeder_audit.py plan --ladder <ladder.json> --fips 47093,47009
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
ROOT = SRC.parent
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would be first on the path and shadow modules of the same name.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

MAP = "https://map.reisift.io"
OUT_DIR = ROOT / "output" / "autopilot" / "feeder_audit"

# ==================================================================================================
# THE TABLE. Edit here, run `plan` again, read the plan.
# ==================================================================================================

# The buy box.
VALUE_CAP = 700000
OBITUARY_EQUITY_FLOOR = 30
NOT_LISTED = ["not_listed"]

# Filters that must stay off, by id, with the reason. Never propose anything for these.
NEVER_ON = {
    3241: "the old nightly 'recently sold' tagging",
    4076: "the old nightly 'recently sold' tagging (Knox)",
    4084: "the old nightly 'recently sold' tagging (Blount)",
}
NEVER_ON_SINCE = "2026-11-12"
SOLD_TAG = "recently sold"          # any other filter that hands out this tag is treated the same way

# The retired program: a feeder named for it is switched off, and its tag comes off the feeders
# that were renamed into the Priority program but kept stamping it.
RETIRED_WORD = "tier 2"
RETIRED_TAG = "Tier 2"
PRIORITY_WORDS = ("priority 1", "priority 2", "prioity 1", "prioity 2")     # one live feeder is spelled this way

# What makes a feeder an obituary feeder, and the tag its records should carry.
OBITUARY_WORD = "obituary"
OBITUARY_DISTRESS = ("is_obituary_property", "is_pre_probate")
OBITUARY_TAG = "Obituary"

# Filter keys worth a sentence when a feeder carries them (measured on earlier dates, not tonight).
KEY_NOTES = {
    "preset_absentee_owners": "uses the absentee preset switch, which measured as a wider group than the absentee "
                              "distress flag the list ranking is built on (31,574 against 20,154 in Knox, 2026-08)",
    "preset_free_and_clear": "carries a key SiftMap ignores (measured 2026-08), so it is wider than its name",
    "preset_vacant_property": "carries a key SiftMap ignores (measured 2026-08), so it is wider than its name",
    "year_built_min": "carries a year key SiftMap ignores (measured 2026-09); the working key starts with extra_",
    "year_built_max": "carries a year key SiftMap ignores (measured 2026-09); the working key starts with extra_",
}

# The ladder check: what a Priority 1 feeder should carry (lessons 3-03 and the Challenge).
LADDER_PRIORITY = 1
BUILT_BY = 2010                    # built this year or earlier: new construction stays out
YEARS_OWNED = 7                    # owned at least this long, in place of an equity filter
UNKNOWN_SALE_EQUITY_FLOOR = 30     # the second feeder, for records with no known sale date
YEAR_KEY, ALIAS_YEAR_KEY = "extra_year_built_max", "year_built_max"
OWNED_KEY, UNKNOWN_SALE_KEY = "extra_years_owned_min", "extra_last_sale_date_unknown"
AI_KEY = "investor_off_market_score_min"
# The same list under the names the two ladder files and the feeders use for it.
FREE_CLEAR = "free and clear"
SAME_KEY = {"is_free_and_clear": FREE_CLEAR, "preset_free_and_clear": FREE_CLEAR, "preset_free_clear": FREE_CLEAR}
AI_MARKS = ("investor_score", "investor_off_market_score", "ai_score")
SENIOR_VACANT = {"is_senior_homeowners", "is_vacant_property"}

# ==================================================================================================
# End of the table.
# ==================================================================================================

_STD = {"accept": "application/json, text/plain, */*", "origin": "https://beta.reisift.io",
        "referer": "https://beta.reisift.io/"}


class FeederAuthError(RuntimeError):
    """The key was refused. The run stops here; no other credential is tried."""


class FeederError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------ reading
def http_get(url: str, key: str, timeout: float = 45.0):
    """One GET to SiftMap with the Open API key. There is no method argument on purpose."""
    if not url.startswith(MAP + "/"):
        raise FeederError("refusing to read %s: not a SiftMap address" % url.split("?")[0])
    req = urllib.request.Request(url, headers=dict(_STD, authorization="Api-Key " + key), method="GET")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 401:
                raise FeederAuthError("SiftMap answered 401 to the Open API key (%s). Stopped: this tool does not look "
                                      "for another credential." % body)
            if e.code == 429 and attempt < 3:
                try:
                    wait = float((e.headers.get("Retry-After") if e.headers else "") or "")
                except ValueError:
                    wait = 10.0 * (attempt + 1)
                time.sleep(min(wait, 90))
                continue
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(3 + 4 * attempt)
                continue
            raise FeederError("HTTP %s from SiftMap: %s" % (e.code, body))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(3 + 4 * attempt)
                continue
            raise FeederError("could not reach SiftMap: %s" % e)
    raise FeederError("SiftMap kept refusing")


def fetch_filters(key: str, *, get=http_get, gap: float = 2.0, max_pages: int = 40, sleep=time.sleep) -> dict:
    """Every filter on the account. The listing pages at ten rows and ignores a limit, so the only
    honest way to the end is to follow `next` until there is none."""
    rows, url, pages, total = [], MAP + "/filters/?page_size=100", 0, None
    while url:
        if pages >= max_pages:
            raise FeederError("the listing did not end after %d pages" % max_pages)
        if pages:
            sleep(gap)
        r = get(url, key)
        pages += 1
        if isinstance(r, list):
            rows += r
            break
        if not isinstance(r, dict) or not isinstance(r.get("results"), list):
            shape = sorted(r)[:8] if isinstance(r, dict) else type(r).__name__
            raise FeederError("unexpected listing shape: %s" % shape)
        rows += r["results"]
        total = r.get("count", total)
        url = r.get("next") or None
    ids = [r.get("id") for r in rows]
    if len(set(ids)) != len(ids):
        raise FeederError("the listing repeated a filter across pages; read it again")
    if isinstance(total, int) and total != len(rows):
        raise FeederError("the listing says %d filters and %d came back" % (total, len(rows)))
    return {"rows": rows, "pages": pages, "at": datetime.now().isoformat(timespec="seconds")}


# -------------------------------------------------------------------------------------------- facts
def _names(items) -> list[str]:
    out = []
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def _codes(value) -> list[str]:
    return [c.strip() for c in str(value or "").split(",") if c.strip()]


def facts(row: dict) -> dict:
    """What one saved filter is, in the words the report uses. Nothing personal is carried over
    (who created it stays in the account)."""
    fd = row.get("filter_data") or {}
    f = fd.get("filters") or {}
    counties = sorted({str(a.get("county") or a.get("title") or a.get("search") or "") for a in fd.get("addresses") or []
                       if isinstance(a, dict)} - {""})
    shown = ("value_min", "value_max", "value_type", "extra_equity_percent_min", "extra_equity_percent_max",
             "extra_year_built_min", "extra_year_built_max", "extra_mls_status", "distressors_include",
             "distressors_include_min_match", "distressors_method", "type_single_family")
    return {
        "id": row.get("id"), "title": str(row.get("name") or "").strip(), "scope": row.get("scope") or "",
        "description": " ".join(str(row.get("description") or "").split()),
        "account_filter": row.get("scope") != "default",       # "default" rows are SiftMap's own built-in filters
        "active": row.get("is_active") is not False, "auto_add": bool(row.get("auto_add_enabled")),
        "replace_owners": bool(row.get("replace_owners_enabled")),
        "tags": _names(row.get("tags")), "lists": _names(row.get("lists")), "counties": counties,
        "value_min": f.get("value_min"), "value_max": f.get("value_max"), "value_type": f.get("value_type"),
        "equity_min": f.get("extra_equity_percent_min"),
        "year_min": f.get("extra_year_built_min"), "year_max": f.get("extra_year_built_max"),
        "listing": f.get("extra_mls_status"),
        "distress": sorted(_codes(f.get("distressors_include"))), "distress_min": f.get("distressors_include_min_match"),
        "single_family": bool(f.get("type_single_family")),
        "other": {k: v for k, v in sorted(f.items()) if k not in shown},
        "limit": row.get("limit"), "updated": str(row.get("updated") or "")[:10],
    }


def kind_of(f: dict) -> str:
    low = f["title"].lower()
    if f["id"] in NEVER_ON or SOLD_TAG in [t.lower() for t in f["tags"]]:
        return "sold_tagging"
    if RETIRED_WORD in low:
        return "retired"
    if OBITUARY_WORD in low or set(f["distress"]) & set(OBITUARY_DISTRESS):
        return "obituary"
    if any(w in low for w in PRIORITY_WORDS):
        return "priority"
    return "other"


def signature(f: dict) -> str:
    """Two feeders with the same signature pull the same houses: same place, same filter, the
    listing filter left out (a copy that only adds 'not listed' is still a copy)."""
    return json.dumps([f["counties"], f["value_min"], f["value_max"], f["equity_min"], f["year_min"], f["year_max"],
                       f["distress"], f["distress_min"], f["single_family"],
                       {k: v for k, v in f["other"].items() if k not in ("distressors_method", "value_type")}],
                      sort_keys=True, default=str)


# ---------------------------------------------------------------------------------------- proposals
def _money(v) -> str:
    return "none" if v in (None, "") else "${:,.0f}".format(float(v))


def _count(n: int, word: str, plural: str = "") -> str:
    return "1 %s" % word if n == 1 else "%d %s" % (n, plural or word + "s")


def propose(feeders: list[dict]) -> list[dict]:
    """The change list, in the order of the plan: retired feeders off, the retired tag off the
    Priority feeders, near-copies down to one, the obituary feeders tightened."""
    out, off = [], set()
    live = [f for f in feeders if f["active"] and f["kind"] != "sold_tagging"]

    for f in live:
        if f["kind"] == "retired" and f["auto_add"]:
            off.add(f["id"])
            out.append({"group": "retired", "id": f["id"], "title": f["title"], "do": "Switch auto-add off",
                        "set": {"auto_add_enabled": False},
                        "why": "The Tier 2 program was retired on 2026-08-25. Its feeders still add records every day, "
                               "and each add spends the record allowance."})

    for f in live:
        hit = [t for t in f["tags"] if t.lower() == RETIRED_TAG.lower()]
        if f["kind"] == "priority" and hit:
            out.append({"group": "tag", "id": f["id"], "title": f["title"], "do": "Stop adding the tag %s" % RETIRED_TAG,
                        "set": {"tags": [t for t in f["tags"] if t not in hit]},
                        "why": "A Priority feeder should stamp its Priority tag only. Records it already added keep "
                               "the old tag until they are cleaned up separately."})

    groups: dict = {}
    for f in live:
        if f["auto_add"] and f["id"] not in off:
            groups.setdefault(signature(f), []).append(f)
    for same in groups.values():
        if len(same) < 2:
            continue
        keep = sorted(same, key=lambda f: (f["listing"] != NOT_LISTED, f["id"] or 0))[0]
        for f in same:
            if f["id"] == keep["id"]:
                continue
            off.add(f["id"])
            diff = "" if f["listing"] == keep["listing"] else " The one kept also leaves out listed houses; this one does not."
            out.append({"group": "copies", "id": f["id"], "title": f["title"], "do": "Switch auto-add off",
                        "set": {"auto_add_enabled": False}, "keep_id": keep["id"], "keep_title": keep["title"],
                        "why": "It pulls the same houses as [%s] %s.%s" % (keep["id"], keep["title"], diff)})

    for f in live:
        if f["kind"] != "obituary" or f["id"] in off:
            continue
        filt, says = {}, []
        if f["value_max"] is None or float(f["value_max"]) > VALUE_CAP:
            filt["value_max"] = VALUE_CAP
            says.append("a value cap of %s (today: %s)" % (_money(VALUE_CAP), _money(f["value_max"])))
            if not f["value_type"]:
                filt["value_type"] = "est_value"
        if f["equity_min"] is None or float(f["equity_min"]) < OBITUARY_EQUITY_FLOOR:
            filt["extra_equity_percent_min"] = OBITUARY_EQUITY_FLOOR
            says.append("an equity floor of %d%% (today: %s)" % (OBITUARY_EQUITY_FLOOR,
                                                               "none" if f["equity_min"] is None else "%s%%" % f["equity_min"]))
        if f["listing"] != NOT_LISTED:
            filt["extra_mls_status"] = list(NOT_LISTED)
            says.append("leave out listed houses")
        change = {"group": "obituary", "id": f["id"], "title": f["title"], "set": {}}
        if filt:
            change["set"]["filters"] = filt
        if OBITUARY_TAG.lower() not in [t.lower() for t in f["tags"]]:
            change["set"]["tags"] = f["tags"] + [OBITUARY_TAG]
            says.append("add the tag %s to what it brings in" % OBITUARY_TAG)
        if says:
            change["do"] = "Gain " + ", ".join(says)
            change["why"] = ("An obituary feeder should stay inside the buy box and mark what it adds."
                             + ("" if f["auto_add"] else " Auto-add is off on this one today; the edit keeps it ready."))
            out.append(change)
    return out


def observations(feeders: list[dict]) -> list[str]:
    notes = []
    for f in feeders:
        if f["kind"] == "sold_tagging" and f["auto_add"]:
            notes.append("ALARM: [%s] %s hands out the %s tag and its auto-add is ON. It was switched off on purpose "
                         "on %s; it must be off." % (f["id"], f["title"], SOLD_TAG, NEVER_ON_SINCE))
    live = [f for f in feeders if f["active"] and f["auto_add"] and f["kind"] != "sold_tagging"]

    def named(rows: list[dict]) -> str:
        return ", ".join("[%s] %s" % (f["id"], f["title"]) for f in rows)

    ghosts = [f for f in feeders if not f["active"] and f["auto_add"]]
    if ghosts:
        notes.append("Deleted but still marked auto-add: %s. A deleted filter should add nothing; confirm that in the "
                     "account's activity before trusting it." % named(ghosts))

    for key, note in KEY_NOTES.items():
        hit = [f for f in live if key in f["other"]]
        if hit:
            notes.append("Auto-adding feeders %s: each %s." % (", ".join("[%s]" % f["id"] for f in hit), note))
    loose = [f for f in live if len(f["distress"]) > 1 and f["distress_min"] is None]
    if loose:
        notes.append("Two or more distress flags with nothing said on how many must match, on these auto-adding "
                     "feeders: %s. If SiftMap reads that as 'any one of them', each is wider than its name. Not "
                     "measured tonight." % named(loose))
    untyped = [f for f in live if f["value_max"] is not None and not f["value_type"]]
    if untyped:
        notes.append("A value cap that does not say which value it means (estimated, last sold or asking), on %s. "
                     "Those run on SiftMap's default; the newer feeders say estimated value."
                     % _count(len(untyped), "auto-adding feeder"))
    uncapped = [f for f in live if f["value_max"] is None and f["kind"] not in ("obituary",)]
    if uncapped:
        notes.append("No value cap at all on these auto-adding feeders: %s." % named(uncapped))
    unmarked = [f for f in live if not f["tags"] and not f["lists"]]
    if unmarked:
        notes.append("No tag and no list on these auto-adding feeders, so their records cannot be told apart later: %s."
                     % named(unmarked))
    return notes


# ------------------------------------------------------------------------------------------ ladder
def _number_in(text: str):
    digits = "".join(ch if ch.isdigit() else " " for ch in str(text or "")).split()
    return int(digits[0]) if digits else None


def ladder_rungs(source, fips: str = "", variant: str = "ai", county: str = "") -> list[dict]:
    """A county's doors-per-deal ladder as plain rungs, in ladder order.

    Both published files are read: the market share ladder (variants -> ai, each rung with a label,
    its keys, an AI score floor and its marginal doors per deal) and the standalone table (rows,
    each with a segment name, its keys and its own doors per deal). `source` may be a whole state
    file keyed by county code, one county, or a list of rungs."""
    if isinstance(source, dict) and fips and isinstance(source.get(fips), dict):
        source = source[fips]
    name = county
    if isinstance(source, dict):
        name = name or str(source.get("name") or "")
        variants = source.get("variants")
        raw = (variants.get(variant) or variants.get("lists") or []) if isinstance(variants, dict) else source.get("rows") or []
    else:
        raw = source or []
    out = []
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            continue
        label = str(r.get("label") or r.get("seg") or "").strip()
        keys = [str(k) for k in (r.get("keys") or [])]
        ai = r.get("ai")
        if not isinstance(ai, (int, float)) or isinstance(ai, bool):
            ai = _number_in(label) if (r.get("type") == "ai" or set(keys) & set(AI_MARKS)) else None
        dpd = r.get("marg_dpd") if r.get("marg_dpd") is not None else r.get("dpd")
        out.append({"n": i, "label": label or "+".join(keys), "county": name, "priority": r.get("priority"),
                    "keys": sorted({SAME_KEY.get(k, k) for k in keys if k not in AI_MARKS}),
                    "ai": int(ai) if ai is not None else None,
                    "dpd": dpd if isinstance(dpd, (int, float)) and not isinstance(dpd, bool) else None,
                    "dpd_6mo": r.get("dpd_6mo") if isinstance(r.get("dpd_6mo"), (int, float)) else None,
                    "size": r.get("marg_supply") if r.get("marg_supply") is not None else r.get("list_size"),
                    "thin": bool(r.get("thin") or r.get("est"))})
    return out


def feeder_keys(f: dict) -> tuple[list, int | None]:
    """(the lists a feeder pulls, its AI score floor). A 100% equity floor is free and clear."""
    keys = {SAME_KEY.get(k, k) for k in f["distress"]}
    keys |= {SAME_KEY.get(k, k) for k, v in f["other"].items() if k.startswith("preset_") and v is True}
    if f["equity_min"] is not None and float(f["equity_min"]) >= 100:
        keys.add(FREE_CLEAR)
    ai = f["other"].get(AI_KEY)
    return sorted(keys), (int(ai) if isinstance(ai, (int, float)) and not isinstance(ai, bool) else None)


def _same_county(f: dict, rung: dict) -> bool:
    want = (rung.get("county") or "").strip().lower()
    return not want or want in [c.strip().lower() for c in f["counties"]]


def covers(f: dict, rung: dict) -> bool:
    keys, ai = feeder_keys(f)
    return _same_county(f, rung) and keys == rung["keys"] and ai == rung["ai"]


def _is_priority(f: dict, n: int) -> bool:
    word = "priority %d" % n
    return word in f["title"].lower().replace("prioity", "priority") or word in [t.lower() for t in f["tags"]]


def _figures(rung: dict) -> list[str]:
    out = []
    for v in (rung.get("dpd"), rung.get("dpd_6mo")):
        if v is not None:
            for s in ("%.1f" % v, "%g" % round(v, 1)):
                if s not in out:
                    out.append(s)
    return out


def _gaps(f: dict, rung: dict, companions: list[dict]) -> list[str]:
    """What one Priority 1 feeder lacks, in the order a person would fix it."""
    gaps = []
    if not _is_priority(f, LADDER_PRIORITY):
        other = next((n for n in (LADDER_PRIORITY + 1, LADDER_PRIORITY + 2) if _is_priority(f, n)), 0)
        gaps.append("is named Priority %d, and this rung is Priority %d" % (other, LADDER_PRIORITY) if other
                    else "is not named or tagged Priority %d" % LADDER_PRIORITY)
    need = f["distress_min"]
    if len(f["distress"]) > 1 and isinstance(need, (int, float)) and not isinstance(need, bool) and need < len(f["distress"]):
        gaps.append("asks for any %d of its %d flags, so it pulls more than the rung" % (need, len(f["distress"])))
    year = f["year_max"]
    if year is None or float(year) > BUILT_BY:
        how = (" (it carries %s, a key measured as ignored)" % ALIAS_YEAR_KEY) if ALIAS_YEAR_KEY in f["other"] else ""
        gaps.append("no built-by-%d filter%s" % (BUILT_BY, how))
    owned = f["other"].get(OWNED_KEY)
    if not isinstance(owned, (int, float)) or isinstance(owned, bool) or owned < YEARS_OWNED:
        gaps.append("no owned-%d-years filter" % YEARS_OWNED)
    elif not companions:
        # The years-owned filter drops every record with no known sale date, which can be as many
        # records as the list itself. They are pulled by a second feeder with an equity floor.
        gaps.append("no second feeder for the records with no known sale date (%d%% equity floor)" % UNKNOWN_SALE_EQUITY_FLOOR)
    figs = _figures(rung)
    said = f.get("description") or ""
    if figs and not ("door" in said.lower() and any(x in said for x in figs)):
        gaps.append("the description does not carry the doors-per-deal figure (%s)" % figs[0])
    return gaps


def _is_companion(f: dict) -> bool:
    """The second feeder of a pair: the records with no known sale date, behind an equity floor,
    and without the years-owned filter that would drop every one of them."""
    eq = f["equity_min"]
    return (f["other"].get(UNKNOWN_SALE_KEY) is True and eq is not None and float(eq) >= UNKNOWN_SALE_EQUITY_FLOOR
            and not f["other"].get(OWNED_KEY))


def ladder_check(feeders: list[dict], rungs: list[dict], top: int = 0) -> dict:
    """The Priority 1 rungs of a ladder against the feeders that should mirror them.

    A rung is covered when a live feeder with auto-add on pulls exactly that list in that county.
    Nothing here becomes a change: the result names what is missing and stops."""
    live = [f for f in feeders if f["active"] and f.get("kind") != "sold_tagging"]
    p1 = [r for r in rungs if r.get("priority") == LADDER_PRIORITY]
    if top:
        p1 = p1[:top]
    rows, used = [], set()
    for r in p1:
        match = [f for f in live if covers(f, r)]
        mates = [f for f in live if _is_companion(f) and _same_county(f, r)
                 and (sorted(set(feeder_keys(f)[0]) - {FREE_CLEAR}), feeder_keys(f)[1]) == (sorted(set(r["keys"]) - {FREE_CLEAR}), r["ai"])]
        main = [f for f in match if f not in mates] or match
        # When several feeders pull the same rung, the one named for it speaks for the rung.
        adding = sorted((f for f in main if f["auto_add"]), key=lambda f: not _is_priority(f, LADDER_PRIORITY))
        used |= {f["id"] for f in main + mates}
        row = {"n": r["n"], "label": r["label"], "county": r["county"], "dpd": r["dpd"], "dpd_6mo": r["dpd_6mo"],
               "thin": r["thin"], "feeders": [{"id": f["id"], "title": f["title"], "auto_add": f["auto_add"]} for f in main],
               "gaps": []}
        if adding:
            row["status"] = "covered"
            row["gaps"] = _gaps(adding[0], r, [f for f in mates if f["auto_add"]])
        elif main:
            row["status"] = "saved, not adding"
        else:
            row["status"] = "small sample" if r["thin"] else "missing"
        rows.append(row)
    notes = []
    p2 = [r for r in rungs if r.get("priority") == LADDER_PRIORITY + 1]
    here = {(r.get("county") or "").strip().lower() for r in rungs} - {""}
    for f in live:
        if not f["auto_add"] or f["id"] in used or _is_companion(f):
            continue
        if here and not here & {c.strip().lower() for c in f["counties"]}:
            continue        # a feeder for another county is not judged against this county's ladder
        keys, ai = feeder_keys(f)
        named1, named2 = _is_priority(f, LADDER_PRIORITY), _is_priority(f, LADDER_PRIORITY + 1)
        if named1 and keys == [FREE_CLEAR] and ai is None:
            notes.append("[%s] %s: free and clear alone is a Priority %d list. Stacked with senior and vacant it is Priority %d."
                         % (f["id"], f["title"], LADDER_PRIORITY + 1, LADDER_PRIORITY))
        elif named2 and set(keys) == {FREE_CLEAR} | SENIOR_VACANT and ai is None:
            notes.append("[%s] %s: free and clear with senior and vacant is a Priority %d list, and this feeder is named Priority %d."
                         % (f["id"], f["title"], LADDER_PRIORITY, LADDER_PRIORITY + 1))
        elif named1 and any(covers(f, r) for r in p2):
            r = next(r for r in p2 if covers(f, r))
            notes.append("[%s] %s: named Priority %d, and on this ladder it is rung %d, Priority %d."
                         % (f["id"], f["title"], LADDER_PRIORITY, r["n"], LADDER_PRIORITY + 1))
        elif named1 and rungs and not any(covers(f, r) for r in rungs):
            notes.append("[%s] %s: named Priority %d, and no rung on this ladder is that list." % (f["id"], f["title"], LADDER_PRIORITY))
    count = lambda s: sum(1 for x in rows if x["status"] == s)  # noqa: E731
    return {"rungs": rows, "notes": notes,
            "summary": {"priority_1_rungs": len(rows), "covered": count("covered"), "missing": count("missing"),
                        "saved_not_adding": count("saved, not adding"), "small_sample": count("small sample"),
                        "covered_with_gaps": sum(1 for x in rows if x["status"] == "covered" and x["gaps"])}}


# -------------------------------------------------------------------------------------------- plan
def build_plan(rows: list[dict], *, day: str = "", pages: int = 0, read_at: str = "", ladder: list | None = None,
               ladder_source: str = "", ladder_top: int = 0) -> dict:
    every = [facts(r) for r in rows]
    feeders = [f for f in every if f["account_filter"]]
    for f in feeders:
        f["kind"] = kind_of(f)
    feeders.sort(key=lambda f: (not f["active"], not f["auto_add"], f["title"].lower()))
    changes = propose(feeders)
    by_id = {c["id"]: c for c in changes}
    for f in feeders:
        if f["kind"] == "sold_tagging":
            f["plan"] = "Off on purpose since %s. Never switch back on." % NEVER_ON_SINCE
        elif f["id"] in by_id:
            f["plan"] = by_id[f["id"]]["do"]
        else:
            f["plan"] = "No change" if f["active"] else "Deleted; nothing to do"
    live = [f for f in feeders if f["active"]]
    extra = {}
    if ladder is not None:
        # Named, never proposed: this block is not in `changes`, which is the only thing
        # feeder_apply sends. Creating a feeder is a person's decision and spends the allowance.
        extra["ladder"] = dict(ladder_check(feeders, ladder, top=ladder_top), source=ladder_source,
                               limits=["The audit names what is missing and stops. It does not create or edit a feeder: "
                                       "a new feeder starts adding records, and every add spends the record allowance.",
                                       "The base filters are %s, %s and, for the second feeder, %s with an equity floor. All "
                                       "three are in the published SiftMap reference; prove each with a count that moves "
                                       "before saving a feeder on it." % (YEAR_KEY, OWNED_KEY, UNKNOWN_SALE_KEY),
                                       "A rung marked small sample has too few sales in the data to rank on. County "
                                       "records are the source for those lists, not a SiftMap feeder."])
    return {
        "tool": "feeder_audit", "plan_date": day or date.today().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"), "read_at": read_at, "requests": pages,
        "summary": {
            "filters_listed": len(every), "built_in_skipped": len(every) - len(feeders), "feeders": len(feeders),
            "active": len(live), "deleted": len(feeders) - len(live),
            "auto_add_on": sum(1 for f in live if f["auto_add"]), "auto_add_off": sum(1 for f in live if not f["auto_add"]),
            "changes": len(changes), "by_group": {g: sum(1 for c in changes if c["group"] == g)
                                                  for g in ("retired", "tag", "copies", "obituary")},
            "auto_add_on_after": sum(1 for f in live if f["auto_add"]) - sum(
                1 for c in changes if (c["set"] or {}).get("auto_add_enabled") is False),
        },
        "never_on": [{"id": f["id"], "title": f["title"], "auto_add": f["auto_add"], "active": f["active"],
                      "why": NEVER_ON.get(f["id"]) or "it hands out the %s tag" % SOLD_TAG}
                     for f in sorted(feeders, key=lambda f: f["id"] or 0) if f["kind"] == "sold_tagging"],
        "never_on_missing": sorted(set(NEVER_ON) - {f["id"] for f in feeders}),
        "changes": changes, "observations": observations(feeders), "feeders": feeders,
        "limits": ["Nothing here was sized. Counting what a feeder pulls is a search request that sends a body to "
                   "SiftMap, and this audit only reads. The keys the obituary edits use (value_max, value_type, "
                   "extra_equity_percent_min, extra_mls_status) are in the published SiftMap reference and are "
                   "already carried by other feeders on this account; prove each with a count that moves before the "
                   "edit is applied.",
                   "Switching auto-add off deletes nothing: the saved filter stays and can be opened in the app.",
                   "Taking a tag off a feeder changes what it stamps from now on. Records already tagged keep the tag."],
        **extra,
    }


def ladder_lines(ladder: dict, limit: int = 8) -> list[str]:
    """The ladder check as plain lines, shortest first: what a monthly report posts."""
    s = ladder["summary"]
    out = ["%d Priority %d rung(s) on the ladder: %d with a feeder adding records, %d with none, %d saved but not adding"
           % (s["priority_1_rungs"], LADDER_PRIORITY, s["covered"], s["missing"], s["saved_not_adding"])
           + (", %d small sample (county records are the source)" % s["small_sample"] if s["small_sample"] else "") + "."]
    for r in ladder["rungs"]:
        where = (" (%s)" % r["county"]) if r["county"] else ""
        fig = (", %s doors per deal" % _figures(r)[0]) if _figures(r) else ""
        if r["status"] == "missing":
            out.append("No feeder for rung %d: %s%s%s." % (r["n"], r["label"], where, fig))
        elif r["status"] == "saved, not adding":
            out.append("Rung %d, %s%s: saved as [%s] %s, with auto-add off." % (
                r["n"], r["label"], where, r["feeders"][0]["id"], r["feeders"][0]["title"]))
        elif r["status"] == "covered" and r["gaps"]:
            out.append("Rung %d, [%s] %s: %s." % (r["n"], r["feeders"][0]["id"], r["feeders"][0]["title"], "; ".join(r["gaps"])))
    return out[:1] + out[1:1 + limit] + (["and %d more" % (len(out) - 1 - limit)] if len(out) - 1 > limit else []) + ladder["notes"][:limit]


# ------------------------------------------------------------------------------------------ report
def _cell(v) -> str:
    return str(v).replace("|", "/") if v not in (None, "", []) else "-"


def _value(f: dict) -> str:
    if f["value_min"] is None and f["value_max"] is None:
        return "-"
    kind = {"est_value": "estimated", "sold": "last sold", "for_sale": "asking"}.get(f["value_type"] or "", "type not set")
    return "%s to %s (%s)" % (_money(f["value_min"]), _money(f["value_max"]), kind)


def _filters(f: dict) -> str:
    parts = []
    if f["distress"]:
        how = ""
        if len(f["distress"]) > 1:
            need = f["distress_min"]
            how = " (how many must match is not set)" if need is None else " (at least %s of %d)" % (need, len(f["distress"]))
        flags = [d.replace("is_", "").replace("_", " ") for d in f["distress"]]
        parts.append((" + ".join(flags) if len(flags) <= 4 else "%d distress flags" % len(flags)) + how)
    for k, v in f["other"].items():
        if isinstance(v, bool):
            v = "yes" if v else "no"
        elif isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        parts.append("%s: %s" % (k.replace("extra_", "").replace("_", " "), v))
    return "; ".join(parts) or "-"


def render_md(plan: dict) -> str:
    s, out = plan["summary"], []
    w = out.append
    w("# SiftMap feeders: audit and change list for %s" % plan["plan_date"])
    w("")
    w("Nothing has been changed. Every filter on the account was read on %s (%s to SiftMap, no other kind of request)."
      % ((plan["read_at"] or plan["generated_at"]).replace("T", " at "), _count(plan["requests"], "read request")))
    w("")
    w("## The short version")
    w("")
    w("- Filters that belong to this account: %d (built-in SiftMap filters skipped: %d). Live: %d. Deleted: %d."
      % (s["feeders"], s["built_in_skipped"], s["active"], s["deleted"]))
    w("- Adding records every day (auto-add on): %d. Saved filters that add nothing: %d." % (s["auto_add_on"], s["auto_add_off"]))
    w("- Changes proposed: %d. Auto-add off on %s, the Tier 2 tag off %s, %s switched off, and %s tightened."
      % (s["changes"], _count(s["by_group"]["retired"], "retired Tier 2 feeder"),
         _count(s["by_group"]["tag"], "Priority feeder"), _count(s["by_group"]["copies"], "near-copy", "near-copies"),
         _count(s["by_group"]["obituary"], "obituary feeder")))
    w("- After the changes, feeders adding records daily: %d instead of %d." % (s["auto_add_on_after"], s["auto_add_on"]))
    w("")
    w("## Off on purpose: never switch back on")
    w("")
    w("These handed out the '%s' tag every night. They were switched off on %s because the rule behind them marked "
      "records sold on the owner's own purchase. Switching one back on re-tags every restored lead the same night."
      % (SOLD_TAG, NEVER_ON_SINCE))
    w("")
    for n in plan["never_on"]:
        state = "auto-add is OFF, as it should be" if not n["auto_add"] else "AUTO-ADD IS ON: THIS IS WRONG"
        w("- [%s] %s: %s%s." % (n["id"], n["title"], state, "" if n["active"] else " (the filter is deleted)"))
    for i in plan["never_on_missing"]:
        w("- [%s] was not in the listing at all (deleted for good, which is also fine)." % i)
    w("")
    w("## Every feeder")
    w("")
    w("Feeders that add records daily come first, then saved filters that add nothing, then deleted ones.")
    w("")
    w("| Id | Feeder | County | Live | Auto-add | Tags it adds | Lists it adds | Value | Equity floor | Year built | "
      "Listing filter | Other filters | Plan |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for f in plan["feeders"]:
        year = "-" if f["year_min"] is None and f["year_max"] is None else "%s to %s" % (f["year_min"] or "any", f["year_max"] or "any")
        w("| %s |" % " | ".join(_cell(x) for x in (
            f["id"], f["title"], ", ".join(f["counties"]), "yes" if f["active"] else "deleted",
            "ON" if f["auto_add"] else "off", ", ".join(f["tags"]), ", ".join(f["lists"]), _value(f),
            None if f["equity_min"] is None else "%s%%" % f["equity_min"], year,
            ", ".join(f["listing"] or []).replace("_", " "), _filters(f), f["plan"])))
    w("")
    w("## Proposed changes")
    sections = (("retired", "1. Switch auto-add off: the retired Tier 2 feeders"),
                ("tag", "2. Take the Tier 2 tag off the Priority feeders"),
                ("copies", "3. Near-copies become one"),
                ("obituary", "4. Obituary feeders: value cap, equity floor, no listed houses, a tag"))
    for group, title in sections:
        rows = [c for c in plan["changes"] if c["group"] == group]
        w("")
        w("### %s" % title)
        w("")
        if not rows:
            w("Nothing to do: no feeder needs this today.")
            continue
        whys = sorted({c["why"] for c in rows})
        if len(whys) == 1:
            w(whys[0])
            w("")
        for c in rows:
            w("- [%s] %s: %s.%s" % (c["id"], c["title"], c["do"], "" if len(whys) == 1 else " " + c["why"]))
    if plan["observations"]:
        w("")
        w("## Worth a look")
        w("")
        for o in plan["observations"]:
            w("- %s" % o)
    ladder = plan.get("ladder")
    if ladder:
        ls = ladder["summary"]
        w("")
        w("## The ladder: Priority %d rungs against the feeders" % LADDER_PRIORITY)
        w("")
        w("Nothing in this section is in the change list. It names what is missing; a person creates a feeder.")
        if ladder.get("source"):
            w("Ladder read from %s." % ladder["source"])
        w("")
        w("- Priority %d rungs on the ladder: %d. A feeder adds records for %d of them; %d have none; %d are saved "
          "with auto-add off; %d are small sample." % (LADDER_PRIORITY, ls["priority_1_rungs"], ls["covered"], ls["missing"],
                                                     ls["saved_not_adding"], ls["small_sample"]))
        w("- Of the %d with a feeder, %d lack a base filter, the second feeder or the figure in the description."
          % (ls["covered"], ls["covered_with_gaps"]))
        w("")
        for r in ladder["rungs"]:
            fig = (", %s doors per deal" % _figures(r)[0]) if _figures(r) else ""
            where = (" (%s)" % r["county"]) if r["county"] else ""
            head = "Rung %d: %s%s%s" % (r["n"], r["label"], where, fig)
            if r["status"] == "missing":
                w("- %s. NO FEEDER." % head)
            elif r["status"] == "small sample":
                w("- %s. Small sample on the ladder and no feeder: county records are the source." % head)
            elif r["status"] == "saved, not adding":
                w("- %s. Saved as %s, with auto-add off." % (head, ", ".join("[%s] %s" % (f["id"], f["title"]) for f in r["feeders"])))
            else:
                first = r["feeders"][0]
                w("- %s. Fed by [%s] %s.%s" % (head, first["id"], first["title"],
                                               " Lacks: " + "; ".join(r["gaps"]) + "." if r["gaps"] else " Nothing missing."))
        if ladder["notes"]:
            w("")
            w("Priority names that do not match the ladder:")
            w("")
            for n in ladder["notes"]:
                w("- %s" % n)
        w("")
        for line in ladder["limits"]:
            w("- %s" % line)
    w("")
    w("## What this audit did not do")
    w("")
    for line in plan["limits"]:
        w("- %s" % line)
    return "\n".join(out).rstrip() + "\n"


def write_plan(plan: dict, out_dir: Path | str = OUT_DIR) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / ("plan_%s.json" % plan["plan_date"])
    m = out_dir / ("plan_%s.md" % plan["plan_date"])
    j.write_text(json.dumps(plan, indent=1), encoding="utf-8", newline="\n")
    m.write_text(render_md(plan), encoding="utf-8", newline="\n")
    return j, m


# --------------------------------------------------------------------------------------------- cli
def open_api_key() -> str:
    """The Open API key from the environment or the repo's .env. Nothing else is ever used."""
    import datasift_api_upload as up
    key = (up.env().get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise FeederAuthError("REISIFT_API_KEY is not set (run from the repo root)")
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SiftMap feeders: read-only audit and change list.")
    sub = ap.add_subparsers(dest="cmd")
    pl = sub.add_parser("plan", help="read every filter, write the audit and the change list (the default)")
    pl.add_argument("--date", default="", help="the day the plan is for, YYYY-MM-DD (default today)")
    pl.add_argument("--out-dir", default=str(OUT_DIR))
    pl.add_argument("--cache", default="", help="a file that keeps the listing, so a re-run does not re-read")
    pl.add_argument("--gap", type=float, default=2.0, help="seconds between page reads")
    pl.add_argument("--ladder", default="", help="a doors-per-deal ladder file already on disk (a state file or one "
                                                 "county); the audit then names the Priority 1 rungs with no feeder")
    pl.add_argument("--fips", default="", help="with --ladder: the county codes to read from a state file, comma separated")
    pl.add_argument("--rungs", type=int, default=0, help="with --ladder: only the first N Priority 1 rungs of each county")
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("plan", "-h", "--help"):
        raw = ["plan"] + raw
    args = ap.parse_args(raw)
    cache = Path(args.cache) if args.cache else None
    read_now = 0
    if cache and cache.exists():
        got = json.loads(cache.read_text(encoding="utf-8"))      # the report still says when it was really read
    else:
        try:
            got = fetch_filters(open_api_key(), gap=args.gap)
        except FeederAuthError as stop:
            print("STOPPED: %s" % stop)
            return 4
        read_now = got["pages"]
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(got, indent=1), encoding="utf-8")
    ladder = None
    if args.ladder:
        source = json.loads(Path(args.ladder).read_text(encoding="utf-8"))
        codes = [c.strip() for c in args.fips.split(",") if c.strip()] or [""]
        ladder = []
        for code in codes:
            rungs = ladder_rungs(source, code)
            p1 = [r for r in rungs if r.get("priority") == LADDER_PRIORITY]
            keep = {r["n"] for r in (p1[:args.rungs] if args.rungs else p1)}
            ladder += [r for r in rungs if r.get("priority") != LADDER_PRIORITY or r["n"] in keep]
        if not ladder:
            print("STOPPED: no rungs could be read from %s (check --fips)" % args.ladder)
            return 5
    plan = build_plan(got["rows"], day=args.date, pages=got["pages"], read_at=got.get("at") or "", ladder=ladder,
                      ladder_source=Path(args.ladder).name if args.ladder else "")
    j, m = write_plan(plan, args.out_dir)
    s = plan["summary"]
    print("%d feeders (%d live, %d auto-add on); %d changes proposed; %d read requests in this run"
          % (s["feeders"], s["active"], s["auto_add_on"], s["changes"], read_now))
    if ladder is not None:
        for line in ladder_lines(plan["ladder"]):
            print(line)
    print(j)
    print(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
