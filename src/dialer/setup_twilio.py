"""One-time Twilio setup for the dialer: the TwiML App, an API key, and the numbers' inbound route.

    python -m dialer.cli setup --public-url https://siftstack-dialer.fly.dev --numbers 8655550100,8655550101
    python -m dialer.cli setup ... --commit            # makes the changes
    python -m dialer.cli setup ... --commit --write-env    # and appends the new values to the repo .env

It needs TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN (the two values on the Twilio console's front page) and
nothing else. Dry by default: it prints what it would create or change. What it does:

  * the TwiML App "SiftStack dialer": its Voice URL is the app's /twilio/voice (the browser leg) and its
    status callback is /twilio/agent_status. An app with that name is updated in place, never duplicated.
  * an API key for the dialer's REST calls, only when TWILIO_API_KEY_SID is not already set. Twilio shows a
    key's secret ONCE, at creation, so it is printed (and written to .env with --write-env) right then.
  * each number named with --numbers gets /twilio/inbound as its Voice URL, so a homeowner who calls back
    reaches the caller who dialed them. Its current Voice URL is printed first: a number already routed
    somewhere (a smrtPhone or Kixie number ported in, a call flow) is left alone unless named.

Nothing here buys a number: buying is a purchase, and the console's Buy a Number page is one click.
"""
from __future__ import annotations

from . import config

APP_NAME = "SiftStack dialer"


def plan(client, public_url: str, numbers: list[str], commit: bool = False) -> dict:
    """Work out (and with commit, make) the changes. Returns what was found, done and still to do."""
    from . import phone
    base = public_url.rstrip("/")
    voice_url, status_url, inbound_url = base + "/twilio/voice", base + "/twilio/agent_status", base + "/twilio/inbound"
    out: dict = {"commit": commit, "app": {}, "key": {}, "numbers": [], "env": {}, "notes": []}
    if not base.startswith("https://"):
        out["notes"].append("the public URL must start with https://")
        return out

    apps = [a for a in client.applications.list(friendly_name=APP_NAME, limit=20)]
    if apps:
        a = apps[0]
        need = (a.voice_url != voice_url or (a.voice_method or "").upper() != "POST" or a.status_callback != status_url)
        out["app"] = {"sid": a.sid, "action": "update" if need else "keep"}
        if need and commit:
            client.applications(a.sid).update(voice_url=voice_url, voice_method="POST", status_callback=status_url,
                                              status_callback_method="POST")
        out["env"]["TWILIO_TWIML_APP_SID"] = a.sid
        if len(apps) > 1:
            out["notes"].append("%d TwiML Apps are named %r; the first was used" % (len(apps), APP_NAME))
    else:
        out["app"] = {"sid": "", "action": "create"}
        if commit:
            a = client.applications.create(friendly_name=APP_NAME, voice_url=voice_url, voice_method="POST",
                                           status_callback=status_url, status_callback_method="POST")
            out["app"]["sid"] = a.sid
            out["env"]["TWILIO_TWIML_APP_SID"] = a.sid

    if config.S.api_key_sid:
        out["key"] = {"sid": config.S.api_key_sid, "action": "keep"}
    else:
        out["key"] = {"sid": "", "action": "create"}
        if commit:
            k = client.new_keys.create(friendly_name=APP_NAME)
            out["key"]["sid"] = k.sid
            out["env"]["TWILIO_API_KEY_SID"] = k.sid
            out["env"]["TWILIO_API_KEY_SECRET"] = k.secret

    want = {phone.digits(n) for n in numbers if phone.digits(n)}
    have = {}
    for n in client.incoming_phone_numbers.list(limit=500):
        have[phone.digits(n.phone_number)] = n
    for d in sorted(want):
        n = have.get(d)
        if n is None:
            out["numbers"].append({"number": d, "action": "missing", "note": "not a number on this Twilio account"})
            continue
        current = n.voice_url or ("TwiML App %s" % n.voice_application_sid if getattr(n, "voice_application_sid", "") else "")
        if n.voice_url == inbound_url and not getattr(n, "voice_application_sid", ""):
            out["numbers"].append({"number": d, "action": "keep", "current": current})
            continue
        out["numbers"].append({"number": d, "action": "route", "current": current or "nothing"})
        if commit:
            client.incoming_phone_numbers(n.sid).update(voice_url=inbound_url, voice_method="POST", voice_application_sid="")
    others = sorted(set(have) - want)
    if others:
        out["notes"].append("%d other number(s) on the account were not touched: %s" % (
            len(others), ", ".join(phone.pretty(x) for x in others[:8])))
    out["env"]["DIALER_PUBLIC_URL"] = base
    return out


def write_env(values: dict) -> list[str]:
    """Append new values to the repo .env (never overwriting a line that is already there)."""
    path = config.ROOT / ".env"
    have = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    keys = {line.split("=", 1)[0].strip() for line in have.splitlines() if "=" in line and not line.lstrip().startswith("#")}
    added = []
    lines = []
    for k, v in values.items():
        if k in keys or not v:
            continue
        lines.append("%s=%s" % (k, v))
        added.append(k)
    if lines:
        with path.open("a", encoding="utf-8") as f:
            f.write(("\n" if have and not have.endswith("\n") else "") + "# SiftStack dialer (python -m dialer.cli setup)\n"
                    + "\n".join(lines) + "\n")
    return added


def describe(res: dict) -> list[str]:
    from . import phone
    mode = "Done" if res.get("commit") else "Would do (dry run; add --commit)"
    out = [mode + ":"]
    app = res.get("app") or {}
    out.append("  TwiML App %r: %s%s" % (APP_NAME, app.get("action", "?"), (" (%s)" % app["sid"]) if app.get("sid") else ""))
    key = res.get("key") or {}
    out.append("  API key: %s%s" % (key.get("action", "?"), (" (%s)" % key["sid"]) if key.get("sid") else ""))
    for n in res.get("numbers") or []:
        out.append("  %s: %s%s" % (phone.pretty(n["number"]), n["action"],
                                   (" (was %s)" % n["current"]) if n.get("current") else (" (%s)" % n["note"] if n.get("note") else "")))
    for note in res.get("notes") or []:
        out.append("  note: " + note)
    return out
