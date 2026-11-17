"""Put every Instantly sending inbox on the course's settings, plus sender name and signature.

Dry by default: prints what would change. --commit backs up every inbox first, patches ONE,
reads it back, and only then does the rest. The sender name comes from .env
(COLD_EMAIL_SENDER_FIRST, COLD_EMAIL_SENDER_LAST) so it never sits in this public repo.
The signature is the name alone (Ty, 2026-11-16: no address in the signature). The postal
address a commercial email needs is a campaign footer decision, not an inbox setting.

    python src/cold_email/setup_accounts.py            # show the plan
    python src/cold_email/setup_accounts.py --commit   # apply it

Course settings (B2C Cold Email Lab, "Instantly Email Settings" lesson): 10 a day per inbox to
start (never above 30), 10 minutes between sends, slow ramp on, inbox placement tests 10, no
reply-to, warmup kept on with +1 a day, warmup volume matched to sending volume, 95% reply,
open, spam-save and mark-important rates, weekdays only, read emulation on.
The custom tracking domain Instantly created is left in place on purpose: campaigns run with
open and link tracking off and no links, so nothing ever uses it, and removing it has no
documented behavior.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from instantly import ROOT, Instantly  # noqa: E402

BACKUPS = ROOT / "output" / "cold_email" / "backups"
DAILY_START = 10


def env(name: str) -> str:
    import os
    v = os.environ.get(name, "").strip()
    if not v and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                v = line.split("=", 1)[1].strip()
    return v


def signature_html(first: str, last: str) -> str:
    return f"{first} {last}".strip()


def desired(daily: int) -> dict:
    first, last = env("COLD_EMAIL_SENDER_FIRST"), env("COLD_EMAIL_SENDER_LAST")
    if not first:
        raise SystemExit("set COLD_EMAIL_SENDER_FIRST in .env first")
    return {
        "first_name": first,
        "last_name": last,
        "signature": signature_html(first, last),
        "daily_limit": daily,
        "sending_gap": 10,
        "enable_slow_ramp": True,
        "inbox_placement_test_limit": 10,
        "reply_to": None,
        "warmup": {
            "limit": daily, "increment": "1", "reply_rate": 95,
            "advanced": {"weekday_only": True, "read_emulation": True, "warm_ctd": True,
                         "open_rate": 95, "spam_save_rate": 95, "important_rate": 95},
        },
    }


READABLE = ("first_name", "last_name", "signature", "daily_limit", "sending_gap",
            "enable_slow_ramp", "inbox_placement_test_limit", "reply_to")


def diff(acc: dict, want: dict) -> dict:
    return {k: (acc.get(k), want[k]) for k in READABLE if acc.get(k) != want[k]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--daily", type=int, default=DAILY_START, help="per-inbox daily limit (course: 10, cap 30)")
    a = ap.parse_args()
    if not 1 <= a.daily <= 30:
        raise SystemExit("the course caps an inbox at 30 a day")
    api = Instantly()
    want = desired(a.daily)
    accs = sorted(api.accounts(), key=lambda x: x["email"])
    print(f"{len(accs)} inboxes")
    changes = {x["email"]: diff(x, want) for x in accs}
    fields = sorted({k for d in changes.values() for k in d})
    print("fields that change:", ", ".join(fields) or "none (warmup is always sent; the API does not return it)")
    sample = next(iter(changes.values()), {})
    for k, (old, new) in sample.items():
        print(f"  {k}: {json.dumps(old)} -> {json.dumps(new)}")
    if not a.commit:
        print("dry run: nothing changed. Re-run with --commit.")
        return 0

    BACKUPS.mkdir(parents=True, exist_ok=True)
    full = [api.account(x["email"]) for x in accs]
    bk = BACKUPS / f"accounts_{time.strftime('%Y%m%d_%H%M%S')}.json"
    bk.write_text(json.dumps(full, indent=1), encoding="utf-8")
    print("backup:", bk.relative_to(ROOT))

    body = dict(want)
    first = accs[0]["email"]
    try:
        api.patch_account(first, body)
    except Exception as exc:  # the advanced warmup block is plan-gated on some workspaces
        print("full patch refused:", str(exc)[:300])
        body["warmup"] = {k: v for k, v in body["warmup"].items() if k != "advanced"}
        api.patch_account(first, body)
        print("applied without the advanced warmup settings")
    got = api.account(first)
    left = diff(got, want)
    if left:
        print("READ-BACK MISMATCH on", first, json.dumps(left))
        return 2
    print("verified on", first, "| warmup returned:", json.dumps(got.get("warmup")))

    bad = []
    for x in accs[1:]:
        try:
            api.patch_account(x["email"], body)
        except Exception as exc:
            bad.append((x["email"], str(exc)[:200]))
    after = {x["email"]: diff(x, want) for x in api.accounts()}
    off = {e: d for e, d in after.items() if d}
    print(f"patched {len(accs) - len(bad)} of {len(accs)}; still off after read-back: {len(off)}")
    for e, err in bad:
        print("  failed", e, err)
    for e, d in list(off.items())[:5]:
        print("  off", e, json.dumps(d))
    return 0 if not bad and not off else 2


if __name__ == "__main__":
    sys.exit(main())
