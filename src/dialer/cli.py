"""The dialer's command line (run from src/, or with PYTHONPATH=src).

    python -m dialer.cli serve [--port 8090]
    python -m dialer.cli selftest
    python -m dialer.cli doctor
    python -m dialer.cli vendor                         # the pinned Twilio Voice SDK, hash checked
    python -m dialer.cli setup --public-url URL --numbers 8655550100[,...] [--commit] [--write-env]
    python -m dialer.cli add-caller casey Casey --number 8655550100 [--role manager] [--user <DataSift user uuid>]
    python -m dialer.cli set-number casey 8655550100
    python -m dialer.cli link casey                   # a one-time sign-in link, good for 7 days
    python -m dialer.cli revoke casey                 # signs them out everywhere
    python -m dialer.cli callers
    python -m dialer.cli calls [--today]
    python -m dialer.cli undo <call id> [--commit]      # put back what a call's outcome wrote to DataSift
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dialer import config  # noqa: E402

# The Voice JavaScript SDK the page loads, pinned and hash checked. 2.18.5 (2026-09-10) has two months in
# the field; 2.19.0 was a week old when this was set. The same bytes are published on npm and jsDelivr.
SDK_VERSION = "2.18.5"
SDK_URL = "https://cdn.jsdelivr.net/npm/@twilio/voice-sdk@%s/dist/twilio.min.js" % SDK_VERSION
SDK_SHA256 = "35d3cb1b22e309f9884724a89250aecd2de4f1556ad7b67a7bc5e06c73dcb74a"
VENDOR = Path(__file__).resolve().parent / "static" / "vendor" / "twilio.min.js"


def cmd_vendor(_a) -> int:
    import requests
    r = requests.get(SDK_URL, timeout=60)
    r.raise_for_status()
    got = hashlib.sha256(r.content).hexdigest()
    if got != SDK_SHA256:
        print("REFUSED: the downloaded SDK's sha256 is %s, not the pinned %s" % (got, SDK_SHA256))
        return 2
    VENDOR.parent.mkdir(parents=True, exist_ok=True)
    VENDOR.write_bytes(r.content)
    print("Voice SDK %s saved (%d bytes, sha256 matches)" % (SDK_VERSION, len(r.content)))
    return 0


def cmd_doctor(_a) -> int:
    from dialer import sift, store
    store.init()
    probs = config.problems()
    for p in probs:
        print("SETUP:", p)
    print("calls=%s writeback=%s record=%s amd=%s vm_drop=%s" % (config.S.calls, config.S.writeback, config.S.record,
                                                                 config.S.amd, config.S.vm_drop))
    print("callers:", ", ".join("%s (%s)" % (c["name"], c["identity"]) for c in store.callers()) or "none yet")
    if config.S.sift_key:
        try:
            n = len(sift.client().presets())
            print("DataSift: the key reads %d presets" % n)
        except Exception as exc:  # noqa: BLE001
            print("DataSift: FAILED (%s)" % str(exc)[:160])
            probs.append("datasift")
    if config.S.account_sid and config.S.auth_token:
        try:
            from twilio.rest import Client
            acct = Client(config.S.account_sid, config.S.auth_token).api.accounts(config.S.account_sid).fetch()
            print("Twilio: account %s is %s (%s)" % (acct.friendly_name, acct.status, acct.type))
        except Exception as exc:  # noqa: BLE001
            print("Twilio: FAILED (%s)" % str(exc)[:160])
            probs.append("twilio")
    print("Voice SDK:", "present" if VENDOR.exists() else "missing (python -m dialer.cli vendor)")
    return 1 if probs else 0


def cmd_setup(a) -> int:
    from twilio.rest import Client
    from dialer import setup_twilio
    if not (config.S.account_sid and config.S.auth_token):
        print("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN first (the Twilio console's front page shows both).")
        return 2
    client = Client(config.S.account_sid, config.S.auth_token)
    nums = [x for x in (a.numbers or "").split(",") if x.strip()]
    res = setup_twilio.plan(client, a.public_url, nums, commit=a.commit)
    print("\n".join(setup_twilio.describe(res)))
    env = res.get("env") or {}
    if a.commit and env:
        if a.write_env:
            added = setup_twilio.write_env(env)
            print("Added to .env:", ", ".join(added) or "nothing new")
        else:
            print("Set these (the key secret is shown only now):")
            for k, v in env.items():
                print("  %s=%s" % (k, v))
    return 0


def cmd_add_caller(a) -> int:
    from dialer import phone, store
    store.init()
    num = phone.digits(a.number or "")
    if a.number and not num:
        print("That number is not ten digits.")
        return 2
    c = store.add_caller(a.identity, a.name, role=a.role, caller_id=num, user_uuid=a.user or "")
    print("Saved %s (%s), %s, dials from %s" % (c["name"], c["identity"], c["role"], phone.pretty(c["caller_id"]) or "no number yet"))
    return 0


def cmd_set_number(a) -> int:
    from dialer import phone, store
    store.init()
    if not store.get_caller(a.identity):
        print("No caller %r" % a.identity)
        return 2
    store.update_caller(a.identity, caller_id=phone.digits(a.number))
    print("Done.")
    return 0


def cmd_link(a) -> int:
    from dialer import auth, store
    store.init()
    if not store.get_caller(a.identity):
        print("No caller %r (add-caller first)" % a.identity)
        return 2
    print(auth.link(a.identity, a.days))
    return 0


def cmd_revoke(a) -> int:
    from dialer import store
    store.init()
    print("Signed out of %d session(s)." % store.revoke_sessions(a.identity))
    return 0


def cmd_callers(_a) -> int:
    from dialer import phone, store
    store.init()
    for c in store.callers(active_only=False):
        print("%-12s %-12s %-8s %-16s vm=%s %s" % (c["identity"], c["name"], c["role"], phone.pretty(c["caller_id"]),
                                                  "yes" if c["vm_file"] else "no", "" if c["active"] else "(inactive)"))
    return 0


def cmd_calls(a) -> int:
    from dialer import phone, rules, store
    store.init()
    rows = store.recent_calls(limit=200, since=rules.day_start_utc() if a.today else "")
    for c in rows:
        print("%s %-9s %-9s %s %-10s %-14s %ss" % (c["created_at"][:16], c["identity"] or "", c["direction"], phone.pretty(c["number"]),
                                                  c["status"], c["disposition"] or "", c["seconds"] or 0))
    return 0


def cmd_undo(a) -> int:
    """Put back what one call's outcome wrote (the phone, the status, tags, lists, the counter). A note or a
    task is named for a person to delete: removing a note someone may already have read is their call."""
    from dialer import sift, store
    store.init()
    call = store.get_call(a.call_id)
    if not call:
        print("No call %s" % a.call_id)
        return 2
    s = sift.client()
    uuid = call.get("record_uuid") or ""
    steps = [w for w in store.writes_for(a.call_id) if w["ok"] and w["mode"] == "live"]
    if not steps:
        print("Nothing live was written for this call.")
        return 0
    for w in reversed(steps):
        before, after = json.loads(w["before"] or "null"), json.loads(w["after"] or "null")
        act = w["action"]
        line = "%s: %r -> back to %r" % (act, after, before)
        if not a.commit:
            print("would undo", line)
            continue
        try:
            if act == "status":
                s.call(("/api/internal/property/%s/status/" % uuid), "POST", {"status": before or None}, read=False)
            elif act.startswith("tag:"):
                s.call("/api/internal/property/%s/remove-tags/" % uuid, "POST", {"tags": act[4:]}, read=False)
            elif act.startswith("list:"):
                s.call("/api/internal/property/%s/remove-lists/" % uuid, "POST", {"lists": act[5:]}, read=False)
            elif act == "call_attempts":
                s.call_attempts(uuid, int(before or 0))
            elif act == "phone":
                rec = s.record(uuid, fresh=True, interactive=False)
                owner = rec.get("owner") or {}
                p = next((x for x in owner.get("phones") or [] if sift.digits(x.get("number")) == call["number"]), None)
                if p is None:
                    print("skip phone: the number is no longer on the record")
                    continue
                s.upsert_phone(owner["uuid"], sift.phone_payload(p, status=(before or {}).get("status") or "UNKNOWN",
                                                                  tags=(before or {}).get("tags") or []))
            elif act in ("note", "task"):
                print("left for a person: the %s (delete it in DataSift if it should go)" % act)
                continue
            print("undid", line)
            store.add_write(a.call_id, 0, w["target"], "undo:" + act, after, before, True, "live")
        except Exception as exc:  # noqa: BLE001
            print("FAILED to undo %s: %s" % (act, str(exc)[:160]))
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    uvicorn.run("dialer.app:app", host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_selftest(_a) -> int:
    from dialer import selftest
    return selftest.main([])


def main(argv: list[str] | None = None) -> int:
    config.load_dotenv()
    config.reload()
    ap = argparse.ArgumentParser(prog="python -m dialer.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    sub.add_parser("selftest")
    sub.add_parser("doctor")
    sub.add_parser("vendor")
    p = sub.add_parser("setup")
    p.add_argument("--public-url", required=True)
    p.add_argument("--numbers", default="")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--write-env", action="store_true")
    p = sub.add_parser("add-caller")
    p.add_argument("identity")
    p.add_argument("name")
    p.add_argument("--number", default="")
    p.add_argument("--role", default="caller", choices=("caller", "manager"))
    p.add_argument("--user", default="")
    p = sub.add_parser("set-number")
    p.add_argument("identity")
    p.add_argument("number")
    p = sub.add_parser("link")
    p.add_argument("identity")
    p.add_argument("--days", type=int, default=7)
    p = sub.add_parser("revoke")
    p.add_argument("identity")
    sub.add_parser("callers")
    p = sub.add_parser("calls")
    p.add_argument("--today", action="store_true")
    p = sub.add_parser("undo")
    p.add_argument("call_id")
    p.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    fn = {"serve": cmd_serve, "selftest": cmd_selftest, "doctor": cmd_doctor, "vendor": cmd_vendor, "setup": cmd_setup,
          "add-caller": cmd_add_caller, "set-number": cmd_set_number, "link": cmd_link, "revoke": cmd_revoke,
          "callers": cmd_callers, "calls": cmd_calls, "undo": cmd_undo}[a.cmd]
    return fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
