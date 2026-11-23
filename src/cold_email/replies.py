"""Cold email replies: read them from Instantly, act on the clear ones, and hand every hand raise to the
same people, in the same Slack channel, as a text hand raise.

    python src/cold_email/replies.py                      # dry: what it would do with new replies
    python src/cold_email/replies.py --commit             # do it (the brain's cold_email_replies stage runs this)
    python src/cold_email/replies.py --render "yes what would you pay"   # show the Slack post for a reply
    python src/cold_email/replies.py --init-state         # a new install only: start the watermark now

A committed pass runs only on the install home.json names (coldstate.py) and one at a time on a
machine. It refuses to start, reading nothing, when the text leads channel is not set (a hand raise
read then would reach nobody), when replies_state.json is missing (the old default re-read the last two
days and would post those hand raises a second time), or when the ledger cannot be read (a stop would
then never reach the DNC list).

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
A hand raise the channel refuses is read again next pass, like any other failure: it is the one post
that must reach a person.
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
import coldstate  # noqa: E402
from instantly import Instantly  # noqa: E402
from setup_accounts import env  # noqa: E402

# The state folder: the brain's volume on the box, output/cold_email on a workstation (coldstate.py).
# Everything else a pass keeps (its lock, its heartbeat) sits beside STATE, so a test that points STATE
# at a temporary folder keeps all of it there.
STATE = coldstate.state_dir() / "replies_state.json"
LEDGER = coldstate.state_dir() / "ledger.json"
LOG = coldstate.state_dir() / "replies.jsonl"
ET = ZoneInfo("America/New_York")
RECORD_URL = "https://app.reisift.io/records/properties/{uuid}/details"
UNIBOX = "https://app.instantly.ai/app/unibox"

# Email-only ways of saying stop, checked before the classifier (which also has its own opt-out rules).
# "Do not notify me again" read as a plain no (0.60) on 2026-11-19 and never reached the DNC list.
STOP_RX = re.compile(r"\b(unsubscribe|remove me|take me off|stop (e-?mailing|contacting|sending|messaging|notifying|texting)|"
                     r"do not (e-?mail|contact|message|notify|text|reach out)|"
                     r"don'?t (e-?mail|contact|message|notify|text|reach out)|leave me alone|"
                     r"lose my (email|address))\b", re.I)
# A reply whose handling raised is read again on the next pass, this many times, before it goes to a
# person as "could not be handled" (2026-11-19: two replies failed on a retired notes route, the
# watermark moved past them, and they were never read again).
MAX_TRIES = 6
# A reply that has failed this many passes in a row is said out loud (a problem the brain posts once a
# day), well before MAX_TRIES hands it to a person: one failure is usually a blip the next pass clears.
SAY_AFTER = 3

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


def _short(exc) -> str:
    return re.sub(r"\s+", " ", str(exc))[:160]


def handle(e: dict, ledger: dict, api: Instantly, crm_box: dict, commit: bool) -> dict:
    """One reply. The person gets the hand raise BEFORE any CRM write is tried, and a CRM write that
    fails is written down and passed over, so DataSift being down never costs a lead (2026-11-19: a
    retired notes route raised before the Slack post). Only what must not be lost raises, so the reply is
    read again next pass: the hand raise itself (2026-11-20), the Instantly block (the email promised no
    more email) and a stop's DNC list."""
    sender = (e.get("lead") or e.get("from_address_email") or "").lower()
    text = reply_text(e)
    owner = ledger["emails"].get(sender)
    entry = (ledger["owners"].get(owner) or {}) if owner else {}
    rec_uuid = entry.get("record")
    rec, read_err = {}, ""
    if rec_uuid:
        try:
            if "crm" not in crm_box:
                from agents.autopilot.crm import CRM
                crm_box["crm"] = CRM(priority=True)
            rec = crm_box["crm"].record(rec_uuid) or {}
        except Exception as exc:  # noqa: BLE001  (the reply is still handed to a person)
            read_err = _short(exc)
    a = rec.get("address") or {}
    street = a.get("street") or ""
    where = ", ".join(x for x in (street, a.get("city"), a.get("state")) if x)
    first = ((rec.get("owner") or {}).get("first_name") or "").split(" ")[0].title()
    kind, rationale, conf = classify(text, street)
    numbers = []
    if rec:
        try:
            from agents.autopilot import gate
            numbers = gate.usable_numbers(rec, "call", gate.GateConfig())
        except Exception:  # noqa: BLE001
            numbers = []
    out = {"email_id": e.get("id"), "from": sender, "record": rec_uuid, "kind": kind, "confidence": conf,
           "rationale": rationale, "text": text, "acts": []}
    if read_err:
        out["acts"].append("record not read (%s)" % read_err)
    print(f"- {kind} ({conf:.2f}): {where or sender} | {text[:70]!r}")
    if not commit:
        if kind in POSTED:
            print("  would post to text leads:\n    " + post_text(kind, first, where, text, numbers, rationale, rec_uuid).replace("\n", "\n    "))
        return out
    if kind in BLOCKED:
        api.block([sender])
        out["acts"].append("blocked in Instantly")
    if kind in POSTED:
        # Nothing has been written for this reply yet, so a refused post is simply tried again next pass
        # (until 2026-11-20 it was written down and passed over, and the hand raise was gone).
        if not post(post_text(kind, first, where, text, numbers, rationale, rec_uuid)):
            raise RuntimeError("the text leads channel did not take the post")
        out["acts"].append("posted to text leads")
    if kind == "stop" and rec_uuid and "crm" not in crm_box:
        raise RuntimeError("DataSift could not be reached to put a stop on the DNC list (%s)" % (read_err or "no client"))
    if rec_uuid and "crm" in crm_box:
        crm = crm_box["crm"]
        if kind == "stop":
            crm.add_lists(rec_uuid, ["DNC"])          # raises: a stop that did not land is tried again
            out["acts"].append("DNC list")
        when = datetime.now(ET).strftime("%Y-%m-%d %I:%M %p")
        try:
            crm.message(rec_uuid, f"Cold email reply ({when}), {kind.replace('_', ' ')}: {text[:900]}")
            out["acts"].append("note on the record")
        except Exception as exc:  # noqa: BLE001
            out["acts"].append("note FAILED (%s)" % _short(exc))
        if kind == "interested":
            try:
                out["acts"].append(hand_raise_status(crm, rec_uuid, rec))
            except Exception as exc:  # noqa: BLE001
                out["acts"].append("status not set (%s)" % _short(exc))
    return out


def _parse(ts: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _before(ts: str) -> str:
    """One second before an Instantly timestamp, so a reply AT the watermark is read again."""
    t = _parse(ts)
    if t == datetime.min.replace(tzinfo=timezone.utc):
        return ts
    return (t - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def give_up_text(e: dict, err: str) -> str:
    sender = (e.get("lead") or e.get("from_address_email") or "").lower()
    return "\n".join([f"*{handoff_name()}: an email reply could not be handled on its own. Please read it.*",
                      f"From {sender or 'an unknown sender'}",
                      "> " + reply_text(e)[:400].replace("\n", "\n> "),
                      f"Tried {MAX_TRIES} times; the last error: {no_dashes(err)[:160]}",
                      f"Answer it in <{UNIBOX}|Instantly's Unibox>."])


class LedgerError(RuntimeError):
    pass


def read_ledger(strict: bool) -> dict:
    """ledger.json: which owner and record each address we emailed belongs to. A committed pass needs
    it whole: read as empty, every reply would be handled with no record, and a stop would never reach
    the DNC list. So a committed pass refuses a missing or unreadable ledger; a dry run reads it as empty."""
    try:
        d = json.loads(LEDGER.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not a mapping")
        d.setdefault("owners", {})
        d.setdefault("emails", {})
        return d
    except FileNotFoundError:
        if strict:
            raise LedgerError("there is no ledger.json in %s, so no reply could be matched to its record" % LEDGER.parent)
    except (OSError, ValueError) as exc:
        if strict:
            raise LedgerError("ledger.json could not be read (%s)" % _short(exc))
    return {"owners": {}, "emails": {}}


def _post_quietly(text: str) -> bool:
    try:
        return bool(post(text))
    except Exception:  # noqa: BLE001  (a give-up post that fails is tried again next pass, never a crash mid-pass)
        return False


def _log(row: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(row, at=datetime.now(timezone.utc).isoformat())) + "\n")


def _save(state: dict, seen: set, tries: dict) -> None:
    """Saved after every reply, not only at the end: a pass that dies halfway (a redeploy of the box)
    would otherwise read the replies it had already handled again and post their hand raises twice.
    The watermark (`since`) moves only at the end; until then `seen` keeps the handled ones out."""
    state["tries"] = tries
    state["seen"] = sorted(seen)[-5000:]
    coldstate.write_json(STATE, state)


def _act_problem(act: str) -> str:
    """A part of a reply's handling that was written down and passed over, as a problem to say."""
    detail = act[act.find("("):] if "(" in act else ""
    if act.startswith("note FAILED"):
        return "could not write the cold email reply note on a record %s" % detail
    if act.startswith("record not read"):
        return "could not read the record a cold email reply belongs to %s; the reply was handed on without it" % detail
    if act.startswith("status not set"):
        return "could not set New Lead after an interested cold email reply %s" % detail
    return "a cold email reply was handled, but: %s" % act


def peek() -> dict:
    """What a pass would read, without the classifier (no model call) and without writing a thing: the
    brain's plan for this stage, cheap enough to run every five minutes."""
    api = Instantly()
    state = load(STATE, {"seen": [], "since": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()})
    seen = set(state.get("seen") or [])
    got = api.call("GET", "/emails", params={"email_type": "received", "min_timestamp_created": state["since"],
                                             "sort_order": "asc", "limit": 100})
    items = [e for e in (got.get("items") or []) if e.get("id") not in seen]
    auto = sum(1 for e in items if e.get("is_auto_reply"))
    return {"new": len(items) - auto, "auto": auto, "state": STATE.exists()}


def run_pass(commit: bool) -> dict:
    """One pass over the new replies, and what happened:
        new       replies read that were not seen before (auto replies included)
        handled   replies acted on and written down (a committed pass)
        auto      automatic replies skipped
        kinds     {kind: n} for every reply classified
        retry     replies that failed and are read again next pass
        gave_up   replies handed to a person after MAX_TRIES failures
        problems  sentences for a person: a refusal to start, a reply failing pass after pass, a note
                  or a status that could not be written
        skipped   why the pass did not run ("" when it ran)
    A committed pass refuses to start (reading nothing) without the text leads channel, without
    replies_state.json, or without a readable ledger, and runs one at a time on a machine."""
    out = {"new": 0, "handled": 0, "auto": 0, "kinds": {}, "retry": 0, "gave_up": 0, "problems": [], "skipped": ""}
    lock = None
    if commit:
        if not webhook():
            out["problems"].append("could not read the replies: no text leads channel is set "
                                   "(SMS_AGENT_SLACK_WEBHOOK), so a hand raise read now would reach nobody")
            out["skipped"] = "no text leads channel"
            return out
        if not STATE.exists():
            out["problems"].append("could not read the replies: there is no replies_state.json in %s. Copy it "
                                   "from the install that ran cold email before, or start a new install with "
                                   "replies.py --init-state" % STATE.parent)
            out["skipped"] = "no reply state"
            return out
        lock = coldstate.RunLock("replies", STATE.parent)
        if not lock.acquire():
            out["skipped"] = "another reply pass is running"
            return out
    try:
        return _pass(commit, out)
    finally:
        if lock is not None:
            lock.release()


def _pass(commit: bool, out: dict) -> dict:
    try:
        ledger = read_ledger(strict=commit)
    except LedgerError as exc:
        out["problems"].append("could not read the replies: %s" % exc)
        out["skipped"] = "no ledger"
        return out
    default = {"seen": [], "since": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
    if commit:
        # Read as the default, a broken state file would start two days back with nothing seen, and every
        # hand raise of those two days would be posted again.
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not state.get("since"):
                raise ValueError("no watermark in it")
        except (OSError, ValueError) as exc:
            out["problems"].append("could not read the replies: replies_state.json could not be read (%s)"
                                   % _short(exc))
            out["skipped"] = "reply state unreadable"
            return out
    else:
        state = load(STATE, default)
    api = Instantly()
    seen = set(state.get("seen") or [])
    tries = dict(state.get("tries") or {})
    got = api.call("GET", "/emails", params={"email_type": "received", "min_timestamp_created": state["since"],
                                             "sort_order": "asc", "limit": 100})
    items = [e for e in (got.get("items") or []) if e.get("id") not in seen]
    out["new"] = len(items)
    crm_box: dict = {}
    pending: list[str] = []               # replies that failed and will be read again: the watermark waits
    for e in items:
        if e.get("is_auto_reply"):
            seen.add(e["id"])
            out["auto"] += 1
            continue
        try:
            r = handle(e, ledger, api, crm_box, commit)
        except Exception as exc:          # one bad reply must not stop the rest; it is tried again next pass
            print("  failed:", str(exc)[:300])
            if commit:
                n = int(tries.get(e["id"]) or 0) + 1
                tries[e["id"]] = n
                if n >= MAX_TRIES:        # past the last try a person reads it, so no reply is ever lost
                    ok = _post_quietly(give_up_text(e, str(exc)))
                    _log({"email_id": e.get("id"), "kind": "gave_up", "error": _short(exc),
                          "acts": ["posted to text leads" if ok else "text leads post FAILED"]})
                    if ok:
                        seen.add(e["id"])
                        tries.pop(e["id"], None)
                        out["gave_up"] += 1
                        _save(state, seen, tries)
                        continue
                    out["problems"].append("could not hand a cold email reply to a person after %d tries (%s); it is "
                                           "read again every pass" % (n, _short(exc)))
                elif n >= SAY_AFTER:
                    out["problems"].append("a cold email reply has failed %d passes in a row (%s); it is read again "
                                           "every pass and goes to a person after %d" % (n, _short(exc), MAX_TRIES))
                out["retry"] += 1
                pending.append(e.get("timestamp_created") or state["since"])
                _save(state, seen, tries)
            continue
        out["kinds"][r["kind"]] = out["kinds"].get(r["kind"], 0) + 1
        for act in r.get("acts") or []:
            if "FAILED" in act or act.startswith(("record not read", "status not set")):
                p = _act_problem(act)
                if p not in out["problems"]:
                    out["problems"].append(p)
        if commit:
            _log(r)
            seen.add(e["id"])
            tries.pop(e.get("id"), None)
            out["handled"] += 1
            _save(state, seen, tries)
    if commit:
        if pending:
            # read again from just before the earliest failure; what came after it is in `seen`
            state["since"] = _before(min(pending, key=_parse))
        elif items:
            state["since"] = max(e.get("timestamp_created") or state["since"] for e in items)
        _save(state, seen, tries)
        # The watcher's own proof of life, read by the morning health check wherever it runs.
        coldstate.beat(STATE.parent, new=out["new"], handled=out["handled"])
    print(f"{datetime.now(ET):%Y-%m-%d %H:%M} {len(items)} new replies, {out['handled']} handled"
          + ("" if commit else " (dry run: nothing done)"))
    return out


def run_once(commit: bool) -> int:
    """One pass; how many replies were handled (the count the command line and the tests read)."""
    return run_pass(commit)["handled"]


def init_state() -> int:
    """A new install's first watermark: now, so the first pass reads only what arrives from here on."""
    if STATE.exists():
        print("replies_state.json already exists in %s; nothing changed" % STATE.parent)
        return 1
    coldstate.write_json(STATE, {"seen": [], "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    if not LEDGER.exists():
        coldstate.write_json(LEDGER, {"owners": {}, "emails": {}})
    print("started the reply watermark in %s" % STATE.parent)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--render", help="classify this reply text and show the Slack post, nothing sent")
    ap.add_argument("--init-state", action="store_true", help="a new install only: start the reply watermark now")
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
    if a.init_state:
        coldstate.require("start the reply watermark", STATE.parent)
        return init_state()
    if a.commit:
        coldstate.require("handle cold email replies", STATE.parent)
    r = run_pass(a.commit)
    for p in r["problems"]:
        print("problem:", p)
    return 2 if a.commit and r["skipped"] and r["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
