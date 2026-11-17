"""Stdlib HTTP client for apiv2.reisift.io and map.reisift.io.

Auth kinds:
  jwt           a pasted access token (Bearer), no refresh
  api_key       an Open API key ("Api-Key <key>"), no expiry; export only,
                because it carries no claims to gate a write on
  password      minted from email + password via POST /api/token/, re-minted
                every 30 minutes and once on a 401 (datasift_api_upload.Api)
  impersonated  POST /api/internal/impersonate/{email}/ from a staff token,
                held in memory only (reisift_session._mint)

Every non-2xx RAISES ApiError after the retry policy is exhausted. A listing
never comes back as {} on an error: the exists-check that reads "nothing
here" because a 429 was swallowed is the bug that re-creates lists, tags and
sequences, none of which have a unique-title constraint.
"""
from __future__ import annotations

import base64
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
_REMINT_AFTER_S = 1800


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


def decode_claims(jwt: str) -> dict:
    body = jwt.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


class Client:
    def __init__(self, auth_value: str, kind: str, *, email: str = "",
                 password: str = "", staff: "Client | None" = None,
                 min_interval: float = 0.35, sleep=time.sleep):
        self.kind = kind
        self._auth = auth_value
        self.email, self._password, self._staff = email, password, staff
        self.min_interval = min_interval
        self._sleep = sleep
        self._last = 0.0
        self._minted = time.time()
        self.n_calls = 0

    # ------------------------------------------------------------ constructors
    @classmethod
    def from_jwt(cls, jwt: str) -> "Client":
        return cls(jwt.strip(), "jwt")

    @classmethod
    def from_api_key(cls, key: str) -> "Client":
        return cls(key.strip(), "api_key")

    @classmethod
    def from_password(cls, email: str, password: str) -> "Client":
        c = cls("", "password", email=email, password=password)
        c._remint()
        return c

    @classmethod
    def impersonate(cls, staff: "Client", email: str) -> "Client":
        c = cls("", "impersonated", email=email, staff=staff)
        c._remint()
        return c

    # ------------------------------------------------------------------ tokens
    @property
    def token(self) -> str:
        return self._auth

    def claims(self) -> dict:
        if self.kind == "api_key" or not self._auth:
            return {}
        try:
            return decode_claims(self._auth)
        except Exception:
            return {}

    def _auth_header(self) -> str:
        return ("Api-Key " if self.kind == "api_key" else "Bearer ") + self._auth

    def _remint(self) -> None:
        if self.kind == "password":
            body = json.dumps({"email": self.email, "password": self._password}).encode()
            req = urllib.request.Request(API + "/api/token/", data=body, method="POST",
                                         headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    self._auth = json.loads(r.read())["access"]
            except urllib.error.HTTPError as e:
                raise ApiError(e.code, "POST", "/api/token/",
                               e.read().decode(errors="replace"))
        elif self.kind == "impersonated":
            path = "/api/internal/impersonate/%s/" % self.email
            req = urllib.request.Request(
                API + path, method="POST", data=b"",
                headers=dict(HEADERS_STD, **{
                    "authorization": self._staff._auth_header(),
                    "content-type": "application/x-www-form-urlencoded",
                    "content-length": "0"}))
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    payload = json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise ApiError(e.code, "POST", path, e.read().decode(errors="replace"))
            tok = payload.get("access")
            if not tok:
                raise ApiError(0, "POST", path, "no access token in %s" % list(payload))
            self._auth = tok
        self._minted = time.time()

    # ------------------------------------------------------------------- calls
    def _throttle(self) -> None:
        gap = self.min_interval - (time.time() - self._last)
        if gap > 0:
            self._sleep(gap)
        self._last = time.time()

    def call(self, path: str, method: str = "GET", body=None, *, base: str = API,
             method_override: str | None = None, timeout: int = 90, _reminted=False):
        if self.kind in ("password",) and time.time() - self._minted > _REMINT_AFTER_S:
            self._remint()
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
                if e.code == 401 and not _reminted and self.kind in ("password", "impersonated"):
                    self._remint()
                    return self.call(path, method, body, base=base,
                                     method_override=method_override,
                                     timeout=timeout, _reminted=True)
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
        """Record count for a preset-style `must`. The ONLY caller of
        /api/internal/property/ in this package, and it hard-codes the
        override header because a bare POST there CREATES a record."""
        r = self.call("/api/internal/property/", "POST",
                      {"limit": 1, "offset": 0, "query": {"must": must}},
                      method_override="GET")
        return int(r.get("count") or 0) if isinstance(r, dict) else 0
