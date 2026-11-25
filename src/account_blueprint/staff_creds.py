"""Staff-only. DataSift staff credentials, kept out of the community path.

The community path is the Open API key (client.Client.from_api_key) and needs
nothing in this module. Everything here is for staff:

  * the Deal Room auth store (one workstation): the source key for export by
    account name, a staff JWT for the per-family export fallback, and a pasted
    target token for apply;
  * StaffClient: a pasted JWT, a token minted from email + password
    (POST /api/token/, re-minted every 30 minutes and once on a 401), or an
    impersonated token (POST /api/internal/impersonate/{email}/ from a staff
    JWT, held in memory only);
  * the staff flags on the command line, registered hidden so `--help` never
    shows them: --jwt, --jwt-file, --email, --password, --impersonate,
    --allow-staff-target, --source-account, plus REISIFT_TARGET_JWT;
  * the target gate for a token, which reads the token's own claims.

Staff usage:
    python src/clone_account.py --phase export                     # source key from the store
    python src/clone_account.py --phase plan  --blueprint <bp> --target you@x.com --jwt <paste>
    python src/clone_account.py --phase apply --blueprint <bp> --target client@x.com --impersonate --commit
    python src/clone_account.py --phase apply --blueprint <bp> --target you@x.com --email you@x.com --password ... --commit
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request

from .client import API, HEADERS_STD, ApiError, Client

STORE = os.environ.get(
    "REISIFT_AUTH_STORE",
    r"C:\Users\you\OneDrive\Desktop\Deal Room Coaching Call\_api\clients\config\reisift_auth.json")
API_KEY_ACCOUNT = "datasift-apikey"
STAFF_ACCOUNT = "account-direct"
REMINT_AFTER_S = 1800


def decode_claims(jwt: str) -> dict:
    body = jwt.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


class StaffClient(Client):
    """A Bearer-token client: kind "jwt" (pasted), "password" (minted) or "impersonated"."""

    def __init__(self, token: str, kind: str, *, email: str = "", password: str = "",
                 staff: "StaffClient | None" = None):
        super().__init__("Bearer " + token if token else "", kind)
        self.email, self._password, self._staff = email, password, staff
        if kind == "password":
            self.can_renew, self.renew_after_s = True, REMINT_AFTER_S
        elif kind == "impersonated":
            self.can_renew = True

    @property
    def token(self) -> str:
        return self._header[len("Bearer "):] if self._header.startswith("Bearer ") else ""

    def claims(self) -> dict:
        try:
            return decode_claims(self.token) if self.token else {}
        except Exception:
            return {}

    def renew(self) -> None:
        if self.kind == "password":
            body = json.dumps({"email": self.email, "password": self._password}).encode()
            req = urllib.request.Request(API + "/api/token/", data=body, method="POST",
                                         headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    self._header = "Bearer " + json.loads(r.read())["access"]
            except urllib.error.HTTPError as e:
                raise ApiError(e.code, "POST", "/api/token/", e.read().decode(errors="replace"))
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
            self._header = "Bearer " + tok


def token_client(token: str) -> StaffClient:
    return StaffClient(token.strip(), "jwt")


def login_client(email: str, password: str) -> StaffClient:
    c = StaffClient("", "password", email=email, password=password)
    c.renew()
    return c


def impersonated_client(staff: StaffClient, email: str) -> StaffClient:
    c = StaffClient("", "impersonated", email=email, staff=staff)
    c.renew()
    return c


# ---------------------------------------------------------------- the store

def _accounts() -> dict:
    with open(STORE, encoding="utf-8") as f:
        return (json.load(f).get("accounts") or {})


def api_key_client(name: str = API_KEY_ACCOUNT) -> tuple[Client, dict]:
    env = os.environ.get("REISIFT_API_KEY", "").strip()
    if env:
        return Client.from_api_key(env), {"email": "", "account_id": ""}
    acct = _accounts().get(name) or {}
    if not acct.get("api_key"):
        raise SystemExit("no api_key on account %r in %s (or set REISIFT_API_KEY)" % (name, STORE))
    return Client.from_api_key(acct["api_key"]), acct


def staff_client(name: str = STAFF_ACCOUNT, min_left_s: int = 600) -> StaffClient | None:
    tok = os.environ.get("REISIFT_STAFF_JWT", "").strip()
    if not tok:
        try:
            tok = (_accounts().get(name) or {}).get("access_token") or ""
        except OSError:
            return None
    if not tok:
        return None
    claims = decode_claims(tok)
    if int(claims.get("exp", 0)) - time.time() < min_left_s:
        return None
    if "staff" not in (claims.get("feature_flags") or []):
        return None
    return token_client(tok)


def stored_target_jwt(email: str) -> str:
    """A directly pasted, unexpired token for `email` in the store, or ""."""
    try:
        accts = _accounts()
    except OSError:
        return ""
    for acct in accts.values():
        tok = acct.get("access_token") or ""
        if not tok:
            continue
        try:
            claims = decode_claims(tok)
        except Exception:
            continue
        if ((claims.get("email") or "").lower() == email.lower()
                and int(claims.get("exp", 0)) > time.time() + 600):
            return tok
    return ""


# ---------------------------------------------------------- the command line

def add_staff_arguments(ap: argparse.ArgumentParser) -> None:
    """The staff flags, registered hidden: a community `--help` never shows them."""
    hide = argparse.SUPPRESS
    for flag in ("--jwt", "--jwt-file", "--email", "--password", "--source-account"):
        ap.add_argument(flag, default=None, help=hide)
    ap.add_argument("--impersonate", action="store_true", help=hide)
    ap.add_argument("--allow-staff-target", action="store_true", help=hide)


def staff_target_client(a) -> Client | None:
    """A target client from a staff flag or REISIFT_TARGET_JWT, or None when none was given."""
    jwt = (getattr(a, "jwt", None) or os.environ.get("REISIFT_TARGET_JWT", "")).strip()
    if getattr(a, "jwt_file", None):
        with open(a.jwt_file, encoding="utf-8") as f:
            jwt = f.read().strip()
    if jwt:
        return token_client(jwt)
    if getattr(a, "email", None) and getattr(a, "password", None):
        return login_client(a.email, a.password)
    if getattr(a, "impersonate", False):
        staff = staff_client()
        if staff is None:
            raise SystemExit("--impersonate needs a valid staff JWT (REISIFT_STAFF_JWT or the store)")
        return impersonated_client(staff, a.target)
    return None


def stored_target_client(target: str) -> Client | None:
    """The internal convenience: a pasted token for `target` in the store. None anywhere else."""
    tok = stored_target_jwt(target or "")
    if not tok:
        return None
    print("[cli] using the pasted token for %s from the Deal Room store" % target)
    return token_client(tok)


def staff_export_client(a) -> tuple[Client, dict] | None:
    """The export source from the store, by account name. None when there is no store."""
    try:
        acct = _accounts().get(getattr(a, "source_account", None) or API_KEY_ACCOUNT) or {}
    except OSError:
        return None
    if not acct.get("api_key"):
        return None
    return Client.from_api_key(acct["api_key"]), acct


def export_fallback() -> StaffClient | None:
    try:
        return staff_client()
    except Exception:
        return None


# ------------------------------------------------------------------ the gate

def verify_staff_target(client, bp: dict, target_email: str, opts, refuse, live_check) -> dict:
    """The target gate for a token: its own claims decide. `refuse` is apply.Refused (exit 3),
    `live_check` is apply's live read of the target account."""
    claims = client.claims()
    email = (claims.get("email") or "").strip().lower()
    if email != target_email.strip().lower():
        raise refuse("token email is %r, expected %r - REFUSING to write" % (email, target_email))
    account = str(claims.get("account") or "").lower()
    if not account:
        raise refuse("token has no account claim - REFUSING")
    src = str((bp.get("source") or {}).get("account_uuid") or "").lower()
    if account == src and not opts.allow_same_account:
        raise refuse("target account == blueprint source account %s. "
                     "A different email is not a different account; cloning an account onto "
                     "itself re-creates everything deleted since the export - REFUSING" % src[:8])
    left = int(claims.get("exp", 0)) - time.time()
    if left < opts.min_token_left_s:
        raise refuse("token has %.0f min left (< %d); paste a fresh one - REFUSING"
                     % (left / 60, opts.min_token_left_s // 60))
    flags = claims.get("feature_flags") or []
    if "staff" in flags and not opts.allow_staff_target:
        raise refuse("target token carries the staff flag (an internal account); "
                     "pass --allow-staff-target if that is intended - REFUSING")
    if client.kind == "impersonated":
        if not claims.get("impersonated"):
            raise refuse("impersonated token lacks the impersonated flag - REFUSING")
        staff_acct = str(client._staff.claims().get("account") or "").lower()
        if account == staff_acct:
            raise refuse("impersonated token still carries the staff account - REFUSING")
    elif claims.get("impersonated"):
        raise refuse("a pasted token carries the impersonated flag (stale state) - REFUSING")
    live_check(client, account, src, opts)
    return {"email": claims.get("email"), "account": account, "user_id": claims.get("user_id"),
            "impersonated": bool(claims.get("impersonated")), "kind": client.kind,
            "valid_until": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(claims["exp"]))),
            "feature_flags": flags, "addons": claims.get("addons") or []}
