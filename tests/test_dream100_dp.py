"""Zero-network checks on the Dream 100 deep-prospecting write-back and redaction rules.

python tests/test_dream100_dp.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import dream100_dp as dp  # noqa: E402

n = 0


def check(cond, msg):
    global n
    n += 1
    assert cond, msg


def row(number, tier, *, reaches, on_crm=False, status=None, sources=("SmartSkip",), litigator=False, type_="MOBILE"):
    r = {"number": number, "tier": tier, "score": 90, "type": type_, "on_crm": on_crm, "crm_status": status,
         "sources": list(sources), "litigator": litigator, "reaches": reaches, "note": "",
         "reaches_label": "; ".join(x["name"] for x in reaches)}
    r["do_not_dial"] = (status or "") in dp.BAD_STATUSES or litigator or tier == "Drop"
    return r


OWNER = {"name": "Robin Tester", "relationship": "Wife", "tag": "Wife", "role": "decision maker", "owner_like": False}
SELF = {"name": "Bao Sample", "relationship": "Owner", "tag": "", "role": "owner", "owner_like": True}
KID = {"name": "Christie Example", "relationship": "Daughter", "tag": "Daughter", "role": "signer", "owner_like": False}
d = {"x": {"owner_uuid": "o1"}, "dial": [
    row("8651110001", "Dial First", reaches=[OWNER]),
    row("8651110002", "Dial First", reaches=[SELF, KID]),                 # shared household line
    row("8651110003", "Dial First", reaches=[KID], on_crm=True),          # already on the record
    row("8651110004", "Dial Fourth", reaches=[KID]),                       # tier too low to add
    row("8651110005", "Dial First", reaches=[KID], litigator=True),        # litigator risk
    row("8651110006", "Dial First", reaches=[KID], status="WRONG"),        # a caller marked it wrong
    row("8651110007", "Dial Second", reaches=[SELF], sources=("Tracerfy",), type_="LANDLINE"),
]}
plan = dp.writeback_plan(d)
by = {p["number"]: p for p in plan["phones"]}
check(set(by) == {"8651110001", "8651110002", "8651110007"}, "only new, dialable, First to Third numbers: %s" % sorted(by))
check(by["8651110001"]["tags"] == ["Wife", "Dial First", "SmartSkip"], by["8651110001"]["tags"])
check(by["8651110002"]["tags"] == ["Dial First", "SmartSkip"], "owner rule: a shared line carries no relationship")
check(by["8651110007"]["tags"] == ["Dial Second", "Tracerfy"], "the owner's own number carries tier and source")
check(by["8651110007"]["type"] == "LANDLINE", "line type rides along, never forced to UNKNOWN")
check(all(not any(ch.isdigit() for ch in "".join(p["tags"])) for p in plan["phones"]), "no number in a tag")
check(all(t not in ("Robin Tester", "Christie Example", "Bao Sample") for p in plan["phones"] for t in p["tags"]),
      "never a person's name in a phone tag")
check(all(set(p) >= {"number", "type", "tags", "status", "is_connected", "verified"} for p in plan["phones"]),
      "full phone objects, because upsert-phones replaces by number")

red = dp.Redactor(["Charles Hexample", "Jo Ann Hextest", "X Gexample"], ["1904 Example Ave"])
s = red("Call Charles Hexample at (865) 555-0135 or 865-555-0172 about 1904 Example Ave. "
        "Hexample family, Gexample trust, email charlie.h@example.com, https://example.com/obit")
check("555-0135" not in s and "(865) xxx-0135" in s and "(865) xxx-0172" in s, s)
check("Hexample" not in s and "Charles H." in s and "H. family" in s, s)
check("Gexample" not in s and "G. trust" in s, "a surname fed from a county string is masked: %s" % s)
check("@" not in s and "http" not in s and "1904 Example" not in s and "xxxx Example Ave" in s, s)
check(red("Jo Ann Hextest") == "Jo H.", red("Jo Ann Hextest"))

tl = dp._timeline([
    {"event_type": "owner.call.made", "timestamp": "2026-07-10 19:39:00",
     "payload": {"call": {"uuid": "c1", "created": "2026-07-10T19:39:00", "origin_number": "8655550150",
                          "destination_number": "8655550163", "direction": "outbound", "source": "smrtphone"}}},
    {"event_type": "owner.call.answered", "timestamp": "2026-07-10 19:41:00",
     "payload": {"call": {"uuid": "c1", "created": "2026-07-10T19:39:00", "origin_number": "8655550150",
                          "destination_number": "8655550163", "direction": "outbound", "duration": 124,
                          "recording": "https://rec.example/x.mp3", "external_user": {"name": "Adriana"}}}},
    {"event_type": "owner.sms.sent", "timestamp": "2026-07-14 14:52:38",
     "payload": {"sms": {"created": "2026-07-14T14:52:38+0000", "origin_number": "8655550150",
                         "destination_number": "8655550172", "direction": "outbound", "message": "Me again."}}},
    {"event_type": "owner.sms.updated", "timestamp": "2026-07-14 14:54:33",
     "payload": {"sms": {"updated": ["2026-07-14T14:52:38+0000", "2026-07-14T14:54:33+0000"],
                         "status": [None, "failed"], "status_message": [None, "landline"]}}},
])
calls = [t for t in tl if t["kind"] == "call"]
sms = [t for t in tl if t["kind"] == "sms"]
check(len(calls) == 1 and calls[0]["outcome"] == "answered" and calls[0]["seconds"] == 124, calls)
check(calls[0]["recording"] and calls[0]["who"] == "Adriana", "one row per call, carrying the recording")
check(len(sms) == 1 and sms[0]["status"] == "failed", "a status update folds onto its own send: %s" % sms)

check(dp._gist({"transcript": "x", "recording": "r", "transcript_reliable": False}) == "recording not usable",
      "an implausible transcript is never summarized")
check(dp._gist({"transcript_reliable": True, "transcript_strict": "CONTACT I'm a renter."}) == "a renter answered", "")
check(dp._words("one two three\nDELIVERY SUMMARY:\n- pace: four five") == 3, "delivery notes do not count as speech")
check(33 > dp.MAX_WORDS_PER_SEC * 10, "660 words in 11 seconds is impossible")

x = {"record": {"owner": {"first_name": "George", "last_name": "Hextest"},
                "secondary_owners": [{"full_name": "Jo Ann Hextest"}]},
     "title_owner": "HEXTEST GEORGE & JO ANN LEEK"}
check(dp._title_people(x) == [("George", "", "Hextest"), ("Jo Ann", "", "Hextest")], dp._title_people(x))
x = {"record": {"owner": {"first_name": "Charles", "last_name": "Hexample"}},
     "title_owner": "HEXAMPLE CHARLES B  JR"}
check(dp._title_people(x) == [("Charles", "B", "Hexample")], dp._title_people(x))
x = {"record": {"owner": {"first_name": "Sharon M", "last_name": "Gexample"}}, "title_owner": "GEXAMPLE SHARON"}
check(dp._title_people(x) == [("Sharon", "M", "Gexample")], dp._title_people(x))

toks = dp._name_tokens("Survivors: wife: Kelly Hall Hexample; step-children: Samantha Tolvexample, Bryton Moorexample; "
                       "grandchildren Celissa Fryexample (Tyler) Laisa and Silas Bullexample")
check({"Hall", "Hexample", "Tolvexample", "Moorexample", "Fryexample", "Bullexample"} <= toks, "maiden and surnames harvested: %s" % toks)
check(not ({"Kelly", "Samantha", "Celissa", "Laisa"} & toks), "first names survive: %s" % toks)
check(dp._name_tokens("Knox County Chancery Court, Probate Division") == set(), "institutions are not people")
check("\x08" not in Path(dp.__file__).read_text(encoding="utf-8"), "no backspace byte from a heredoc in the module")
check(dp._na("6517 Sample Rd") != dp._na("6519 Sample Rd"), "house numbers keep two homes on one road apart")
check(dp.no_dash("a " + chr(0x2014) + " b " + chr(0x2013) + " c") == "a ,  b - c", dp.no_dash("a — b – c"))
check(dp._short("5619 Sample Grove Pike") == "Sample Grove" and dp._short("122 Elm Ave") == "Elm", "tab names drop the house number")

print("test_dream100_dp: %d checks passed" % n)
