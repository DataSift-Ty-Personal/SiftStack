"""In-process impersonation session for acting on a DataSift account.

Built for the ty+1 staging build-out: the only live credential is the ty+2
staff JWT in the Deal Room _api auth store, and ty+1's own token is expired.
This module reads that staff token (READ-ONLY, it never writes the shared
config, so it cannot race the ty+2 cron jobs the pin_impersonation hack
exists for), impersonates the target email, and holds the resulting token in
process memory only.

The part that matters: verify_target() is a hard gate that every write phase
runs before touching anything. active_account in the shared store points at
PRODUCTION ty+2, so nothing here ever falls back to it.

Duck-compatible with siftmap_pull.Map (needs .token and ._mint()) and
siftmap_pull.usage (needs .call()).

Env overrides:
    REISIFT_STAFF_JWT    use this staff token instead of reading the store
    REISIFT_TARGET_JWT   skip impersonation, use this token as the target
                         (fresh ty+1 paste); verify_target() still gates it
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

API = "https://apiv2.reisift.io"
STAFF_CONFIG = (r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call"
                r"\_api\clients\config\reisift_auth.json")
STAFF_ACCOUNT = "ty2-direct"
DEFAULT_TARGET = "ty+1@dataflik.com"

HEADERS_STD = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://app.reisift.io",
    "referer": "https://app.reisift.io/",
    "x-reisift-ui-version": "2022.02.01.7",
}


class SessionError(RuntimeError):
    pass


def decode_claims(jwt: str) -> dict:
    body = jwt.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


def load_staff_jwt(min_left_s: int = 2 * 3600) -> str:
    tok = os.environ.get("REISIFT_STAFF_JWT", "").strip()
    if not tok:
        with open(STAFF_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        acct = (cfg.get("accounts") or {}).get(STAFF_ACCOUNT) or {}
        tok = acct.get("access_token", "")
        if not tok:
            raise SessionError("no access_token for %r in %s"
                               % (STAFF_ACCOUNT, STAFF_CONFIG))
    claims = decode_claims(tok)
    left = int(claims["exp"]) - time.time()
    if left < min_left_s:
        raise SessionError(
            "staff token (%s) has %.0f min left (< %.0f min). Refresh the "
            "ty2-direct cURL paste or set REISIFT_STAFF_JWT."
            % (claims.get("email"), left / 60, min_left_s / 60))
    if "staff" not in (claims.get("feature_flags") or []):
        raise SessionError("token for %s has no staff flag; cannot impersonate"
                           % claims.get("email"))
    return tok


def api_call(token: str, path: str, *, method: str = "GET", body=None,
             method_override: str | None = None, base: str = API,
             timeout: int = 120):
    """One apiv2 request with 429 backoff. Raises SessionError with body."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        headers = dict(HEADERS_STD)
        headers["authorization"] = "Bearer " + token
        if data is not None:
            headers["content-type"] = "application/json"
        if method_override:
            headers["x-http-method-override"] = method_override
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                t = r.read().decode()
                return json.loads(t) if t.strip().startswith(("{", "[")) else t
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                # /api/internal throttles hard; honor a linear backoff.
                time.sleep(3 * (attempt + 1))
                continue
            raise SessionError("HTTP %s on %s %s: %s" % (
                e.code, method, path, e.read().decode(errors="replace")[:300]))
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            # Transient network blip (TLS reset, timeout). Retrying a POST
            # here is safe for this API surface: creates are guarded by
            # unique-title constraints and the callers upsert by title.
            if attempt < 5:
                time.sleep(3 * (attempt + 1))
                continue
            raise SessionError("network error on %s %s: %s"
                               % (method, path, e))
    raise SessionError("gave up (429) on " + path)


class Session:
    """Impersonated session against one target account."""

    def __init__(self, target_email: str = DEFAULT_TARGET):
        self.target_email = target_email
        self.override = os.environ.get("REISIFT_TARGET_JWT", "").strip()
        self.staff_jwt = ""
        if not self.override and not self._config_target_token():
            # Only the impersonation path needs the staff token at all.
            self.staff_jwt = load_staff_jwt()
        self.token = ""
        self._mint()

    def _config_target_token(self) -> str:
        """A directly-pasted token for the target account in the _api store
        (account 'datasift-admin' for ty+1). Preferred over impersonation:
        staff-to-staff impersonation is refused (403, verified 2026-08-19).
        """
        try:
            with open(STAFF_CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
        except OSError:
            return ""
        for acct in (cfg.get("accounts") or {}).values():
            tok = acct.get("access_token") or ""
            if not tok:
                continue
            try:
                claims = decode_claims(tok)
            except Exception:
                continue
            if ((claims.get("email") or "").lower() == self.target_email.lower()
                    and int(claims.get("exp", 0)) > time.time() + 600):
                return tok
        return ""

    def _mint(self) -> None:
        if self.override:
            self.token = self.override
            return
        tok = self._config_target_token()
        if tok:
            self.token = tok
            return
        if not self.staff_jwt:
            raise SessionError(
                "no valid credential for %s: paste a fresh JWT "
                "(reisift_auth.py add datasift-admin <jwt>) or set "
                "REISIFT_TARGET_JWT" % self.target_email)
        req = urllib.request.Request(
            "%s/api/internal/impersonate/%s/" % (API, self.target_email),
            method="POST", data=b"",
            headers=dict(HEADERS_STD, **{
                "authorization": "Bearer " + self.staff_jwt,
                "content-type": "application/x-www-form-urlencoded",
                "content-length": "0",
            }))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SessionError("impersonate %s returned %s: %s" % (
                self.target_email, e.code,
                e.read().decode(errors="replace")[:300]))
        tok = payload.get("access")
        if not tok:
            raise SessionError("impersonate response missing access token; "
                               "keys: %s" % list(payload))
        self.token = tok

    def call(self, path: str, *, method: str = "GET", body=None,
             method_override: str | None = None, timeout: int = 120):
        try:
            return api_call(self.token, path, method=method, body=body,
                            method_override=method_override, timeout=timeout)
        except SessionError as e:
            if "HTTP 401" in str(e):
                self._mint()
                return api_call(self.token, path, method=method, body=body,
                                method_override=method_override,
                                timeout=timeout)
            raise

    def verify_target(self) -> dict:
        """Hard gate: prove this token is the target account, then live-read.

        Runs before ANY write phase. SystemExit on mismatch so no caller can
        swallow it as a per-record error.
        """
        claims = decode_claims(self.token)
        email = (claims.get("email") or "").strip().lower()
        if email != self.target_email.lower():
            raise SystemExit("verify_target: token email is %r, expected %r "
                             "- REFUSING to write" % (email, self.target_email))
        if self.staff_jwt:
            staff = decode_claims(self.staff_jwt)
            if claims.get("account") == staff.get("account"):
                raise SystemExit("verify_target: impersonated token still "
                                 "carries the staff ACCOUNT uuid - REFUSING")
            if not claims.get("impersonated"):
                print("[session] WARN: token lacks the 'impersonated' flag")
        # Live read with this token proves the server accepts it for the
        # target account, not just that the claims parse.
        self.call("/api/internal/sequence/?limit=1")
        exp = int(claims["exp"])
        return {
            "email": claims.get("email"),
            "account": claims.get("account"),
            "impersonated": bool(claims.get("impersonated")),
            "exp_iso": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)),
            "feature_flags": claims.get("feature_flags") or [],
            "addons": claims.get("addons") or [],
        }
