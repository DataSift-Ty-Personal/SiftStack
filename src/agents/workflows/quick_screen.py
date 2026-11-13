"""quick_screen: a new lead screened for Roma: the record, comps, a rough value, a go or no-go.

A lead used to reach the lead manager with nothing but the estimate on the record, and comps were
pulled by hand only for the houses someone was already excited about. The screen runs on every lead
it is handed (an ask in Slack, a DataSift webhook, or a walkthrough the appointments stage booked,
which queues it so the numbers exist before anyone drives out) and says in a few lines what the house
is worth, roughly what it costs, the most we could pay, and whether to keep going (build plan 2.8).

What a person can paste, read the way the comp_package and post_walkthrough flags read it:
  * `boundary`: a box (lat_min,lat_max,lon_min,lon_max), a list of lat,lon points, or the coordinates
    of a Google My Maps KML or GeoJSON export; "radius" (or nothing) means a circle around the house;
  * `streets`: names apart by | (the --streets form), a filter on top of the area;
  * `notes`: county card facts ("2/1, 1946 sqft, built 1938") that beat the record exactly as the
    --beds/--baths/--sqft/--year-built flags do, an asking price ("asking 95k"), and anything else,
    which is carried into the result as context.

Where the comps come from, cheapest first:
  * SiftMap's deeds by distance (Ty's standing rule, deed comps first): free, capped per run and per
    day, because Acceptable use forbids detail pulls in volume without adding the records;
  * the Zillow sold pull (OpenWebNinja, the comp_package band pull): PAID, and the point of a comp
    request, so it runs when the operator asked in Slack (an employee queued it on a Slack thread),
    in dry mode too since nothing is written to DataSift, inside AGENTS_COMPS_DAILY_USD. A screen a
    webhook or a stage started never pulls it. A pull for the same ZIP and window inside
    AGENTS_ZILLOW_CACHE_DAYS is reused instead of bought again.
The value is post_walkthrough's own math (bucket refinement, dual track, tight ARV) on those comps,
with the house held to the layout it has today: a screen never adds a bedroom (Ty, 1541 Holman).

It writes nothing to DataSift unless the box is live; then one note goes on the record and is read
back. It never texts, calls or mails anyone, and no phone number reaches Slack.

This module also holds what Roma's other deal workflows share: the record as a subject, the pasted
inputs, the comp pulls, the file lookup, the run folder and links, settings, the spend check and the
text scrub.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from .. import config, slack, store

log = logging.getLogger(__name__)
NAME = "quick_screen"

# Injected by the selftest; otherwise built on first use, over the Open API key only.
CRM = None        # agents.autopilot.crm.CRM: budgeted, shared with the autopilot stages
SIFTMAP = None    # siftmap_standalone.SiftMapClient: the documented search and detail routes
ZILLOW = None     # zillow_market_api.ZillowMarketAPI: paid

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
REGIONS = {"knox": "knoxville", "blount": "blount", "davidson": "nashville", "hamilton": "chattanooga"}
COUNTIES = {"knox": ("47093", "Knox", "TN"), "blount": ("47009", "Blount", "TN")}
DEED_CACHE_DAYS = 120
CARD = (("beds", "bed"), ("baths", "bath"), ("sqft", "sqft"), ("year_built", "built"))
TASK_RX = re.compile(r"^(?:task\s*#?\s*)?(\d+)$", re.I)
ASK_RX = re.compile(r"\bask(?:ing)?(?:\s+price)?\s*(?:is|of|:)?\s*\$?\s*(\d[\d,]*(?:\.\d+)?\s*k?)\b", re.I)


class MissingKey(RuntimeError):
    """A key the box needs is not set. Said plainly instead of falling to another login."""


# ------------------------------------------------------------------------------- settings

def setting(name: str, default):
    """One setting from the environment (Fly secrets on the box), typed like its default."""
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    try:
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


# ------------------------------------------------------------------------------- clients

def crm():
    """The budgeted DataSift client on the Open API key. Built from the key explicitly: the shared
    uploader client logs in with a password when one is configured, and the brain never does that."""
    global CRM
    if CRM is None:
        key = (os.environ.get("REISIFT_API_KEY") or "").strip()
        if not key:
            raise MissingKey("REISIFT_API_KEY is not set on this box, so DataSift cannot be read")
        from datasift_api_upload import Api
        from ..autopilot.crm import CRM as _CRM
        CRM = _CRM(api=Api(api_key=key))
    return CRM


def siftmap():
    global SIFTMAP
    if SIFTMAP is None:
        if not (os.environ.get("REISIFT_API_KEY") or "").strip():
            raise MissingKey("REISIFT_API_KEY is not set on this box, so SiftMap cannot be read")
        from siftmap_standalone import SiftMapClient
        SIFTMAP = SiftMapClient()
    return SIFTMAP


# ------------------------------------------------------------------------------- small helpers

def files_dir(task: dict, workflow: str = "") -> Path:
    d = Path(config.RUNS_DIR) / ("%s-%d" % (workflow or task.get("workflow") or NAME, int(task.get("id") or 0))) / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def link(task: dict, name: str) -> str:
    """A signed link to a file this task made; the name is quoted so a space survives Slack."""
    url = slack.file_url(int(task.get("id") or 0), name)
    return (url[: len(url) - len(name)] + quote(name)) if url else ""


def file_line(task: dict, label: str, name: str) -> str:
    url = link(task, name)
    return "%s: %s" % (label, url or "%s (on the box; set AGENTS_PUBLIC_URL for a link)" % name)


_PHONE = re.compile(r"(?<![\d$])(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")


def safe_text(text: str) -> str:
    """Every render passes through here: a full phone number becomes its last four, and a long or
    short dash that rode in on the data becomes a plain hyphen."""
    text = _PHONE.sub(lambda m: "ends %s" % m.group(3), text or "")
    return text.replace("\u2014", "-").replace("\u2013", "-")


def money(v) -> float:
    """'$92,000', '92000', '92k' or 92000 -> 92000.0; anything unreadable -> 0."""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip().lower().replace(",", "").replace("$", "")
    mult = 1000.0 if s.endswith("k") else 1.0
    try:
        return float(s.rstrip("k").strip()) * mult
    except ValueError:
        return 0.0


def usd(v) -> str:
    v = int(round(float(v or 0)))
    return ("-$%s" % format(-v, ",")) if v < 0 else ("$%s" % format(v, ","))


def cap1(s: str) -> str:
    """First letter up, the rest untouched (str.capitalize would lowercase a setting name)."""
    return (s[:1].upper() + s[1:]) if s else s


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _title(v) -> str:
    if isinstance(v, dict):
        return str(v.get("title") or v.get("name") or v.get("label") or "")
    return str(v or "")


def inputs_of(task: dict) -> tuple[dict, str]:
    p = store.payload_of(task)
    return dict(p.get("inputs") or {}), str(p.get("details") or "")


def record_id(task: dict, inputs: dict, key: str = "record") -> str:
    """The record a task is about: resolved by the workflow tool, or a uuid a caller passed."""
    u = (task.get("record_uuid") or "").strip()
    if not u and UUID_RE.match(str(inputs.get(key) or "").strip()):
        u = str(inputs[key]).strip()
    return u


def subject_of(rec: dict) -> dict:
    """The house as the record knows it. These are the county-sourced facts the comp math keys on."""
    a = rec.get("address") or {}
    street = a.get("street") or ""
    return {"uuid": rec.get("uuid") or "", "street": street, "address": street, "city": a.get("city") or "",
            "state": a.get("state") or "", "zip": str(a.get("postal_code") or "").split("-")[0],
            "county": a.get("county") or "", "lat": _f(a.get("latitude")), "lon": _f(a.get("longitude")),
            "beds": int(_f(rec.get("bedrooms"))), "baths": _f(rec.get("bathrooms")),
            "sqft": int(_f(rec.get("sqft") or rec.get("building_sqft"))), "year_built": int(_f(rec.get("year"))),
            "dataflik_id": str(rec.get("dataflik_id") or ""), "estimate_value": _f(rec.get("estimate_value")),
            "equity_percent": rec.get("equity_percent"), "structure_type": str(rec.get("structure_type") or ""),
            "status": _title(rec.get("status"))}


def region_for(county: str) -> str:
    """The rehab engine's labor region. A county outside the four it knows prices at national
    rates (the engine's Knoxville default would quietly price it at Knox labor); no county at all
    takes AGENTS_DEFAULT_REGION, which is the market this team works."""
    c = re.sub(r"\s+county$", "", (county or "").strip().lower())
    if not c:
        return str(setting("AGENTS_DEFAULT_REGION", "knoxville"))
    return REGIONS.get(c, "national")


def county_key(county: str) -> tuple | None:
    return COUNTIES.get(re.sub(r"\s+county$", "", (county or "").strip().lower()))


def field_money(fields: dict, setting_name: str, default_labels: str) -> tuple[float, str]:
    """(dollars, label) off the first custom field whose label is in the setting's list. The account
    has no fixed field for an asking or contract price, so the labels are a setting, not a guess."""
    labels = [x.strip().lower() for x in str(setting(setting_name, default_labels)).split(",") if x.strip()]
    for label, value in (fields or {}).items():
        if label.strip().lower() in labels and money(value):
            return money(value), label
    return 0.0, ""


def read_record(task: dict, inputs: dict, key: str = "record") -> tuple[dict | None, dict, str]:
    """(record, custom fields, "") or (None, {}, why). Never raises for an ordinary miss."""
    u = record_id(task, inputs, key)
    if not u:
        return None, {}, "no record was named: give the record uuid or its street line"
    try:
        rec = crm().record(u) or {}
    except MissingKey as exc:
        return None, {}, str(exc)
    except Exception as exc:  # noqa: BLE001  (a refused or failed read is said, not raised)
        return None, {}, "the record could not be read: %s" % str(exc)[:200]
    if not rec.get("uuid"):
        return None, {}, "no record with id %s" % u
    try:
        fields = crm().custom_fields(u) or {}
    except Exception as exc:  # noqa: BLE001
        log.info("custom fields not read for %s: %s", u, exc)
        fields = {}
    return rec, fields, ""


def locate(task: dict, inputs: dict) -> tuple[dict | None, dict, str, bool]:
    """(record, custom fields, why, not_in_datasift). The house a request names: the record a webhook
    or the workflow tool resolved, a uuid, or a street line, which must match exactly one record.

    A Slack ask names the house by `address` rather than `record` on purpose: the tool resolves a
    `record` input to an id and the queue then refuses the same workflow on the same record for a
    day, so a second screen with a pasted boundary or new notes would never run."""
    import post_walkthrough as pw
    ref = str(inputs.get("address") or inputs.get("record") or "").strip()
    if task.get("record_uuid") or UUID_RE.match(ref):
        rec, fields, why = read_record(task, {"record": ref})
        return rec, fields, why, False
    if not ref:
        return None, {}, "name the house: a record uuid or its street line", False
    try:
        row, why = pw.find_record(crm(), ref)
    except MissingKey as exc:
        return None, {}, str(exc), False
    except Exception as exc:  # noqa: BLE001
        return None, {}, "the record search failed: %s" % str(exc)[:200], False
    if not row:
        return None, {}, why, why.startswith("no Sift record")
    rec, fields, why = read_record({"record_uuid": row["uuid"]}, {})
    return rec, fields, why, False


# ------------------------------------------------------------------------------- what was pasted

def area_inputs(inputs: dict, details: str = "") -> dict:
    """The comp area, the street filter and the county card notes from what the person typed.

    {"boundary": parsed or None, "radius": True when comps were asked for with no drawn area,
     "radius_mi": a radius they gave, "streets": pattern, "notes": parsed notes, "notes_from",
     "ask": an asking price in the notes, "why": one sentence when something could not be read}.
    The notes come from `notes`, or from the request's free text when `notes` is empty."""
    import comp_package as cp
    out = {"boundary": None, "radius": False, "radius_mi": 0.0, "streets": "", "notes": {"context": ""},
           "notes_from": "", "ask": 0.0, "why": ""}
    raw = str(inputs.get("boundary") or "").strip()
    if raw.lower().startswith("radius"):
        out["radius"] = True
        m = re.search(r"(\d+(?:\.\d+)?)", raw)
        out["radius_mi"] = min(float(m.group(1)), 5.0) if m else 0.0
    elif raw:
        b, why = cp.parse_boundary(raw)
        if not b:
            out["why"] = why
            return out
        out["boundary"] = b
    streets, why = cp.parse_streets(str(inputs.get("streets") or ""))
    if why:
        out["why"] = why
        return out
    out["streets"] = streets
    text, src = str(inputs.get("notes") or "").strip(), "notes"
    if not text and details.strip():
        text, src = details.strip(), "the request"
    if text:
        out["notes"], out["notes_from"] = cp.parse_card_notes(text), src
        m = ASK_RX.search(text)
        out["ask"] = money(m.group(1)) if m else 0.0
    return out


def comps_asked(area: dict) -> bool:
    return bool(area.get("boundary") or area.get("radius") or area.get("streets"))


def apply_notes(subj: dict, notes: dict) -> list[str]:
    """County card facts from the notes onto the subject: they beat the record, as the CLI flags do.
    Returns what was taken, in words ("2 bed", "1,946 sqft")."""
    said = []
    for key, word in CARD:
        v = notes.get(key)
        if v:
            subj[key] = int(v) if key != "baths" else float(v)
            said.append(("%s sqft" % format(int(v), ",")) if key == "sqft" else
                        ("built %d" % int(v) if key == "year_built" else "%g %s" % (v, word)))
    return said


def _layout(beds, baths) -> str:
    return "%s bed %s bath" % (("%g" % float(beds)) if beds else "?", ("%g" % float(baths)) if baths else "?")


def notes_beat_walk(walk: dict, notes: dict) -> tuple[dict, list[str]]:
    """An explicit operator note beats what a photo suggests: a layout in the notes holds the walk's
    target and its single plan, unless the walk says the reconfiguration was verified."""
    beds, baths = notes.get("beds"), notes.get("baths")
    if not (beds or baths) or walk.get("reconfig_verified"):
        return walk, []
    w, said = dict(walk), []
    for key in ("target_config", "single_scenario"):
        part = dict(w.get(key) or {})
        if key == "single_scenario" and not part:
            continue
        before = (part.get("beds"), part.get("baths"))
        if beds:
            part["beds"] = int(beds)
        if baths:
            part["baths"] = float(baths)
        if any(before) and before != (part.get("beds"), part.get("baths")):
            said.append("the walk's %s of %s was held to the %s in the notes (an operator note beats a photo)" % (
                "target" if key == "target_config" else "plan", _layout(*before),
                _layout(part.get("beds"), part.get("baths"))))
        w[key] = part
    return w, said


# ------------------------------------------------------------------------------- files named in a request

def deals_dir() -> Path:
    return Path(str(setting("AGENTS_DEALS_DIR", str(Path(config.DATA_DIR) / "deals"))))


def workspace_of(task: dict) -> Path:
    """The employee's own workspace (team.workspace): where Roma writes a walk from the photos."""
    return Path(config.WORKSPACES_DIR) / str(task.get("agent") or "sales")


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def find_file(ref: str, kind: str, task: dict) -> tuple[Path | None, str]:
    """(path, "") for a deal file named in a request, or (None, why). kind: pack, walk or spec.

    Three ways to name one: a task number ("task 41": the file that underwrite run saved), a name or
    a relative path (looked up in the employee's workspace, then the deals folder), or an absolute
    path. Only the employee's workspace, the run folders and the deals folder are ever opened: a path
    typed in Slack must never read the box's own files, so anything else is refused and said."""
    ref = str(ref or "").strip().strip("'\"")
    if not ref:
        return None, "no %s file was named" % kind
    m = TASK_RX.match(ref)
    if m:
        hits = sorted(Path(config.RUNS_DIR).glob("*-%s/files/*_%s.json" % (m.group(1), kind)))
        return (hits[0], "") if hits else (None, "task %s saved no %s file" % (m.group(1), kind))
    roots = [workspace_of(task), Path(config.RUNS_DIR), deals_dir()]
    p = Path(ref)
    if p.is_absolute():
        cands = [p]
    else:
        cands = [base / p for base in (workspace_of(task), deals_dir())]
        if not p.suffix:
            cands += [base / (ref + ".json") for base in (workspace_of(task), deals_dir())]
    for c in cands:
        if not any(_inside(c, r) for r in roots):
            return None, "%s is outside the workspace, the run folders and the deals folder, so it was not opened" % ref
        if c.is_file():
            return c, ""
    return None, "no %s file named %s in the workspace or the deals folder" % (kind, ref)


def read_json(p: Path) -> tuple[dict | None, str]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "%s could not be read: %s" % (p.name, str(exc)[:160])
    return (data, "") if isinstance(data, dict) else (None, "%s is not a JSON object" % p.name)


def json_input(ref, kind: str, task: dict) -> tuple[dict | None, str, str]:
    """(data, where it came from, why) for an input that is either inline JSON or a file name."""
    if isinstance(ref, dict):
        return ref, "the request", ""
    text = str(ref or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            return None, "", "the %s JSON in the request could not be read: %s" % (kind, str(exc)[:160])
        return (data, "the request", "") if isinstance(data, dict) else (None, "", "the %s JSON is not an object" % kind)
    path, why = find_file(text, kind, task)
    if not path:
        return None, "", why
    data, why = read_json(path)
    return data, path.name, why


# ------------------------------------------------------------------------------- spending

class Budget:
    """A paid step asks first and pays second. Run cap from a setting, day cap from the brain
    (config.DAILY_CAP_USD, counted on everything that is not research). The pattern is
    research/deep_prospect.Budget's."""

    def __init__(self, task_id: int, run_cap: float):
        self.task_id, self.run_cap, self.skipped = int(task_id or 0), float(run_cap), []

    def allows(self, usd_: float, what: str) -> bool:
        run_total = store.spent(task_id=self.task_id) + usd_
        day_total = store.spent(day=store.today(), research=False) + usd_
        if run_total > self.run_cap or day_total > config.DAILY_CAP_USD:
            self.skipped.append("%s skipped: about $%.2f would pass the cap (run $%.2f, day $%.0f)"
                                % (what, usd_, self.run_cap, config.DAILY_CAP_USD))
            return False
        return True

    def charge(self, usd_: float, what: str) -> None:
        store.add_spend(self.task_id, usd_, what)


def from_slack(task: dict) -> bool:
    """The operator asked in Slack: an employee queued this on a Slack thread. A DataSift webhook
    (no source) or an autopilot stage (source autopilot:...) never buys a comp pull."""
    return str(task.get("source") or "").startswith("agent:") and bool(task.get("slack_thread") or task.get("slack_channel"))


# ------------------------------------------------------------------------------- comps

def _cache_path() -> Path:
    return Path(config.DATA_DIR) / "siftmap" / "deeds.json"


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cut = (date.today() - timedelta(days=DEED_CACHE_DAYS)).isoformat()
    return {k: v for k, v in data.items() if isinstance(v, dict) and str(v.get("at") or "") >= cut}


def _save_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    tmp.replace(p)


def deed_reads_left() -> int:
    """SiftMap detail reads left today across every deal workflow (shared with deal_buyers)."""
    used = int(store.kv_get("deals:siftmap_reads:%s" % store.today()) or 0)
    return max(0, int(setting("AGENTS_DEAL_READS_PER_DAY", 300)) - used)


def count_reads(n: int) -> None:
    if n:
        key = "deals:siftmap_reads:%s" % store.today()
        store.kv_set(key, str(int(store.kv_get(key) or 0) + int(n)))


def radius_of(area: dict | None) -> float:
    return float((area or {}).get("radius_mi") or setting("AGENTS_COMPS_RADIUS_MI", 1.0))


def free_comps(subj: dict, area: dict | None = None, *, cap: int | None = None) -> dict:
    """SiftMap deed comps around the subject (or inside the pasted boundary), inside the run and
    day read caps. The search pages up to AGENTS_COMPS_SEARCH_ROWS rows; only deeds cost a read."""
    import comp_package
    months = int(setting("AGENTS_COMPS_MONTHS", 18))
    want = int(setting("AGENTS_COMPS_DEED_READS", 15)) if cap is None else int(cap)
    left = deed_reads_left()
    cache = _load_cache()
    b = (area or {}).get("boundary")
    res = comp_package.siftmap_sold(subj, siftmap(), radius_mi=radius_of(area), months=months,
                                    detail_cap=min(want, left), cache=cache,
                                    polygon=comp_package.boundary_ring(b) if b else None,
                                    max_rows=int(setting("AGENTS_COMPS_SEARCH_ROWS", 300)))
    count_reads(res["read"])
    _save_json(_cache_path(), cache)
    res["cap"] = min(want, left)
    res["day_cap_hit"] = left < want
    return res


def clip_area(listings: list, subj: dict, area: dict | None) -> list:
    """The pasted boundary and street list, applied with post_walkthrough's own clip (polygon, then
    box, then streets); with no drawn boundary, the radius circle around the house. A comp set with
    no boundary at all is how a pocket bleeds across a highway into another market."""
    import comp_package
    import post_walkthrough as pw
    area = area or {}
    b = area.get("boundary")
    if b or area.get("streets"):
        ns = argparse.Namespace(bbox=None, streets=area.get("streets") or None, polygon="")
        if b and b["kind"] == "bbox":
            ns.bbox = "%s,%s,%s,%s" % b["bbox"]
        elif b:
            ns._polygon = list(b["points"])
        listings = pw._clip(list(listings), ns)
        if b:
            return listings
    radius = radius_of(area)
    return [l for l in listings
            if comp_package.miles_between(subj["lat"], subj["lon"], l.latitude, l.longitude) <= radius]


def _zcache(zip5: str, months: int) -> Path:
    return Path(config.DATA_DIR) / "zillow" / ("%s_%dm.json" % (zip5, int(months)))


def zillow_pull(task: dict, subj: dict, months: int, budget: Budget) -> dict:
    """The Zillow sold and active pull for the subject's ZIP (comp_package's band pull).

    Only when the operator asked in Slack. A pull for the same ZIP and window from the last
    AGENTS_ZILLOW_CACHE_DAYS is reused for nothing. A new one needs the key, room under
    AGENTS_COMPS_DAILY_USD and the run and day caps, and is stopped at AGENTS_ZILLOW_MAX_CALLS calls;
    its cost is calls x AGENTS_ZILLOW_CALL_USD, recorded as spend and said in the result. The cache
    holds the raw rows under "sold", which is the shape post_walkthrough --sold-json reads."""
    out = {"sold": [], "active": [], "land": [], "why": "", "calls": 0, "spent_usd": 0.0, "cached_from": "",
           "asked": from_slack(task)}
    if not out["asked"]:
        return out
    if not setting("AGENTS_ZILLOW_COMPS", True):
        out["why"] = "the Zillow pull is switched off (AGENTS_ZILLOW_COMPS=0)"
        return out
    if not subj.get("zip"):
        out["why"] = "the record has no ZIP, so there is no Zillow area to pull"
        return out
    import comp_package
    from zillow_market_api import normalize
    cut = comp_package.since(months)
    path = _zcache(subj["zip"], months)
    try:
        hit = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hit = {}
    fresh = (date.today() - timedelta(days=int(setting("AGENTS_ZILLOW_CACHE_DAYS", 7)))).isoformat()
    if str(hit.get("pulled") or "") >= fresh:
        out["sold"] = [l for l in (normalize(i) for i in hit.get("sold") or []) if l.sold_date >= cut]
        out["land"] = [l for l in (normalize(i) for i in hit.get("land") or []) if l.sold_date >= cut]
        out["active"] = [normalize(i) for i in hit.get("active") or []]
        out["cached_from"] = str(hit["pulled"])
        return out
    key = (os.environ.get("OPENWEBNINJA_API_KEY") or "").strip()
    if not key and ZILLOW is None:
        out["why"] = "OPENWEBNINJA_API_KEY is not set on this box, so Zillow was not pulled"
        return out
    price = float(setting("AGENTS_ZILLOW_CALL_USD", 0.01))
    ceiling = int(setting("AGENTS_ZILLOW_MAX_CALLS", 90))
    most = round(price * ceiling, 2)
    day_key = "deals:zillow_usd:%s" % store.today()
    used = _f(store.kv_get(day_key))
    day_cap = float(setting("AGENTS_COMPS_DAILY_USD", 5.0))
    if used + most > day_cap:
        out["why"] = "Zillow not pulled: today's comp budget is $%.2f of $%.2f used (AGENTS_COMPS_DAILY_USD)" % (used, day_cap)
        return out
    if not budget.allows(most, "Zillow sold pull"):
        out["why"] = budget.skipped[-1]
        return out
    from zillow_market_api import ZillowMarketAPI
    api = ZILLOW or ZillowMarketAPI(key)
    calls = {"n": 0, "stopped": False}
    original = api.search

    def counted(location, home_status="FOR_SALE", **params):
        if calls["n"] >= ceiling:
            calls["stopped"] = True
            return []
        calls["n"] += 1
        return original(location, home_status, **params)

    api.search = counted
    location = "%s, %s %s" % (subj.get("city") or "", subj.get("state") or "TN", subj["zip"])
    try:
        sold = api.pull_sold(location, months_back=int(months))
        land = list(getattr(api, "last_land", []) or [])
        active = api.pull_active(location)
    finally:
        try:
            del api.search
        except AttributeError:
            pass
        spent = round(calls["n"] * price, 2)
        budget.charge(spent, "Zillow sold pull (%d calls)" % calls["n"])
        store.kv_set(day_key, str(round(used + spent, 4)))
    _save_json(path, {"pulled": date.today().isoformat(), "zip": subj["zip"], "months": int(months),
                      "calls": calls["n"], "sold": [l.raw for l in sold], "land": [l.raw for l in land],
                      "active": [l.raw for l in active]})
    out.update(sold=sold, land=land, active=active, calls=calls["n"], spent_usd=spent)
    if calls["stopped"]:
        out["why"] = "the Zillow pull stopped at %d calls (AGENTS_ZILLOW_MAX_CALLS), so older sales may be missing" % ceiling
    return out


def merge_comps(deeds: list, zillow: list) -> list:
    """Deed comps plus the Zillow rows on streets the deeds do not already cover (the deed wins)."""
    import post_walkthrough as pw
    have = {pw._norm_addr(c.address) for c in deeds}
    return list(deeds) + [z for z in zillow if pw._norm_addr(z.address) not in have]


# ------------------------------------------------------------------------------- the pack

def pinned_walk(subj: dict, walk: dict | None = None) -> tuple[dict, str]:
    """(walk, note). With no target layout the engine prices mid and gut scopes as a 3 bed 2 bath,
    adding rooms nobody committed to; a walk that names none is held to the layout the house has.
    The labor region follows the county unless the walk names one."""
    w = dict(walk or {})
    note = ""
    if not w.get("target_config"):
        w["target_config"] = {"beds": subj["beds"] or None, "baths": subj["baths"] or None}
        note = "held to the existing %s bed %s bath layout" % (subj["beds"] or "?", ("%g" % subj["baths"]) if subj["baths"] else "?")
    w.setdefault("region", region_for(subj["county"]))
    return w, note


def lead_for(rec: dict, fields: dict, with_board: bool = False) -> dict:
    """The lead dict post_walkthrough reads, from a record already in hand. with_board also reads
    the owner's message board and the record's log (two documented reads) for the Overview sheet."""
    import post_walkthrough as pw
    msgs, logs = [], []
    if with_board:
        owner = (rec.get("owner") or {}).get("uuid") or ""
        try:
            if owner:
                r = crm().call("/api/internal/owner/%s/message/?limit=12&offset=0" % owner)
                msgs = r.get("results") if isinstance(r, dict) else (r or [])
            logs = crm().record_logs(rec["uuid"], limit=25)
        except Exception as exc:  # noqa: BLE001  (the board is context, not a number)
            log.info("message board not read for %s: %s", rec.get("uuid"), exc)
    return pw.lead_from_record(rec, fields, msgs, logs, {})


def build_pack(subj: dict, lead: dict, sold: list, records: list, walk: dict, months: int,
               overrides: dict | None = None, active: list | None = None, land: list | None = None) -> dict:
    """post_walkthrough.assemble on these comps, exactly as the command line runs it on a saved pack:
    no Zillow subject read, no second CRM read, the deeds joined for the bucket refinement, and the
    county card overrides in the slots --beds/--baths/--sqft/--year-built fill."""
    import post_walkthrough as pw
    o = overrides or {}
    args = argparse.Namespace(
        address=subj["street"], city=subj["city"], state=subj["state"] or "TN", zip_code=subj["zip"],
        beds=o.get("beds") or None, baths=o.get("baths") or None, sqft=o.get("sqft") or None,
        year_built=o.get("year_built") or None, months=int(months), bbox=None, polygon=None,
        streets=None, sold_json=None, walkthrough=None, buyers=None, outreach=None, max_buyers=25, spec=None,
        soft_pct=pw.SOFT_COST_PCT, no_sift=True, sift_account=pw.DEFAULT_SIFT_ACCOUNT, no_api=True,
        save_pack=None, out=None, record_uuid=subj["uuid"], deal_room_clients="")
    args._lead = lead
    args._deeds = {pw._norm_addr(r["address"]): {"buyer": r["buyer"], "cash": bool(r.get("cash")),
                                                 "sale_date": r.get("sale_date"), "sale_price": r.get("sale_price")}
                   for r in records if r.get("address") and r.get("buyer")}
    spec = {"sold_comps": list(sold), "active_comps": list(active or [])}
    if land:
        spec["land_comps"] = list(land)
    return pw.assemble(args, spec, walk)


def arv_of(pack: dict) -> tuple[int, int, str]:
    """(value, comps behind it, how it was read)."""
    arv = pack.get("arv") or {}
    base = arv.get("base") or {}
    tight = arv.get("tight") or {}
    value = int(round(_f(base.get("arv"))))
    n = int(tight.get("n") or base.get("n") or 0)
    how = "tight ARV" if tight.get("arv") else ("dual-track base" if value else "")
    notes = "; ".join(tight.get("notes") or [])
    return value, n, (how + (" (%s)" % notes if notes else "")).strip()


def rehab_pick(pack: dict, key: str) -> tuple[int, str]:
    """(total, label) of one scenario: the named one, else the first. Never the menu."""
    scen = (pack.get("rehab") or {}).get("scenarios") or []
    pick = next((s for s in scen if s.get("key") == key), scen[0] if scen else {})
    return int(pick.get("grand") or 0), str(pick.get("label") or "")


def materials_line() -> str:
    import sku_pricing
    st = sku_pricing.status()
    return st["stamp"].replace("locked_sku", "locked SKU list") if st["locked"] else \
        "engine tables (%s)" % st["why"]


def comps_csv(path: Path, pack: dict, subj: dict) -> None:
    import comp_package
    import post_walkthrough as pw
    buckets = pack.get("buckets") or {}
    deeds = pack.get("deeds") or {}
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Address", "City", "Sold", "Price", "Beds", "Baths", "Sqft", "$/sf", "Miles", "Bucket",
                    "Buyer (deed)", "Cash", "Source"])
        for c in sorted(pack.get("sold") or [], key=lambda x: x.sold_date, reverse=True):
            d = deeds.get(pw._norm_addr(c.address)) or {}
            w.writerow([c.address, c.city, c.sold_date, int(c.price), c.beds, c.baths, c.sqft,
                        round(c.ppsf) if c.ppsf else "",
                        round(comp_package.miles_between(subj["lat"], subj["lon"], c.latitude, c.longitude), 2),
                        buckets.get(c.zpid or c.address, ""), d.get("buyer", ""), "yes" if d.get("cash") else "",
                        "SiftMap deed" if str(c.zpid).startswith("sm") else "Zillow"])


def comps_for(task: dict, subj: dict, area: dict, budget: Budget, *, cap: int | None = None) -> dict:
    """Deeds (free) and the Zillow pull (paid, Slack asks only), each clipped to the area.
    {"deeds": the SiftMap result, "zillow": the pull, "sold", "active", "land"}."""
    deeds = free_comps(subj, area, cap=cap)
    months = int(setting("AGENTS_COMPS_MONTHS", 18))
    z = zillow_pull(task, subj, months, budget)
    sold = merge_comps(clip_area(deeds["sold"], subj, area), clip_area(z["sold"], subj, area))
    return {"deeds": deeds, "zillow": z, "sold": sold, "active": clip_area(z["active"], subj, area),
            "land": clip_area(z["land"], subj, area)}


def area_line(area: dict) -> str:
    b = area.get("boundary")
    if b:
        where = "inside your %s" % {"bbox": "box", "corners": "box", "kml": "KML boundary",
                                     "geojson": "GeoJSON boundary"}.get(b["source"], "boundary")
    else:
        where = "within %g mi" % radius_of(area)
    return where + (" on %s" % area["streets"].replace("|", ", ") if area.get("streets") else "")


def zillow_line(z: dict) -> str:
    if z.get("cached_from"):
        return "Zillow: reused the pull from %s, nothing spent" % z["cached_from"]
    if z.get("calls"):
        return "Zillow: %d call(s), $%.2f" % (z["calls"], z.get("spent_usd") or 0.0)
    return ""


# ------------------------------------------------------------------------------- the call

def verdict(*, value: int, n_comps: int, rehab: int, rule: float, fee: float, est_value: float,
            equity_pct, ask: float, dead: int | None, structure: str, buy_box_max: float,
            min_comps: int = 3, dead_months: int = 6, radius: float = 1.0) -> dict:
    """Go, No go, Look closer or No call, with the reasons in plain words. Every rule is one line,
    so a lead manager can see why and overrule it; the screen suggests, the person moves the card."""
    if not value:
        return {"call": "No call", "no": ["there are not enough comps to put a value on it"], "look": [],
                "buyer_max": 0, "ceiling": 0, "owed": 0}
    buyer_max = round(rule * value - rehab)
    ceiling = buyer_max - fee
    no, look = [], []
    if ceiling <= 0:
        no.append("%d%% of %s less %s of rehab and the %s fee leaves nothing to offer" % (
            round(rule * 100), usd(value), usd(rehab), usd(fee)))
    if dead == 0:
        no.append("dead area: no investor bought a house within %g mi in %d months, so it will be hard to sell"
                  % (radius, dead_months))
    if est_value and est_value > buy_box_max:
        no.append("the estimate of %s is over the %s buy box" % (usd(est_value), usd(buy_box_max)))
    owed = 0
    if est_value and equity_pct not in (None, ""):
        owed = round(est_value * max(0.0, 1 - _f(equity_pct) / 100.0))
        if ceiling > 0 and owed > ceiling:
            no.append("the owner likely owes about %s, more than the %s we could pay" % (usd(owed), usd(ceiling)))
    if ask and ceiling > 0 and ask > ceiling:
        look.append("the ask of %s is %s over what we could pay" % (usd(ask), usd(ask - ceiling)))
    if n_comps < min_comps:
        look.append("only %d comp(s) behind the value, so it is a rough read" % n_comps)
    if structure and re.search(r"multi|duplex|condo|town|mobile|manufactured", structure, re.I):
        look.append("the record says %s, not a single family house" % structure)
    if dead is None:
        look.append("the dead-area check did not run")
    call = "No go" if no else ("Look closer" if look else "Go")
    return {"call": call, "no": no, "look": look, "buyer_max": buyer_max, "ceiling": ceiling, "owed": owed}


def _note_text(r: dict) -> str:
    why = (r["no"] or r["look"] or ["the numbers work"])[0]
    return ("Quick screen %s: value about %s from %d comps, rough rehab %s, most we could pay %s. Call: %s, %s."
            % (date.today().isoformat(), usd(r["value"]), r["comps_n"], usd(r["rehab"]), usd(r["ceiling"]),
               r["call"].lower(), why))


def write_note(uuid: str, text: str) -> str:
    """One note on the record, live only, read back. Returns what happened in plain words."""
    if not config.live():
        return "nothing written to the record (dry run)"
    if not setting("AGENTS_QUICK_SCREEN_NOTE", True):
        return "nothing written to the record (AGENTS_QUICK_SCREEN_NOTE is off)"
    try:
        crm().message(uuid, text)
        back = str((crm().record(uuid) or {}).get("notes") or "")
    except Exception as exc:  # noqa: BLE001
        return "the note on the record failed: %s" % str(exc)[:160]
    return "note written to the record and read back" if text[:60] in back else \
        "the note was sent but did not read back on the record"


def run(task: dict) -> dict:
    inputs, details = inputs_of(task)
    folder = files_dir(task, NAME)
    out = {"ok": False, "run_dir": str(folder.parent), "live": config.live(), "files": [], "skipped": []}
    area = area_inputs(inputs, details)
    if area["why"]:
        out["why"] = area["why"]
        return out
    rec, fields, why, _ = locate(task, inputs)
    if not rec:
        out["why"] = why
        return out
    subj = subject_of(rec)
    out.update(street=subj["street"] or rec["uuid"], uuid=subj["uuid"])
    notes = area["notes"]
    out["from_notes"] = apply_notes(subj, notes)
    out["context"] = notes.get("context") or ""
    if not (subj["lat"] and subj["lon"]):
        out["why"] = "the record has no map point, so there is no area to pull comps from"
        return out
    if not subj["sqft"]:
        out["why"] = "the record has no square footage; put it in the notes (for example 3/1, 1,400 sqft, built 1950)"
        return out
    ask, ask_from = area["ask"], "your notes"
    if not ask:
        ask, ask_from = money(inputs.get("ask")), "the request"
    if not ask:
        ask, ask_from = field_money(fields, "AGENTS_SELLER_ASK_FIELDS", "Asking Price,Ask Price,Seller Asking Price")
    budget = Budget(task.get("id") or 0, float(setting("AGENTS_DEALS_RUN_CAP_USD", 2.0)))
    try:
        got = comps_for(task, subj, area, budget)
    except MissingKey as exc:
        out["why"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001  (SiftMap down or refusing: said, not raised)
        out["why"] = "the comp search failed: %s" % str(exc)[:200]
        return out
    comps, zillow = got["deeds"], got["zillow"]
    walk, held = pinned_walk(subj)
    walk, _ = notes_beat_walk(walk, notes)
    months = int(setting("AGENTS_COMPS_MONTHS", 18))
    pack = build_pack(subj, lead_for(rec, fields), got["sold"], comps["records"], walk, months,
                      overrides=notes, active=got["active"], land=got["land"])
    value, n, how = arv_of(pack)
    rehab, rehab_label = rehab_pick(pack, str(setting("AGENTS_SCREEN_REHAB", "mid")))
    radius = radius_of(area)
    dead_months = int(setting("AGENTS_SCREEN_DEAD_MONTHS", 6))
    dead = None
    try:
        import comp_package
        dead = comp_package.investor_activity(siftmap(), subj["lat"], subj["lon"], radius_mi=radius, months=dead_months)
    except Exception as exc:  # noqa: BLE001
        out["skipped"].append("the dead-area check failed: %s" % str(exc)[:160])
    from offer_sheet import DEFAULT_FEE, DEFAULT_RULE
    rule = float(setting("AGENTS_SCREEN_RULE", DEFAULT_RULE))
    fee = float(setting("AGENTS_SCREEN_FEE", float(DEFAULT_FEE)))
    v = verdict(value=value, n_comps=n, rehab=rehab, rule=rule, fee=fee, est_value=subj["estimate_value"],
                equity_pct=subj["equity_percent"], ask=ask, dead=dead, structure=subj["structure_type"],
                buy_box_max=float(setting("AGENTS_BUY_BOX_MAX", 700000.0)), dead_months=dead_months, radius=radius)
    as_is = int(_f((pack.get("exits") or {}).get("inputs", {}).get("as_is_point")))
    out.update(ok=True, value=value, comps_n=n, value_how=how, rehab=rehab, rehab_label=rehab_label,
               rule=rule, fee=fee, ask=ask, ask_from=ask_from if ask else "", as_is=as_is, dead=dead,
               dead_months=dead_months, radius=radius, held=held, region=walk.get("region"),
               area=area_line(area), materials=materials_line(), estimate=subj["estimate_value"], **v)
    out["comps"] = {k: comps.get(k) for k in ("found", "read", "cached", "not_read", "cap", "day_cap_hit", "why",
                                               "dropped", "months")}
    out["sold_n"] = len(got["sold"])
    out["zillow"] = {k: zillow.get(k) for k in ("why", "calls", "spent_usd", "cached_from", "asked")}
    out["zillow_n"] = len(clip_area(zillow["sold"], subj, area))
    out["skipped"] += budget.skipped
    out["spent_usd"] = round(store.spent(task_id=int(task.get("id") or 0)), 2)
    comps_csv(folder / "comps.csv", pack, subj)
    (folder / "screen.json").write_text(json.dumps({k: out[k] for k in out if k != "files"}, indent=1,
                                                   default=str), encoding="utf-8")
    out["files"] = ["comps.csv", "screen.json"]
    out["note"] = write_note(subj["uuid"], _note_text(out)) if value else \
        "nothing written to the record (no value to write)"
    return out


def render(task: dict, r: dict) -> str:
    head = ":mag: *Quick screen: %s*  (%s)" % (r.get("street") or task.get("subject") or "this lead",
                                              "live" if r.get("live") else "dry run")
    if not r.get("ok"):
        return safe_text(head + "\nNot screened: %s" % (r.get("why") or "no reason given"))
    reasons = r.get("no") or r.get("look") or []
    lines = [head, "Call: *%s*. %s" % (r["call"], cap1(reasons[0]) if reasons else
                                        "The numbers work at the most we could pay.")]
    if r.get("value"):
        lines.append("Value about %s from %d comp(s) (%s) | rough rehab %s (%s, sight unseen%s)" % (
            usd(r["value"]), r["comps_n"], r.get("value_how") or "deeds", usd(r["rehab"]), r.get("rehab_label") or "",
            ", " + r["held"] if r.get("held") else ""))
        bits = ["Most we could pay %s (%d%% rule, %s fee)" % (usd(r["ceiling"]), round(r["rule"] * 100), usd(r["fee"]))]
        if r.get("ask"):
            bits.append("ask %s (from %s)" % (usd(r["ask"]), r.get("ask_from") or "the record"))
        if r.get("owed"):
            bits.append("owes about %s" % usd(r["owed"]))
        lines.append(" | ".join(bits))
    c = r.get("comps") or {}
    comp_bits = "Comps %s: %d sold in %s months, %d deed(s) read, %d from the cache" % (
        r.get("area") or "", r.get("sold_n") or 0, c.get("months") or "?", c.get("read") or 0, c.get("cached") or 0)
    if c.get("not_read"):
        comp_bits += ", %d left unread at the cap of %d%s" % (c["not_read"], c.get("cap") or 0,
                                                               " (today's read limit)" if c.get("day_cap_hit") else "")
    if r.get("dead") is not None:
        comp_bits += " | investor buys nearby in %d months: %d" % (r.get("dead_months") or 6, r["dead"])
    lines.append(comp_bits)
    z = zillow_line(r.get("zillow") or {})
    if z:
        lines.append(z)
    if r.get("from_notes"):
        lines.append("From your notes: %s" % ", ".join(r["from_notes"]))
    if r.get("context"):
        lines.append("Your notes: %s" % r["context"][:300])
    if c.get("why") and not r.get("value"):
        lines.append("Comps: %s" % c["why"])
    extra = list(reasons[1:3]) + [w for w in ((r.get("zillow") or {}).get("why"),) if w] + list(r.get("skipped") or [])
    for w in extra[:3]:
        lines.append(":warning: " + cap1(w))
    lines.append("Rehab materials: %s. %s." % (r.get("materials") or "unknown", cap1(r.get("note") or "")))
    lines.append(file_line(task, "Comps", "comps.csv"))
    return safe_text("\n".join(lines))
