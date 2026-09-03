"""Stage the heir texting campaign for the FTM probate/obituary book.

The owner campaign texts the person on title. This one texts the RELATIVES we
resolved through SmartSkip, by their own first name, about a property somebody
in their family looks after.

Why it is a separate module rather than a flag on the owner campaign:

  * The QUESTION is different. Owner copy asks "is {addr} yours?". A son whose
    mother owned the house is not the owner and that question misreads his
    situation, so the heir pools ask who LOOKS AFTER the property instead.
  * The RECIPIENT is not the record's owner, so the name on the message and the
    name on the CRM record deliberately differ. Everything else about the
    record (street, city, county, assignee) still comes from the record.
  * We are often WRONG about the relationship. 64% of SmartSkip's labels are
    generic, so only SPECIFIC kinship (child/parent/sibling/spouse) is texted,
    and no message ever states the relationship back to them.

Nothing here bypasses a guard. Candidates are built by seed.build(), which
applies suppression, the dial-tier floor, live-conversation checks, the signed
sender rule and the human-voice validator; this module only substitutes the
heir wording afterwards and re-runs the voice check on the result.

    python src/sms_agent/heir_campaign.py --touch 1              # dry, counts only
    python src/sms_agent/heir_campaign.py --touch 1 --queue      # stage as HELD
    python src/sms_agent/cli.py release --touch 1                # the go/no-go
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sms_agent import respond, seed, store  # noqa: E402
from sms_agent.knowledge import touches  # noqa: E402

POOL = "output/dp/ftm_heir_sms_pool.json"

# Only kinship we can defend. In-Law and SmartSkip's bare "Relative" are
# excluded upstream when the pool is built: texting somebody about a house
# because an aggregator guessed they are an in-law is how a campaign earns a
# spam complaint from a person with no connection to the property.
SPECIFIC = {"Son", "Daughter", "Child", "Mother", "Father", "Parent",
            "Brother", "Sister", "Sibling", "Wife", "Husband", "Spouse"}


def rows_from_pool(limit: int = 0) -> list[dict]:
    pool = json.load(io.open(POOL, encoding="utf-8"))
    rows = []
    for p in pool:
        if p.get("rel") not in SPECIFIC:
            continue
        rows.append({
            "uuid": p["uuid"],
            "phone": p["number"],
            "first": p["first"],
            "last": "",
            "owner": p["first"],          # the message is addressed to the HEIR
            "street": p.get("street") or "",
            "city": p.get("city") or "",
            "county": p.get("county") or "",
            "_rel": p.get("rel"),
            "_tier": p.get("tier"),
        })
    # Best numbers first, because the daily cap cuts the tail.
    order = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3}
    rows.sort(key=lambda r: (order.get(r.get("_tier") or "", 9),
                             -(r.get("_score") or 0)))
    return rows[:limit] if limit else rows


def restage(cands, touch: int, rel_by_phone=None):
    """Swap owner wording for heir wording, then re-run the voice check.

    Re-validating is the point: the heir copy is not what seed.build vetted, so
    accepting it unchecked would put unvetted text in the outbox.
    """
    rel_by_phone = rel_by_phone or {}
    kept, rejected = [], []
    for c in cands:
        if c.status != "ready":
            continue
        c.message = touches.render(touch, c.record_uuid or c.phone, c.first,
                                   c.street, c.city, c.sender, variant="heir")
        c._rel = rel_by_phone.get(c.phone, "")  # type: ignore[attr-defined]
        problems = respond.voice_problems(c.message) if hasattr(respond, "voice_problems") \
            else _fallback_voice(c.message)
        if problems:
            c.hold("heir copy failed the human-voice check: " + "; ".join(problems))
            rejected.append(c)
        else:
            kept.append(c)
    return kept, rejected


def _fallback_voice(msg: str) -> list[str]:
    out = []
    if respond._BANNED.search(msg):
        out.append("ai tell: " + respond._BANNED.search(msg).group(0))
    if respond._SEMICOLON.search(msg):
        out.append("semicolon")
    if respond._FORM_LETTER.search(msg):
        out.append("form letter opener")
    if len(msg) > 320:
        out.append("too long")
    if msg.count("?") > 1:
        out.append("more than one question")
    return out


def notify_staged(kept, touch: int) -> None:
    """Tell Slack the heir campaign is going out (Ty, 2026-08-25).

    kind="campaign" is on escalate.ALWAYS_POST, so this lands in the channel
    even under SLACK_INTERESTED_ONLY. That is deliberate: this is the one
    notice that says a NEW audience started receiving texts, and if the copy
    or the targeting is wrong, this message is how somebody finds out in
    minutes rather than from a reply.
    """
    from collections import Counter
    from sms_agent import escalate

    rels = Counter(getattr(c, "_rel", "") or "?" for c in kept)
    by_rel = ", ".join("%s %d" % (k, v) for k, v in rels.most_common(6) if k != "?")
    sample = kept[0].message if kept else ""
    detail = "\n".join([
        "Touch %d to RELATIVES of the owner (not the owner)." % touch,
        "Queued: %d messages across %d records."
        % (len(kept), len({c.record_uuid for c in kept})),
        ("Relationships: %s" % by_rel) if by_rel else "",
        "Signed as Adriana, sending from her number pool.",
        "",
        "Example of what goes out:",
        sample,
        "",
        "Replies route to Adriana. Nothing in this copy mentions a death,"
        " an estate or probate, on purpose.",
    ])
    try:
        # CHECK THE RETURN. alert() reports failure with False rather than an
        # exception, so an ignored return prints "posted" for a notice that
        # never left the building, and the one signal that a new audience
        # started receiving texts goes missing silently.
        ok = escalate.alert("FTM heir texting campaign is sending",
                            detail, kind="campaign")
        print("  Slack: launch notice %s"
              % ("posted" if ok else "FAILED TO POST (sending continues)"))
    except Exception as exc:  # noqa: BLE001 - never block a send on Slack
        print("  Slack notice failed (send is unaffected): %s" % str(exc)[:90])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--touch", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="cap the day's set")
    ap.add_argument("--queue", action="store_true", help="stage as HELD")
    a = ap.parse_args()

    rows = rows_from_pool(a.limit)
    print("heir pool rows: %d" % len(rows))
    cands = seed.build(rows, touch=a.touch)

    ready = [c for c in cands if c.status == "ready"]
    held = [c for c in cands if c.status != "ready"]
    reasons: dict = {}
    for c in held:
        for r in c.reasons:
            key = r.split("(")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1

    rel_by_phone = {r["phone"]: r.get("_rel", "") for r in rows}
    kept, rejected = restage(cands, a.touch, rel_by_phone)
    print("  passed every guard      : %d" % len(ready))
    print("  held by a guard         : %d" % len(held))
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("      %-52s %d" % (k[:52], v))
    print("  heir copy rejected      : %d" % len(rejected))
    print("  READY TO STAGE          : %d" % len(kept))

    if kept:
        print("\nsample of what would send:")
        for c in kept[:4]:
            print("  -> %s  %s" % (c.phone, c.message))

    if a.queue and kept:
        res = seed.queue(kept, touch=a.touch)
        print("\nstaged as HELD: %s" % res)
        notify_staged(kept, a.touch)
        print("nothing has been sent. Release with:")
        print("  python src/sms_agent/cli.py release --touch %d" % a.touch)
    elif not a.queue:
        print("\nDRY RUN - nothing staged. Add --queue to stage as HELD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
