"""doorknock_route: the week's door knocks as a printable route sheet (build plan 1.8; research item 11).

The hot auction stage already decides who gets a door knock: a record tagged Auction Soon whose sale
is inside a week, and only then (auctiontasks._last_weeks). It hands that over as one task whose
notes are the streets in date order. A list in date order is not a route: a knocker driving it as
written crosses the county twice. The course puts the yield at about one hot lead per eight or nine
doors (C6.33) and 50 to 100 doors a month (L13.9), so the doors are worth driving well.

WHAT IT KNOCKS. By default the same houses by the same rule: tagged Auction Soon, the next sale
inside the lane's door-knock window (7 days unless the playbook says otherwise, or the days asked
for), passing the suppression gate as a visit (everything that stops a call task stops a knock,
except the numbers), and owned by a person (people only, Ty 2026-11-14). Or the houses a person
names, by record id or street line, which still pass the gate and the people rule.

THE ORDER. Nearest neighbor from the start, on straight-line miles between the records' own map
positions. No routing service and nothing paid. A typed start address is placed through the US
Census geocoder (free, public); lat,lon is used as given, and AGENTS_DOORKNOCK_GEOCODER=off never
sends an address out. With no start placed the route begins at the soonest sale. A house with no
map position on its record goes last, in sale date order, and is marked so. The sheet's map links
open the stops in Google Maps in groups of ten.

NO PHONE NUMBERS. The sheet is printed and left on a car seat. It is built from five fields (the
address, the sale date, the owner's first name, what to leave, the miles), never from the owner's
phones, and every cell is scrubbed of anything shaped like a phone number before it is written.

Read only: it writes nothing to DataSift and spends nothing, in dry and in live mode alike.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from .. import config, slack, store
from ..autopilot import auctiontasks, lanes
from ..autopilot import policy as policy_mod
from ..autopilot import stages

NAME = "doorknock_route"

# Tests swap these for fakes.
POLICY = None
CRM = None
GEOCODE = None
NOW = None

PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
LATLON_RX = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
EARTH_MILES = 3958.8
GROUP = 10                  # points in one map link: the start or a stop, eight waypoints, and the next stop


def setting(name: str, default):
    """One setting from the environment (Fly secrets on the box), typed like its default."""
    raw = (os.environ.get(name) or "").strip()
    if raw == "":
        return default
    try:
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


def _crm():
    if CRM is not None:
        return CRM
    from ..autopilot.crm import CRM as Client  # noqa: PLC0415
    return Client()


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task.get("id") or 0)))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def scrub(text) -> str:
    """Anything shaped like a phone number comes out of text that is printed."""
    return " ".join(PHONE_RX.sub("", str(text or "")).split())


def first_name(owner: dict) -> str:
    """The name to ask for at the door: the first word longer than an initial ("J Marcus" is Marcus)."""
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", str((owner or {}).get("first_name") or "")):
        if len(w) >= 2:
            return w[:1].upper() + w[1:].lower()
    return ""


def nice(d: date) -> str:
    return "%s %d" % (d.strftime("%b"), d.day)


def miles(a: tuple, b: tuple) -> float:
    """Straight-line miles between two (lat, lon) points."""
    la1, lo1, la2, lo2 = (math.radians(x) for x in (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_MILES * math.asin(min(1.0, math.sqrt(h)))


def _coord(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f != 0.0 else None


# ---------------------------------------------------------------------- where the route starts
def census_geocode(address: str):
    """(lat, lon) from the US Census one-line geocoder, or None. Free and keyless; never run while
    this was built (no network), so the first live placement is the one to watch."""
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    req = urllib.request.Request(CENSUS_URL + "?" + q, headers={"Accept": "application/json", "User-Agent": "route-sheet/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode() or "{}")
    found = ((data.get("result") or {}).get("addressMatches") or [])
    if not found:
        return None
    c = found[0].get("coordinates") or {}
    return float(c["y"]), float(c["x"])


def start_point(text: str) -> dict | None:
    """{"label", "lat", "lon", "how"} for the start, or None when none was given."""
    t = " ".join(str(text or "").split())
    if not t:
        return None
    m = LATLON_RX.match(t)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {"label": t, "lat": lat, "lon": lon, "how": "given"}
        return {"label": t, "lat": None, "lon": None, "how": "not a map position"}
    if str(setting("AGENTS_DOORKNOCK_GEOCODER", "census")).lower() == "off":
        return {"label": t, "lat": None, "lon": None, "how": "not placed (the geocoder is off)"}
    try:
        got = (GEOCODE or census_geocode)(t)
    except Exception:  # noqa: BLE001  (a geocoder that is down only moves where the route starts)
        got = None
    if got:
        return {"label": t, "lat": float(got[0]), "lon": float(got[1]), "how": "placed"}
    return {"label": t, "lat": None, "lon": None, "how": "not placed (the geocoder found no match)"}


# ---------------------------------------------------------------------- which houses
def window_days(ctx, asked) -> int:
    """The days asked for (at most 45), else the stage's door_knock_days, else the lane's window."""
    try:
        n = int(float(str(asked).strip())) if str(asked or "").strip() else 0
    except ValueError:
        n = 0
    if n > 0:
        return min(n, 45)
    st = ctx.stage
    if st.get("door_knock_days"):
        return int(st.get("door_knock_days"))
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    tl = lanes_cfg.timeline(str(st.get("lane") or "hot_auction")) or {}
    return int(tl.get("door_knock_days", lanes.DOOR_KNOCK_DAYS))


def _next_sale(dates: dict, today: date) -> tuple[date | None, str]:
    """(the soonest sale on or after today, the field it came from)."""
    best = (None, "")
    for field, v in (dates or {}).items():
        d = stages._as_date(v)
        if d and d >= today and (best[0] is None or d < best[0]):
            best = (d, str(field))
    return best


def _packet(field: str) -> str:
    if field == "tax_auction_date":
        return str(setting("AGENTS_DOORKNOCK_TAX_PACKET", "Tax sale packet"))
    return str(setting("AGENTS_DOORKNOCK_PACKET", "Foreclosure packet"))


def _is_company(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return is_entity(owner or {})


def stop_of(ctx, rec: dict, row: dict | None, sale: date | None, left: int | None, field: str) -> tuple[dict | None, str]:
    """One stop, or (None, why it is left off). The five printed fields and nothing else."""
    if not (rec or {}).get("uuid"):
        return None, "the record could not be read"
    owner = rec.get("owner") or {}
    if _is_company(owner):
        return None, "a company or trust owns it (people only)"
    v = ctx.verdict(rec, "task", row=row, allow={"exhausted"})
    if not v.ok:
        return None, "held back: %s" % v.reason()
    a = rec.get("address") or {}
    return {"record": rec["uuid"], "street": scrub(a.get("street")), "city": scrub(a.get("city")),
            "zip": str(a.get("postal_code") or a.get("zip5") or "")[:5], "lat": _coord(a.get("latitude")),
            "lon": _coord(a.get("longitude")), "sale": sale.isoformat() if sale else "", "days_left": left,
            "first_name": scrub(first_name(owner)), "leave": scrub(_packet(field))}, ""


def knock_list(ctx, days: int, max_stops: int, max_reads: int) -> tuple[list, list, int]:
    """(stops, left off, how many more wait) by the auction stage's own door-knock rule."""
    st = ctx.stage
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    tag = str(st.get("tag") or "Auction Soon")
    tid = ctx.crm.tag_uuid(tag)
    if not tid:
        return [], [{"street": "", "why": "there is no %s tag in this account" % tag}], 0
    rows = ctx.crm.rows({"all_tags": [tid], "must_not": {"any_property_status": stages.GONE_STATUSES}},
                        cap=int(setting("AGENTS_DOORKNOCK_MAX_SCAN", 2000)))
    state = {"reads": 0, "max_reads": max_reads}
    near, off = [], []
    for row in rows:
        u = row.get("uuid") or ""
        dates = auctiontasks.sale_dates(ctx, u, state)
        if dates is None:
            off.append({"street": stages.addr(row), "why": "its sale date was not read (read cap)"})
            continue
        sale, field = _next_sale(dates, today)
        if sale is None:
            continue
        left = (sale - today).days
        if 0 <= left <= days:
            near.append((sale, left, field, row))
    near.sort(key=lambda x: (x[0], stages.addr(x[3])))
    stops, more = [], 0
    for sale, left, field, row in near:
        if len(stops) >= max_stops:
            more += 1
            continue
        if state["reads"] >= state["max_reads"]:
            off.append({"street": stages.addr(row), "why": "not read (read cap)"})
            continue
        state["reads"] += 1
        rec = ctx.crm.record(row.get("uuid") or "")
        stop, why = stop_of(ctx, rec, row, sale, left, field)
        if why:
            off.append({"street": stages.addr(rec or row), "why": why})
            continue
        stops.append(stop)
    return stops, off, more


def named_items(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[;\n]+", str(text or "")) if x.strip()]


def named_stops(ctx, items: list[str], max_stops: int, max_reads: int) -> tuple[list, list, int]:
    """(stops, left off, how many more) for the houses a person named: a record id or a street line,
    matched to exactly one record (never a guess), then the same gate and people rule."""
    from ..autopilot.taxsale import find_record
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    state = {"reads": 0, "max_reads": max_reads}
    stops, off, more = [], [], 0
    for item in items:
        if len(stops) >= max_stops:
            more += 1
            continue
        if state["reads"] >= state["max_reads"]:
            off.append({"street": scrub(item), "why": "not read (read cap)"})
            continue
        if UUID_RX.match(item):
            u, why = item, ""
        else:
            street = item.split(",")[0].strip()
            z = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", item)
            u, why = find_record(ctx.crm, street, "", z.group(1) if z else "")
        if not u:
            off.append({"street": scrub(item), "why": why or "not in the account"})
            continue
        state["reads"] += 1
        rec = ctx.crm.record(u)
        dates = auctiontasks.sale_dates(ctx, u, state, fresh_only=False) or {}
        sale, field = _next_sale(dates, today)
        stop, why = stop_of(ctx, rec, None, sale, (sale - today).days if sale else None, field)
        if why:
            off.append({"street": stages.addr(rec) if rec.get("uuid") else scrub(item), "why": why})
            continue
        stops.append(stop)
    return stops, off, more


# ---------------------------------------------------------------------- the order
def route(start: dict | None, stops: list[dict]) -> tuple[list[dict], str]:
    """The stops in driving order, each with the straight-line miles from the point before it."""
    placed = [dict(s) for s in stops if s.get("lat") is not None and s.get("lon") is not None]
    unplaced = sorted((dict(s) for s in stops if s.get("lat") is None or s.get("lon") is None),
                      key=lambda s: (s.get("days_left") if s.get("days_left") is not None else 999, s.get("street") or ""))
    order: list[dict] = []
    if start and start.get("lat") is not None:
        cur, how = (start["lat"], start["lon"]), "from the start"
    elif placed:
        first = min(placed, key=lambda s: (s.get("days_left") if s.get("days_left") is not None else 999, s.get("street") or ""))
        placed.remove(first)
        first["leg"] = None
        order.append(first)
        cur, how = (first["lat"], first["lon"]), "from the soonest sale"
    else:
        cur, how = None, "in sale date order (no map positions)"
    while placed:
        nxt = min(placed, key=lambda s: (miles(cur, (s["lat"], s["lon"])),
                                         s.get("days_left") if s.get("days_left") is not None else 999, s.get("street") or ""))
        placed.remove(nxt)
        nxt["leg"] = round(miles(cur, (nxt["lat"], nxt["lon"])), 1)
        order.append(nxt)
        cur = (nxt["lat"], nxt["lon"])
    for s in unplaced:
        s["leg"] = None
        order.append(s)
    return order, how


def map_links(start: dict | None, order: list[dict]) -> list[tuple[str, str]]:
    """Google Maps directions in groups of ten points; a link a person opens, nothing called from here."""
    pts = [("start", start["lat"], start["lon"])] if start and start.get("lat") is not None else []
    pts += [(i, s["lat"], s["lon"]) for i, s in enumerate(order, 1) if s.get("lat") is not None and s.get("lon") is not None]
    out, a = [], 0
    while a < len(pts) - 1:
        b = min(a + GROUP - 1, len(pts) - 1)
        seg = pts[a:b + 1]
        ll = ["%.6f,%.6f" % (p[1], p[2]) for p in seg]
        url = "https://www.google.com/maps/dir/?api=1&origin=%s&destination=%s&travelmode=driving" % (ll[0], ll[-1])
        if len(ll) > 2:
            url += "&waypoints=" + urllib.parse.quote("|".join(ll[1:-1]), safe=",")
        nums = [p[0] for p in seg if p[0] != "start"]
        out.append(("stops %d to %d" % (nums[0], nums[-1]) if len(nums) > 1 else "stop %d" % nums[0], url))
        a = b
    return out


# ---------------------------------------------------------------------- the sheet
def write_sheet(path: Path, title: str, summary: str, order: list[dict], links: list, notes: list[str]) -> None:
    """A landscape letter PDF to print: the stops in order, five fields each, a blank column for the result."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    navy, muted, line = colors.HexColor("#0A1130"), colors.HexColor("#4A5068"), colors.HexColor("#D5D9E2")
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=navy)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13, textColor=navy)
    cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=9, leading=11.5, textColor=navy)
    small = ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=11, textColor=muted)
    data = [["Stop", "Address", "Sale", "Owner", "Leave", "Miles", "Result"]]
    for i, s in enumerate(order, 1):
        where = ", ".join(x for x in (s.get("street"), s.get("city")) if x)
        if s.get("lat") is None:
            where += " (no map position on the record)"
        data.append([str(i), Paragraph(escape(scrub(where)), cell),
                     nice(date.fromisoformat(s["sale"])) if s.get("sale") else "", scrub(s.get("first_name") or ""),
                     scrub(s.get("leave") or ""), ("%.1f" % s["leg"]) if s.get("leg") is not None else "", ""])
    table = Table(data, colWidths=[0.45 * inch, 3.6 * inch, 0.75 * inch, 1.0 * inch, 1.45 * inch, 0.6 * inch, 2.15 * inch],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9), ("TEXTCOLOR", (0, 0), (-1, -1), navy),
        ("LINEBELOW", (0, 0), (-1, 0), 1, navy), ("LINEBELOW", (0, 1), (-1, -1), 0.5, line),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (0, -1), "RIGHT"), ("ALIGN", (5, 0), (5, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story = [Paragraph(escape(scrub(title)), h1), Spacer(1, 4), Paragraph(escape(scrub(summary)), body), Spacer(1, 10), table]
    if links:
        story.append(Spacer(1, 10))
        story.append(Paragraph("Open the route in Google Maps: " + ", ".join(
            '<a href="%s" color="#316AFF">%s</a>' % (escape(u), escape(label)) for label, u in links) + ".", body))
    for n in notes:
        story.append(Spacer(1, 6))
        story.append(Paragraph(escape(scrub(n)), small))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("_PENDING_" + path.name)
    doc = SimpleDocTemplate(str(tmp), pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch, title=scrub(title), author="SiftStack")
    doc.build(story)
    tmp.replace(path)


# ---------------------------------------------------------------------- the workflow
def run(task: dict) -> dict:
    inputs = store.payload_of(task).get("inputs") or {}
    now = NOW or datetime.now(timezone.utc)
    folder = _folder(task)
    pol = POLICY or policy_mod.load()
    sid = str(setting("AGENTS_DOORKNOCK_STAGE", "hot_auction_tasks"))
    if pol is None or sid not in getattr(pol, "stages", {}):
        return {"run_dir": str(folder), "problem": "the playbook has no %s stage, so there is no knock list to read" % sid}
    st = pol.stage(sid)
    ctx = stages.Ctx(policy=pol, stage=st, crm=_crm(), run_id="doorknock-%s" % task.get("id"), now=now, live=False,
                     out=stages.Summary(stage=sid, kind=st.kind, owner=st.owner, mode="plan", live=False))
    max_stops = max(1, int(setting("AGENTS_DOORKNOCK_MAX_STOPS", 25)))
    max_reads = max(1, int(setting("AGENTS_DOORKNOCK_MAX_READS", 60)))
    days = window_days(ctx, inputs.get("days"))
    items = named_items(inputs.get("addresses") or "")
    if items:
        stops, off, more = named_stops(ctx, items, max_stops, max_reads)
    else:
        stops, off, more = knock_list(ctx, days, max_stops, max_reads)
    start = start_point(inputs.get("start") or setting("AGENTS_DOORKNOCK_START", ""))
    order, how = route(start, stops)
    road = float(setting("AGENTS_DOORKNOCK_ROAD_FACTOR", 1.3))
    straight = sum(s["leg"] for s in order if s.get("leg") is not None)
    drive = int(round(straight * road))
    local = now.astimezone(ZoneInfo(pol.tz)).date()
    phrase = ("from %s" % start["label"]) if how == "from the start" else how
    result = {"run_dir": str(folder), "named": bool(items), "days": days, "how": how, "from": phrase, "drive_miles": drive,
              "start": (start or {}).get("label") or "", "start_how": (start or {}).get("how") or "",
              "stops": [{k: s.get(k) for k in ("record", "street", "city", "zip", "sale", "days_left", "first_name", "leave", "leg")}
                        for s in order],
              "left_off": [{"street": scrub(x.get("street")), "why": x.get("why")} for x in off], "more": more,
              "unplaced": sum(1 for s in order if s.get("lat") is None), "file": "",
              "at": now.isoformat(timespec="seconds")}
    if not order:
        return result
    links = map_links(start, order)
    name = "Door_Knock_Route_%s.pdf" % local.isoformat()
    title = "Door-knock route for %s" % ("%s, %s" % (local.strftime("%a"), nice(local)))
    what = ("the houses asked for" if items else "houses with a sale inside %d days" % days)
    summary = "%d %s, about %d miles of driving %s. Leave the printed packet at every door." % (len(order), what, drive, phrase)
    notes = ["Knock, say who you are, and leave the packet whether or not anyone answers. Write what happened in the "
             "Result column and note every knock on the record the same day."]
    if result["unplaced"]:
        notes.append(("1 house has no map position on the record, so it comes last; place it by hand." if result["unplaced"] == 1
                      else "%d houses have no map position on the record, so they come last; place them by hand."
                      % result["unplaced"]))
    write_sheet(folder / "files" / name, title, summary, order, links, notes)
    result["file"] = name
    result["maps"] = [label for label, _ in links]
    return result


def render(task: dict, result: dict) -> str:
    if result.get("problem"):
        return "The door-knock route did not run: %s." % result["problem"]
    order = result.get("stops") or []
    off = result.get("left_off") or []
    why = Counter(x.get("why") or "left off" for x in off)
    tail = ("Left off: %s." % "; ".join("%s (%d)" % (w, n) for w, n in why.most_common(3))) if off else ""
    if not order:
        if result.get("named"):
            lead = "None of the houses asked for can be knocked."
        elif off:
            lead = "No house can be knocked today."
        else:
            lead = "No house tagged Auction Soon has a sale inside %d days, so there is no route today." % int(result.get("days") or 0)
        return "\n".join(scrub(x) for x in (lead, tail) if x)
    lines = ["Door-knock route: %d house%s, about %d miles of driving %s." % (
        len(order), "" if len(order) == 1 else "s", int(result.get("drive_miles") or 0), result.get("from") or result.get("how") or "")]
    first = order[0]
    if first.get("sale"):
        lines.append("First stop: %s (sale %s)." % (first.get("street") or "the first house", nice(date.fromisoformat(first["sale"]))))
    else:
        lines.append("First stop: %s." % (first.get("street") or "the first house"))
    if result.get("start_how", "").startswith("not"):
        lines.append("The start was %s, so the route begins at the soonest sale." % result["start_how"])
    if result.get("unplaced"):
        lines.append("1 house has no map position and comes last." if result["unplaced"] == 1 else
                     "%d houses have no map position and come last." % result["unplaced"])
    if result.get("more"):
        lines.append("%d more wait for a second route." % result["more"])
    if tail:
        lines.append(tail)
    lines = [scrub(x) for x in lines]
    if result.get("file"):
        # The signed link is added after the scrub: its signature is hex and must reach Slack unchanged.
        url = slack.file_url(int(task.get("id") or 0), result["file"])
        lines.append("%s: %s" % (result["file"], url) if url else result["file"])
    return "\n".join(lines)
