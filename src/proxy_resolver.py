"""Resolve the egress proxy the notice scrape runs through.

WHY THIS EXISTS. tnpublicnotice.com decides per-IP whether it will serve notice
detail pages at all. Verified live 2026-08-14 against the same logged-in account:

  office IP          -> no CAPTCHA on the page, just "You are not permitted to
                        view public notices from this computer at this time."
  Apify datacenter   -> normal Cloudflare Turnstile gate, solvable, notice served
  Scrapfly residential -> notice served with no gate at all

So the scrape is gated on egress, not on code, and a cloud box (whose IP is a
datacenter range by definition) needs this configured before it will ever work.

Resolution order:
  1. SIFTSTACK_PROXY_URL : explicit, wins. Any http://user:pass@host:port.
  2. APIFY_PROXY_GROUPS  : build an Apify proxy URL, fetching the proxy
                            password from the Apify API using APIFY_TOKEN.
                            (The token is NOT the proxy password; it is the
                            credential used to look the password up.)
  3. None                : run direct, which works only from an allowed IP.

The resolved password is cached in-process; the lookup is one call per run.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

import config

logger = logging.getLogger(__name__)

APIFY_PROXY_HOST = "proxy.apify.com"
APIFY_PROXY_PORT = 8000

_apify_password: str | None = None


def _fetch_apify_proxy_password(token: str) -> str | None:
    """Look up the account's proxy password (distinct from the API token)."""
    global _apify_password
    if _apify_password is not None:
        return _apify_password or None
    try:
        import requests

        resp = requests.get(
            "https://api.apify.com/v2/users/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
        pw = ((resp.json().get("data") or {}).get("proxy") or {}).get("password") or ""
    except Exception as exc:
        logger.warning("Could not fetch Apify proxy password: %s", exc)
        _apify_password = ""
        return None
    _apify_password = pw
    if not pw:
        logger.warning("Apify account returned no proxy password")
        return None
    return pw


def _safe_session(name: str) -> str:
    """Make a session id Apify will accept.

    Apify allows only letters, digits, `_` and `.` in a session id. A HYPHEN
    makes the whole proxy request fail with 407 Proxy Authentication Required,
    which reads exactly like a bad password and sends you hunting through
    credentials instead of the one character that broke it. Verified live
    2026-08-14 from a Fly machine: `session-tnpn-fly` 407s while `tnpn_fly`,
    `tnpnfly` and `tnpn.fly` all return 200 on the same token and password.

    Silently rewriting is the right call here: the session id only has to be
    stable and unique per caller, so a substitution costs nothing, while a 407
    costs a whole scheduled run.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.]", "_", name or "")
    if cleaned != name:
        logger.warning(
            "Apify session %r is not a legal session id (letters, digits, _ and . "
            "only); using %r. A hyphen here returns 407 and looks like bad auth.",
            name, cleaned,
        )
    return cleaned or "default"


def resolve_proxy_url(session_suffix: str = "") -> str | None:
    """Return the proxy URL for this run, or None to run direct.

    session_suffix pins a sticky upstream IP per logical run. The site ties its
    ASP.NET session to the IP, so rotating mid-run drops the login.
    """
    if config.PROXY_URL:
        logger.info("Egress: SIFTSTACK_PROXY_URL")
        return config.PROXY_URL

    groups = config.APIFY_PROXY_GROUPS
    token = config.APIFY_TOKEN
    if groups and token:
        pw = _fetch_apify_proxy_password(token)
        if pw:
            session = _safe_session(config.APIFY_PROXY_SESSION + (session_suffix or ""))
            # Apify encodes options in the username field.
            username = f"groups-{groups},session-{session}"
            logger.info("Egress: Apify proxy (groups=%s session=%s)", groups, session)
            return (
                f"http://{quote(username, safe='-,')}:{quote(pw, safe='')}"
                f"@{APIFY_PROXY_HOST}:{APIFY_PROXY_PORT}"
            )

    logger.warning(
        "Egress: DIRECT (no proxy configured). tnpublicnotice.com blocks notice "
        "detail pages from most IPs, set SIFTSTACK_PROXY_URL or APIFY_TOKEN "
        "+ APIFY_PROXY_GROUPS if the run reports 'not permitted to view'."
    )
    return None


def describe(session_suffix: str = "") -> str:
    """One-line, credential-free summary for logs and the doctor command.

    Takes the same session_suffix as resolve_proxy_url so the logged session
    matches the one actually used. Without it this printed the BASE name while
    the run was on a rotated id, which is exactly the wrong thing to be wrong
    about when you are chasing which IP got blocked.
    """
    if config.PROXY_URL:
        safe = config.PROXY_URL
        if "@" in safe:
            safe = safe.split("@", 1)[1]
        return f"SIFTSTACK_PROXY_URL -> {safe}"
    if config.APIFY_PROXY_GROUPS and config.APIFY_TOKEN:
        return (
            f"Apify proxy groups={config.APIFY_PROXY_GROUPS} "
            f"session={_safe_session(config.APIFY_PROXY_SESSION + (session_suffix or ''))} "
            f"host={APIFY_PROXY_HOST}"
        )
    return "DIRECT (no proxy), expect 'not permitted to view' from most IPs"


if __name__ == "__main__":  # quick check: python src/proxy_resolver.py
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(describe())
    url = resolve_proxy_url()
    if url:
        host = url.split("@", 1)[1] if "@" in url else url
        print(f"resolved -> {host}")
        try:
            import requests

            r = requests.get(
                "https://api.ipify.org?format=json",
                proxies={"http": url, "https": url},
                timeout=45,
            )
            print(f"egress IP: {r.json().get('ip')}")
        except Exception as exc:
            print(f"egress check FAILED: {type(exc).__name__}: {exc}")
    else:
        print("resolved -> direct")
    _ = os.environ  # keep os imported for callers that patch env in tests
