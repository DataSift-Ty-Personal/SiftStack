"""What each call outcome writes, as one table.

The texting app already has the table every texted "no" follows (src/sms_agent/dispositions.py: a stop,
a wrong number, a relative, a plain no, not now, a hand raise). A call outcome is the same fact arriving
by voice, so each row here names the texting kind it is (`sms`), and the texting app applies its OWN half
of that row when the dialer tells it about the call: the thread paused or closed, queued texts cancelled,
the Smarter Contact label and do-not-contact. The DataSift half (phone status, record status, tags,
lists) is written here, by the dialer, once.

The rules the rows encode, each with its reason:
  * Someone who answers about their own house has proved the line reaches the owner, so a no, a not now
    and a yes all mark the number Correct. A wrong number marks only the phone.
  * Family is never a wrong number (2026-11-19: eight numbers that reached an owner's children were filed
    as wrong). The phone is tagged Relative and the record keeps working.
  * A demand to be left alone stops every channel, mail included, exactly as a texted one does (decision
    D40): the phone goes do-not-call (keeping Correct as CORRECT_DNC), the record is tagged Do Not Market
    and put on the DNC list.
  * A reported death is a research lead, not a number to burn: Verify Deceased on the record (the gate
    then holds every channel until research settles it), Relative on the phone that answered.
  * A status is never moved down. "Call back later" on a Hot Lead keeps it a Hot Lead; "Interested" on a
    record that is already a lead leaves its status alone.
  * A no-answer or a voicemail writes no note (three hundred dials a day would bury the record). The
    attempt counter still moves, and a note the caller types is always kept.
"""
from __future__ import annotations

from dataclasses import dataclass

from sms_agent import gate_rules


@dataclass(frozen=True)
class Row:
    key: str
    label: str                    # the button
    group: str                    # contact | no_contact | person
    what: str                     # the rule in one sentence
    phone: str = ""               # CORRECT | WRONG | DEAD | DNC (keeps what was known) | "" untouched
    phone_tags: tuple = ()
    status: str = ""              # "" untouched | not_interested | follow_up | LEAD (the lead status setting)
    status_if: str = "always"     # always | not_lead (never moves a lead down)
    tags: tuple = ()
    lists: tuple = ()
    task: bool = False            # asks the caller for a call-back time
    note: str = "always"          # always | typed (only the caller's own words)
    needs_note: bool = False      # the caller must say who answered
    sms: str = ""                 # the texting app's kind (its own half of the same row)
    notify: bool = False          # tell the team (a lead)
    contact: bool = False         # a person who owns the house was reached


TABLE = {r.key: r for r in (
    Row("no_answer", "No answer", "no_contact", "Nobody picked up. The attempt counts; nothing else is written.",
        note="typed"),
    Row("voicemail", "Left voicemail", "no_contact", "A voicemail was left. The attempt counts; nothing else is written.",
        note="typed"),
    Row("bad_number", "Dead number", "no_contact", "The number does not work: it is marked Dead.",
        phone="DEAD"),
    Row("wrong_number", "Wrong number", "person", "A stranger answered: only the phone is marked Wrong.",
        phone="WRONG", sms="wrong_number"),
    Row("relative", "Family, not the owner", "person",
        "A relative of the owner answered. Family is never a wrong number: the phone is tagged Relative.",
        phone_tags=("Relative",), needs_note=True, sms="relative"),
    Row("owner_deceased", "Owner has died", "person",
        "Family says the owner died: Verify Deceased on the record holds every channel for research.",
        phone_tags=("Relative",), tags=("Verify Deceased",), needs_note=True, sms="bereavement"),
    Row("not_interested", "Not interested", "contact", "The owner said no: Not Interested, and the number is Correct.",
        phone="CORRECT", status="not_interested", status_if="not_lead", sms="plain_no", contact=True),
    Row("listed", "Listed with an agent", "contact", "The house is listed: Not Interested today, and the number is Correct.",
        phone="CORRECT", status="not_interested", status_if="not_lead", sms="listed", contact=True),
    Row("call_back", "Call back later", "contact", "Not now: Follow Up, a task at the time they gave, the number is Correct.",
        phone="CORRECT", status="follow_up", status_if="not_lead", task=True, sms="not_now", contact=True),
    Row("interested", "Interested (lead)", "contact", "The owner wants an offer: a lead, the number is Correct, the team is told.",
        phone="CORRECT", status="LEAD", status_if="not_lead", sms="hand_raise", notify=True, contact=True),
    Row("do_not_contact", "Do not contact", "contact",
        "They asked us to stop: do-not-call on the phone, Do Not Market and the DNC list, every channel stops.",
        phone="DNC", tags=("Do Not Market",), lists=("DNC",), sms="stop_all", contact=True),
)}

ORDER = tuple(TABLE)
NO_CONTACT = tuple(k for k, r in TABLE.items() if r.group == "no_contact")
SMS_KINDS = {"wrong_number", "relative", "bereavement", "plain_no", "listed", "not_now", "hand_raise", "stop_all"}


def suggest(end_status: str, sip_code: str = "", answered: bool = False, seconds: int = 0, amd: str = "",
            vm_dropped: bool = False) -> str:
    """What the caller most likely saw, so one click (or none) dispositions a call nobody answered."""
    if vm_dropped or (amd or "").startswith("machine"):
        return "voicemail"
    code = str(sip_code or "")
    if end_status == "failed" and code in ("404", "410", "484", "604"):
        return "bad_number"
    if end_status in ("no-answer", "busy", "canceled", "failed") or not answered:
        return "no_answer"
    return ""


def phone_target(row: Row, current: str) -> str:
    """The phone status this row writes, given what the phone says now ('' when nothing changes). A
    do-not-call keeps what was known (Correct becomes CORRECT_DNC); an opt-out is never undone by a
    later Correct."""
    cur = (current or "UNKNOWN").upper()
    if not row.phone:
        return ""
    if row.phone == "DNC":
        want = {"CORRECT": "CORRECT_DNC", "WRONG": "WRONG_DNC"}.get(cur, cur if cur in ("CORRECT_DNC", "WRONG_DNC", "DNC") else "DNC")
    else:
        want = row.phone
        if cur in ("DNC", "CORRECT_DNC", "WRONG_DNC"):
            return ""          # an opt-out sticks
        if want == "CORRECT" and cur == "CORRECT":
            return ""
    return "" if want == cur else want


def status_target(row: Row, current: str, lead_status: str) -> str:
    want = lead_status if row.status == "LEAD" else row.status
    if not want:
        return ""
    cur = (current or "").strip().lower()
    if row.status_if == "not_lead" and cur in gate_rules.LEAD_STATUSES:
        return ""
    return "" if cur == want.lower() else want


def describe() -> list[str]:
    return ["%s: %s" % (r.label, r.what) for r in TABLE.values()]
