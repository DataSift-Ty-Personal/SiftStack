"""One table for every no (build plan 0.4), and what a hand raise writes (2.3).

There are two texting paths. A reply to a smrtPhone text comes in through engine.py; a reply to a
Smarter Contact campaign comes in through sc_inbound.py. They grew separately, and by November
they disagreed about the same sentence. "Not interested" on Smarter Contact set the record Not
Interested, marked the number Correct and labeled the contact. "Not interested" on smrtPhone, at
the phase production runs, did nothing at all: the record stayed in Ready to Call and the same
owner was dialed the next morning.

So the answer to "what does this reply write" lives here, once, as a table. `decide()` turns a
classified reply into one KIND. `TABLE[kind]` says, as data, what that kind writes: the phone
status, the record status, the tags, the list, the local do-not-text reason, what happens to the
thread, the Smarter Contact label, and how far the stop reaches. `apply()` performs it. Both
paths call the same three functions, so they cannot drift again, and the selftest replays one
reply of each kind through both and compares the writes.

The rules the table encodes, each with its reason:

  * A texted demand to be left alone stops EVERY channel, mail included (decision D40, made
    2026-11-13: keep the rule this agent already had). The teaching says "never call me" still
    gets mail; a family that texts "leave us alone" does not want a letter either.
  * A plain no sets Not Interested and marks the number Correct, because someone who answers
    about their own house has proved the line reaches the owner. On SELLER threads only: a buyer
    who passes on one deal stays on the buyer list.
  * A hostile no does not mark the number Correct. Anger is not proof the person owns the house.
  * A bare "No" is Not Interested with the number left unverified (it can as well mean "not my
    number"), and the record is tagged for one confirmation call.
  * "Not now" goes to Follow Up, not to a status nothing works.
  * A wrong number marks only the phone. The stranger who answered said nothing about the owner.
  * A relative of the owner is a valid contact on an estate, not a wrong number: the phone is
    tagged Relative and a person takes the thread.
  * Mail Only is kept for a person who asked not to be called or texted, and for a reported
    death. A record with simply no number gets its own tag (config.TAG_NO_NUMBER), so new numbers
    from research can never reopen someone who asked.

The three oldest kinds (a stop, a wrong number, a sensitive reply) were already one shared piece
of code that both paths called. They keep that code: `apply()` hands them to it unchanged, and
their rows are the written statement of what it does, which the selftest holds it to.

Nothing here runs unless SMS_AGENT_DISPOSITION_TABLE is on. With it off each path keeps the
handling it had.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import classify, config, crm, escalate, gate_rules, revocation, store
from .knowledge import sc_replies

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Row:
    kind: str
    what: str                 # the rule in one plain sentence
    phone: str = ""           # CORRECT | WRONG | WRONG_DNC | DNC (keeps what was known: CORRECT becomes CORRECT_DNC) | "" untouched
    status: str = ""          # "" untouched | not_interested | follow_up | cold_lead
    tags: tuple = ()          # record tags, by role: DO_NOT_MARKET, MAIL_ONLY, HANDLED, ESCALATED, CONFIRM_CALL, OVER_VALUE, DEATH
    lists: tuple = ()         # DNC
    phone_tags: tuple = ()    # RELATIVE
    suppress: str = ""        # the local do-not-text reason; "" leaves the number textable
    thread: str = ""          # what the conversation becomes: opted_out | closed | paused | "" untouched
    scope: str = "record"     # phone: this number and nothing else. record: the record texted. everywhere: every record carrying the number
    stops_mail: bool = False  # the record is out of every channel, mail included
    revoke: str = ""          # what comes out of this owner's other queued texts: "" | stop | no | handoff | pause
    sc_label: str = ""        # the Smarter Contact label, by role
    sc_dnc: bool = False      # the contact goes on Smarter Contact's own do-not-contact list
    sc_reply: str = ""        # the one approved text back, if any
    notify: str = ""          # "" nobody | person | handoff | urgent
    seller_only: bool = False # a buyer's reply of this kind writes nothing to the record
    executor: str = ""        # the shared function that already performs this row ("" = performed here)


TABLE = {r.kind: r for r in (
    Row("stop_all", "A texted demand to be left alone stops every channel, mail included.",
        phone="DNC", tags=("DO_NOT_MARKET",), lists=("DNC",), suppress="opt_out", thread="opted_out",
        scope="everywhere", stops_mail=True, revoke="stop", sc_label="UNKNOWN_DNC", sc_dnc=True, executor="opt_out"),
    Row("wrong_stop", "A wrong number that also says stop: the phone is wrong and do-not-contact, and the stop still holds.",
        phone="WRONG_DNC", tags=("DO_NOT_MARKET",), lists=("DNC",), suppress="opt_out", thread="opted_out",
        scope="everywhere", stops_mail=True, revoke="stop", sc_label="WRONG_DNC", sc_dnc=True, executor="opt_out"),
    Row("wrong_number", "A wrong number marks only the phone.",
        phone="WRONG", suppress="wrong_number", thread="closed", scope="phone", sc_label="WRONG_NUMBER",
        sc_dnc=True, sc_reply="apology", executor="wrong_number"),
    Row("relative", "A relative of the owner is a valid contact, not a wrong number. A person takes the thread.",
        phone_tags=("RELATIVE",), thread="paused", revoke="pause", sc_label="RELATIVE", notify="person"),
    Row("legal_stop", "A lawyer, a regulator, a harassment claim or a child on the line: a hard stop everywhere.",
        phone="DNC", tags=("DO_NOT_MARKET",), lists=("DNC",), suppress="asked to be left alone", thread="opted_out",
        scope="everywhere", stops_mail=True, revoke="stop", sc_dnc=True, executor="sensitive"),
    Row("leave_alone", "A grieving or angry family that says leave us alone: every channel stops, mail included.",
        phone="DNC", tags=("DO_NOT_MARKET",), lists=("DNC",), suppress="asked to be left alone", thread="opted_out",
        stops_mail=True, revoke="stop", sc_dnc=True, executor="sensitive"),
    Row("bereavement", "A reported death is a probate lead, not a number to burn: mail only, and a person reads it.",
        tags=("MAIL_ONLY", "DEATH"), thread="paused", revoke="pause", executor="sensitive"),
    Row("sensitive_other", "A bankruptcy or a sale tomorrow: the agent stops and a person reads it.",
        tags=("MAIL_ONLY",), thread="paused", revoke="pause", executor="sensitive"),
    Row("crisis", "Self-harm language always reaches a person. It is never a marketing disposition.",
        thread="paused", revoke="pause", notify="urgent", executor="sensitive"),
    Row("plain_no", "A plain no sets Not Interested and marks the number Correct.",
        phone="CORRECT", status="not_interested", tags=("HANDLED",), thread="closed", revoke="no",
        sc_label="NOT_INTERESTED", seller_only=True),
    Row("hostile_no", "A hostile no sets Not Interested and does not mark the number Correct.",
        status="not_interested", tags=("HANDLED",), thread="closed", revoke="no",
        sc_label="NOT_INTERESTED", seller_only=True),
    Row("bare_no", "A bare No is Not Interested with the number unverified, and gets one confirmation call.",
        status="not_interested", tags=("HANDLED", "CONFIRM_CALL"), thread="closed", revoke="no",
        sc_label="NOT_INTERESTED", seller_only=True),
    Row("not_now", "Not now goes to Follow Up on a confirmed number.",
        phone="CORRECT", status="follow_up", tags=("HANDLED",), thread="closed", revoke="no",
        sc_label="NURTURE", seller_only=True),
    Row("hand_raise", "An interested reply is handed to a person at once. The agent stops texting.",
        phone="CORRECT", tags=("ESCALATED", "HANDLED"), thread="paused", revoke="handoff", notify="handoff",
        executor="hot_lead"),
    Row("ask_over_value", "An ask well over value: Not Interested today, a Cold lead when decision D14 says so.",
        phone="CORRECT", status="not_interested", tags=("HANDLED",), thread="closed", revoke="no",
        sc_label="NOT_INTERESTED", seller_only=True),
    Row("listed", "An interested reply on a listed house: Not Interested today, nurture when the setting names a status.",
        phone="CORRECT", status="not_interested", tags=("HANDLED",), thread="closed", revoke="no",
        sc_label="NOT_INTERESTED", seller_only=True),
)}

SENSITIVE = ("legal_stop", "leave_alone", "bereavement", "sensitive_other", "crisis")
NO_KINDS = ("plain_no", "hostile_no", "bare_no", "not_now")
STOP_KINDS = ("stop_all", "wrong_stop", "legal_stop", "leave_alone")

# Only the whole message: "No" alone, never "no, but call me next year".
BARE_NO = re.compile(r"^\s*(?:no+|nope|nah|naw|negative|no\s+(?:sir|ma'?am))\s*[.!]*\s*$", re.I)

# "Not now" is a later lead, not a no. Read only on a reply already classified as a soft no, and
# never when the same message carries a firm no ("not now, not ever").
NOT_NOW = re.compile(
    r"\b(?:not\s+(?:right\s+)?now|not\s+at\s+this\s+time|not\s+yet|not\s+ready|"
    r"maybe\s+(?:later|someday|one\s+day|next\s+year|in\s+a\s+(?:few|couple))|"
    r"(?:check|get|try|ask)\s+(?:back\s+)?(?:with\s+)?(?:me\s+)?(?:back\s+)?(?:later|next\s+year|in\s+a\s+(?:few|couple))|"
    r"in\s+a\s+(?:few|couple\s+of|couple)\s+(?:months|years)|"
    r"next\s+(?:year|spring|summer|fall|winter)|down\s+the\s+road|"
    r"when\s+(?:i|we)(?:'m|'re|\s+am|\s+are)?\s+ready)\b", re.I)

_KIN = (r"(?:son|daughter|wife|husband|brother|sister|mother|mom|mama|father|dad|daddy|grandson|granddaughter|"
        r"grandma|grandmother|grandpa|grandfather|niece|nephew|uncle|aunt|cousin|widow|spouse|parents?|"
        r"step ?(?:son|daughter|mom|dad|mother|father)|(?:son|daughter|mother|father|brother|sister)[- ]in[- ]law)")
# The sender says who they are to the owner, or whose house it is. Deliberately narrow: a wrong
# guess here keeps a stranger's number on the record as a contact.
RELATIVE = re.compile(
    r"\b(?:this\s+is|i\s+am|i'?m|im)\s+(?:his|her|their|the\s+owner'?s)\s+" + _KIN + r"\b"
    r"|\b(?:that'?s|that\s+is|that\s+was|it'?s|it\s+is|it\s+was|he\s+is|he'?s|she\s+is|she'?s|you\s+want|you\s+need)\s+my\s+"
    + _KIN + r"\b"
    r"|\bmy\s+(?:late\s+)?" + _KIN + r"(?:'s|s)?\s+(?:house|home|property|place|land|lot|number|phone)\b"
    r"|\bbelongs?\s+to\s+my\s+" + _KIN + r"\b", re.I)


@dataclass
class Env:
    """One reply, as either path hands it over."""
    phone: str
    record_uuid: str
    body: str
    result: Any = None                # the classification
    context: dict = field(default_factory=dict)
    matches: Optional[list] = None    # every record carrying this number, when the path looked
    program: str = "seller"
    inbox: str = "smrtPhone"          # where the thread lives, for the note
    allow_status: bool = True         # may this path flip a record status (the autonomy ladder)
    reason: str = ""                  # extra words for the note


@dataclass
class Outcome:
    kind: str
    action: str
    actions: list = field(default_factory=list)


def is_bare_no(body: str) -> bool:
    return bool(BARE_NO.match(body or ""))


def is_not_now(body: str) -> bool:
    return bool(NOT_NOW.search(body or "")) and not sc_replies.is_firm_no(body or "")


def is_relative(body: str) -> bool:
    return bool(RELATIVE.search(body or ""))


def is_hostile(body: str) -> bool:
    """Anger without a demand: an insult or an accusation in a reply that is otherwise a no."""
    from . import engine

    return bool(engine._HOSTILE_TONE_RX.search(body or ""))


def decide(intent: str, body: str, program: str = "seller") -> str:
    """The kind of reply this is, or "" when it is not a disposition (a question, small talk).

    Order matters and follows the classifier's own: a stop is decided before anything else, a
    sensitive reply next, and only then the softer readings.
    """
    from . import engine

    text = (body or "").strip()
    low = text.lower()
    if intent == "OPT_OUT":
        return "wrong_stop" if classify.classify_rules_wrong_number(text) else "stop_all"
    if intent == "ESCALATE":
        if engine._is_crisis(low):
            return "crisis"
        if classify._hit(low, classify.HOSTILE_NOW):
            return "legal_stop"
        if engine._wants_no_contact(low):
            return "leave_alone"
        if classify._hit(low, classify.BEREAVEMENT_NOW):
            return "bereavement"
        return "sensitive_other"
    if is_bare_no(text):
        # However sure the read: 85 bare "No"s read as wrong numbers got an apology and a
        # do-not-contact in four days (2026-10-30 to 11-02).
        return "bare_no"
    if intent in ("WRONG_NUMBER", "ASKING_WHO", "OTHER") and is_relative(text):
        # "That's my mom's house" denies owning it, which reads as a wrong number, and asks
        # nothing, which reads as small talk. It is neither: family on an estate is who signs.
        return "relative"
    if intent == "WRONG_NUMBER":
        return "wrong_number"
    if intent == "NOT_INTERESTED":
        if is_hostile(text):
            return "hostile_no"
        if is_not_now(text):
            return "not_now"
        return "plain_no"
    if intent == "INTERESTED":
        return "hand_raise"
    return ""


# ---------------------------------------------------------------------- names
def tag_names(roles) -> list[str]:
    """Record tag titles for a row's tag roles, as this install spells them."""
    names = {"DO_NOT_MARKET": config.TAG_OPT_OUT, "MAIL_ONLY": config.TAG_MAIL_ONLY, "HANDLED": config.TAG_AI_HANDLED,
             "ESCALATED": config.TAG_ESCALATED, "CONFIRM_CALL": config.TAG_CONFIRM_CALL,
             "OVER_VALUE": config.TAG_OVER_VALUE, "DEATH": config.TAG_DEATH_REPORTED}
    return [names[r] for r in roles if names.get(r)]


def sc_label(kind: str) -> str:
    """The Smarter Contact label a kind sets ("" for none).

    The three labels the plan adds (and Nurture) are used only once SMARTERCONTACT_EXTRA_LABELS
    is on, because a label the account does not have fails the write. Until then a wrong number
    that says stop keeps the label it gets today, and the others set none.
    """
    role = TABLE[kind].sc_label
    today = {"NOT_INTERESTED": sc_replies.LABELS["NOT_INTERESTED"], "WRONG_NUMBER": sc_replies.LABELS["WRONG_NUMBER"],
             "UNKNOWN_DNC": sc_replies.LABELS["OPT_OUT"]}
    if role in today:
        return today[role]
    if not config.SC_EXTRA_LABELS:
        return sc_replies.LABELS["OPT_OUT"] if role == "WRONG_DNC" else ""
    return {"WRONG_DNC": config.SC_LABEL_WRONG_DNC, "CORRECT": config.SC_LABEL_CORRECT,
            "RELATIVE": config.SC_LABEL_RELATIVE, "NURTURE": config.SC_LABEL_NURTURE}.get(role, "")


def status_value(kind: str) -> str:
    """The record status a kind writes, as the account stores it."""
    role = TABLE[kind].status
    if kind == "ask_over_value" and config.OVER_VALUE == "cold_lead":
        return config.COLD_LEAD_STATUS
    if kind == "listed" and config.LISTED_STATUS:
        return config.LISTED_STATUS
    return {"not_interested": config.SC_NI_STATUS, "follow_up": config.NOT_NOW_STATUS,
            "cold_lead": config.COLD_LEAD_STATUS}.get(role, "")


# ---------------------------------------------------------------------- the writes
def record_status(record_uuid: str) -> str:
    status = (crm.get_record(record_uuid) or {}).get("status")
    if isinstance(status, dict):
        status = status.get("slug") or status.get("title") or status.get("value")
    return str(status or "").strip().lower()


def set_status(record_uuid: str, want: str, inbox: str, said: str) -> str:
    """Write a status, but never over a record somebody is working.

    Only a record no one has touched (no status, or prospecting) is flipped. A sold, contracted
    or live-lead record keeps its status and gets a note saying what the owner texted.
    """
    from . import engine

    current = record_status(record_uuid)
    if current == want.strip().lower():
        return f"status already {current}"
    if current not in config.SC_NI_OVERWRITE:
        crm.post_note(record_uuid, f"Texted '{said}' in {inbox}; status left as {current} "
                                   "because someone is working this record.")
        return f"status left as {current} (record is being worked)"
    return f"status -> {want}: {engine._report(crm.set_status(record_uuid, want))}"


def _no(kind: str, env: Env) -> Outcome:
    """The no family: plain, hostile, bare, not now, an ask over value, a listed house."""
    from . import engine

    row = TABLE[kind]
    if row.seller_only and env.program in ("buyer", "dispo"):
        # No status, no close, no change to the number: the next deal still goes to them.
        return Outcome(kind, "buyer_passed", ["a buyer passed on this deal; left on the buyer list, nothing written"])

    label = "firm no" if sc_replies.is_firm_no(env.body) else "soft no"
    if kind == "not_now":
        label = "soft no (not now)"
    words = {"hostile_no": "hostile, so the number is not marked Correct",
             "bare_no": "bare no, may be a wrong number", "not_now": "said not now",
             "ask_over_value": "asked well over value", "listed": "the house is listed"}.get(kind, "")
    note = f'{label} by text in {env.inbox}: "{env.body[:150]}"' + "".join(
        f" ({w})" for w in (words, env.reason) if w)
    verified = row.phone == "CORRECT"
    acts = [engine._close_not_interested(env.phone, env.record_uuid, note, verified=verified)]
    extra = [t for t in tag_names(row.tags) if t != config.TAG_AI_HANDLED]
    if kind == "ask_over_value" and config.OVER_VALUE == "cold_lead":
        extra.append(config.TAG_OVER_VALUE)
    if extra:
        acts.append(f"tagged {', '.join(extra)}: {engine._report(crm.add_tags(env.record_uuid, extra))}")
    want = status_value(kind)
    if want and env.allow_status:
        acts.append(set_status(env.record_uuid, want, env.inbox, "not now" if kind == "not_now" else "not interested"))
    elif want:
        acts.append(f"status not changed (phase {config.PHASE}: the agent may not flip a status yet)")
    action = {"not_now": "follow_up"}.get(kind, "not_interested")
    if want and want == config.COLD_LEAD_STATUS and kind == "ask_over_value":
        action = "cold_lead"
    elif want and kind == "listed" and want != config.SC_NI_STATUS:
        action = "nurture"
    return Outcome(kind, action, acts)


def _relative(env: Env) -> Outcome:
    """A relative answered. Keep the number, mark whose it is, and hand the thread to a person."""
    from . import engine

    acts = []
    tag = config.TAG_RELATIVE
    if tag and env.record_uuid:
        acts.append(f"phone tagged {tag}: {engine._report(crm.add_phone_tag(env.record_uuid, env.phone, tag))}")
        crm.post_note(env.record_uuid, f'{env.phone} is a relative of the owner, by text in {env.inbox}: '
                                       f'"{env.body[:180]}". The number was kept, not marked wrong.')
    store.pause_conversation(env.phone, "a relative of the owner replied")
    store.cancel_queued(env.phone, "a relative replied")
    if config.PHASE >= 2 and store.mark_notified(env.phone, "relative"):
        ctx = env.context or {}
        where = ", ".join(x for x in (ctx.get("street"), ctx.get("city")) if x)
        lines = [f"{escalate._fmt_phone(env.phone)}" + (f"  {where}" if where else ""), f"> {env.body[:300]}"]
        if ctx.get("reply_link"):
            lines.append(f"<{ctx['reply_link']}|Open the conversation in {env.inbox}>")
        lines.append("_The agent has not answered. This is family, not the owner: read the record first._")
        ok = escalate.alert(f"{config.HANDOFF_NAME}: a relative of the owner replied", "\n".join(lines),
                            env.record_uuid, kind="needs_reply")
        acts.append("posted: a relative replied" if ok else "notify FAILED")
    return Outcome("relative", "relative", acts)


def apply(kind: str, env: Env) -> Outcome:
    """Perform one row of the table for one reply."""
    from . import engine

    if kind not in TABLE:
        raise ValueError("unknown disposition %r" % kind)
    row = TABLE[kind]
    if row.executor == "opt_out":
        out = Outcome(kind, "opted_out", engine._do_opt_out(env.phone, env.record_uuid, env.matches, env.body))
    elif row.executor == "wrong_number":
        out = Outcome(kind, "wrong_number",
                      engine._do_wrong_number(env.phone, env.record_uuid, env.body, env.context, env.matches))
    elif row.executor == "sensitive":
        out = Outcome(kind, "escalated",
                      engine._do_sensitive(env.phone, env.record_uuid, env.body, env.result, env.matches))
        out.actions += _top_up(kind, env)
    elif kind == "relative":
        out = _relative(env)
        out.actions += revocation.on_event("pause", env.phone, env.record_uuid, why="a relative replied")
    elif kind == "hand_raise":
        raise ValueError("a hand raise is performed by the path itself (engine._do_hot_lead)")
    else:
        out = _no(kind, env)
    if is_relative(env.body) and kind in ("bereavement", "sensitive_other", "plain_no", "not_now"):
        out.actions += mark_relative(env)
    return out


def _top_up(kind: str, env: Env) -> list[str]:
    """What a sensitive row declares that the shared handler does not write on its own.

    "Leave this family alone" tagged the record Do Not Market and stopped this app, but left the
    phone unmarked in Sift and off the DNC list, so a dialer could still reach it. Every stop in
    the table writes the same things, so that gap is closed here. A reported death can also tag
    the record for research when the install names a tag for it.
    """
    from . import engine

    acts = []
    if not env.record_uuid or config.PHASE < 2:
        return acts
    if kind == "leave_alone":
        status = crm.dnc_status(env.record_uuid, env.phone)
        acts.append(f"phone -> {status}: {engine._report(crm.set_phone_status(env.record_uuid, env.phone, status))}")
        acts.append(f"list {config.LIST_DNC}: {engine._report(crm.add_list(env.record_uuid, config.LIST_DNC))}")
    if kind == "bereavement" and config.TAG_DEATH_REPORTED:
        acts.append(f"tagged {config.TAG_DEATH_REPORTED} for research: "
                    f"{engine._report(crm.add_tags(env.record_uuid, [config.TAG_DEATH_REPORTED]))}")
    return acts


def mark_relative(env: Env) -> list[str]:
    """Tag the phone Relative on a reply that is something else first (a death, a no)."""
    from . import engine

    if not (config.TAG_RELATIVE and env.record_uuid):
        return []
    return [f"phone tagged {config.TAG_RELATIVE}: "
            f"{engine._report(crm.add_phone_tag(env.record_uuid, env.phone, config.TAG_RELATIVE))}"]


# ---------------------------------------------------------------------- a hand raise (2.3)
def hand_raise(phone: str, record_uuid: str, body: str, program: str = "", context: Optional[dict] = None) -> list[str]:
    """Set New Lead on an interested reply, never over a status that already means lead.

    The agent stopped doing this on 2026-08-11: the status change fired a sequence that assigned
    the record to someone other than the person who had just been paged, and the hottest leads
    were the ones that vanished from her queue. With the alert and the sequence naming the same
    person (plan 2.1) the status can be set again, which is what puts the lead on the board with
    a task and a clock.

    A record that already holds a lead status is NOT set back to New Lead: that would restart
    its clock and could make a second card. It is tagged, and the lead manager is told to move
    the card back. A sold, closed or opted-out record is left for a person to look at.
    """
    from . import engine

    if not config.HAND_RAISE_NEW_LEAD or program in ("buyer", "dispo") or not record_uuid or config.PHASE < 2:
        return []
    want = config.NEW_LEAD_STATUS
    current = record_status(record_uuid)
    if current == want.strip().lower():
        return [f"status already {want}"]
    if current in gate_rules.LEAD_STATUSES:
        acts = [f"already a lead ({current}): New Lead NOT set"]
        if config.TAG_REPLY_ON_LEAD:
            acts.append(f"tagged {config.TAG_REPLY_ON_LEAD}: "
                        f"{engine._report(crm.add_tags(record_uuid, [config.TAG_REPLY_ON_LEAD]))}")
        if store.mark_notified(phone, "lead_reply_" + store.now()[:10]):
            ctx = context or {}
            where = ", ".join(x for x in (ctx.get("street"), ctx.get("city")) if x)
            ok = escalate.alert(
                f"{config.LEAD_CALLER_NAME}: a lead texted again",
                f"{escalate._fmt_phone(phone)}" + (f"  {where}" if where else "") + f"\n> {body[:300]}\n"
                f"_Already a lead ({current}). Move its card back to New Lead and call._",
                record_uuid, kind="handoff")
            acts.append("lead manager told to move the card back" if ok else "lead manager alert FAILED")
        return acts
    if current in config.NEW_LEAD_PROTECTED:
        crm.post_note(record_uuid, f"Interested text from {phone}, but the record reads {current}. "
                                   "Status left alone for a person to look at.")
        return [f"status left as {current} (a person looks first)"]
    return [f"status -> {want}: {engine._report(crm.set_status(record_uuid, want))}"]


def skip_notice(phone: str, record_uuid: str, body: str, context: dict, why: str, action: str) -> list[str]:
    """Stop a handoff that should not happen after all.

    A burst still in its debounce simply never posts. When the "yes" was already posted and the
    reason not to call arrives after it (the price, in the next text), the channel gets one line
    so nobody spends the call.
    """
    acts = []
    if store.escalation_posted(phone):
        where = (context or {}).get("street") or escalate._fmt_phone(phone)
        marked = {"cold_lead": "Passed as a Cold lead", "nurture": "Sent to nurture",
                  "follow_up": "Sent to Follow Up"}.get(action, "Marked Not Interested")
        ok = escalate.alert(f"{config.HANDOFF_NAME}: skip {where}",
                            f"> {body[:200]}\n_Not worth the call: {why}. {marked}._",
                            record_uuid, kind="handoff")
        acts.append("posted: skip this lead" if ok else "skip notice FAILED")
    store.clear_escalation(phone)
    return acts


def lead_quality(record_uuid: str, body: str) -> tuple[str, str]:
    """(kind, why) when an interested reply is not worth the call today, ("", "") when it is.

    The rule is the Smarter Contact path's own (Ty, 2026-10-30): a house on the market, or an ask
    above the ceiling times the record's value. With the table on, a smrtPhone reply is held to
    the same rule, so the same sentence cannot be a lead on one path and a no on the other.
    """
    from . import sc_inbound

    category, why = sc_inbound.push_verdict(record_uuid, body)
    if not category:
        return "", ""
    return ("ask_over_value" if category == "over_value" else "listed"), why


def describe() -> list[str]:
    """The table as plain lines, for the CLI and the docs."""
    out = []
    for kind, row in TABLE.items():
        writes = [x for x in (
            f"phone {row.phone}" if row.phone else "",
            f"status {row.status}" if row.status else "",
            ("tags " + ", ".join(tag_names(row.tags))) if tag_names(row.tags) else "",
            ("list " + ", ".join(config.LIST_DNC if x == "DNC" else x for x in row.lists)) if row.lists else "",
            ("phone tag " + config.TAG_RELATIVE) if row.phone_tags else "",
            f"suppressed as {row.suppress}" if row.suppress else "",
            f"thread {row.thread}" if row.thread else "",
            "mail stops too" if row.stops_mail else "",
            f"Smarter Contact label {sc_label(kind)}" if sc_label(kind) else "",
            "Smarter Contact do-not-contact" if row.sc_dnc else "",
            "seller threads only" if row.seller_only else "",
        ) if x]
        out.append(f"{kind}: {row.what}")
        out.append("    " + ("; ".join(writes) if writes else "nothing written"))
    return out
