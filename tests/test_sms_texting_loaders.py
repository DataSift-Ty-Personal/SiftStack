"""Zero-network checks that the Smarter Contact loaders ask the same questions the seeder asks
(build plan 0.1 and 0.22).

Three tools put a number in front of a texting campaign: the smrtPhone seeder (sms_agent/seed.py),
the land loader (smarter_contact_land.py) and the two Priority 1 audits (smarter_contact_audit.py,
smarter_contact_remaining.py). Each used to carry its own copy of "which phone statuses are never
texted" and its own way of reading the do-not-call registry flag. They now share one function,
sms_agent/gate_rules.do_not_text, and one suppression gate behind SMS_AGENT_GATE. This file holds
the loaders to that: the shared rule, and the gate in its three modes (off changes nothing).

Every name, street and number below is invented.

    python tests/test_sms_texting_loaders.py
"""
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# A throwaway database, chosen BEFORE the modules bind to one, and no network at all: the gate
# reads this app's own store (the numbers it suppressed as litigators), never the live one.
TMP = Path(tempfile.mkdtemp(prefix="sms_texting_loaders_"))
os.environ["SMS_AGENT_DB"] = str(TMP / "loaders.db")
os.environ["SMS_AGENT_DATA_DIR"] = str(TMP)
os.environ["SMS_AGENT_DRY_RUN"] = "1"

TRIED = []
_connect = socket.socket.connect


def _local_only(sock, address, *a, **k):
    host = address[0] if isinstance(address, tuple) else str(address)
    if str(host) not in ("127.0.0.1", "::1", "localhost"):
        TRIED.append(str(host))
        raise OSError("this test is offline, and something tried %s" % host)
    return _connect(sock, address, *a, **k)


socket.socket.connect = _local_only

from sms_agent import config, crm, gate_rules, gatekeeper, seed, store  # noqa: E402

config.DB_PATH = Path(os.environ["SMS_AGENT_DB"])
store._local.__dict__.pop("conn", None)
store.init()
crm.client = lambda: None       # no CRM in this file: every record below is handed in

import smarter_contact_audit as A  # noqa: E402
import smarter_contact_land as L  # noqa: E402
import smarter_contact_remaining as R  # noqa: E402

N = 0


def check(cond, msg):
    global N
    N += 1
    if not cond:
        raise AssertionError(msg)


def phone(number, status="UNKNOWN", kind="MOBILE", tags=("Dial First",), **extra):
    return dict({"number": number, "status": status, "type": kind, "tags": list(tags)}, **extra)


def record(uuid="rec-loader", status=None, tags=(), lists=(), owner=None, **extra):
    rec = {"uuid": uuid, "status": status, "tags": list(tags), "lists": list(lists), "mls": "Off Market",
           "address": {"street": "40 Birch Ln", "city": "Maryville", "state": "TN", "postal_code": "37801"},
           "owner": dict({"first_name": "Pat", "last_name": "Lane", "phones": [phone("8655550101")]}, **(owner or {}))}
    rec.update(extra)
    return rec


def in_mode(mode, fn, *args):
    was = config.GATE_MODE
    config.GATE_MODE = mode
    try:
        return fn(*args)
    finally:
        config.GATE_MODE = was


# ---- one set, one rule ----
check(seed.SKIP_PHONE_STATUSES is gate_rules.BAD_PHONE and A.SKIP_PHONE is gate_rules.BAD_PHONE
      and R.SKIP_PHONE is gate_rules.BAD_PHONE,
      "the seeder and both audits read the gate's own set of never-text phone statuses, not three copies")
check(gate_rules.BAD_PHONE == {"DNC", "CORRECT_DNC", "WRONG_DNC", "WRONG", "DEAD"},
      "and the set is the five statuses every one of them always skipped")
check(not gate_rules.BAD_PHONE & L.PHONE_STATUS_OK,
      "the land loader's allow-list holds none of them")

# ---- the land loader's phone pick ----
PHONES = [
    phone("8655550101"),
    phone("8655550102", doNotCall=True),                       # the registry flag on the phone itself
    phone("8655550103", status="CORRECT_DNC"),
    phone("8655550104", status="WRONG"),
    phone("8655550105", kind="LANDLINE"),
    phone("8655550106", tags=("Drop",)),
    phone("8655550107", status="CORRECT", tags=()),
    phone("8655550108", status="DEAD"),
    phone("8655550109", tags=("Dial Second",)),
]
picked = L.pick_phones(PHONES, blocked=set())
check(picked == ["8655550101", "8655550109", "8655550107"],
      "best tier first, at most three, and never a flagged, do-not-call, wrong, dead, landline or Drop number: %s" % picked)
check("8655550102" not in L.pick_phones(PHONES[:2], blocked=set()),
      "a phone that carries the do-not-call registry flag is never picked, whatever its tier")
check(L.pick_phones(PHONES, blocked={"8655550101", "8655550109"}) == ["8655550107"],
      "numbers flagged on the search rows arrive in the blocked set and are left out, as before")
every = [phone("86555502%02d" % i, status=s, kind=k, tags=t, **({"doNotCall": True} if f else {}))
         for i, (s, k, t, f) in enumerate(
             (s, k, t, f) for s in ("UNKNOWN", "CORRECT", "NO_ANSWER", "DNC", "CORRECT_DNC", "WRONG_DNC", "WRONG", "DEAD", "")
             for k in ("MOBILE", "VOIP") for t in (("Dial First",), ()) for f in (False, True))]
for start in range(0, len(every), 3):
    chunk = every[start:start + 3]
    got = set(L.pick_phones(chunk, blocked=set()))
    refused = {p["number"] for p in chunk if gate_rules.do_not_text(p)}
    allowed = {p["number"] for p in chunk if not gate_rules.do_not_text(p)}
    check(not got & refused and got == allowed,
          "across every status, type, tier and flag: the loader picks exactly what the one rule allows (%s)" % chunk)

# ---- the registry flag, read off a page of search rows ----
PAGE = [
    {"uuid": "r1", "phone": {"number": "+1 (865) 555-0301", "doNotCall": True}},
    {"uuid": "r2", "phone": {"number": "8655550302", "doNotCall": False}},
    {"uuid": "r3", "phone": {"number": "8655550303"}},
    {"uuid": "r4", "phone": None},
    {"uuid": "r5", "phone": {"number": "18655550305", "doNotCall": True}},
    {"uuid": "r6"},
]
old_way = {L.digits10((x.get("phone") or {}).get("number")) for x in PAGE if (x.get("phone") or {}).get("doNotCall")}
check(gate_rules.flagged_numbers(PAGE) == old_way == {"8655550301", "8655550305"},
      "the shared reader collects the same flagged numbers the land loader's own line did")
check(gate_rules.do_not_text({"number": "8655550301"}, gate_rules.flagged_numbers(PAGE)) == gate_rules.REGISTRY,
      "and a number collected there is refused on a full record, where the flag itself is absent")
check(gate_rules.do_not_text({"number": "8655550302", "doNotCall": False}) == ""
      and gate_rules.do_not_text({"number": "8655550302", "doNotCall": None}) == "",
      "a flag that is false or unknown refuses nothing")
check(gate_rules.do_not_text({"status": "wrong_dnc"}) == "phone status WRONG_DNC",
      "the audits hand in a status alone, in any case")

# ---- the land loader's record check: off is exactly what it was ----
CASES = {
    "clean": record(),
    "dead owner flag": record(owner={"deceased": True}),
    "owner opted out": record(owner={"opt_out": True}),
    "DNC list": record(lists=["DNC"]),
    "Do Not Market": record(tags=["Do Not Market"]),
    "a worked status": record(status="Hot Lead"),
    "deceased tag": record(tags=["deceased"]),
    "Obituary list": record(lists=["Obituary"]),
    "Mail Only": record(tags=["Mail Only"]),
    "sold tag": record(tags=["recently sold"]),
    "listed": record(mls="Listed"),
    "litigator phone": record(owner={"phones": [phone("8655550401", tags=("Dial First", "Litigator"))]}),
}
LEGACY = {"clean": "", "dead owner flag": "owner deceased", "owner opted out": "owner DNC or opted out",
          "DNC list": "on the DNC list", "Do Not Market": "tagged Do Not Market", "a worked status": "status Hot Lead",
          "deceased tag": "", "Obituary list": "", "Mail Only": "", "sold tag": "", "listed": "", "litigator phone": ""}
check(config.GATE_MODE == "off", "the gate is off unless SMS_AGENT_GATE says otherwise")
off = {k: in_mode("off", L.record_skip, rec) for k, rec in CASES.items()}
check(off == LEGACY, "with the gate off the land loader skips exactly what it always skipped: %s" % off)
check(all(in_mode("off", L.gate_note, rec) == "" for rec in CASES.values()),
      "and with it off the gate says nothing at all")

report_skip = {k: in_mode("report", L.record_skip, rec) for k, rec in CASES.items()}
report_note = {k: in_mode("report", L.gate_note, rec) for k, rec in CASES.items()}
check(report_skip == LEGACY, "in report mode nobody extra is skipped")
check(report_note["clean"] == "" and all(report_note[k] for k in CASES if k != "clean"),
      "but the gate's note is there to be counted for every record it would hold: %s" % report_note)

enforce = {k: in_mode("enforce", L.record_skip, rec) for k, rec in CASES.items()}
check(enforce["clean"] == "" and all(enforce[k] == LEGACY[k] for k in CASES if LEGACY[k]),
      "enforced, the loader's own reasons still come first, in its own words")
check(enforce["deceased tag"] == "the owner has died or may have (tag deceased); research first"
      and enforce["Obituary list"] == "the owner has died or may have (Obituary list); research first",
      "enforced, a death recorded as a tag or on the Obituary list is skipped: %s" % enforce["deceased tag"])
check(enforce["Mail Only"].startswith("mail only") and enforce["sold tag"] == "the house sold"
      and enforce["listed"] == "listed on the MLS" and enforce["litigator phone"] == "a litigator's number is on the record",
      "and so are Mail Only, a sold tag, a listed house and a litigator's number")

store.suppress("8655550101", config.LITIGATOR_SUPPRESSION_REASON)
check(in_mode("enforce", L.record_skip, record()) == "a litigator's number is on the record",
      "a number this app already suppressed as a litigator's holds the record it sits on")
with store.tx() as c:
    c.execute("DELETE FROM suppression WHERE phone=?", ("8655550101",))

# ---- the audits: only what the gate ADDS, so one fact is not counted twice ----
check(all(in_mode("off", A.gate_extra, rec) == "" for rec in CASES.values()),
      "with the gate off the audits get nothing from it")
extra = {k: in_mode("report", A.gate_extra, rec) for k, rec in CASES.items()}
check(extra["a worked status"] == "" and extra["Do Not Market"] == "" and extra["DNC list"] == "",
      "a status, Do Not Market and the DNC list are the audit's own checks and are not repeated: %s" % extra)
check(extra["deceased tag"].startswith("the owner has died") and extra["Obituary list"].startswith("the owner has died")
      and extra["Mail Only"].startswith("mail only") and extra["sold tag"] == "the house sold"
      and extra["litigator phone"].startswith("a litigator"),
      "a death, Mail Only, a sold tag and a litigator are what the gate adds")
check(extra["owner opted out"].startswith("the owner opted out") and extra["dead owner flag"].startswith("the owner has died"),
      "and the owner's own opt-out and deceased flags, which the audits never read")
check(R.gate_extra is A.gate_extra, "the second audit uses the same function")
check(in_mode("report", A.gate_extra, record(status="Brand New Status")) == "",
      "a status the gate has never seen is already the audit's own 'someone acted on this record'")

# ---- nothing here may write to an account or reach the network ----
check(TRIED == [], "nothing in this file reached the network: %s" % TRIED)
for mod in (A, L, R, gate_rules, gatekeeper):
    with open(mod.__file__, "rb") as fh:
        check(b"\x08" not in fh.read(), "no backspace byte in %s" % os.path.basename(mod.__file__))

socket.socket.connect = _connect
print("test_sms_texting_loaders: %d checks passed" % N)
