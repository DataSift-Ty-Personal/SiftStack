"""Cold email replies: read them from Instantly, act on the clear ones, and hand every hand raise to the
same people, in the same Slack channel, as a text hand raise.

    python src/cold_email/replies.py                      # dry: what it would do with new replies
    python src/cold_email/replies.py --commit             # do it (the scheduled task runs this)
    python src/cold_email/replies.py --render "yes what would you pay"   # show the Slack post for a reply

Each reply is read by the texting side's own classifier (sms_agent.classify: word rules first, then the
model), so an email and a text are judged the same way. A stop is decided by word patterns only.
  * INTERESTED      -> the text-leads channel, the same "yours, call within 5 minutes" handoff a text
                       gets (SMS_AGENT_SLACK_WEBHOOK, SMS_AGENT_HANDOFF_NAME), with the record's callable
                       numbers and link; a note on the record. New Lead status only when the texting
                       side's switch is on (SMS_AGENT_HAND_RAISE_NEW_LEAD; off since 2026-08-11 because
                       the status change assigned the record to someone other than the person paged).
  * ASKING WHO / OTHER -> the text-leads channel as "needs an answer", and a note.
  * NEEDS A PERSON (a death, a lawyer, hostility) -> the text-leads channel, and a note.
  * STOP            -> Instantly block list, DataSift DNC list (a stop on any channel ends all contact),
                       a note. No Slack post.
  * NOT INTERESTED / WRONG PERSON -> Instantly block (the email promised no more email), a note.
Nothing is ever sent back by email automatically. A reply finds its record through ledger.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from instantly import ROOT, Instantly  # noqa: E402
from setup_accounts import env  # noqa: E402

STATE = ROOT / "output" / "cold_email" / "replies_state.json"
LEDGER = ROOT / "output" / "cold_email" / "ledger.json"
LOG = ROOT / "output" / "cold_email" / "replies.jsonl"
ET = ZoneInfo("America/New_York")
RECORD_URL = "https://app.reisift.io/records/properties/{uuid}/details"
UNIBOX = "https://app.instantly.ai/app/unibox"

# Email-only ways of saying stop, checked before the classifier (which also has its own opt-out rules).
STOP_RX = re.compile(r"\b(unsubscribe|remove me|take me off|stop (e-?mailing|contacting|sending|messaging)|"
                     r"do not (e-?mail|contact|message)|don'?t (e-?mail|contact|message)|leave me alone|"
                     r"lose my (email|address))\b", re.I)

KIND = {"OPT_OUT": "stop", "WRONG_NUMBER": "wrong_person", "NOT_INTERESTED": "not_interested",
        "INTERESTED": "interested", "ASKING_WHO": "needs_reply", "OTHER": "needs_reply", "EMPTY": "needs_reply",
        "ESCALATE": "sensitive"}
POSTED = {"interested", "needs_reply", "sensitive"}
BLOCKED = {"stop", "not_interested", "wrong_person"}


def load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def reply_text(e: dict) -> str:
    b = e.get("body") or {}
    t = b.get("text") if isinstance(b, dict) else ""
    if not t and isinstance(b, dict) and b.get("html"):
        t = re.sub(r"<[^>]+>", "\n", b["html"])
    t = t or e.get("content_preview") or ""
    t = re.split(r"\n\s*On .{5,120}wrote:", t)[0]
    lines = [l for l in t.splitlines() if not l.strip().startswith(">")]
    return "\n".join(lines).strip()[:1500]


def classify(text: str, street: str) -> tuple[str, str, float]:
    """(kind, rationale, confidence) from the texting side's classifier."""
    if STOP_RX.search(text or ""):
        return "stop", "asked to stop emailing", 1.0
    from sms_agent import classify as sms_classify
    c = sms_classify.classify(text, [{"direction": "out", "body": f"Would you be open to an offer on {street or 'your house'}?"}])
    return KIND.get(c.intent, "needs_reply"), c.rationale, c.confidence


def webhook() -> str:
    return env("COLD_EMAIL_SLACK_WEBHOOK") or env("SMS_AGENT_SLACK_WEBHOOK")


def handoff_name() -> str:
    return env("COLD_EMAIL_HANDOFF_NAME") or env("SMS_AGENT_HANDOFF_NAME") or "Emerson"


def fmt_phone(n: str) -> str:
    return f"({n[:3]}) {n[3:6]}-{n[6:]}" if len(n) == 10 else n


def no_dashes(s: str) -> str:
    """Words we write never carry an em or en dash (the model's rationale often does)."""
    return re.sub("\\s*[\u2014\u2013]\\s*", ", ", s or "").strip(", ")


def post_text(kind: str, first: str, where: str, text: str, numbers: list[dict], rationale: str, rec_uuid: str) -> str:
    rationale = no_dashes(rationale)
    who = first or "Unknown owner"
    if kind == "interested":
        head = [f"*{handoff_name()}: yours. Call within 5 minutes.*", f"*Positive email reply - {who}*"]
    elif kind == "sensitive":
        head = [f"*{handoff_name()}: a person needs to handle this one.*", f"*Email reply - {who}*"]
    else:
        head = [f"*{handoff_name()}: an email reply needs an answer today.*", f"*Email reply - {who}*"]
    lines = head + ([where] if where else []) + ["> " + text[:400].replace("\n", "\n> ")]
    if numbers:
        lines.append("Call: " + ", ".join(f"{fmt_phone(p['number'])}" + (f" ({p['tier']})" if p.get("tier") else "")
                                          for p in numbers[:3]))
    else:
        lines.append("No callable number on the record: answer by email.")
    lines.append(f"Read: *{kind.replace('_', ' ')}*" + (f" - {rationale}" if rationale else ""))
    lines.append(f"Answer the email in <{UNIBOX}|Instantly's Unibox>. No more cold emails go to them.")
    if rec_uuid:
        lines.append(RECORD_URL.format(uuid=rec_uuid))
    return "\n".join(lines)


def post(text: str) -> bool:
    url = webhook()
    if not url:
        print("  no SMS_AGENT_SLACK_WEBHOOK; not posted")
        return False
    r = requests.post(url, json={"text": text}, timeout=15)
    if r.status_code >= 300:
        print("  Slack refused:", r.status_code, r.text[:160])
        return False
    return True


def hand_raise_status(crm, rec_uuid: str, rec: dict) -> str:
    """New Lead, the texting side's rule exactly: only when its switch is on, never over a lead status
    or a status a person must look at first."""
    flag = (env("COLD_EMAIL_HAND_RAISE_NEW_LEAD") or env("SMS_AGENT_HAND_RAISE_NEW_LEAD") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return "status left alone (hand raise to New Lead is switched off, same as texts)"
    from agents.autopilot import lanes as lanes_mod
    current = lanes_mod.status_of(rec)
    if current in lanes_mod.LEAD_STATUSES:
        return f"already a lead ({current}); status left alone"
    if current in {"sold", "closed", "close_out", "dnc", "opt_out", "buyer", "buyer_found", "buyer_lost"}:
        return f"status left as {current} for a person to look at"
    crm.set_status(rec_uuid, "new_lead")
    return "status set to New Lead"


def handle(e: dict, ledger: dict, api: Instantly, crm_box: dict, commit: bool) -> dict:
    sender = (e.get("lead") or e.get("from_address_email") or "").lower()
    text = reply_text(e)
    owner = ledger["emails"].get(sender)
    entry = (ledger["owners"].get(owner) or {}) if owner else {}
    rec_uuid = entry.get("record")
    rec = {}
    if rec_uuid:
        if "crm" not in crm_box:
            from agents.autopilot.crm import CRM
            crm_box["crm"] = CRM(priority=True)
        rec = crm_box["crm"].record(rec_uuid)
    a = rec.get("address") or {}
    street = a.get("street") or ""
    where = ", ".join(x for x in (street, a.get("city"), a.get("state")) if x)
    first = ((rec.get("owner") or {}).get("first_name") or "").split(" ")[0].title()
    kind, rationale, conf = classify(text, street)
    numbers = []
    if rec:
        from agents.autopilot import gate
        numbers = gate.usable_numbers(rec, "call", gate.GateConfig())
    out = {"email_id": e.get("id"), "from": sender, "record": rec_uuid, "kind": kind, "confidence": conf,
           "rationale": rationale, "text": text, "acts": []}
    print(f"- {kind} ({conf:.2f}): {where or sender} | {text[:70]!r}")
    if not commit:
        if kind in POSTED:
            print("  would post to text leads:\n    " + post_text(kind, first, where, text, numbers, rationale, rec_uuid).replace("\n", "\n    "))
        return out
    if kind in BLOCKED:
        api.block([sender])
        out["acts"].append("blocked in Instantly")
    if rec_uuid:
        crm = crm_box["crm"]
        when = datetime.now(ET).strftime("%Y-%m-%d %I:%M %p")
        crm.message(rec_uuid, f"Cold email reply ({when}), {kind.replace('_', ' ')}: {text[:900]}")
        out["acts"].append("note on the record")
        if kind == "stop":
            crm.add_lists(rec_uuid, ["DNC"])
            out["acts"].append("DNC list")
        if kind == "interested":
            out["acts"].append(hand_raise_status(crm, rec_uuid, rec))
    if kind in POSTED:
        ok = post(post_text(kind, first, where, text, numbers, rationale, rec_uuid))
        out["acts"].append("posted to text leads" if ok else "text leads post FAILED")
    return out


def run_once(commit: bool) -> int:
    api = Instantly()
    state = load(STATE, {"seen": [], "since": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()})
    ledger = load(LEDGER, {"owners": {}, "emails": {}})
    seen = set(state["seen"])
    got = api.call("GET", "/emails", params={"email_type": "received", "min_timestamp_created": state["since"],
                                             "sort_order": "asc", "limit": 100})
    items = [e for e in (got.get("items") or []) if e.get("id") not in seen]
    crm_box: dict = {}
    done = 0
    for e in items:
        if e.get("is_auto_reply"):
            seen.add(e["id"])
            continue
        try:
            out = handle(e, ledger, api, crm_box, commit)
        except Exception as exc:          # one bad reply must not stop the rest; it is tried again next pass
            print("  failed:", str(exc)[:300])
            continue
        if commit:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(dict(out, at=datetime.now(timezone.utc).isoformat())) + "\n")
            seen.add(e["id"])
            done += 1
    if commit:
        if items:
            state["since"] = max(e.get("timestamp_created") or state["since"] for e in items)
        state["seen"] = sorted(seen)[-5000:]
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"{datetime.now(ET):%Y-%m-%d %H:%M} {len(items)} new replies, {done} handled"
          + ("" if commit else " (dry run: nothing done)"))
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--render", help="classify this reply text and show the Slack post, nothing sent")
    a = ap.parse_args()
    if a.render:
        kind, why, conf = classify(a.render, "100 Example St")
        print(f"{kind} ({conf:.2f}): {why}")
        if kind in POSTED:
            print(post_text(kind, "Pat", "100 Example St, Knoxville, TN", a.render,
                            [{"number": "8650000000", "tier": "Dial First"}], why, "RECORD-UUID"))
        else:
            print("(no Slack post: handled automatically)")
        return 0
    run_once(a.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
