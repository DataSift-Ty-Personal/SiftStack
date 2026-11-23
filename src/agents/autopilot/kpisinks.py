"""Where the KPI tracker's numbers go: the brain's own page, an optional Google Sheet, and one Slack
message a day.

Ty, 2026-11-16: "a Google Sheet that's just updated, and anytime the KPIs are updated (which can be
maybe 3 times a day, how we have it), that is then updated inside the Slack channel just to keep it a
little cleaner". The same day: "we don't want to use Lambda, AWS, etc. ... keep this as core and as
simple for any universal users to run ... Just the simplest structure."

The tracker stage (kind kpitracker) computes ONE snapshot a run and hands it to publish(). This module
only puts that snapshot in front of people, in this order:

  1. The brain's own page, always, with no credential of any kind. One HTML page (a section per tab,
     the Refreshed line on top) and one CSV per tab, kept on the volume under DATA_DIR/kpi/site and
     served by the brain at signed links that never change (GET /kpi/<sig>/<name>). Each run replaces
     the files in place, so a Google Sheet mirrors a tab with one formula, =IMPORTDATA("<CSV link>"),
     and Excel with Data, From Web. Nothing runs on an outside host.
  2. A Google Sheet written through the Sheets API, only when a Google credential is set (a service
     account key, or an OAuth refresh token). No credential is the normal case and is said nowhere.
  3. One Slack message a day in the pipeline channel: posted by the first run of the day and edited in
     place by the later ones, so the channel holds one KPI message a day. Its first line links the page.

Rules carried over from Ty's own DataSift KPI tracker (hubspot-ops kpi/sheet.py), each one something
that went wrong or was asked for there:
  - every tab is rewritten in place under one plain "Refreshed ..." line, never appended to;
  - a tab whose numbers could not be computed keeps its last good version and its note says so,
    because a failed source written as zeros reads exactly like a quiet day;
  - a tab a person deleted from the Google Sheet is never added back (Ty deleted Notes on 2026-10-02).

Privacy: the page and CSV links are secret but not access controlled, so anyone holding one can read
it. Every cell shaped like a phone number becomes [number] on every surface, and on the page and the
CSVs a street address shape becomes [street]. The snapshot itself carries no homeowner names or
streets; these guards are the last line, not the plan.

    python -m agents.autopilot.kpisinks links     # the page link and each tab's IMPORTDATA formula
    python -m agents.autopilot.kpisinks check     # optional Google route: sign in and open the sheet, read only
    python -m agents.autopilot.kpisinks oauth --client-id ... --client-secret ...   # one-time Google sign-in
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .. import config, slack_app, store, voice

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------- hooks (the selftest swaps these)
GOOGLE = None      # (method, url, json=None, data=None, headers=None) -> (status, dict)
SLACK = None       # (method, payload) -> dict, in the Slack Web API's own shape
_http = requests   # the real Google transport; the selftest puts a wall here that refuses every call

# ---------------------------------------------------------------------- settings
# From the stage's `publish:` block in team/autopilot.yaml, overridden by the environment (a Fly secret
# turns the Slack message off without a deploy). The sheet id and the share list are environment only:
# the playbook is in a public repo, and a sheet id or a teammate's email does not belong there.
DEFAULTS = {"title": "KPI tracker", "slack": "edit", "how_tab": "How it is counted"}
ENV = {"title": "AGENTS_KPI_TITLE", "slack": "AGENTS_KPI_SLACK"}
SLACK_MODES = ("edit", "posts", "off")
DEFAULT_TZ = "America/New_York"

# ---------------------------------------------------------------------- the page and its files
PAGE = "tracker.html"
NAME_RX = re.compile(r"[a-z0-9][a-z0-9._-]{0,80}\.(?:html|csv)")
MEDIA = {".html": "text/html; charset=utf-8", ".csv": "text/csv; charset=utf-8"}
# No caching anywhere: a mirror or a browser must see this run's numbers. No index, no referrer: the
# link is the only key, and a click on a DataSift record link would otherwise hand it to that site.
SERVE_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer",
                 "X-Content-Type-Options": "nosniff"}
PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
META_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
CELL_MAX = 45000                  # Google holds 50,000 characters a cell
HEADER_ROWS = 4                   # the Refreshed line, the note, a blank row, the column headers
MIRROR_HEAD = "Mirror a tab in your own sheet"
MIRROR_INTRO = ("In Google Sheets, make a blank tab, click cell A1 and paste that tab's formula; Google refreshes it "
                "about once an hour. In Excel: Data, From Web, then paste the CSV link. Anyone holding one of these "
                "links can read that tab, so share them only with the team.")

# ---------------------------------------------------------------------- Google
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Sheets to write; drive.file to share a sheet this app made or was given, and nothing else in Drive.
# The same pair Ty's DataSift tracker signs in with (hubspot google-sheets-oauth-setup.py).
SCOPES = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SA_ENV = "AGENTS_GOOGLE_SA_JSON"
OAUTH_ENV = ("AGENTS_GOOGLE_CLIENT_ID", "AGENTS_GOOGLE_CLIENT_SECRET", "AGENTS_GOOGLE_REFRESH_TOKEN")
INK = {"red": 0.039, "green": 0.067, "blue": 0.188}       # #0A1130
MUTED = {"red": 0.29, "green": 0.333, "blue": 0.408}      # #4A5568
RULE = {"red": 0.882, "green": 0.902, "blue": 0.937}      # #E1E6EF
CAVEAT = {"red": 0.549, "green": 0.353, "blue": 0.078}    # #8C5A14, the brand gold darkened for AA on white
_TOKENS: dict = {}

# ---------------------------------------------------------------------- Slack
# Errors that mean the day's message is gone or can no longer be edited: post a new one. Any other
# error (rate limit, outage) keeps the old one, because a second message is what this design removes.
GONE = {"message_not_found", "cant_update_message", "edit_window_closed", "channel_not_found", "is_archived"}
SLACK_WORDS = {"not_in_channel": "the app is not in that channel", "channel_not_found": "the channel was not found",
               "ratelimited": "Slack asked to slow down", "invalid_auth": "the Slack token was refused",
               "not_authed": "no Slack token was sent", "msg_too_long": "the message was too long",
               "is_archived": "the channel is archived"}

# ---------------------------------------------------------------------- the guards
# A phone number: 10 digits, or 11 with a leading 1, in the shapes people write. Bounded by anything but
# a letter or a digit, so a record id's hex run is never mistaken for one.
PHONE_RX = re.compile(r"(?<![0-9A-Za-z])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?![0-9A-Za-z])")
# A street address: a house number, up to three capitalized words and a street suffix. Case matters, so
# "2 calls on the way" is left alone while "2 Example Way" is not.
STREET_RX = re.compile(r"(?<![\w,.$])\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Z0-9][A-Za-z0-9'.]*\s+){0,3}"
                       r"(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Ct|Court|Blvd|Boulevard|Way|Pike|Hwy|"
                       r"Highway|Pl|Place|Cir|Circle|Trl|Trail|Pkwy|Parkway|Ter|Terrace|Cv|Cove|Xing|Sq)\b\.?")
NUMERIC_RX = re.compile(r"[+-]?\$?\d[\d,]*(?:\.\d+)?%?")

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
ZONE_WORDS = {"America/New_York": "Eastern", "America/Detroit": "Eastern", "America/Indiana/Indianapolis": "Eastern",
              "America/Chicago": "Central", "America/Denver": "Mountain", "America/Phoenix": "Mountain",
              "America/Los_Angeles": "Pacific"}


class GoogleError(Exception):
    def __init__(self, status: int, message: str = "", reason: str = ""):
        super().__init__("%s %s" % (status, message))
        self.status, self.message, self.reason = int(status or 0), str(message or ""), str(reason or "")


# ====================================================================== settings
def settings(st=None) -> dict:
    out = dict(DEFAULTS)
    block = st.get("publish") if hasattr(st, "get") else None
    block = block if isinstance(block, dict) else {}
    for k in DEFAULTS:
        if block.get(k) not in (None, ""):
            out[k] = block[k]
        env = (os.environ.get(ENV[k]) or "").strip() if k in ENV else ""
        if env:
            out[k] = env
    out["title"] = str(out["title"]).strip()[:100] or DEFAULTS["title"]
    out["slack"] = str(out["slack"]).strip().lower()
    if out["slack"] not in SLACK_MODES:
        out["slack"] = "edit"
    out["how_tab"] = str(out["how_tab"]).strip()
    return out


def block_problems(block: dict) -> list[str]:
    """For the tracker stage's own block_problems: a mistyped publish block is refused when the playbook
    loads, not discovered at 7:45 in the morning."""
    pub = block.get("publish") if isinstance(block, dict) else None
    if pub is None:
        return []
    if not isinstance(pub, dict):
        return ["publish must be a block of settings"]
    out = []
    if str(pub.get("slack", "edit")).strip().lower() not in SLACK_MODES:
        out.append("publish.slack must be edit, posts or off")
    extra = sorted(set(pub) - set(DEFAULTS))
    if extra:
        out.append("publish has settings this module does not read: %s" % ", ".join(map(str, extra)))
    return out


# ====================================================================== time words
def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except Exception:  # noqa: BLE001  (a zone the box does not know reads as the business default)
        return ZoneInfo(DEFAULT_TZ)


def clock(dt: datetime) -> str:
    return "%d:%02d %s" % (dt.hour % 12 or 12, dt.minute, "AM" if dt.hour < 12 else "PM")


def long_when(dt: datetime, tz: str) -> str:
    """'Monday, November 16 at 7:45 AM Eastern'. Names are spelled here, not by strftime, so a box with
    another locale never prints them in another language."""
    key = tz or DEFAULT_TZ
    loc = dt.astimezone(_zone(key))
    word = ZONE_WORDS.get(key) or loc.tzname() or ""
    return ("%s, %s %d at %s %s" % (DAYS[loc.weekday()], MONTHS[loc.month - 1], loc.day, clock(loc), word)).strip()


def refreshed_line(dt: datetime, tz: str) -> str:
    return "Refreshed " + long_when(dt, tz)


def when_of(snapshot: dict, ctx=None) -> datetime:
    """The snapshot's own refresh time (a naive time is read as UTC, like store.now()); the run's clock
    when the snapshot carries none."""
    raw = (snapshot or {}).get("refreshed")
    dt = raw if isinstance(raw, datetime) else None
    if dt is None and raw:
        try:
            dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        dt = getattr(ctx, "now", None) or datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ====================================================================== the guards
def _is_phone_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (float, Decimal)):
        if v != v or v in (float("inf"), float("-inf")) or v != int(v):
            return False
        v = int(v)
    if isinstance(v, int):
        s = str(abs(v))
        return len(s) == 10 or (len(s) == 11 and s.startswith("1"))
    return False


def guard_cell(v, public: bool = False, seen: dict | None = None):
    """The last guard on one cell: a phone number becomes [number] everywhere; on the public page and
    CSVs a street address becomes [street] as well."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        if _is_phone_number(v):
            _count(seen, "phone")
            return "[number]"
        return float(v) if isinstance(v, Decimal) else v
    s = v if isinstance(v, str) else (", ".join(map(str, v)) if isinstance(v, (list, tuple, set)) else str(v))
    t = PHONE_RX.sub("[number]", s)
    if t != s:
        _count(seen, "phone")
    if public:
        u = STREET_RX.sub("[street]", t)
        if u != t:
            _count(seen, "street")
        t = u
    return t[:CELL_MAX]


def _count(seen, what: str) -> None:
    if seen is not None:
        seen[what] = seen.get(what, 0) + 1


def _guard_tab(t: dict, public: bool, seen: dict | None) -> dict:
    g = dict(t)
    g["name"] = str(guard_cell(t["name"], public, seen))
    g["note"] = str(guard_cell(t["note"], public, seen) or "")
    g["columns"] = [str(guard_cell(c, public, seen) or "") for c in t["columns"]]
    g["rows"] = [[guard_cell(v, public, seen) for v in r] for r in t["rows"]]
    return g


def _tabs(snapshot: dict) -> list[dict]:
    """The snapshot's tabs in a shape every surface can rely on. A tab with no name, or a second tab of
    the same name, is dropped: its link would collide with the first one's."""
    out, seen = [], set()
    for t in (snapshot or {}).get("tabs") or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()[:90]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        cols = ["" if c is None else str(c) for c in (t.get("columns") or [])]
        rows = []
        for r in t.get("rows") or []:
            if isinstance(r, dict):
                r = [r.get(c, "") for c in cols]
            elif not isinstance(r, (list, tuple)):
                r = [r]
            rows.append(list(r))
        out.append({"name": name, "note": str(t.get("note") or "").strip(), "columns": cols, "rows": rows,
                    "failed": bool(t.get("failed"))})
    return out


# ====================================================================== files on the volume
def kpi_dir() -> Path:
    return Path(config.DATA_DIR) / "kpi"


def site_dir() -> Path:
    return kpi_dir() / "site"


def _atomic_write(path: Path, text: str) -> None:
    """Write beside, then swap: a request that arrives mid-write still gets the whole last version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, obj) -> None:
    _atomic_write(Path(path), json.dumps(obj, indent=1, default=str))


def _short(exc: Exception) -> str:
    return (getattr(exc, "strerror", "") or str(exc) or type(exc).__name__).strip()[:120]


# ====================================================================== the signed links
def link_key(create: bool = True) -> bytes:
    """A random key kept on the volume, made by the first publish. Its own key, not the webhook secret,
    so a tracker link that leaks can be changed (delete DATA_DIR/kpi/link.key) without touching the
    DataSift webhook, and a box with no webhook secret still gets links nobody can guess."""
    p = kpi_dir() / "link.key"
    try:
        k = p.read_text(encoding="utf-8").strip()
    except OSError:
        k = ""
    if k or not create:
        return k.encode()
    k = secrets.token_urlsafe(32)
    _atomic_write(p, k)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return k.encode()


def _sign(key: bytes, name: str) -> str:
    return hmac.new(key, ("kpi:" + name).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def link(name: str) -> str:
    """The absolute link to one file, the same every run. Empty without AGENTS_PUBLIC_URL."""
    base = (config.PUBLIC_URL or "").rstrip("/")
    if not base:
        return ""
    return "%s/kpi/%s/%s" % (base, _sign(link_key(), name), name)


def served_file(sig: str, name: str) -> Path | None:
    """The file a signed link opens, or None. A name is one plain file name (no folder can be walked),
    the signature must be that name's own, and only the page and the CSVs live where it looks."""
    name = str(name or "")
    if not sig or not NAME_RX.fullmatch(name):
        return None
    key = link_key(create=False)
    if not key or not hmac.compare_digest(str(sig), _sign(key, name)):
        return None
    p = site_dir() / name
    return p if p.is_file() else None


def media_type(path: Path) -> str:
    return MEDIA.get(Path(path).suffix.lower(), "text/plain; charset=utf-8")


def serve(sig: str, name: str):
    """The brain's route body: GET /kpi/{sig}/{name}. The page is sent inline (no download box) with a
    policy that runs no script; a wrong link is a plain 404 that says nothing about what exists."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    path = served_file(sig, name)
    if path is None:
        raise HTTPException(status_code=404)
    headers = dict(SERVE_HEADERS)
    if path.suffix == ".html":
        headers["Content-Security-Policy"] = PAGE_CSP
    return FileResponse(path, media_type=media_type(path), headers=headers)


def read_site(name: str) -> list[list[str]]:
    """One tab's CSV as rows, read from the volume (the tracker's --check compares against this)."""
    state = _load(kpi_dir() / "site.json", {}) or {}
    slug = (state.get("slugs") or {}).get(name)
    if not slug:
        return []
    try:
        return list(csv.reader(io.StringIO((site_dir() / (slug + ".csv")).read_text(encoding="utf-8"))))
    except OSError:
        return []


# ====================================================================== cells for people
def _show(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return "{:,}".format(v)
    if isinstance(v, float):
        if v.is_integer():
            return "{:,}".format(int(v))
        return "{:,.2f}".format(v).rstrip("0").rstrip(".")
    return str(v)


def _is_url(v) -> bool:
    return isinstance(v, str) and v.startswith(("https://", "http://")) and " " not in v


def _numeric(v) -> bool:
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float)) or (isinstance(v, str) and bool(NUMERIC_RX.fullmatch(v.strip())))


def _csv_cell(v):
    """A cell for a CSV file. A text that a spreadsheet would run as a formula when the file is opened
    (=, @, or a + or - that does not start a number) gets a leading apostrophe; +12% and -3 stay."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, str) and v:
        c = v[0]
        if c in "=@\t\r" or (c in "+-" and len(v) > 1 and not (v[1].isdigit() or v[1] in " .$%")):
            return "'" + v
    return v


def _slug(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "tab"
    s, n = base, 2
    while s in taken or s == "tracker":
        s, n = "%s-%d" % (base, n), n + 1
    return s


def _reason(t: dict) -> str:
    return (t.get("note") or "").strip().rstrip(".") or "its numbers could not be read"


def _and(names: list[str]) -> str:
    names = [str(n) for n in names]
    if len(names) <= 1:
        return "".join(names)
    return "%s and %s" % (", ".join(names[:-1]), names[-1])


def _sentence(text: str) -> str:
    t = str(text or "").strip().rstrip(".")
    return (t[:1].upper() + t[1:] + ".") if t else ""


# ====================================================================== 1. the page and the CSVs
def csv_text(line: str, tab: dict) -> str:
    """One tab as a CSV, laid out like the sheet: the Refreshed line, the note, a blank row, the column
    headers, the rows. Every row is padded to the same width, because Excel's From Web takes its column
    count from the first row and a one-cell first row would drop every other column."""
    cols, rows = list(tab.get("columns") or []), list(tab.get("rows") or [])
    width = max([1, len(cols)] + [len(r) for r in rows])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for r in [[line], [tab.get("note") or ""], [], cols] + rows:
        cells = [_csv_cell(v) for v in r]
        w.writerow(cells + [""] * (width - len(cells)))
    return buf.getvalue()


def mirror_lines(names: list[str], slugs: dict) -> list[str]:
    """'Scoreboard: =IMPORTDATA("<that tab's CSV link>")', one a tab. Empty without a public address."""
    out = []
    for n in names:
        url = link(slugs[n] + ".csv") if n in slugs else ""
        if url:
            out.append('%s: =IMPORTDATA("%s")' % (n, url))
    return out


def formulas() -> list[str]:
    """The mirror formula for every tab published so far, read from the volume with no network: for the
    tracker's own notes, Quinn's answer when someone asks for the links, or a person setting up a mirror."""
    state = _load(kpi_dir() / "site.json", {}) or {}
    if not link_key(create=False):
        return []
    return mirror_lines(state.get("tabs") or [], state.get("slugs") or {})


def write_site(tabs: list[dict], opts: dict, when: datetime, tz: str, notes: list[str] | None = None,
               sheet_url: str = "") -> dict:
    """The primary surface: the page and one CSV per tab, rewritten in place under the same links. A
    failed tab keeps its last good CSV and shows its last good numbers under a note that says when they
    are from. Each tab's link is fixed the first time it is seen (site.json), so a mirror never moves."""
    state = _load(kpi_dir() / "site.json", {}) or {}
    slugs = dict(state.get("slugs") or {})
    line = refreshed_line(when, tz)
    local = when.astimezone(_zone(tz))
    shown, problems = [], []
    link_key()                                     # made once, before any link is printed
    for t in tabs:
        if t["name"] not in slugs:
            slugs[t["name"]] = _slug(t["name"], set(slugs.values()))
        s = slugs[t["name"]]
        csv_path, good_path = site_dir() / (s + ".csv"), kpi_dir() / "good" / (s + ".json")
        if t["failed"]:
            last = _load(good_path, None)
            if isinstance(last, dict) and last.get("at"):
                at = when_of({"refreshed": last.get("at")})
                stale = "Not refreshed at %s: %s. These are the numbers from %s." % (
                    clock(local), _reason(t), long_when(at, last.get("tz") or tz))
                shown.append(dict(last, name=t["name"], slug=s, stale=stale))
            else:
                stale = "Not filled yet: %s." % _reason(t)
                shown.append({"name": t["name"], "slug": s, "note": "", "columns": [], "rows": [], "stale": stale})
                if not csv_path.exists():
                    try:
                        _atomic_write(csv_path, csv_text("Not refreshed yet", {"note": stale, "columns": [], "rows": []}))
                    except OSError as exc:
                        problems.append("the tracker CSV for %s could not be saved (%s)" % (t["name"], _short(exc)))
            continue
        good = {"name": t["name"], "note": t["note"], "columns": t["columns"], "rows": t["rows"],
                "at": when.isoformat(), "tz": tz}
        try:
            _atomic_write(csv_path, csv_text(line, t))
            _save(good_path, good)
        except OSError as exc:
            problems.append("the tracker CSV for %s could not be saved (%s), so it still shows the last good "
                            "numbers" % (t["name"], _short(exc)))
        shown.append(dict(good, slug=s, stale=""))
    names = [t["name"] for t in tabs]
    mirror = mirror_lines(names, slugs)
    page = render_page(opts, line, shown, notes or [], mirror, sheet_url)
    try:
        _atomic_write(site_dir() / PAGE, page)
    except OSError as exc:
        problems.append("the tracker page could not be saved (%s), so it still shows the last good numbers" % _short(exc))
    state.update(slugs=slugs, tabs=names, at=when.isoformat())
    try:
        _save(kpi_dir() / "site.json", state)
    except OSError as exc:
        problems.append("the tracker's list of links could not be saved (%s)" % _short(exc))
    return {"page": link(PAGE), "files": {n: link(slugs[n] + ".csv") for n in names}, "mirror": mirror,
            "slugs": {n: slugs[n] for n in names}, "problems": problems}


CSS = (
    ":root{--navy:#0A1130;--ink:#0A1130;--ink2:#141D45;--muted:#4A5568;--blue:#316AFF;--gold:#B7791F;"
    "--bg:#F5F7FB;--card:#FFFFFF;--line:#E1E6EF;--rule:#CDD5E3;--gold-bg:#FBF3E2;--gold-ink:#4A3208;"
    "--r:12px;--r-in:8px;--r-ctl:6px;--r-tag:4px}"
    "*{box-sizing:border-box}"
    "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}"
    "body{margin:0;background:var(--bg);color:var(--ink);"
    "font:15px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "main{max-width:1120px;margin:0 auto;padding:16px}"
    ".top{background:var(--navy);color:#FFFFFF;border-radius:var(--r);padding:16px 20px;margin:0 0 16px}"
    ".top h1{margin:0;font-size:20px;line-height:1.3;font-weight:700}"
    ".top p{margin:4px 0 0;color:#C9D3EA;overflow-wrap:anywhere}"
    ".top a{color:#FFFFFF}"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);"
    "box-shadow:0 1px 2px rgba(10,17,48,.06);padding:16px;margin:0 0 16px}"
    ".notes{margin:0 0 16px;padding:0 0 0 20px;color:var(--muted)}"
    "nav{display:flex;flex-wrap:wrap;gap:6px 18px}"
    "a{color:var(--blue)}"
    "nav a{text-decoration:none}"
    "nav a:hover,nav a:focus{text-decoration:underline}"
    "h2{margin:0 0 4px;font-size:17px;line-height:1.3}"
    "h3{margin:16px 0 4px;font-size:15px;line-height:1.3}"
    ".note{margin:0 0 12px;color:var(--muted);font-size:13px;overflow-wrap:anywhere}"
    ".stale{margin:0 0 12px;padding:8px 12px;background:var(--gold-bg);color:var(--gold-ink);"
    "border-left:3px solid var(--gold);border-radius:var(--r-in);font-size:13px}"
    "table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}"
    "th,td{padding:7px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere;"
    "border-bottom:1px solid var(--line)}"
    "th{background:var(--bg);color:var(--ink2);font-size:12px;font-weight:700;border-bottom:2px solid var(--rule)}"
    "td.n,th.n{text-align:right}"
    ".empty{margin:0;color:var(--muted)}"
    ".csv{margin:10px 0 0;font-size:13px}"
    ".mirror{list-style:none;margin:8px 0 0;padding:0}"
    ".mirror li{padding:8px 0;border-top:1px solid var(--line)}"
    ".mirror b{display:block;margin:0 0 4px}"
    "code{font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,'Liberation Mono',monospace;overflow-wrap:anywhere;"
    "word-break:break-all;background:var(--bg);border-radius:var(--r-tag);padding:1px 4px}"
)
# A wide table folds into one card per row below the width it needs, so no screen ever scrolls
# sideways. A table that fits a 320px phone never folds.
FOLDS = ((560, "w1"), (760, "w2"), (1000, "w3"), (1280, "w4"), (1600, "w5"))
# Folded, the cards sit two or more abreast where they fit (a tablet) and one per line on a phone. The
# label never shrinks below its own words: a long value squeezing it broke "Meaning" into "Meani ng".
FOLD = ("table.{c} thead{display:none}"
        "table.{c},table.{c} tbody,table.{c} tr,table.{c} td{display:block;width:100%}"
        "table.{c} tbody{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:10px}"
        "table.{c} tr{border:1px solid var(--line);border-radius:var(--r-in);padding:4px 0}"
        "table.{c} td{display:flex;justify-content:space-between;gap:12px;border:0;padding:4px 12px;text-align:left}"
        "table.{c} td.n{text-align:right}"
        "table.{c} td::before{content:attr(data-label);flex:none;max-width:45%;color:var(--muted);text-align:left;"
        "font-weight:400;overflow-wrap:normal;word-break:normal}"
        "table.{c} td:first-child{font-weight:700}"
        "table.{c} td:first-child::before{content:none}"
        "table.{c} td:empty{display:none}")


def _need_px(columns: list, rows: list) -> int:
    width = max([len(columns)] + [len(r) for r in rows] + [1])
    total = 0.0
    for ci in range(width):
        longest = 0
        for r in rows:
            v = r[ci] if ci < len(r) else ""
            longest = max(longest, 4 if _is_url(v) else len(_show(v)))
        head = len(str(columns[ci])) if ci < len(columns) else 0
        total += min(max(longest, min(head, 16)), 26) * 7.6 + 22
    return int(total) + 70


def _fold_class(columns: list, rows: list) -> str:
    px = _need_px(columns, rows)
    if px <= 320:
        return ""
    for bp, cls in FOLDS:
        if px <= bp:
            return cls
    return FOLDS[-1][1]


def _table_html(t: dict) -> tuple[str, str]:
    e = html.escape
    cols, rows = list(t.get("columns") or []), list(t.get("rows") or [])
    if not cols and not rows:
        return '<p class="empty">Nothing to show yet.</p>', ""
    width = max([len(cols)] + [len(r) for r in rows])
    cols = cols + [""] * (width - len(cols))
    num = []
    for ci in range(width):
        # A column is set right, as numbers are, when most of it is numbers: one "on track" row under six
        # months of counts must not push the whole column left.
        vals = [r[ci] for r in rows if ci < len(r) and r[ci] not in (None, "")]
        num.append(bool(vals) and sum(1 for v in vals if _numeric(v)) * 2 > len(vals))
    cls = _fold_class(cols, rows)
    out = ['<table%s>' % (' class="%s"' % cls if cls else ""), "<thead><tr>"]
    for ci, c in enumerate(cols):
        out.append('<th scope="col"%s>%s</th>' % (' class="n"' if num[ci] else "", e(str(c))))
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for ci in range(width):
            v = r[ci] if ci < len(r) else ""
            if _is_url(v):
                inner = '<a href="%s" rel="noreferrer noopener" target="_blank">Open</a>' % e(v, quote=True)
            else:
                inner = e(_show(v))
            out.append('<td%s data-label="%s">%s</td>' % (' class="n"' if num[ci] else "", e(str(cols[ci]), quote=True),
                                                          inner))
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out), cls


def _mirror_html(mirror: list[str]) -> str:
    e = html.escape
    if not mirror:
        return ('<h3>%s</h3><p class="note">The links appear here once the brain knows its public address '
                '(AGENTS_PUBLIC_URL).</p>' % e(MIRROR_HEAD))
    items = []
    for m in mirror:
        name, _, formula = m.partition(": ")
        items.append("<li><b>%s</b><code>%s</code></li>" % (e(name), e(formula)))
    return '<h3>%s</h3><p class="note">%s</p><ul class="mirror">%s</ul>' % (e(MIRROR_HEAD), e(MIRROR_INTRO), "".join(items))


def render_page(opts: dict, line: str, shown: list[dict], notes: list[str], mirror: list[str], sheet_url: str = "") -> str:
    """The whole tracker on one page. Flat and plain on purpose (Ty's design rules): a navy title card,
    one Refreshed line, the tab links, then a card per tab. No script at all, every value escaped."""
    e = html.escape
    key = link_key()
    how = (opts.get("how_tab") or "").lower()
    sections, used, how_seen = [], set(), False
    for t in shown:
        body, cls = _table_html(t)
        if cls:
            used.add(cls)
        csv_name = t["slug"] + ".csv"
        part = ['<section class="card" id="t-%s" aria-labelledby="h-%s">' % (t["slug"], t["slug"]),
                '<h2 id="h-%s">%s</h2>' % (t["slug"], e(t["name"]))]
        if t.get("stale"):
            part.append('<p class="stale">%s</p>' % e(t["stale"]))
        if t.get("note"):
            part.append('<p class="note">%s</p>' % e(t["note"]))
        part.append(body)
        part.append('<p class="csv"><a href="../%s/%s">CSV of this tab</a></p>' % (_sign(key, csv_name), csv_name))
        if how and t["name"].lower() == how:
            part.append(_mirror_html(mirror))
            how_seen = True
        part.append("</section>")
        sections.append("".join(part))
    if not how_seen:
        sections.append('<section class="card" id="t-mirror">%s</section>' % _mirror_html(mirror))
    css = CSS + "".join("@media (max-width:%dpx){%s}" % (bp, FOLD.replace("{c}", cls)) for bp, cls in FOLDS if cls in used)
    head = ['<!doctype html>', '<html lang="en">', "<head>", '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="robots" content="noindex, nofollow">', '<meta name="referrer" content="no-referrer">',
            '<meta http-equiv="Content-Security-Policy" content="%s">' % e(META_CSP, quote=True),
            "<title>%s</title>" % e(opts["title"]), "<style>%s</style>" % css, "</head>", "<body>", "<main>"]
    top = ['<header class="top"><h1>%s</h1><p>%s</p>' % (e(opts["title"]), e(line))]
    if sheet_url:
        top.append('<p><a href="%s" rel="noreferrer noopener" target="_blank">Open the Google Sheet</a></p>'
                   % e(sheet_url, quote=True))
    top.append("</header>")
    if notes:
        top.append('<ul class="notes">%s</ul>' % "".join("<li>%s</li>" % e(n) for n in notes))
    if len(shown) > 1:
        top.append('<nav class="card" aria-label="Tabs">%s</nav>'
                   % "".join('<a href="#t-%s">%s</a>' % (t["slug"], e(t["name"])) for t in shown))
    return "\n".join(head + top + sections + ["</main>", "</body>", "</html>", ""])


def preview_text(tabs: list[dict], opts: dict, when: datetime, tz: str, notes: list[str] | None = None) -> str:
    """Every tab as plain text (markdown tables), for the tracker's --no-sheet --preview check."""
    def cell(v) -> str:
        return _show(v).replace("|", "\\|").replace("\n", " ")
    out = ["# %s" % opts["title"], "", refreshed_line(when, tz)]
    out += ["- %s" % n for n in notes or []]
    for t in tabs:
        out += ["", "## %s" % t["name"]]
        if t["failed"]:
            out.append("Not refreshed: %s." % _reason(t))
            continue
        if t["note"]:
            out.append(t["note"])
        if t["columns"] or t["rows"]:
            width = max([len(t["columns"])] + [len(r) for r in t["rows"]] + [1])
            cols = list(t["columns"]) + [""] * (width - len(t["columns"]))
            out += ["", "| %s |" % " | ".join(cell(c) for c in cols), "|%s" % ("---|" * width)]
            out += ["| %s |" % " | ".join(cell(r[i] if i < len(r) else "") for i in range(width)) for r in t["rows"]]
    return "\n".join(out) + "\n"


# ====================================================================== 2. the optional Google Sheet
def _google_http(method, url, json=None, data=None, headers=None):
    r = _http.request(method, url, json=json, data=data, headers=headers, timeout=30)
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"message": (getattr(r, "text", "") or "")[:200]}}
    return r.status_code, body


def _g(method: str, url: str, *, body=None, form=None, token: str = "") -> dict:
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        status, data = (GOOGLE or _google_http)(method, url, json=body, data=form, headers=headers)
    except Exception as exc:  # noqa: BLE001  (an unreachable Google is a problem to report, never a crash)
        raise GoogleError(0, "Google could not be reached (%s)" % _short(exc)) from exc
    data = data if isinstance(data, dict) else {}
    if int(status or 0) >= 300:
        err = data.get("error")
        if isinstance(err, dict):
            reasons = [x.get("reason", "") for x in (err.get("errors") or []) if isinstance(x, dict)]
            raise GoogleError(status, str(err.get("message") or "")[:160],
                              (reasons[0] if reasons and reasons[0] else str(err.get("status") or "")))
        raise GoogleError(status, str(data.get("error_description") or err or "")[:160], str(err or ""))
    return data


def _service_account(raw: str) -> tuple[dict | None, str]:
    """The key file itself, its text, or (for a one-line .env value) its text in base64."""
    text = raw
    if not raw.lstrip().startswith("{"):
        try:
            p = Path(raw).expanduser()
            if len(raw) < 1024 and p.is_file():
                text = p.read_text(encoding="utf-8")
            else:
                text = base64.b64decode(raw).decode("utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            return None, "it is not the key file, its text, or the text in base64"
    try:
        info = json.loads(text)
    except ValueError:
        return None, "it is not valid JSON"
    if not isinstance(info, dict) or not info.get("client_email") or "PRIVATE KEY" not in str(info.get("private_key") or ""):
        return None, "it has no client_email or private_key"
    return info, ""


def credential() -> tuple[dict | None, str]:
    """(credential, problem). (None, "") when no Google setting exists at all: that is the normal case
    now that the brain's own page is the tracker, and it is said nowhere. A half-set one is a problem."""
    raw = (os.environ.get(SA_ENV) or "").strip()
    if raw:
        info, why = _service_account(raw)
        if info:
            return {"mode": "service_account", "info": info, "who": info["client_email"]}, ""
        return None, "the Google service account key in %s could not be read: %s" % (SA_ENV, why)
    vals = {k: (os.environ.get(k) or "").strip() for k in OAUTH_ENV}
    if any(vals.values()):
        missing = [k for k, v in vals.items() if not v]
        if missing:
            return None, "the Google sign-in is incomplete: %s %s not set" % (_and(missing), "is" if len(missing) == 1 else "are")
        return {"mode": "oauth", "client_id": vals[OAUTH_ENV[0]], "client_secret": vals[OAUTH_ENV[1]],
                "refresh_token": vals[OAUTH_ENV[2]], "who": vals[OAUTH_ENV[0]]}, ""
    return None, ""


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def service_account_assertion(info: dict, now: int) -> str:
    """The signed JWT a service account trades for an access token: RS256 over the header and claims,
    good for one hour, for the two scopes above, addressed to Google's token endpoint."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    head = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        head["kid"] = str(info["private_key_id"])
    claims = {"iss": info["client_email"], "scope": SCOPES, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}
    signing = (_b64u(json.dumps(head, separators=(",", ":")).encode()) + "."
               + _b64u(json.dumps(claims, separators=(",", ":")).encode()))
    key = serialization.load_pem_private_key(str(info["private_key"]).encode(), password=None)
    return signing + "." + _b64u(key.sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()))


def access_token(cred: dict) -> str:
    """An access token, reused until a minute before it expires (one run makes five or six calls)."""
    k = (cred["mode"], cred.get("who", ""))
    hit = _TOKENS.get(k)
    if hit and hit[1] - 60 > time.time():
        return hit[0]
    if cred["mode"] == "service_account":
        form = {"grant_type": JWT_GRANT, "assertion": service_account_assertion(cred["info"], int(time.time()))}
    else:
        form = {"client_id": cred["client_id"], "client_secret": cred["client_secret"],
                "refresh_token": cred["refresh_token"], "grant_type": "refresh_token"}
    data = _g("POST", TOKEN_URL, form=form)
    tok = str(data.get("access_token") or "")
    if not tok:
        raise GoogleError(200, "Google returned no access token")
    try:
        life = float(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        life = 3600.0
    _TOKENS[k] = (tok, time.time() + life)
    return tok


def share_list() -> list[tuple[str, str]]:
    """AGENTS_KPI_SHEET_SHARE: comma-separated emails, each read-only unless written email=writer."""
    out = []
    for part in re.split(r"[,;\s]+", os.environ.get("AGENTS_KPI_SHEET_SHARE") or ""):
        email, _, role = part.partition("=")
        role = role.strip().lower() or "reader"
        if "@" in email:
            out.append((email.strip().lower(), role if role in ("reader", "commenter", "writer") else "reader"))
    return out


def _q(name: str) -> str:
    return "'%s'" % name.replace("'", "''")


def _grid(rows: list, width: int) -> dict:
    return {"rowCount": max(1000, len(rows) + 100), "columnCount": max(26, width)}


def _sheet_rows(line: str, t: dict, extra: list[str] | None = None) -> list[list]:
    rows = [[line], [t["note"] or ""], [], list(t["columns"])] + [["" if v is None else v for v in r] for r in t["rows"]]
    if extra:
        rows += [[]] + [[x] for x in extra]
    return rows


def _col_px(rows: list, width: int, upto: int = 0) -> list[int]:
    """Column widths from the header and the table only; the Refreshed line and any note rows below the
    table run past their cell on purpose and must not widen column A."""
    body = rows[HEADER_ROWS - 1:(upto or len(rows))]
    out = []
    for ci in range(width):
        longest = max([len(_show(r[ci])) for r in body if ci < len(r)] or [0])
        out.append(min(max(longest * (8 if ci == 0 else 7) + 24, 160 if ci == 0 else 70), 360 if ci == 0 else 280))
    return out


def _fmt(gid: int, rows: list, width: int, extra_from: int) -> list[dict]:
    """Flat, as Ty's tracker: plain text, a gray Refreshed line and note, a bold header over a 1px rule,
    the header frozen, no colored bands anywhere."""
    def rng(r0, r1):
        return {"sheetId": gid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": 0, "endColumnIndex": max(width, 1)}

    def text(r0, r1, fmt):
        return {"repeatCell": {"range": rng(r0, r1), "cell": {"userEnteredFormat": {"textFormat": fmt}},
                               "fields": "userEnteredFormat.textFormat"}}
    reqs = [
        {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": HEADER_ROWS}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": gid}, "cell": {"userEnteredFormat": {
            "textFormat": {"fontSize": 10, "foregroundColor": INK}, "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat"}},
        text(0, 1, {"fontSize": 10, "foregroundColor": MUTED}),
        text(1, 2, {"fontSize": 9, "foregroundColor": MUTED}),
        {"repeatCell": {"range": rng(3, 4), "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": INK}, "wrapStrategy": "WRAP",
            "verticalAlignment": "BOTTOM"}},
            "fields": "userEnteredFormat.textFormat,userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}},
        {"updateBorders": {"range": rng(3, 4), "bottom": {"style": "SOLID", "color": RULE}}},
    ]
    if extra_from and extra_from < len(rows):
        reqs.append(text(extra_from, len(rows), {"fontSize": 9, "foregroundColor": MUTED}))
    for ci, px in enumerate(_col_px(rows, width, extra_from)):
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": ci,
                                                             "endIndex": ci + 1},
                                                   "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    return reqs


def _open_or_create(tok: str, meta: dict, env_id: str, cred: dict, opts: dict, plan: list, tz: str, out: dict):
    """The sheet's properties, opening the remembered or configured sheet, or making one. A sheet this
    module made and someone deleted is made again once; a sheet named in AGENTS_KPI_SHEET_ID is never
    replaced, because it is the owner's own file."""
    if meta.get("id"):
        try:
            info = _g("GET", "%s/%s?fields=spreadsheetId,spreadsheetUrl,sheets.properties" % (SHEETS, meta["id"]),
                      token=tok)
            meta["url"] = info.get("spreadsheetUrl") or meta.get("url")
            return info
        except GoogleError as exc:
            if env_id and exc.status in (403, 404):
                who = cred.get("who") if cred.get("mode") == "service_account" else "the Google account that signed in"
                out["problems"].append(
                    "the Google Sheet in AGENTS_KPI_SHEET_ID could not be opened (Google answered %d): check the id, "
                    "and that the sheet is shared with %s as an editor" % (exc.status, who))
                return None
            if exc.status != 404 or env_id:
                raise
            out["notes"].append("the Google Sheet this tracker made is gone, so a new one was made")
            meta.clear()
    body = {"properties": {"title": opts["title"], "locale": "en_US", "timeZone": tz},
            "sheets": [{"properties": {"title": name, "index": i, "gridProperties": _grid(rows, w)}}
                       for i, (name, rows, w, _x) in enumerate(plan)]}
    try:
        made = _g("POST", SHEETS, body=body, token=tok)
    except GoogleError as exc:
        storage = "storage" in (exc.message + " " + exc.reason).lower()
        if cred["mode"] == "service_account" and exc.status == 403 and storage:
            out["problems"].append(
                "the Google service account cannot own a new sheet: make a blank Google Sheet, share it with %s as "
                "an editor, and set AGENTS_KPI_SHEET_ID to its id" % cred.get("who", "the service account"))
            return None
        raise
    meta.update(id=made.get("spreadsheetId"), url=made.get("spreadsheetUrl"), created=[p[0] for p in plan],
                shared=[], good={})
    out["notes"].append("the Google Sheet was made")
    return made


def _share(tok: str, meta: dict, out: dict) -> None:
    """Shares the sheet once with each email in AGENTS_KPI_SHEET_SHARE, quietly (no email from Google:
    the Slack message carries the link), and once more only for an email added later."""
    done = set(meta.get("shared") or [])
    for email, role in share_list():
        key = "%s=%s" % (email, role)
        if key in done:
            continue
        try:
            _g("POST", "%s/%s/permissions?sendNotificationEmail=false&supportsAllDrives=true" % (DRIVE, meta["id"]),
               body={"type": "user", "role": role, "emailAddress": email}, token=tok)
            done.add(key)
        except GoogleError as exc:
            out["problems"].append("the Google Sheet could not be shared with %s: Google answered %d" % (email, exc.status))
    meta["shared"] = sorted(done)


def _write_tabs(tok: str, meta: dict, info: dict, tabs: list, plan: list, when: datetime, tz: str, out: dict) -> None:
    sid = meta["id"]
    props = {}
    for s in info.get("sheets") or []:
        p = s.get("properties") or {}
        if p.get("title") is not None:
            props[p["title"]] = p
    created = set(meta.get("created") or [])
    good = dict(meta.get("good") or {})
    local = when.astimezone(_zone(tz))
    index_of = {title: int(p.get("index") or 0) for title, p in props.items()}
    order = [t["name"] for t in tabs]
    pre, write, stale, fmt_later = [], [], [], []
    for t, (name, rows, width, extra_from) in zip(tabs, plan):
        if name not in props and name in created:
            out["notes"].append("the %s tab was deleted from the Google Sheet by hand, so it is not added back" % name)
            continue
        if t["failed"]:
            at = good.get(name)
            if name in props and at:
                # Its last good numbers stay; only the note cell says this run could not refresh them.
                stale.append((name, "Not refreshed at %s: %s. These are the numbers from %s." % (
                    clock(local), _reason(t), long_when(when_of({"refreshed": at}), tz))))
                continue
            # Never filled (a new tab, or a sheet made this run): there are no numbers to keep.
            rows, width, extra_from = [["Not refreshed yet"], ["Not filled yet: %s." % _reason(t)]], 1, 0
        if name not in props:
            before = [n for n in order[:order.index(name)] if n in index_of]
            idx = index_of[before[-1]] + 1 if before else 0
            for k in index_of:
                if index_of[k] >= idx:
                    index_of[k] += 1
            index_of[name] = idx
            pre.append({"addSheet": {"properties": {"title": name, "index": idx, "gridProperties": _grid(rows, width)}}})
            created.add(name)
        else:
            g = props[name].get("gridProperties") or {}
            have_r, have_c = int(g.get("rowCount") or 0), int(g.get("columnCount") or 0)
            if have_r < len(rows) or have_c < width:
                pre.append({"updateSheetProperties": {"properties": {"sheetId": props[name]["sheetId"], "gridProperties": {
                    "rowCount": max(have_r, len(rows) + 100), "columnCount": max(have_c, width)}},
                    "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
        write.append((name, rows, width, extra_from, name in props, t["failed"]))
    meta["created"] = sorted(created)
    if pre:
        r = _g("POST", "%s/%s:batchUpdate" % (SHEETS, sid), body={"requests": pre}, token=tok)
        for rep in r.get("replies") or []:
            p = (rep or {}).get("addSheet", {}).get("properties")
            if p:
                props[p["title"]] = p
    clear = [_q(name) for name, _r, _w, _x, existed, _f in write if existed]
    if clear:
        _g("POST", "%s/%s/values:batchClear" % (SHEETS, sid), body={"ranges": clear}, token=tok)
    data = [{"range": "%s!A1" % _q(name), "values": rows} for name, rows, _w, _x, _e, _f in write]
    data += [{"range": "%s!A2" % _q(name), "values": [[note]]} for name, note in stale]
    if data:
        _g("POST", "%s/%s/values:batchUpdate" % (SHEETS, sid), body={"valueInputOption": "RAW", "data": data}, token=tok)
    for name, rows, width, extra_from, _e, failed in write:
        if not failed:
            good[name] = when.isoformat()
        if name in props:
            fmt_later += _fmt(int(props[name]["sheetId"]), rows, width, extra_from)
    for name, _note in stale:
        gid = int(props[name]["sheetId"])
        fmt_later.append({"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 0,
                                                   "endColumnIndex": 1},
                                         "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 10,
                                                                                      "foregroundColor": CAVEAT}}},
                                         "fields": "userEnteredFormat.textFormat"}})
    meta["good"] = good
    if fmt_later:
        _g("POST", "%s/%s:batchUpdate" % (SHEETS, sid), body={"requests": fmt_later}, token=tok)


def _google_problem(exc: GoogleError, cred: dict) -> str:
    if cred.get("mode") == "oauth" and "invalid_grant" in (exc.reason + " " + exc.message):
        return ("the Google sign-in has expired or was revoked, so the Google Sheet was not updated: run the sign-in "
                "again (python -m agents.autopilot.kpisinks oauth) and replace AGENTS_GOOGLE_REFRESH_TOKEN")
    if exc.status == 0:
        why = "Google could not be reached"
    else:
        why = "Google answered %d%s" % (exc.status, (" (%s)" % exc.message.strip().rstrip(".")) if exc.message.strip() else "")
    return "the Google Sheet could not be updated: %s. It still shows its last good numbers; the tracker page is up to date" % why


def write_sheet(tabs: list[dict], opts: dict, when: datetime, tz: str, how_extra: list[str] | None = None) -> dict:
    """The optional Google Sheet. Without a credential it does nothing and says nothing."""
    out = {"url": None, "problems": [], "notes": []}
    cred, why = credential()
    if not cred:
        if why:
            out["problems"].append(why)
        return out
    meta_path = kpi_dir() / "sheet.json"
    meta = _load(meta_path, {}) or {}
    env_id = (os.environ.get("AGENTS_KPI_SHEET_ID") or "").strip()
    if env_id and meta.get("id") != env_id:
        meta = {"id": env_id, "created": [], "shared": [], "good": {}}
    line = refreshed_line(when, tz)
    how = (opts.get("how_tab") or "").lower()
    plan = []
    for t in tabs:
        extra = how_extra if (how and t["name"].lower() == how and how_extra) else None
        rows = _sheet_rows(line, t, extra)
        width = max(len(r) for r in rows) or 1
        plan.append((t["name"], rows, width, len(rows) - len(extra) if extra else 0))
    try:
        tok = access_token(cred)
        info = _open_or_create(tok, meta, env_id, cred, opts, plan, tz, out)
        if info is None:
            return out
        if share_list():
            _share(tok, meta, out)
        _write_tabs(tok, meta, info, tabs, plan, when, tz, out)
        out["url"] = meta.get("url") or "https://docs.google.com/spreadsheets/d/%s/edit" % meta["id"]
    except GoogleError as exc:
        out["problems"].append(_google_problem(exc, cred))
    finally:
        if meta.get("id"):
            try:
                _save(meta_path, meta)
            except OSError as exc:
                out["problems"].append("the Google Sheet's id could not be saved (%s)" % _short(exc))
    return out


# ====================================================================== 3. Slack
def _slack(method: str, payload: dict) -> dict:
    try:
        r = (SLACK or slack_app._api)(method, payload)
    except Exception as exc:  # noqa: BLE001  (a Slack outage must not fail the tracker)
        return {"ok": False, "error": _short(exc)}
    return r if isinstance(r, dict) else {"ok": False, "error": "no answer"}


def _slack_words(err: str) -> str:
    return SLACK_WORDS.get(err or "") or (err or "no answer").replace("_", " ")


def _employee(owner: str):
    try:
        from .. import team
        return team.load().employees.get(owner or "ops")
    except Exception:  # noqa: BLE001  (no roster: the app posts under its own name)
        return None


def _post(channel: str, text: str, emp=None) -> tuple[str, str]:
    """(ts, error). Never unfurled: Slack would otherwise fetch the secret page to build a preview."""
    body = {"channel": channel, "text": text[:3900], "unfurl_links": False, "unfurl_media": False}
    if emp is not None:
        body.update(slack_app.identity(emp))     # the employee's name and portrait (emoji when it cannot be served)
    r = _slack("chat.postMessage", body)
    if r.get("error") == "not_in_channel" and _slack("conversations.join", {"channel": channel}).get("ok"):
        r = _slack("chat.postMessage", body)
    if r.get("ok") and r.get("ts"):
        return str(r["ts"]), ""
    return "", str(r.get("error") or "no answer")


_TIME_COLON = re.compile(r"(?<=\d):(?=\d)")


def _clean(text: str, max_lines: int | None = None) -> str:
    """voice.clean, with every colon between two digits kept. voice.EMOJI_RX reads ":45:" as an emoji
    name, so "Today final at 7:45: 412 dials" came out "Today final at 7 412 dials" (found 2026-11-16).
    The colon is set aside before the cleanup and put back after it."""
    return voice.clean(_TIME_COLON.sub("\x00", text or ""), max_lines=max_lines).replace("\x00", ":")


def slack_text(snapshot: dict, page_url: str = "") -> str:
    """The snapshot's own lines (already a fixed template in Ty's register), cleaned the free way and
    never rewritten by a model: the numbers ARE the message, and the rewrite's number guard would turn
    most paraphrase away anyway. The page link goes on the first line."""
    lines = [str(x) for x in ((snapshot or {}).get("slack") or {}).get("lines") or [] if str(x).strip()]
    text = PHONE_RX.sub("[number]", _clean("\n".join(lines), max_lines=12)) if lines else ""
    if not text:
        return ""
    first, _, rest = text.partition("\n")
    if page_url and "<http" not in first:
        first = "%s <%s|Open the tracker>" % (first.rstrip(), page_url)
    return (first + ("\n" + rest if rest else ""))[:3900]


def post_daily(snapshot: dict, opts: dict, page_url: str, day: str, owner: str = "ops") -> tuple[str, list[str]]:
    """One message a day in the pipeline channel: the first run posts it, later runs edit it in place.
    In the boards layout (2026-11-19) it is the team channel's daily scoreboard instead: the department
    channels carry the work, this one message carries the numbers across all of it."""
    mode = opts.get("slack", "edit")
    if mode == "off":
        return "off", []
    if not config.SLACK_BOT_TOKEN:
        return "not sent: the Slack app is not set up", []
    boards = (getattr(config, "SLACK_LAYOUT", "") or "classic").strip().lower() == "boards"
    channel = slack_app.channel_for("chat" if boards else "pipeline")
    if not channel:
        return "not sent: no Slack channel is set", []
    text = slack_text(snapshot, page_url)
    if not text:
        return "nothing to say", []
    key = "kpi:slack:%s" % day
    try:
        kept = json.loads(store.kv_get(key) or "{}")
    except ValueError:
        kept = {}
    again = False
    if mode == "edit" and kept.get("ts") and kept.get("channel") == channel:
        r = _slack("chat.update", {"channel": channel, "ts": kept["ts"], "text": text})
        if r.get("ok"):
            return "updated", []
        err = str(r.get("error") or "")
        if err not in GONE:
            return "not updated", ["the KPI message in Slack could not be updated: %s" % _slack_words(err)]
        again = True
    ts, err = _post(channel, text, _employee(owner))
    if not ts:
        return "not sent", ["the KPI message could not be posted to Slack: %s" % _slack_words(err)]
    if mode == "edit":
        store.kv_set(key, json.dumps({"channel": channel, "ts": ts}))
    return ("posted again" if again else "posted"), []


def issue_text(when: datetime, tz: str, kept: list[str], problems: list[str]) -> str:
    at = clock(when.astimezone(_zone(tz)))
    lines = []
    if kept:
        lines.append("KPI tracker at %s: %s still %s the last good numbers." % (at, _and(kept),
                                                                              "shows" if len(kept) == 1 else "show"))
    for p in problems[:4]:
        s = _sentence(p)
        if s:
            lines.append(s if lines else "KPI tracker at %s: %s" % (at, s[:1].lower() + s[1:]))
    lines.append("It tries again at the next run.")
    return PHONE_RX.sub("[number]", _clean("\n".join(lines)))


def post_issue(stage_id: str, when: datetime, tz: str, day: str, kept: list[str], said: list[str],
               problems: list[str], owner: str = "ops") -> bool:
    """The tracker's one issues post a day, naming the tabs that still show their last good numbers and
    why. What it says is marked as said for today (voice.first_today, the key sweep.posts reads), so the
    stage's own problems going through the normal routing are not posted a second time. A post that
    could not be sent marks nothing, so the normal routing can still say it."""
    if not (kept or problems):
        return False
    key = "kpi:issue:%s" % day
    if store.kv_get(key):
        for p in said:
            voice.first_today(stage_id, p)
        return False
    if not config.SLACK_BOT_TOKEN:
        return False
    channel = slack_app.channel_for("issues")
    if not channel:
        return False
    ts, _err = _post(channel, issue_text(when, tz, kept, problems), _employee(owner))
    if not ts:
        return False
    store.kv_set(key, ts)
    for p in said:
        voice.first_today(stage_id, p)
    return True


# ====================================================================== the one entry point
def publish(ctx, snapshot: dict, *, preview=None, sheet: bool = True, slack: bool = True) -> dict:
    """Publish one snapshot. Never raises. Returns
        {"page": the brain's page link ("" without AGENTS_PUBLIC_URL), "files": {tab: CSV link},
         "sheet": the Google Sheet link or None, "slack": what happened in Slack, in a few words,
         "problems": [str] for the stage's Summary, "notes": [str], "kept": [tabs showing old numbers]}
    preview=<path> writes every tab as text to that file and touches nothing else; sheet=False skips
    Google; slack=False skips Slack."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    st = getattr(ctx, "stage", None)
    opts = settings(st)
    tz = str(snapshot.get("tz") or getattr(getattr(ctx, "policy", None), "tz", "") or DEFAULT_TZ)
    when = when_of(snapshot, ctx)
    day = when.astimezone(_zone(tz)).date().isoformat()
    stage_id = str(getattr(st, "id", "") or getattr(getattr(ctx, "out", None), "stage", "") or "kpi_tracker")
    owner = str(getattr(st, "owner", "") or "ops")
    tabs = _tabs(snapshot)
    seen: dict = {}
    pub = [_guard_tab(t, True, seen) for t in tabs]
    priv = [_guard_tab(t, False, None) for t in tabs]
    notes_in = [str(guard_cell(str(n), True, seen)) for n in snapshot.get("notes") or [] if str(n).strip()]
    out = {"page": "", "files": {}, "mirror": [], "sheet": None, "slack": "", "problems": [], "notes": [], "kept": []}
    if preview:
        try:
            Path(preview).write_text(preview_text(pub, opts, when, tz, notes_in), encoding="utf-8")
            out["files"] = {"preview": str(preview)}
        except OSError as exc:
            out["problems"].append("the preview could not be written (%s)" % _short(exc))
        out["slack"] = "not sent: preview"
        return out
    kept = [t["name"] for t in tabs if t["failed"]]
    problems: list[str] = []
    if kept:
        problems.append("these tabs still show their last good numbers: %s" % _and(kept))
    # The page links the Google Sheet only while a credential still writes it: a sheet left behind by a
    # removed credential would show numbers that stopped moving.
    old_sheet = ((_load(kpi_dir() / "sheet.json", {}) or {}).get("url") or "") if (sheet and credential()[0]) else ""
    try:
        site = write_site(pub, opts, when, tz, notes_in, sheet_url=old_sheet)
    except Exception as exc:  # noqa: BLE001  (one surface failing must not stop the others)
        log.exception("kpi page failed")
        site = {"page": "", "files": {}, "mirror": [],
                "problems": ["the tracker page could not be written (%s), so it still shows the last good numbers"
                             % _short(exc)]}
    out["page"], out["files"], out["mirror"] = site.get("page") or "", site.get("files") or {}, site.get("mirror") or []
    problems += site.get("problems") or []
    if not out["page"]:
        out["notes"].append("AGENTS_PUBLIC_URL is not set, so the page has no link to give")
    if sheet:
        try:
            g = write_sheet(priv, opts, when, tz, [MIRROR_HEAD + ".", MIRROR_INTRO] + (site.get("mirror") or []) + notes_in
                            if site.get("mirror") else notes_in)
        except Exception as exc:  # noqa: BLE001
            log.exception("kpi google sheet failed")
            g = {"url": None, "problems": ["the Google Sheet could not be updated (%s)" % _short(exc)], "notes": []}
        out["sheet"] = g.get("url")
        problems += g.get("problems") or []
        out["notes"] += g.get("notes") or []
    if slack:
        try:
            status, said = post_daily(snapshot, opts, out["page"], day, owner)
        except Exception as exc:  # noqa: BLE001
            log.exception("kpi slack post failed")
            status, said = "not sent", ["the KPI message could not be posted to Slack (%s)" % _short(exc)]
        out["slack"] = status
        problems += said
    for what, n in sorted(seen.items()):
        out["notes"].append("%d %s shaped like a %s %s replaced with [%s] on the page and in the CSVs" % (
            n, "cell" if n == 1 else "cells", "phone number" if what == "phone" else "street address",
            "was" if n == 1 else "were", "number" if what == "phone" else "street"))
    out["problems"] = list(dict.fromkeys(p for p in problems if p))
    out["kept"] = kept
    # Slack off means this module says nothing in Slack at all; the stage's problems then go through the
    # normal routing (sweep.posts), once a day each.
    if slack and opts["slack"] != "off":
        snap_problems = [str(p) for p in snapshot.get("problems") or [] if str(p).strip()]
        own = [p for p in out["problems"] if not p.startswith("these tabs still show")]
        try:
            post_issue(stage_id, when, tz, day, kept, out["problems"] + snap_problems, snap_problems + own, owner)
        except Exception:  # noqa: BLE001
            log.exception("kpi issue post failed")
    return out


# ====================================================================== command line
def consent_url(client_id: str, redirect: str, state: str) -> str:
    from urllib.parse import urlencode
    return AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                                       "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state})


def _cli_links() -> int:
    state = _load(kpi_dir() / "site.json", {}) or {}
    if not link_key(create=False) or not state.get("tabs"):
        print("Nothing has been published on this box yet.")
        return 1
    if not config.PUBLIC_URL:
        print("AGENTS_PUBLIC_URL is not set, so there are no links to give.")
        return 1
    print("Page: %s" % link(PAGE))
    for line in formulas():
        print(line)
    return 0


def _cli_check() -> int:
    cred, why = credential()
    if not cred:
        print(why or "No Google credential is set. That is fine: the tracker page needs none.")
        return 1 if why else 0
    sid = (os.environ.get("AGENTS_KPI_SHEET_ID") or "").strip() or (_load(kpi_dir() / "sheet.json", {}) or {}).get("id")
    try:
        tok = access_token(cred)
        if not sid:
            print("The Google sign-in works. There is no sheet yet: the next run makes one.")
            return 0
        info = _g("GET", "%s/%s?fields=spreadsheetUrl,sheets.properties.title" % (SHEETS, sid), token=tok)
    except GoogleError as exc:
        print("Google answered %d: %s" % (exc.status, exc.message or exc.reason))
        return 1
    titles = [(s.get("properties") or {}).get("title", "") for s in info.get("sheets") or []]
    print("The Google sign-in works and the sheet opens: %s (tabs: %s)" % (info.get("spreadsheetUrl", ""), ", ".join(titles)))
    return 0


def _cli_oauth(client_id: str, client_secret: str, port: int = 0) -> int:
    """One-time sign-in for the OAuth route: opens Google's consent page, catches the answer on this
    machine, and prints the three lines for .env. The refresh token it prints is a password: keep it there."""
    import http.server
    import urllib.parse
    import webbrowser
    state, got = secrets.token_urlsafe(16), {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  (the stdlib's name)
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            return

    srv = http.server.HTTPServer(("127.0.0.1", port), Back)
    srv.timeout = 300
    redirect = "http://127.0.0.1:%d/" % srv.server_address[1]
    url = consent_url(client_id, redirect, state)
    print("Sign in with the Google account that should own the sheet:\n%s\n" % url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    srv.handle_request()
    srv.server_close()
    if got.get("state") != state or not got.get("code"):
        print("No sign-in came back (%s). Run it again." % (got.get("error") or "timed out"))
        return 1
    try:
        data = _g("POST", TOKEN_URL, form={"code": got["code"], "client_id": client_id, "client_secret": client_secret,
                                           "redirect_uri": redirect, "grant_type": "authorization_code"})
    except GoogleError as exc:
        print("Google refused the sign-in: %s" % (exc.message or exc.reason))
        return 1
    if not data.get("refresh_token"):
        print("Google returned no refresh token. Remove this app under myaccount.google.com, Security, Third-party "
              "access, then run this again.")
        return 1
    print("Add these three lines to .env, then sync the secrets:\nAGENTS_GOOGLE_CLIENT_ID=%s\n"
          "AGENTS_GOOGLE_CLIENT_SECRET=%s\nAGENTS_GOOGLE_REFRESH_TOKEN=%s" % (client_id, client_secret, data["refresh_token"]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agents.autopilot.kpisinks",
                                 description="The KPI tracker's page links, and the optional Google Sheet setup.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("links", help="the page link and each tab's IMPORTDATA formula (reads the volume only)")
    sub.add_parser("check", help="the optional Google route: sign in and open the sheet, read only")
    o = sub.add_parser("oauth", help="one-time Google sign-in for the OAuth route; prints the .env lines")
    o.add_argument("--client-id", required=True)
    o.add_argument("--client-secret", required=True)
    o.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)
    if args.cmd == "links":
        return _cli_links()
    if args.cmd == "check":
        return _cli_check()
    return _cli_oauth(args.client_id, args.client_secret, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
