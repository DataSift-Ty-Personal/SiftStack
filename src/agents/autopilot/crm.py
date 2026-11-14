"""DataSift for the autopilot, over the Open API key.

Every read and write the engine makes goes through this class, so the selftest swaps in a fake and
the writes have exactly one place to be guarded. Writes use the per-record action routes the
published reference documents (developers.datasift.ai, DataSift API 2.0, read 2026-11-11):

  * assign      POST /property/{uuid}/assign/        {"user_uuid": ...}
  * status      POST /property/{uuid}/status/        {"status": ...}
  * tags        POST /property/{uuid}/add-tags/ and remove-tags/   {"tags": "<one title>"}, one per
                call: a comma string would create one tag with the commas in its name
  * notes       POST /property/{uuid}/add-notes/     {"notes": ...}
  * phones      POST /owner/{uuid}/upsert-phones/    {"phones": [full phone objects]}
  * skip trace  POST /property/skip-trace/           {"query": ..., "dry_run" | "expected_count"}

Reads: the filtered records search is POST /api/internal/property/ with x-http-method-override:
GET and a {"query": {"must": ...}} body, the shape the reference names as "the query parameter of
the list endpoint" (a bare POST would CREATE a record); paging hard-stops at offset + limit 10,000.
A saved preset nests ownerPropertiesOwned.show_properties, which the search wants flat, and its
relative date windows are converted by windows.resolve.

Rate limits (published 2026-09-15, enforced per account across EVERY key-based tool on it):
120 requests a minute and 5,000 an hour. This app keeps to its own share of that (budget.py),
counted across every process it runs, and a 429's "available in N seconds" (or Retry-After) is
honored.
"""
from __future__ import annotations

import copy
import logging
import os
import re
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

PAGE = 250
MAX_WINDOW = 10000
_HINT = re.compile(r"available in ([0-9]+)", re.I)
# The preset index (folder listings) and each preset's filter are cached for this long across stage
# runs in one process: a stage that fires every 30 minutes should not re-read 98 presets each time.
# An edit in DataSift takes effect within this window.
PRESET_TTL_S = float(os.environ.get("AUTOPILOT_PRESET_TTL_S") or 600)
_PRESET_CACHE: dict = {"at": 0.0, "index": None, "musts": {}}


def rows_of(r) -> list:
    if isinstance(r, dict):
        return r.get("results") or r.get("data") or []
    return r or []


def titles(items) -> list[str]:
    out = []
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def search_shape(must: dict) -> dict:
    """A saved preset's `must` in the shape the records search accepts."""
    m = copy.deepcopy(must or {})
    opo = m.pop("ownerPropertiesOwned", None)
    if isinstance(opo, dict):
        m.update(opo)
    m.pop("_ui", None)
    return m


# Pacing. The account-wide limit is shared with the first-to-market box and the texting app, so
# this app paces to its own share (budget.py: 45 a minute by day, more at night) and counts every
# request against a budget all of its processes share. AUTOPILOT_API_GAP_S pins a slower pace.
_GAP_ENV = os.environ.get("AUTOPILOT_API_GAP_S")
SUSTAINED_GAP_S = float(_GAP_ENV) if _GAP_ENV else None


class CRM:
    def __init__(self, api=None, min_gap: float | None = SUSTAINED_GAP_S, priority: bool = False):
        if api is None:
            # The Open API key first, always. The shared uploader client logs in with a password
            # when the box carries one (this workstation's .env does), which made every local run
            # a different client from the one on Fly: a stage proven here on a login token was not
            # proven on the key. A user's install has only the key, so that is what gets tested.
            from datasift_api_upload import Api, env
            key = (env().get("REISIFT_API_KEY") or "").strip()
            api = Api(api_key=key) if key else Api()
        self.api = api
        self.min_gap = min_gap
        self.priority = priority      # reply dispositions and speed to lead: may use the whole minute
        self._last = 0.0
        self._presets: dict | None = None
        self._tags: dict | None = None
        self._users: list | None = None
        self.calls = 0

    # ---------------------------------------------------------------- transport
    def call(self, path: str, method: str = "GET", body=None, headers=None, _tries: int = 6):
        from datasift_api_upload import ApiError
        from . import budget
        for attempt in range(_tries):
            budget.acquire(priority=self.priority)
            min_gap = self.min_gap if self.min_gap is not None else budget.gap()
            gap = time.time() - self._last
            if gap < min_gap:
                time.sleep(min_gap - gap)
            self._last = time.time()
            self.calls += 1
            try:
                return self.api.call(path, method, body, headers=headers)
            except ApiError as exc:
                if exc.code == 429 and attempt < _tries - 1:
                    m = _HINT.search(exc.body or "")
                    wait = min(int(m.group(1)) + 1 if m else 2 ** attempt, 90)
                    log.info("429 on %s, waiting %ss", path, wait)
                    time.sleep(wait)
                    continue
                if exc.code in (502, 503, 504) and attempt < 2:
                    time.sleep(3 + attempt * 5)
                    continue
                raise
            except OSError as exc:
                # A dropped connection or a timeout (seen live 2026-11-13: one reset ended a 20,000-row
                # lane scan). A READ is safe to ask again; a write is not, because the server may have
                # applied it before the connection died, so a write surfaces the error instead.
                read_only = method == "GET" or (headers or {}).get("x-http-method-override") == "GET"
                if read_only and attempt < 3:
                    log.info("network error on %s (%s), trying again", path.split("?")[0], str(exc)[:80])
                    time.sleep(2 + attempt * 4)
                    continue
                raise
        raise RuntimeError("unreachable")

    # ---------------------------------------------------------------- presets
    def presets(self) -> dict:
        """title -> {"uuid", "folder"} for every saved records preset (folder listings only; the
        filter itself is read on demand by preset_must). Folder listings default to 10 rows, so
        every listing passes limit=999."""
        c = _PRESET_CACHE
        if self._presets is None and c["index"] is not None and time.time() - c["at"] < PRESET_TTL_S:
            self._presets = c["index"]
        if self._presets is None:
            out = {}
            folders = rows_of(self.call("/api/internal/filter-preset-folder/?type=properties&limit=999"))
            for f in folders:
                for p in rows_of(self.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % f["uuid"])):
                    out[p["title"]] = {"uuid": p["uuid"], "folder": f.get("title") or "",
                                       "must": (p.get("filters") or {}).get("must")}
            self._presets = out
            c.update(at=time.time(), index=out, musts={})
        return self._presets

    def preset_must(self, title: str) -> dict:
        p = self.presets().get(title)
        if p is None:
            raise KeyError("no preset titled %r in this account" % title)
        must = p.get("must")
        if must is None:
            must = _PRESET_CACHE["musts"].get(p["uuid"])
        if must is None:
            d = self.call("/api/internal/filter-preset/%s/" % p["uuid"]) or {}
            must = (d.get("filters") or {}).get("must") or {}
            _PRESET_CACHE["musts"][p["uuid"]] = must
        return search_shape(must)

    # ---------------------------------------------------------------- reads
    def search(self, must: dict, limit: int = PAGE, offset: int = 0) -> tuple[int, list]:
        r = self.call("/api/internal/property/", "POST", {"limit": limit, "offset": offset, "query": {"must": must}},
                      headers={"x-http-method-override": "GET"})
        return (r.get("count") if isinstance(r, dict) else None) or 0, rows_of(r)

    def count(self, must: dict) -> int:
        return self.search(must, limit=1)[0]

    def rows(self, must: dict, cap: int = MAX_WINDOW) -> list:
        out, off = [], 0
        cap = min(cap, MAX_WINDOW)
        while off < cap:
            n = min(PAGE, cap - off)
            _, rows = self.search(must, limit=n, offset=off)
            out += rows
            if len(rows) < n:
                break
            off += n
        return out

    def record(self, uuid: str) -> dict:
        return self.call("/api/internal/property/%s/" % uuid) or {}

    def owner(self, owner_uuid: str) -> dict:
        return self.call("/api/internal/owner/%s/" % owner_uuid) or {}

    def owner_records(self, owner_uuid: str, cap: int = 250) -> list:
        """Every record this owner holds (search rows). `owners` is the one filter key the records
        search accepts for it, proven by count on 2026-11-12: an owner with 5 properties returns 5,
        and every other spelling is dropped as an unknown key."""
        return self.rows({"owners": [owner_uuid]}, cap=cap)

    def custom_fields(self, uuid: str) -> dict:
        """label -> value for the record's custom fields that hold a value."""
        r = self.call("/api/internal/property/%s/custom-field/" % uuid)
        items = r if isinstance(r, list) else rows_of(r)
        out = {}
        for it in items:
            cf = it.get("custom_field") if isinstance(it.get("custom_field"), dict) else {}
            label = (cf.get("label") or cf.get("name") or cf.get("title") or "").strip()
            if label and it.get("value") not in (None, ""):
                out[label] = it["value"]
        return out

    # ---------------------------------------------------------------- activity log
    def logs(self, must: dict, limit: int = 100, cursor: str = "", include_owner: bool = False) -> dict:
        """One page of the ACCOUNT's activity log, newest first (GET /property/logs/, documented).
        `must` takes timestamp [from, to] (either may be null), source, and any_event_type [..];
        any_event_type is the only event filter the route honors (event_type and event_types are
        silently ignored, proven 2026-11-12). Paging is by cursor, with no 10,000 row wall."""
        import json as _json
        import urllib.parse
        q = {"query": _json.dumps({"must": must}), "limit": str(max(1, min(int(limit), 100)))}
        if cursor:
            q["cursor"] = cursor
        if include_owner:
            q["include_owner"] = "true"
        return self.call("/api/internal/property/logs/?" + urllib.parse.urlencode(q)) or {}

    def record_logs(self, uuid: str, event_types: list[str] | None = None, limit: int = 250) -> list:
        """One record's own log, newest first. The log is per OWNER for owner events: calls and
        texts on the owner's other properties show up here too."""
        path = "/api/internal/property/%s/logs/?limit=%d&offset=0" % (uuid, limit)
        if event_types:
            path += "&any_event_type=" + ",".join(event_types)
        return rows_of(self.call(path))

    def users(self) -> list:
        if self._users is None:
            r = self.call("/api/internal/account/user/?offset=0&limit=999")
            self._users = r if isinstance(r, list) else rows_of(r)
        return self._users

    def tag_uuid(self, title: str) -> str:
        if self._tags is None:
            rows = rows_of(self.call("/api/internal/tag/?offset=0&limit=10000&ordering=title"))
            self._tags = {}
            for t in rows:
                self._tags.setdefault((t.get("title") or "").strip().lower(), t.get("uuid") or "")
        return self._tags.get((title or "").strip().lower(), "")

    def list_uuid(self, title: str) -> str:
        if getattr(self, "_lists", None) is None:
            rows = rows_of(self.call("/api/internal/list/?offset=0&limit=1000"))
            self._lists = {(r.get("title") or "").strip().lower(): r.get("uuid") or "" for r in rows}
        return self._lists.get((title or "").strip().lower(), "")

    def skiptrace_stats(self) -> dict:
        """Skip trace counters. NOT in the published reference: used only to refuse the account-wide
        trace on a metered plan."""
        return self.call("/api/internal/activity/skiptrace/stats/") or {}

    def custom_field_uuid(self, label: str) -> str:
        if getattr(self, "_fields", None) is None:
            rows = rows_of(self.call("/api/internal/custom-fields/?limit=999"))
            self._fields = {(r.get("label") or "").strip().lower(): r.get("uuid") or "" for r in rows}
        return self._fields.get((label or "").strip().lower(), "")

    def set_custom_field(self, uuid: str, field_uuid: str, value: str) -> None:
        self.call("/api/internal/property/%s/custom-field/update-values/" % uuid, "PATCH",
                  [{"field_uuid": field_uuid, "value": value}])

    # ---------------------------------------------------------------- writes (documented action routes)
    def assign(self, uuid: str, user_uuid: str) -> None:
        self.call("/api/internal/property/%s/assign/" % uuid, "POST", {"user_uuid": user_uuid})

    def add_tags(self, uuid: str, add: list[str], rec: dict | None = None) -> list[str]:
        have = titles((rec or self.record(uuid)).get("tags"))
        for t in add:
            if t and t not in have:
                self.call("/api/internal/property/%s/add-tags/" % uuid, "POST", {"tags": t})
                have.append(t)
        return have

    def remove_tags(self, uuid: str, drop: list[str]) -> None:
        for t in drop:
            self.call("/api/internal/property/%s/remove-tags/" % uuid, "POST", {"tags": t})

    def add_lists(self, uuid: str, add: list[str]) -> None:
        """One documented call per list. `lists` is ONE title as a string: an array answers 201 and
        attaches nothing (verified live 2026-11-11)."""
        for t in add:
            if t:
                self.call("/api/internal/property/%s/add-lists/" % uuid, "POST", {"lists": t})

    def remove_lists(self, uuid: str, drop: list[str]) -> None:
        """The same contract as add_lists: one title, as a string, per call."""
        for t in drop:
            if t:
                self.call("/api/internal/property/%s/remove-lists/" % uuid, "POST", {"lists": t})

    def set_status(self, uuid: str, status: str) -> None:
        self.call("/api/internal/property/%s/status/" % uuid, "POST", {"status": status})

    def upsert_phones(self, owner_uuid: str, phones: list[dict]) -> None:
        self.call("/api/internal/owner/%s/upsert-phones/" % owner_uuid, "POST", {"phones": phones})

    # The three counters below are documented routes. Each takes the new TOTAL, never a step.
    def call_attempts(self, uuid: str, total: int) -> None:
        """The record's call counter, which is what moves it from Ready to Call to Call Attempt 1."""
        self.call("/api/internal/property/%s/predictivecall-attempts/" % uuid, "POST",
                  {"predictivecall_attempts": int(total)})

    def sms_attempts(self, uuid: str, total: int) -> None:
        self.call("/api/internal/property/%s/sms-attempts/" % uuid, "POST", {"sms_attempts": int(total)})

    def skiptrace_attempts(self, owner_uuid: str, total: int) -> None:
        """The owner's skip trace counter. Numbers from an outside source land through upsert-phones,
        which does not count as a trace, so the record keeps reading 'never skip traced' and stays
        out of every Ready to Call preset until this is written (plan 0.10)."""
        self.call("/api/internal/owner/%s/skiptrace-attempts/" % owner_uuid, "POST",
                  {"skiptrace_attempts": int(total)})

    def message(self, uuid: str, text: str, pin: bool = False) -> str:
        """A provenance note on the record, through the documented add-notes route. (The property
        message board with pinning is a mirror the reference no longer lists; the documented board
        is the owner's, keyed by an owner id the record does not expose.)"""
        self.call("/api/internal/property/%s/add-notes/" % uuid, "POST", {"notes": text[:4000]})
        return ""

    def skiptrace_all(self) -> dict:
        """DataSift's own skip trace over the WHOLE account. The reference requires select_all for an
        empty query; this is never the default (src/datasift_skiptrace.py)."""
        return self.call("/api/internal/property/skip-trace/", "POST", {"query": {}, "select_all": True}) or {}

    def skiptrace_scoped(self, uuids: list[str], dry_run: bool, tags: list[str] | None = None,
                         force_refresh: bool = False) -> dict:
        """DataSift's own skip trace on exactly these records, in the documented shape: the ids go
        INSIDE query.must (a top-level `properties` is what the 2026-08-19 call sent, and it traced
        the whole account), `dry_run` counts without tracing, and the real call carries
        `expected_count` so the server itself refuses a selection of any other size."""
        body: dict = {"query": {"must": {"properties": list(uuids), "property_type": "clean"}}}
        if tags:
            body["tags"] = list(tags)
        if force_refresh:
            body["force_refresh"] = True
        if dry_run:
            body["dry_run"] = True
        else:
            body["expected_count"] = len(uuids)
        return self.call("/api/internal/property/skip-trace/", "POST", body) or {}

    # ---------------------------------------------------------------- direct mail
    def directmail_attempts(self, uuid: str, total: int) -> None:
        """Set the record's mail counter. The value is the new TOTAL, not a step (the app reads the
        count, adds one, and sends the result)."""
        self.call("/api/internal/property/%s/directmail-attempts/" % uuid, "POST", {"directmail_attempts": int(total)})

    def set_last_mailed(self, uuid: str, day: str) -> None:
        self.call("/api/internal/property/%s/" % uuid, "PATCH", {"last_direct_mailed": day})

    def mail_campaign(self, uuids: list[str], title: str, touches: list[dict], estimate: bool,
                      campaign_type: str = "single-touch") -> dict:
        """A SiftMail campaign (DataSift's own mail, printed by Open Letter Connect) on exactly these
        records. estimate=True returns a quote and sends nothing; the stage always quotes first and
        refuses to launch unless the quote covers exactly the records it asked for."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        body: dict = {"query": {"must": {"properties": list(uuids), "property_type": "clean"},
                                "ordering": ["-list_count"]},
                      "title": title, "campaign_type": campaign_type, "estimate": 1 if estimate else 0,
                      "start_date": end.isoformat().replace("+00:00", "Z"), "touches": touches}
        if not estimate:
            body["confirmation_date"] = now.isoformat().replace("+00:00", "Z")
        return self.call("/api/internal/property/direct-mail-campaign/", "POST", body) or {}

    def mail_pricing(self) -> list:
        return rows_of(self.call("/api/internal/accounting/direct-mail-pricing/"))

    def mail_service(self, path: str):
        """GET on mail.reisift.io (SiftMail templates, settings, campaigns) with this client's auth."""
        import json as _json
        import urllib.request
        api = self.api
        auth = ("Api-Key " + api.api_key) if getattr(api, "api_key", "") else ("Bearer " + getattr(api, "token", ""))
        req = urllib.request.Request("https://mail.reisift.io" + path, headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=45) as r:
            t = r.read().decode()
        return _json.loads(t) if t.strip().startswith(("{", "[")) else t
