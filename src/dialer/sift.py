"""DataSift over the Open API key, for the dialer.

Self-contained on purpose (requests only): the dialer's image carries no Deal Room checkout, no brain and
no texting app. Every route here is in DataSift's published reference, the same ones the autopilot and
the texting app already write through:

  * a record           GET   /api/internal/property/{uuid}/
  * the records search POST  /api/internal/property/  with x-http-method-override: GET (a bare POST creates)
  * preset folders     GET   /api/internal/filter-preset-folder/?type=properties&limit=999
  * a folder's presets GET   /api/internal/filter-preset-folder/{uuid}/filter-preset/?limit=999
  * a preset           GET   /api/internal/filter-preset/{uuid}/
  * phones             POST  /api/internal/owner/{uuid}/upsert-phones/  {"phones": [FULL phone objects]}
  * the call counter   POST  /api/internal/property/{uuid}/predictivecall-attempts/  {"predictivecall_attempts": TOTAL}
  * a note             POST  /api/internal/property/{uuid}/message/  {"message": ...}
  * status             POST  /api/internal/property/{uuid}/status/   {"status": ...}
  * a tag              POST  /api/internal/property/{uuid}/add-tags/  {"tags": "<one title>"}
  * a list             POST  /api/internal/property/{uuid}/add-lists/ {"lists": "<one title>"}
  * a task             POST  /api/internal/task/  {title, all_day, due_date, notes, assigned_to_users, assigned_to_property}

Pacing. The account's limit (120 a minute, 5,000 an hour) is shared by every tool on the key, so the
background writes go at DIALER_SIFT_GAP_S apart. A read a caller is waiting on (the record before a call)
does not queue behind them. A 429's "available in N seconds" or Retry-After is honored. A dropped
connection is retried only for a read: a write may have landed before the connection died.

Two contracts that fail silently if forgotten:
  * upsert-phones REPLACES the phone object it matches by number, so the payload is always the whole
    object (type, tags, status, connected flag) with one field changed. `phone_payload` builds it.
  * a full record omits the do-not-call registry flag entirely. It is on a SEARCH row's representative
    phone only, so the power dialer keeps the row it found the record by.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Optional

from . import config

log = logging.getLogger(__name__)

PAGE = 250
MAX_WINDOW = 10000
_HINT = re.compile(r"available in ([0-9]+)", re.I)
PHONE_STATUSES = ("UNKNOWN", "CORRECT", "WRONG", "DEAD", "NO_ANSWER", "DNC", "CORRECT_DNC", "WRONG_DNC")
DNC_FOR = {"CORRECT": "CORRECT_DNC", "WRONG": "WRONG_DNC", "WRONG_DNC": "WRONG_DNC", "CORRECT_DNC": "CORRECT_DNC"}


class SiftError(Exception):
    def __init__(self, code: int, body: str, path: str = ""):
        super().__init__("HTTP %s on %s: %s" % (code, path.split("?")[0], (body or "")[:200]))
        self.code, self.body, self.path = code, body or "", path


def rows_of(r) -> list:
    if isinstance(r, dict):
        return r.get("results") or r.get("data") or []
    return r if isinstance(r, list) else []


def titles(items) -> list[str]:
    out = []
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def digits(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def phone_payload(p: dict, **overrides) -> dict:
    """The FULL phone object upsert-phones needs, with only `overrides` changed."""
    payload = {
        "number": digits(p.get("number")),
        "type": str(p.get("type") or "UNKNOWN").upper(),
        "tags": titles(p.get("tags")),
        "status": str(p.get("status") or "UNKNOWN").upper(),
        "is_connected": p.get("is_connected", True),
    }
    if "verified" in p:
        payload["verified"] = p.get("verified")
    payload.update(overrides)
    return payload


class Sift:
    """The transport. Swap `send` in tests (the selftest's fake answers every route in memory)."""

    def __init__(self, key: str | None = None, base: str | None = None, gap_s: float | None = None):
        self.key = config.S.sift_key if key is None else key
        self.base = (config.S.sift_base if base is None else base).rstrip("/")
        self.gap_s = config.S.sift_gap_s if gap_s is None else gap_s
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0
        self._cache: dict = {}
        self._presets: Optional[tuple] = None

    # ------------------------------------------------------------------ transport
    def send(self, method: str, url: str, headers: dict, body: Any) -> tuple[int, str, dict]:
        import requests
        r = requests.request(method, url, headers=headers, data=None if body is None else json.dumps(body), timeout=45)
        return r.status_code, r.text, dict(r.headers)

    def call(self, path: str, method: str = "GET", body: Any = None, *, read: bool | None = None,
             interactive: bool = False, tries: int = 5) -> Any:
        if not self.key:
            raise SiftError(0, "REISIFT_API_KEY is not set", path)
        read_only = (method == "GET") if read is None else read
        headers = {"authorization": "Api-Key " + self.key, "accept": "application/json",
                   "content-type": "application/json"}
        if method == "POST" and read_only:
            headers["x-http-method-override"] = "GET"
        url = path if path.startswith("http") else self.base + path
        for attempt in range(tries):
            if not interactive:
                with self._lock:
                    wait = self.gap_s - (time.time() - self._last)
                    if wait > 0:
                        time.sleep(wait)
                    self._last = time.time()
            self.calls += 1
            try:
                code, text, hdrs = self.send(method, url, headers, body)
            except OSError as exc:
                if read_only and attempt < tries - 1:
                    time.sleep(1.5 + attempt * 2)
                    continue
                raise SiftError(0, "network: %s" % exc, path) from exc
            if code == 429 and attempt < tries - 1:
                m = _HINT.search(text or "")
                ra = (hdrs or {}).get("Retry-After") or (hdrs or {}).get("retry-after")
                wait = int(m.group(1)) + 1 if m else (int(ra) if str(ra or "").isdigit() else 2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            if code in (502, 503, 504) and read_only and attempt < tries - 1:
                time.sleep(2 + attempt * 3)
                continue
            if code >= 400:
                raise SiftError(code, text, path)
            if code == 204 or not (text or "").strip():
                return {}
            try:
                return json.loads(text)
            except ValueError:
                return {"raw": text[:500]}
        raise SiftError(0, "gave up", path)

    # ------------------------------------------------------------------ reads
    def record(self, uuid: str, fresh: bool = False, interactive: bool = True) -> dict:
        hit = self._cache.get(uuid)
        if hit and not fresh and time.time() - hit[0] < 45:
            return hit[1]
        r = self.call("/api/internal/property/%s/" % uuid, interactive=interactive) or {}
        if isinstance(r, dict) and r.get("uuid"):
            self._cache[uuid] = (time.time(), r)
        return r if isinstance(r, dict) else {}

    def forget(self, uuid: str) -> None:
        self._cache.pop(uuid, None)

    def search(self, must: dict, limit: int = PAGE, offset: int = 0, interactive: bool = False) -> tuple[int, list]:
        r = self.call("/api/internal/property/", "POST", {"limit": limit, "offset": offset, "query": {"must": must}},
                      read=True, interactive=interactive)
        count = r.get("count") if isinstance(r, dict) else None
        return int(count or 0), rows_of(r)

    def find_by_phone(self, number: str, limit: int = 10) -> list[dict]:
        """Every record whose owner carries this number, each one confirmed against the owner's own phone
        list (the search is fuzzy). Skip trace often puts one number on several owners."""
        d = digits(number)
        if not d:
            return []
        _, rows = self.search({"search": d}, limit=limit, interactive=True)
        out = []
        for row in rows:
            uid = row.get("uuid")
            if not uid:
                continue
            rec = self.record(uid)
            for p in ((rec.get("owner") or {}).get("phones") or []):
                if digits(p.get("number")) == d:
                    out.append({"record": rec, "row": row, "phone": p})
                    break
        return out

    def presets(self, fresh: bool = False) -> dict:
        """title -> {uuid, folder} for every saved records preset (10 minutes cached)."""
        if self._presets and not fresh and time.time() - self._presets[0] < 600:
            return self._presets[1]
        out = {}
        for f in rows_of(self.call("/api/internal/filter-preset-folder/?type=properties&limit=999", interactive=True)):
            for p in rows_of(self.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % f["uuid"],
                                       interactive=True)):
                if p.get("title"):
                    out[p["title"]] = {"uuid": p["uuid"], "folder": f.get("title") or "",
                                       "must": (p.get("filters") or {}).get("must")}
        self._presets = (time.time(), out)
        return out

    def preset_must(self, title: str) -> tuple[dict, list[str]]:
        """A preset's filter in the shape the records search takes, with its relative date windows made
        into dates (the search refuses "36-months" style tokens), and what could not be converted."""
        p = self.presets().get(title)
        if p is None:
            raise KeyError("no preset titled %r in this account" % title)
        must = p.get("must")
        if must is None:
            d = self.call("/api/internal/filter-preset/%s/" % p["uuid"], interactive=True) or {}
            must = (d.get("filters") or {}).get("must") or {}
        return search_shape(must)

    # ------------------------------------------------------------------ writes
    def upsert_phone(self, owner_uuid: str, payload: dict) -> Any:
        return self.call("/api/internal/owner/%s/upsert-phones/" % owner_uuid, "POST", {"phones": [payload]}, read=False)

    def call_attempts(self, uuid: str, total: int) -> Any:
        return self.call("/api/internal/property/%s/predictivecall-attempts/" % uuid, "POST",
                         {"predictivecall_attempts": int(total)}, read=False)

    def message(self, uuid: str, text: str) -> str:
        r = self.call("/api/internal/property/%s/message/" % uuid, "POST", {"message": text}, read=False)
        return (r or {}).get("uuid", "") if isinstance(r, dict) else ""

    def set_status(self, uuid: str, status: str) -> Any:
        return self.call("/api/internal/property/%s/status/" % uuid, "POST", {"status": status}, read=False)

    def add_tag(self, uuid: str, title: str) -> Any:
        return self.call("/api/internal/property/%s/add-tags/" % uuid, "POST", {"tags": title}, read=False)

    def add_list(self, uuid: str, title: str) -> Any:
        return self.call("/api/internal/property/%s/add-lists/" % uuid, "POST", {"lists": title}, read=False)

    def create_task(self, title: str, due_iso: str, notes: str = "", user_uuid: str = "", property_uuid: str = "") -> dict:
        body: dict = {"title": " ".join(title.split())[:256], "all_day": False, "event_type": "task", "due_date": due_iso}
        if notes:
            body["notes"] = notes[:4000]
        if user_uuid:
            body["assigned_to_users"] = [user_uuid]
        if property_uuid:
            body["assigned_to_property"] = property_uuid
        r = self.call("/api/internal/task/", "POST", body, read=False)
        return r if isinstance(r, dict) else {}


def search_shape(must: dict) -> tuple[dict, list[str]]:
    """A saved preset's `must` as the search wants it: ownerPropertiesOwned flattened, the app's own _ui key
    dropped, relative windows resolved to dates by the autopilot's converter (the same one every stage
    uses, so the dialer and the stages read a preset alike)."""
    import copy
    m = copy.deepcopy(must or {})
    opo = m.pop("ownerPropertiesOwned", None)
    if isinstance(opo, dict):
        m.update(opo)
    m.pop("_ui", None)
    try:
        from agents.autopilot import windows
        m, dropped = windows.resolve(m)
    except ImportError:                    # pragma: no cover - the image copies windows.py
        dropped = []
    return m, dropped


_CLIENT: list = []


def client() -> Sift:
    if not _CLIENT:
        _CLIENT.append(Sift())
    return _CLIENT[0]


def set_client(c: Optional[Sift]) -> None:
    _CLIENT.clear()
    if c is not None:
        _CLIENT.append(c)


# ---------------------------------------------------------------------- reading a record for a caller
def summary(rec: dict, row: dict | None = None) -> dict:
    """What a caller sees about the record on the phone page. Plain facts only, nothing the caller may
    not read on the record itself."""
    owner = rec.get("owner") or {}
    addr = rec.get("address") or {}
    mail = owner.get("address") or {}
    first = (owner.get("first_name") or "").strip()
    last = (owner.get("last_name") or "").strip()
    company = (owner.get("company") or "").strip()
    flagged = ""
    ph = (row or {}).get("phone") or {}
    if isinstance(ph, dict) and ph.get("doNotCall"):
        flagged = digits(ph.get("number"))
    phones = []
    for p in owner.get("phones") or []:
        n = digits(p.get("number"))
        if not n:
            continue
        tags = titles(p.get("tags"))
        phones.append({"number": n, "type": str(p.get("type") or "").upper(), "status": str(p.get("status") or "UNKNOWN").upper(),
                       "tier": next((t for t in tags if t in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")), ""),
                       "tags": [t for t in tags if t not in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")][:6],
                       "registry": n == flagged})
    status = rec.get("status")
    if isinstance(status, dict):
        status = status.get("value") or status.get("title") or ""
    return {
        "uuid": rec.get("uuid") or "",
        "owner_uuid": owner.get("uuid") or "",
        "owner": (" ".join(x for x in (first, last) if x) or company or "Unknown owner"),
        "company": company,
        "deceased": bool(owner.get("deceased")),
        "street": addr.get("street") or "",
        "city": addr.get("city") or "",
        "state": addr.get("state") or "",
        "zip": addr.get("postal_code") or "",
        "mail_state": mail.get("state") or "",
        "mail_street": mail.get("street") or "",
        "status": str(status or ""),
        "tags": titles(rec.get("tags"))[:40],
        "lists": titles(rec.get("lists"))[:20],
        "attempts": int(rec.get("predictivecall_attempts") or 0),
        "value": rec.get("estimate_value"),
        "equity": rec.get("equity_percent"),
        "phones": phones,
        "link": record_link(rec.get("uuid") or ""),
    }


def record_link(uuid: str) -> str:
    """The record's page in the app. The one definition lives in src/datasift_links.py: the app opens a
    property at /records/properties/<uuid>, and /records/<uuid> opens the records LIST instead."""
    try:
        from datasift_links import record_url
        return record_url(uuid)
    except ImportError:                    # pragma: no cover - the image copies datasift_links.py
        return "%s/records/properties/%s" % (config.S.app_base, uuid)
