"""Stdlib HTTP client for apiv2.reisift.io and map.reisift.io.

A community install reaches DataSift with its Open API key ("Api-Key <key>",
no expiry): Client.from_api_key(). The key comes from REISIFT_API_KEY, or from
the environment variable --key-env names. DataSift allows 120 requests a
minute per account, shared by every tool on it, so a key client paces itself
at about 80.

Client itself takes any Authorization header value and a `kind`, and has two
hooks (renew, claims) that a subclass may fill. This package's key path never
uses them.

Every non-2xx RAISES ApiError after the retry policy is exhausted. A listing
never comes back as {} on an error: the exists-check that reads "nothing
here" because a 429 was swallowed is the bug that re-creates lists, tags and
sequences, none of which have a unique-title constraint.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://apiv2.reisift.io"
MAP = "https://map.reisift.io"

HEADERS_STD = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://app.reisift.io",
    "referer": "https://app.reisift.io/",
    "x-reisift-ui-version": "2022.02.01.7",
}

_RETRY_HINT = re.compile(r"available in (\d+)\s*second", re.I)
KEY_INTERVAL_S = 0.75        # about 80 requests a minute, inside the account's 120


class ApiError(RuntimeError):
    def __init__(self, code, method, path, body=""):
        self.code, self.method, self.path, self.body = code, method, path, body
        super().__init__("HTTP %s on %s %s: %s" % (code, method, path, str(body)[:300]))

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, TypeError):
            return None


class NetworkError(RuntimeError):
    """Transport failed and we do not know whether the server committed."""


class Client:
    # A subclass that can refresh its own header sets these two. The key never needs to.
    renew_after_s = 0
    can_renew = False

    def __init__(self, auth_header: str, kind: str, *, min_interval: float = 0.35, sleep=time.sleep):
        self.kind = kind
        self._header = auth_header
        self.min_interval = min_interval
        self._sleep = sleep
        self._last = 0.0
        self._renewed = time.time()
        self.n_calls = 0

    # ------------------------------------------------------------ constructors
    @classmethod
    def from_api_key(cls, key: str) -> "Client":
        return cls("Api-Key " + key.strip(), "api_key", min_interval=KEY_INTERVAL_S)

    # ------------------------------------------------------------------ hooks
    def claims(self) -> dict:
        """What the credential says about itself. An Open API key says nothing, so the
        target gate reads the key's own user from the server instead (apply.verify_target)."""
        return {}

    def renew(self) -> None:
        """Refresh the Authorization header. Nothing to do for an Open API key."""

    def _auth_header(self) -> str:
        return self._header

    # ------------------------------------------------------------------- calls
    def _throttle(self) -> None:
        gap = self.min_interval - (time.time() - self._last)
        if gap > 0:
            self._sleep(gap)
        self._last = time.time()

    def call(self, path: str, method: str = "GET", body=None, *, base: str = API,
             method_override: str | None = None, timeout: int = 90, _renewed=False):
        if self.renew_after_s and time.time() - self._renewed > self.renew_after_s:
            self.renew()
            self._renewed = time.time()
        data = json.dumps(body).encode() if body is not None else None
        url = path if path.startswith("http") else base + path
        for attempt in range(6):
            headers = dict(HEADERS_STD)
            headers["authorization"] = self._auth_header()
            if data is not None:
                headers["content-type"] = "application/json"
            if method_override:
                headers["x-http-method-override"] = method_override
            self._throttle()
            self.n_calls += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 5:
                    m = _RETRY_HINT.search(txt)
                    wait = min((int(m.group(1)) + 1) if m else 5 * (attempt + 1), 300)
                    self._sleep(wait)
                    continue
                if e.code in (500, 502, 503, 504) and attempt < 3:
                    self._sleep(2 ** attempt)
                    continue
                if e.code == 401 and not _renewed and self.can_renew:
                    self.renew()
                    self._renewed = time.time()
                    return self.call(path, method, body, base=base,
                                     method_override=method_override,
                                     timeout=timeout, _renewed=True)
                if e.code == 401 and self.kind == "api_key":
                    raise ApiError(401, method, path, "the Open API key was refused: check that it is "
                                   "the target account's key (%s)" % txt[:120])
                raise ApiError(e.code, method, path, txt)
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                # A retried POST can double-create anything without a unique
                # title constraint, so the caller decides (re-list first).
                if method == "GET" and attempt < 3:
                    self._sleep(3 * (attempt + 1))
                    continue
                raise NetworkError("%s %s: %s" % (method, path, e))
        raise ApiError(429, method, path, "gave up after 6 attempts")

    def get_all(self, path: str, *, base: str = API) -> list:
        """Every row of a listing: {results}|{data}|flat list, following `next`.

        map.reisift.io pages at 10 and ignores ?limit=, so `next` is the only
        honest way to know a listing is complete.
        """
        out, url, guard = [], path, 0
        while url and guard < 500:
            guard += 1
            r = self.call(url, base=base)
            if isinstance(r, list):
                return out + r
            if not isinstance(r, dict):
                raise ApiError(0, "GET", path, "unexpected listing shape: %r" % (r,)[:120])
            rows = r.get("results")
            if rows is None:
                rows = r.get("data")
            if rows is None:
                raise ApiError(0, "GET", path, "listing has neither results nor data: %s"
                               % sorted(r)[:10])
            out.extend(rows)
            nxt = r.get("next")
            if nxt and isinstance(nxt, str):
                url = nxt
            else:
                break
        return out

    def search_count(self, must: dict) -> int:
        """Record count for a preset-style `must`. A GET with the query in the URL, so a
        count can never create a record (a bare POST to /api/internal/property/ would)."""
        q = urllib.parse.urlencode({"query": json.dumps({"must": must}), "limit": 1})
        r = self.call("/api/internal/property/?" + q)
        return int(r.get("count") or 0) if isinstance(r, dict) else 0
