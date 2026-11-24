"""Who is on the phone page: a caller signed in by a one-time link, kept by a session cookie.

  * An admin makes the link on the command line (python -m dialer.cli link <identity>). The one-time code
    rides in the URL FRAGMENT (https://.../signin#<code>): a browser never sends a fragment to a server,
    so the code cannot land in an access log or a proxy's. The sign-in page posts it once and it is spent.
  * The session is a random id in an HttpOnly, Secure, SameSite=Lax cookie, looked up in the database on
    every request, so revoking a caller (cli revoke) ends every session at once.
  * Every request that changes something must carry the header X-Dialer: 1. A page on another site can
    send a cookie with a form post, but it cannot add a custom header without the browser asking this app
    first, and this app answers no CORS preflight. That is the whole cross-site defense, and it is enough.
"""
from __future__ import annotations

from typing import Optional

from . import config, store

COOKIE = "dlr_s"


def identity_of(cookie_value: str | None) -> Optional[str]:
    if not cookie_value:
        return None
    return store.web_session(cookie_value)


def signin(code: str) -> tuple[Optional[str], str]:
    """(identity, session id) for a one-time code, or (None, '')."""
    ident = store.use_code((code or "").strip())
    if not ident:
        return None, ""
    cal = store.get_caller(ident)
    if not cal or not cal["active"]:
        return None, ""
    return ident, store.new_web_session(ident, config.S.session_days)


def link(identity: str, days: int = 7) -> str:
    code = store.new_code(identity, days)
    base = config.S.public_url or "http://127.0.0.1:8090"
    return "%s/signin#%s" % (base, code)


def cookie_kwargs() -> dict:
    secure = config.S.public_url.startswith("https://")
    return dict(key=COOKIE, httponly=True, secure=secure, samesite="lax", max_age=config.S.session_days * 86400, path="/")
