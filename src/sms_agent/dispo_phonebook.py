"""Turn a finished dispo blast into a durable buyer phonebook.

A blast is worth more than the deal it sells. Blast 1 texted 156 buyers and
produced one contract, but it also produced something reusable: a set of people
who answered, whose numbers are now PROVEN good, and a set who should never be
texted again. Left in the message table that knowledge dies with the campaign.

What this does NOT do is re-derive what the agent already recorded. The engine
sets phone status CORRECT with a Dial First tag the moment someone engages, so
verified-number capture already happened. This groups those people, writes a
summary a caller can actually use, and suppresses the ones who asked out.

Buckets come from what people actually typed, never from a hand-kept list:

  optout        "Stop"                        already suppressed by the engine
  wrong         "wrong number"                suppress + mark the phone WRONG
  no            "not interested", "only do commercial"   suppress
  verified      asked for it, engaged         phonebook
  (silent)      no reply                      left alone, re-texted next blast

DRY by default. Nothing is written without --commit.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sms_agent import crm, store  # noqa: E402

OUT = Path(os.environ.get("SMS_AGENT_DISPO_DIR", "output/dispo_buyers"))
VERIFIED_LIST = "Dispo - Verified Buyers"

WRONG_WORDS = ("wrong number", "wrong recipient", "you have the wrong",
               "not my number", "wrong person")
NO_WORDS = ("not interested", "no thanks", "no thank you", "only do commercial",
            "delete this", "remove me", "take me off")
YES_WORDS = ("send", "yes", "sure", "shoot", "call me", "interested", "address",
             "keep me on your list", "info", "what's the", "how much", "price")


def _digits(p) -> str:
    return "".join(c for c in str(p or "") if c.isdigit())[-10:]


def _j(name):
    f = OUT / name
    return json.load(io.open(f, encoding="utf-8")) if f.exists() else None


def buckets(since: str, pool: str = "Dispo") -> dict:
    """Classify every phone we texted, from its own replies."""
    # SCOPE TO THIS PROGRAM'S NUMBERS. Both programs share one database, so
    # an unscoped pass classified 947 recipients including Adriana's seller
    # traffic, and committing it would have suppressed seller numbers as a
    # side effect of a dispo cleanup. The pools are disjoint, so the sending
    # number is the program.
    from sms_agent import config
    nums = set()
    for name, numbers in config.number_pools().items():
        if (name or '').strip().lower() == (pool or '').strip().lower():
            nums = set(numbers)
    if not nums:
        raise SystemExit(
        "number pool %r not found; refusing to classify every program at once" % pool)
    conn = store._conn()
    sent = {}
    for ph, frm in conn.execute(
            "SELECT phone, from_number FROM outbox WHERE status='sent'"):
        if frm in nums:
            sent[_digits(ph)] = frm
    replies = {}
    for ph, body in conn.execute(
            "SELECT phone, body FROM messages WHERE direction='in'"
            " AND created_at >= ?", (since,)):
        k = _digits(ph)
        if k in sent:
            replies.setdefault(k, []).append(body or "")

    out = {"verified": [], "optout": [], "wrong": [], "no": [], "silent": []}
    for k in sent:
        msgs = replies.get(k)
        if not msgs:
            out["silent"].append(k)
            continue
        joined = " ".join(msgs).lower()
        if store.is_suppressed(k) == "opt_out":
            out["optout"].append(k)
        elif any(w in joined for w in WRONG_WORDS):
            out["wrong"].append(k)
        elif any(w in joined for w in NO_WORDS):
            out["no"].append(k)
        elif any(w in joined for w in YES_WORDS):
            out["verified"].append(k)
        else:
            # Something we cannot read as yes or no. Treated as silent rather
            # than guessed either way: a wrong guess here either suppresses a
            # real buyer or keeps texting someone who asked us not to.
            out["silent"].append(k)
    return out


def summary_for(phone: str) -> str:
    """A buyer summary a caller can read before dialling."""
    reg = {b["buyer_key"]: b for b in (_j("registry.json") or [])}
    profs = {p["buyer_key"]: p for p in (_j("buyer_profiles.json") or [])}
    rec = store.lookup_phone(phone) or {}
    uuid = rec.get("record_uuid") or ""
    hit = None
    for key, b in reg.items():
        if b.get("saved_uuid") == uuid:
            hit = (key, b)
            break
    if not hit:
        return ""
    key, b = hit
    p = profs.get(key) or {}
    band = ""
    if b.get("price_min") and b.get("price_max"):
        band = "$%s to $%s" % (format(b["price_min"], ","),
                               format(b["price_max"], ","))
    elif b.get("price_min"):
        band = "$%s" % format(b["price_min"], ",")
    lines = [
        "VERIFIED DISPO BUYER (replied to a deal blast, number confirmed good)",
        "Buyer: %s" % (b.get("name") or "unknown"),
        "Type: %s" % (p.get("buyer_type") or "unknown"),
        "Purchases: %s (%s held, %s exited, %s cash)"
        % (b.get("n_buys"), b.get("n_held"), b.get("n_exited"), b.get("cash_n")),
        "Price band: %s" % (band or "not enough priced sales"),
        "Counties: %s" % ", ".join(b.get("counties") or []),
        "Portfolio: %s doors, $%s"
        % (b.get("portfolio_n") or "?",
           format(b.get("portfolio_value") or 0, ",")),
        "Last purchase: %s" % (b.get("last_active_buy") or "unknown"),
    ]
    if p.get("principal"):
        lines.append("Principal: %s (%s)"
                     % (p["principal"], p.get("principal_source") or "?"))
    if p.get("principal_is_agent"):
        lines.append("VERIFY: principal came back with an AGENT title; it may"
                     " be the company's lawyer, not the owner.")
    return "\n".join(lines)


def run(since: str, commit: bool) -> int:
    b = buckets(since)
    print("texted %d" % sum(len(v) for v in b.values()))
    for k in ("verified", "silent", "optout", "wrong", "no"):
        print("  %-9s %3d" % (k, len(b[k])))

    suppress = [(p, "opt_out") for p in b["optout"]]
    suppress += [(p, "wrong_number") for p in b["wrong"]]
    suppress += [(p, "not_interested") for p in b["no"]]
    print()
    print("would suppress %d (%d already suppressed)"
          % (len(suppress), sum(1 for p, _ in suppress if store.is_suppressed(p))))

    if not commit:
        print()
        print("DRY RUN. Add --commit to write.")
        return 0

    n_sup = n_wrong = n_note = n_tag = 0
    for phone, reason in suppress:
        if not store.is_suppressed(phone):
            store.suppress(phone, reason)
            n_sup += 1
    for phone in b["wrong"]:
        rec = store.lookup_phone(phone) or {}
        if rec.get("record_uuid"):
            crm.set_phone_status(rec["record_uuid"], phone, "WRONG")
            n_wrong += 1
    for phone in b["verified"]:
        rec = store.lookup_phone(phone) or {}
        uuid = rec.get("record_uuid")
        if not uuid:
            continue
        note = summary_for(phone)
        if note:
            crm.post_note(uuid, note, pinned=True)
            n_note += 1
        crm.add_tags(uuid, ["Dispo Verified Buyer"])
        n_tag += 1
    print("suppressed %d | marked WRONG %d | notes %d | tagged %d"
          % (n_sup, n_wrong, n_note, n_tag))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the dispo buyer phonebook")
    ap.add_argument("--since", default="2026-08-31T17:00",
                    help="only classify replies at or after this timestamp")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    return run(a.since, a.commit)


if __name__ == "__main__":
    raise SystemExit(main())
