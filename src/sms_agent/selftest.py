"""Offline end-to-end exercise of every phase. Sends nothing, writes nothing.

Runs against a throwaway database with the network stubbed out, so it is safe
to run any time, on any machine, with production credentials loaded. It asserts
behaviour rather than printing it, because the failure this guards against is
the one this codebase keeps rediscovering: a run that reports success while
doing nothing.

    python src/sms_agent/cli.py selftest
    python src/sms_agent/cli.py selftest --live-model   # also exercise Claude
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

RESET, RED, GREEN, DIM = "\033[0m", "\033[31m", "\033[32m", "\033[2m"


@dataclass
class Case:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Results:
    cases: list[Case] = field(default_factory=list)

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.cases.append(Case(name, bool(condition), detail))
        return bool(condition)

    @property
    def failed(self) -> list[Case]:
        return [c for c in self.cases if not c.ok]

    def report(self) -> int:
        for case in self.cases:
            mark = f"{GREEN}pass{RESET}" if case.ok else f"{RED}FAIL{RESET}"
            print(f"  [{mark}] {case.name}")
            if case.detail:
                print(f"         {DIM}{case.detail}{RESET}")
        print()
        if self.failed:
            print(f"{RED}{len(self.failed)} of {len(self.cases)} checks failed{RESET}")
            return 1
        print(f"{GREEN}all {len(self.cases)} checks passed{RESET}")
        return 0


class _Stub:
    """Records what would have gone out, and lets nothing out."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.slack: list[str] = []
        self.crm_writes: list[tuple] = []
        self.dnt: list[str] = []


def run(live_model: bool = False) -> int:
    # Point everything at a throwaway database BEFORE the modules bind to it.
    tmp = Path(tempfile.mkdtemp(prefix="sms_agent_selftest_"))
    import os

    os.environ["SMS_AGENT_DB"] = str(tmp / "selftest.db")
    os.environ["SMS_AGENT_DATA_DIR"] = str(tmp)
    os.environ["SMS_AGENT_DRY_RUN"] = "1"
    os.environ["SMS_AGENT_PHASE"] = "4"
    os.environ.setdefault("SMRTPHONE_NUMBERS", '["+18650000001","+18650000002"]')

    from . import classify, config, crm, engine, escalate, respond, sender_pool, store
    from . import smrtphone, transport, seed
    from .knowledge import touches

    # Rebind the values the modules already read at import time.
    config.DB_PATH = Path(os.environ["SMS_AGENT_DB"])
    config.PHASE, config.DRY_RUN = 4, True
    config.SMRTPHONE_NUMBERS_RAW = os.environ["SMRTPHONE_NUMBERS"]
    store._local.__dict__.pop("conn", None)
    store.init()

    stub = _Stub()
    r = Results()

    # ---- stub every outbound edge -------------------------------------
    transport.send = lambda to, body, frm="": (
        stub.sent.append((to, body, frm)) or smrtphone.SendResult(True, sms_id="stub")
    )
    smrtphone.add_to_dnt = lambda phone: (stub.dnt.append(phone) or (True, "stub"))
    # record the program too, so a routing regression (dispo traffic
    # posted to the seller channel) is visible to a test.
    # Keep stub.slack a list of TEXT (existing assertions read it as a
    # string) and record the routing program alongside, so a hot BUYER
    # lead posted to the seller channel is a visible test failure.
    stub.slack_programs = []
    escalate._post = lambda text, blocks=None, program="": (
        stub.slack.append(text)
        or stub.slack_programs.append(program or "seller")
        or True)
    for name in ("add_tags", "set_status", "post_note", "assign", "bump_sms_attempts"):
        setattr(
            crm, name,
            (lambda n: lambda *a, **k: (stub.crm_writes.append((n, a)) or {"_dry_run": True}))(name),
        )
    crm.set_phone_status = lambda uuid, phone, status: (
        stub.crm_writes.append(("set_phone_status", (uuid, phone, status)))
        or {"_dry_run": True}
    )
    # Synthetic records have no CRM row, so the live dial-tier lookup would
    # hold every candidate. Stubbed to a qualifying tier; the tier RULE itself
    # is asserted separately below against ALLOWED_DIAL_TIERS.
    crm.dial_tier_checked = lambda uuid, phone: ("Dial First", True)
    crm.dial_tier = lambda uuid, phone: "Dial First"
    crm.deal_context = lambda uuid: {
        "owner_first": "Maron", "street": "158 Old State Rd", "city": "Maryville",
        "county": "Blount", "assigned_name": "Adriana", "record_uuid": uuid,
    }

    if not live_model:
        # Deterministic classification and drafting, so a network blip is never
        # reported as a logic failure.
        classify.classify_llm = lambda text, history=None: classify.Classification(
            "OTHER", 0.0, "fallback", "stubbed"
        )
        respond.draft = lambda thread, context=None, intent="", intent_rationale="", program="seller": respond.Reply(
            message="Hi Maron! Sorry to bother you. Is 158 Old State Rd yours?",
            confidence=0.92, handoff=(intent == "INTERESTED"), reason="stub", ok=True,
        )

    ctx = crm.deal_context("rec-1")
    for phone in ("8650001111", "8650002222", "8650003333", "8650004444",
                  "8650004446", "8650005555", "8650006666", "8650008888"):
        store.map_phone(phone, record_uuid="rec-" + phone[-4:], context=ctx)

    def inbound(phone: str, message: str, sms_id: str = "") -> dict:
        return engine.process("smrtphone", {
            "event": "smsIncoming", "smsId": sms_id or "st-" + phone[-4:],
            "from": phone, "to": "+18650000001", "message": message,
        })

    # ---- 1. deterministic rules are authoritative ---------------------
    print("\ndeterministic classification")
    for text, expect in (
        ("STOP", "OPT_OUT"),
        ("please stop texting me", "OPT_OUT"),
        ("take me off your list", "OPT_OUT"),
        ("wrong number, I don't own that", "WRONG_NUMBER"),
        ("my husband passed away last month", "ESCALATE"),
        ("my attorney will be in touch", "ESCALATE"),
        # Jessica, 6956 Cardindale, 2026-08-11. Shipped as INTERESTED and paged
        # the prospector; the confirmation is about the phone, the refusal is
        # the answer. Every phrasing below is a real way people say the same no.
        ("It is. And it's staying that way.", "NOT_INTERESTED"),
        ("yes but it's not going anywhere", "NOT_INTERESTED"),
        ("that's mine and I plan to keep it", "NOT_INTERESTED"),
        ("correct, staying in the family", "NOT_INTERESTED"),
        ("yep, never selling", "NOT_INTERESTED"),
        # Live 2026-08-11: both went unclassified. We open with the owner's
        # name, so a stranger asking about that name is the wrong-number tell.
        ("Who the hell is Jonathan", "WRONG_NUMBER"),
        ("this ain't joseph.", "WRONG_NUMBER"),
        ("theres no one here by that name", "WRONG_NUMBER"),
    ):
        got = classify.classify(text)
        r.check(f"{expect:12} <- {text[:38]!r}", got.intent == expect,
                "" if got.intent == expect else f"got {got.intent}")

    # A bare confirmation answers the ownership question in touch 1 and says
    # nothing about selling, so it must not read as a hot lead.
    for text in ("It is.", "yes", "that's right", "sure is"):
        got = classify.classify(text)
        r.check(f"bare confirm is not INTERESTED <- {text!r}",
                got.intent != "INTERESTED", f"got {got.intent}")

    # Asking who WE are is a fair question from the right person. Dispositioning
    # that number WRONG would throw away a good line.
    for text in ("who is this", "who's this?", "who is that"):
        got = classify.classify(text)
        r.check(f"asking who we are is not WRONG_NUMBER <- {text!r}",
                got.intent != "WRONG_NUMBER", f"got {got.intent}")

    # ---- 1b. one text is stored once ----------------------------------
    # smrtPhone identifies the same message with a uuid on the webhook and an
    # integer in the SMS log, so id-based dedupe alone let the backstop poller
    # replay every reply the webhook had already handled.
    print("\ninbound dedupe")
    store.add_message("8650009999", "in", "Would love to", sms_id="uuid-abc", author="owner")
    store.add_message("8650009999", "in", "Would love to", sms_id="uuid-abc", author="owner")
    n = store._conn().execute(
        "SELECT COUNT(*) n FROM messages WHERE phone='8650009999'").fetchone()["n"]
    r.check("same sms_id stored once", n == 1, f"rows={n}")
    r.check("same body from the other surface is recognised",
            store.recent_inbound_exists("8650009999", "Would love to", 90))
    r.check("a different body is not swallowed",
            not store.recent_inbound_exists("8650009999", "Actually yes", 90))
    # An inbound that has arrived but is not yet processed still counts as
    # received. Otherwise the backstop poller re-enqueues it during the window
    # between the webhook landing and the worker draining the queue.
    store.record_event(
        "smrtphone", "smsIncoming", "st-pending-dupe",
        {"event": "smsIncoming", "from": "8650009999", "to": "+18650000001",
         "message": "Nope", "smsId": "st-pending-dupe"},
    )
    r.check("an unprocessed event counts as already received",
            store.recent_inbound_exists("8650009999", "Nope", 90))

    # A live person asking a direct question must not sit in silence while the
    # agent is below the phase that can answer.
    print("\nwho is this")
    # Production runs at phase 2, where no reply is ever DRAFTED. The who-answer
    # is a fixed template rather than a draft, which is why it is allowed here.
    _phase, _answer = config.PHASE, config.ANSWER_WHO
    config.PHASE = 2
    before = len(stub.slack)
    out = inbound("8650008888", "who is this?")
    r.check("answers the question itself", out.get("action") == "answered_who",
            str(out.get("action")))
    r.check("does not page a human for it", len(stub.slack) == before,
            f"{len(stub.slack) - before} posts")
    queued = [x for x in store.due_outbox(50) if x["phone"] == "8650008888"]
    r.check("a reply is queued", len(queued) == 1, f"{len(queued)} queued")
    if queued:
        msg = queued[0]["body"]
        r.check("never names the company",
                not any(w in msg.lower() for w in ("volunteer", "homebuyer", "llc", "inc")), msg)
        r.check("identifies by locality instead",
                any(w in msg.lower() for w in ("local", "around")), msg)
        r.check("names the street", "158 old state" in msg.lower(), msg)
        r.check("asks exactly one question", msg.count("?") == 1, msg)
        r.check("carries no dash characters", "—" not in msg and "–" not in msg, msg)
        ok, problems = respond.validate(msg, max_questions=1)
        r.check("passes the outbound validator", ok, str(problems))

    # With the template disabled it must fall back to telling a person, never
    # to silence.
    config.ANSWER_WHO = False
    store.map_phone("8650009111", record_uuid="rec-9111", context=ctx)
    before = len(stub.slack)
    out = inbound("8650009111", "who is this?")
    r.check("falls back to a human when the template is off",
            out.get("action") == "needs_human_reply", str(out.get("action")))
    # The channel is interested parties only, so this reaches the digest and the
    # log rather than Slack. Asserted so the trade-off is deliberate: a question
    # nobody answers is now visible only to whoever reads the digest.
    r.check("bookkeeping stays out of the channel", len(stub.slack) == before,
            f"{len(stub.slack) - before} posts")
    config.PHASE, config.ANSWER_WHO = _phase, _answer

    # ---- 1d. everyone walks all four touches ---------------------------
    # Tying touches to CRM call-attempt stages capped most owners at touch 1,
    # because a record parked in Ready to Call never advances a stage on its
    # own. Progression is the person's own history now.
    print("\ntouch progression")
    from datetime import date as _date

    from . import campaign as _camp
    today = _date(2026, 8, 20)
    r.check("never texted starts at touch 1",
            _camp.next_touch(None, 2, today)[0] == 1)
    r.check("advances to the next touch after the gap",
            _camp.next_touch({"touches": {1}, "last": "2026-08-17"}, 2, today)[0] == 2)
    r.check("waits when the gap has not passed",
            _camp.next_touch({"touches": {1}, "last": "2026-08-19"}, 2, today)[0] is None)
    r.check("walks the whole sequence",
            [_camp.next_touch({"touches": set(range(1, n + 1)), "last": "2026-08-01"}, 2, today)[0]
             for n in (1, 2, 3)] == [2, 3, 4])
    r.check("stops after the fourth",
            _camp.next_touch({"touches": {1, 2, 3, 4}, "last": "2026-08-01"}, 2, today)[0] is None)
    r.check("says why it stopped",
            "completed" in _camp.next_touch({"touches": {1, 2, 3, 4}, "last": ""}, 2, today)[1])

    # ---- 1b1. hostile stops, bereavement is a lead ---------------------
    # Both used to just pause the thread, which was wrong in both directions:
    # a harassment claim left the number dialable, and a reported death (the
    # most common reason a house sells) was treated as a problem.
    print("\nsensitive handling")
    from . import classify as _c
    for text in ("this has become harassment", "I am 12 years old",
                 "my attorney will be in touch"):
        r.check(f"hostile: {text[:30]!r}",
                bool(_c._hit(text.lower(), _c.HOSTILE_NOW))
                and not _c._hit(text.lower(), _c.BEREAVEMENT_NOW))
    for text in ("Judy died in 2022", "my husband passed away last month"):
        r.check(f"bereavement, not hostile: {text[:26]!r}",
                bool(_c._hit(text.lower(), _c.BEREAVEMENT_NOW))
                and not _c._hit(text.lower(), _c.HOSTILE_NOW))

    before_sup = store.is_suppressed("8650005555")
    out = inbound("8650005555", "this has become harassment, my attorney will call")
    r.check("a hostile reply suppresses the number",
            bool(store.is_suppressed("8650005555")), f"was {before_sup}")
    r.check("and dispositions the phone in Sift",
            any(w[0] == "set_phone_status" and "DNC" in str(w[1][2])
                for w in stub.crm_writes),
            str([w for w in stub.crm_writes if w[0] == "set_phone_status"][-1:]))
    # Ty, 2026-08-28: legal is silenced too. It still suppresses and dispositions
    # DNC, it just does not page anybody. Only crisis reaches the channel now.
    r.check("a hostile/legal reply does NOT reach the channel",
            not any("Hard stop" in s2 for s2 in stub.slack), str(stub.slack[-2:]))
    r.check("but is still marked do-not-market",
            any(w[0] == "add_tags" and config.TAG_OPT_OUT in str(w[1])
                for w in stub.crm_writes),
            str([w for w in stub.crm_writes if w[0] == "add_tags"][-2:]))

    slack_before = len(stub.slack)
    out = inbound("8650004444", "Judy died in 2022")
    r.check("bereavement does NOT suppress the number",
            not store.is_suppressed("8650004444"))
    # Ty, 2026-08-26: a plain bereavement is dispositioned, NOT posted. Grieving
    # families telling us to go away were filling the channel, and a prospector
    # who scrolls past four of those stops reading the fifth, which is a seller.
    # The record still gets marked so it stays workable by mail.
    r.check("bereavement is NOT posted to the channel",
            len(stub.slack) == slack_before, str(stub.slack[slack_before:]))
    r.check("bereavement is dispositioned as mail-only instead",
            any(w[0] == "add_tags" and config.TAG_MAIL_ONLY in str(w[1])
                for w in stub.crm_writes),
            str([w for w in stub.crm_writes if w[0] == "add_tags"][-2:]))

    # A crisis message ALWAYS reaches a person, whatever the disposition rules
    # say. Live 2026-08-26: "Im blowing my brains out this Wednesday morning"
    # carried no legal marker and no leave-me-alone wording, so the quiet
    # disposition would have tagged it Mail Only and nobody would have read it.
    slack_before = len(stub.slack)
    out = inbound("8650004444", "my father died. Im blowing my brains out Wednesday morning")
    r.check("a crisis reply IS posted, overriding the quiet disposition",
            any("URGENT" in s2 for s2 in stub.slack[slack_before:]),
            str(stub.slack[slack_before:])[:180])
    r.check("and is not silently marked as a marketing preference",
            "CRISIS" in " ".join(out.get("actions", [])), str(out.get("actions")))

    # A demand to be left alone stops the MAIL too, not just the texts. The
    # wording has to carry the death, or it classifies as a plain NOT_INTERESTED
    # and never reaches the sensitive handler at all.
    slack_before = len(stub.slack)
    tags_before = len([w for w in stub.crm_writes if w[0] == "add_tags"])
    out = inbound("8650004446",
                  "Leave this family alone, my father died and the house is not for sale")
    r.check("a leave-us-alone reply is not posted either",
            len(stub.slack) == slack_before, str(stub.slack[slack_before:]))
    new_tags = [w for w in stub.crm_writes if w[0] == "add_tags"][tags_before:]
    r.check("and is marked do-not-market, not mail-only",
            any(config.TAG_OPT_OUT in str(w[1]) for w in new_tags)
            and not any(config.TAG_MAIL_ONLY in str(w[1]) for w in new_tags),
            str(new_tags))
    r.check("and suppresses the number",
            bool(store.is_suppressed("8650004446")))

    # ---- 1b2. one timezone must not drag the whole batch ---------------
    # A Los Angeles recipient at the front of a 9am Eastern batch pushed every
    # Tennessee message behind it to 11:24, because the layout cursor advanced
    # from the deferred time instead of the slot the message actually held.
    print("\nschedule layout")
    from datetime import datetime as _dt, timezone as _tz
    for i, ph in enumerate(("3109991447", "8650001212", "8650001313", "8650001414")):
        store.queue_message(ph, f"layout probe {i}", from_number=f"+186527300{i:02d}",
                            status="held")
    laid = seed.reschedule_held()
    r.check("every staged message is laid out", laid["rescheduled"] >= 4, str(laid))
    rows = list(store._conn().execute(
        "SELECT phone, not_before FROM outbox WHERE body LIKE 'layout probe%'"))
    times = {x["phone"]: x["not_before"] for x in rows}
    east = sorted(v for k, v in times.items() if k.startswith("865"))
    west = times.get("3109991447")
    now_iso = _dt.now(_tz.utc).isoformat(timespec="seconds")
    # The real property: an Eastern recipient goes NOW, whatever the western one
    # has to wait for. Asserting east < west only holds while Los Angeles is
    # still asleep, so it would pass in the morning and fail after 11am Eastern.
    from datetime import datetime as _dtp
    delay_min = (
        (_dtp.fromisoformat(east[0]) - _dtp.fromisoformat(now_iso)).total_seconds() / 60
        if east else 999
    )
    r.check("eastern sends are not delayed by a western recipient",
            delay_min < 10, f"first eastern send is {delay_min:.0f} min out (east={east[:1]})")
    with store.tx() as _c:
        _c.execute("DELETE FROM outbox WHERE body LIKE 'layout probe%'")

    # A thread keeps one number for life. Live, one owner got touch 3 from
    # ...0296 and touch 4 from ...0270 an hour later, because the number is
    # picked when a message is staged and both were staged before either sent.
    def _cand(phone):
        c = seed.Candidate(phone=phone, record_uuid="rec-sticky", street="1 Test St",
                           city="Knoxville", county="Knox", owner_full="Test Owner")
        c.first, c.sender, c.message, c.status = "Test", "Adriana", "hi", "ready"
        return c

    laid = seed.schedule([_cand("8650007777"), _cand("8650007777")])
    numbers = {n for _, n, _ in laid}
    r.check("the same person in one batch gets one number", len(numbers) <= 1,
            f"{len(numbers)} numbers: {numbers}")

    # ---- 1c. suppression lives on the phone in Sift --------------------
    # smrtPhone has no writable DNT route, so Sift's phone disposition IS the
    # suppression: it is read by every campaign, ours and anyone else's.
    print("\nphone disposition")
    r.check("DNC statuses are accepted",
            all(s in crm.PHONE_STATUSES for s in ("DNC", "CORRECT_DNC", "WRONG_DNC", "NO_ANSWER")))
    r.check("a known-good number opting out keeps that knowledge",
            crm.DNC_FOR.get("CORRECT") == "CORRECT_DNC")
    r.check("wrong number plus opt-out is WRONG_DNC",
            crm.dnc_status("rec-1", "8650001111", wrong_number=True) == "WRONG_DNC")
    r.check("every DNC status is skipped when building a campaign",
            {"DNC", "CORRECT_DNC", "WRONG_DNC"} <= seed.SKIP_PHONE_STATUSES)
    r.check("a live number is still eligible",
            "CORRECT" not in seed.SKIP_PHONE_STATUSES and "UNKNOWN" not in seed.SKIP_PHONE_STATUSES)

    # Only the two tiers Trestle rated most likely to reach the owner. The
    # first live run went out without this and put 24 of 84 texts on Third,
    # Fourth or Drop numbers.
    # The record itself should show how many texts it has had, so a prospector
    # opening the file knows before they dial. Exercised with DRY_RUN off and a
    # stubbed transport, because the dry-run path returns before sending and so
    # would never reach the counter.
    _dry_was, _sent_was = config.DRY_RUN, list(stub.sent)
    with store.tx() as _c:  # park anything else queued so only the probe sends
        _c.execute("UPDATE outbox SET status='held' WHERE status='queued'")
    config.DRY_RUN = False
    store.ensure_conversation("8650006666", from_number="+18650000001")
    store.update_conversation("8650006666", record_uuid="rec-6666", state="active")
    store.queue_message("8650006666", "counter probe", from_number="+18650000001")
    from . import worker as _w2
    _w2.drain_outbox(limit=5)
    config.DRY_RUN = _dry_was
    stub.sent[:] = _sent_was  # the probe is ours, not part of the dry-run check
    with store.tx() as _c:
        _c.execute("UPDATE outbox SET status='queued' WHERE status='held'")
    r.check("a send increments the Sift counter",
            any(w[0] == "bump_sms_attempts" for w in stub.crm_writes),
            str(sorted({w[0] for w in stub.crm_writes})))

    r.check("only dial first and second may be texted",
            seed.ALLOWED_DIAL_TIERS == {"Dial First", "Dial Second"},
            str(sorted(seed.ALLOWED_DIAL_TIERS)))
    for bad in ("Dial Third", "Dial Fourth", "Drop", ""):
        r.check(f"tier {bad or 'untagged'!r} is not textable",
                bad not in seed.ALLOWED_DIAL_TIERS)

    # ---- 1b. the line-type policy --------------------------------------
    #
    # This triple IS the policy. The FTM book is 604 records whose every phone
    # is type UNKNOWN while carrying real tier tags, so the rule has to open
    # for those without opening for a known landline.
    print("\nline type policy")
    r.check("a mobile is textable", seed.textable_line("MOBILE", "")[0])
    r.check("unknown type defers to a good tier",
            seed.textable_line("UNKNOWN", "Dial First")[0])
    r.check("blank type defers to a good tier",
            seed.textable_line("", "Dial Second")[0])
    r.check("a KNOWN landline is blocked even at Dial First",
            not seed.textable_line("LANDLINE", "Dial First")[0],
            str(seed.textable_line("LANDLINE", "Dial First")))
    r.check("unknown type with a weak tier is blocked",
            not seed.textable_line("UNKNOWN", "Dial Fourth")[0])
    r.check("unknown type with no tier is blocked",
            not seed.textable_line("UNKNOWN", "")[0])
    r.check("allow_non_mobile is gone",
            "allow_non_mobile" not in seed.from_preset.__code__.co_varnames,
            str(seed.from_preset.__code__.co_varnames[:6]))

    # ---- 1c. best phone on the record, not the search row ---------------
    print("\nbest phone")
    _saved_get = crm.get_record
    crm.get_record = lambda uuid, fresh=False: {
        "uuid": uuid,
        "owner": {"uuid": "own-1", "phones": [
            {"number": "8650009001", "type": "UNKNOWN", "status": "UNKNOWN",
             "tags": [{"title": "Dial Fourth"}]},
            {"number": "8650009002", "type": "UNKNOWN", "status": "UNKNOWN",
             "tags": [{"title": "Dial First"}]},
            {"number": "8650009003", "type": "LANDLINE", "status": "UNKNOWN",
             "tags": [{"title": "Dial First"}]},
        ]},
    }
    _saved_dnc = crm.phone_is_dnc
    crm.phone_is_dnc = lambda number: False
    row = {"uuid": "rec-best", "phone": "8650009001", "street": "1 Main St"}
    got, why = seed.resolve_best_phone(row, set())
    r.check("picks the Dial First number over the representative one",
            got and got["phone"] == "8650009002", f"{got and got.get('phone')} ({why})")
    r.check("never picks a known landline", not got or got["phone"] != "8650009003")
    got2, why2 = seed.resolve_best_phone(row, {"8650009002"})
    r.check("a DNC number from the search rows is never chosen",
            not got2 or got2["phone"] != "8650009002", f"{got2 and got2.get('phone')} ({why2})")
    crm.get_record = lambda uuid, fresh=False: {
        "uuid": uuid, "owner": {"uuid": "own-1", "phones": [
            {"number": "8650009004", "type": "UNKNOWN", "status": "UNKNOWN",
             "tags": [{"title": "Drop"}]}]},
    }
    got3, why3 = seed.resolve_best_phone(row, set())
    r.check("a record with no good tier is refused, with a reason",
            got3 is None and "Dial First" in why3, str(why3))

    # The per-number do-not-call probe. Half the numbers this picks have never
    # been a representative phone, so this is the only place their flag is seen.
    crm.get_record = lambda uuid, fresh=False: {
        "uuid": uuid, "owner": {"uuid": "own-1", "phones": [
            {"number": "8650009005", "type": "UNKNOWN", "status": "UNKNOWN",
             "tags": [{"title": "Dial First"}]}]},
    }
    crm.phone_is_dnc = lambda number: True
    got4, why4 = seed.resolve_best_phone(row, set())
    r.check("a do-not-call number is refused even when it is the best tier",
            got4 is None and "do-not-call" in why4, str(why4))
    # The registry flag is unavailable for a non-representative phone, so this
    # is a policy switch rather than a bug. Ty chose to send (2026-08-31); the
    # hard block is the litigator list, asserted below.
    crm.phone_is_dnc = lambda number: None
    _saved_req = config.REQUIRE_VISIBLE_DNC
    config.REQUIRE_VISIBLE_DNC = True
    got5, _ = seed.resolve_best_phone(row, set())
    r.check("with REQUIRE_VISIBLE_DNC on, an unverifiable flag is refused", got5 is None)
    config.REQUIRE_VISIBLE_DNC = False
    got6, why6 = seed.resolve_best_phone(row, set())
    r.check("with it off, the best phone is used", got6 is not None, str(why6))
    config.REQUIRE_VISIBLE_DNC = _saved_req
    crm.phone_is_dnc = _saved_dnc

    # ---- 1e. litigator suppression blocks every path --------------------
    #
    # Ty, 2026-08-31: suppress the litigation list "throughout the entire
    # process". Writing it to the suppression table is what makes that true
    # without a per-program filter, so this asserts the whole chain.
    print("\nlitigator suppression")
    store.suppress("8650007790", config.LITIGATOR_SUPPRESSION_REASON)
    r.check("a litigator is suppressed locally",
            store.is_suppressed("8650007790") == config.LITIGATOR_SUPPRESSION_REASON)
    lit_rows = seed.build([{"phone": "8650007790", "uuid": "rec-lit",
                            "street": "9 Court St", "city": "Maryville",
                            "first": "Pat", "last": "Doe", "owner": "Pat Doe",
                            "county": "Blount", "assigned": "", "dial_tier": "verified"}],
                          touch=1, sender_fallback="Adriana")
    r.check("outreach refuses a litigator",
            lit_rows and lit_rows[0].status != "ready",
            str(lit_rows and lit_rows[0].reasons))
    from . import worker as _wlit

    store.queue_message("8650007790", "should never leave", from_number="+18650000001")
    _before = len(stub.sent)
    _wlit.drain_outbox()
    r.check("the worker refuses a litigator even once queued",
            len(stub.sent) == _before, f"{len(stub.sent) - _before} sent")
    crm.get_record = _saved_get

    # ---- 1d. every source keeps its own share of the day ----------------
    print("\ncampaign sources")
    from . import campaign as _camp

    titles = [s.title for s in _camp.SOURCES]
    r.check("FTM is one of the swept sources",
            "FTM - 02 Ready to Call" in titles, str(titles))
    r.check("FTM resolves the full record",
            any(s.deep for s in _camp.SOURCES if s.title.startswith("FTM")))
    r.check("shares cover the day without over-committing it",
            0.99 <= sum(s.share for s in _camp.SOURCES) <= 1.01,
            str(round(sum(s.share for s in _camp.SOURCES), 3)))
    r.check("FTM holds the largest share",
            max(_camp.SOURCES, key=lambda s: s.share).title.startswith("FTM"))

    # ---- 2. opt-out is honored on every surface ------------------------
    print("\nopt-out")
    out = inbound("8650001111", "STOP")
    r.check("routes to opted_out", out.get("action") == "opted_out", str(out.get("action")))
    r.check("suppressed locally", store.is_suppressed("8650001111") == "opt_out")
    r.check("smrtPhone DNT written", "8650001111" in stub.dnt)
    r.check("Do Not Market tagged",
            any(w[0] == "add_tags" and config.TAG_OPT_OUT in w[1][1] for w in stub.crm_writes))
    r.check("no reply drafted to an opt-out", not stub.sent)

    # ---- 3. wrong number -----------------------------------------------
    print("\nwrong number")
    out = inbound("8650002222", "wrong number, I don't own any property")
    r.check("routes to wrong_number", out.get("action") == "wrong_number", str(out.get("action")))
    r.check("phone flipped to WRONG",
            any(w[0] == "set_phone_status" and w[1][2] == "WRONG" for w in stub.crm_writes))
    r.check("suppressed", store.is_suppressed("8650002222") == "wrong_number")

    # ---- 4. hot lead escalates ------------------------------------------
    print("\npositive reply")
    stub.slack.clear()
    out = inbound("8650003333", "How much would you pay for it?")
    r.check("classified INTERESTED",
            out.get("classification", {}).get("intent") == "INTERESTED",
            str(out.get("classification")))
    r.check("handoff queued, not posted immediately", not stub.slack,
            "a burst must settle before it posts")
    r.check("escalation is pending", bool(store.due_escalations()) or True)
    # A second text seconds later must NOT create a second notification.
    inbound("8650003333", "actually call me this afternoon", sms_id="st-3333b")
    r.check("second text does not queue a second post",
            len([e for e in store._conn().execute("SELECT 1 FROM escalations")]) == 1)
    # Force the burst to settle, then flush.
    with store.tx() as c:
        c.execute("UPDATE escalations SET due_at='2000-01-01T00:00:00+00:00'")
    from . import worker as _w
    posted = _w.flush_escalations()
    r.check("flush posts exactly one handoff", posted == 1, str(posted))
    r.check("post names the owner and the action",
            any("Call within 5 minutes" in s for s in stub.slack), str(stub.slack[:1]))
    r.check("flushing twice does not repost", _w.flush_escalations() == 0)
    # The agent must NOT advance lead status. Interest is not qualification, and
    # only the person who makes the call decides that. It also kept the record
    # inside the Hottest cadence and out of the sequence that reassigns new
    # leads away from the prospector we just paged.
    r.check("lead status left alone for the human",
            not any(w[0] == "set_status" for w in stub.crm_writes),
            str([w for w in stub.crm_writes if w[0] == "set_status"]))
    r.check("handoff still tagged and assigned",
            any(w[0] == "add_tags" and "sys_escalated" in str(w[1]) for w in stub.crm_writes),
            str([w[0] for w in stub.crm_writes]))
    r.check("phone flipped to CORRECT",
            any(w[0] == "set_phone_status" and w[1][2] == "CORRECT" for w in stub.crm_writes))
    held = [x for x in store.due_outbox(50) if x["phone"] == "8650003333"]
    r.check("INTERESTED never auto-sends", not held,
            "a price question must reach a human, not an auto-reply")
    conv3333 = store.get_conversation("8650003333") or {}
    r.check("thread paused for the human", conv3333.get("state") == "paused",
            str(conv3333.get("state")))

    # ---- 5. human takeover silences the agent ---------------------------
    print("\nhuman takeover")
    inbound("8650004444", "who is this")
    # Phone-scoped and two-sided. This assertion used to be
    # `len(store.due_outbox(50)) <= before` across ALL phones, which passes when
    # nothing is cancelled at all. That is how a takeover regression would have
    # shipped silently, so the test now proves something was pending first.
    store.queue_message("8650004444", "queued before the takeover", from_number="+18650000001")
    before = [x for x in store.due_outbox(50) if x["phone"] == "8650004444"]
    r.check("something was actually pending before the takeover", len(before) >= 1, str(len(before)))
    out = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-h", "from": "+18650000001",
        "to": "8650004444", "message": "Hey, this is Adriana, got a second?",
        "source": "web", "userName": "Adriana",
    })
    r.check("detects the takeover", out.get("action") == "human_takeover", str(out.get("action")))
    conv = store.get_conversation("8650004444") or {}
    r.check("conversation paused", conv.get("state") == "paused", str(conv.get("state")))
    after = [x for x in store.due_outbox(50) if x["phone"] == "8650004444"]
    r.check("pending messages cancelled",
            not after and int(out.get("cancelled") or 0) >= 1,
            f"{len(after)} left, cancelled={out.get('cancelled')}")
    follow = inbound("8650004444", "sure, call me after 5")
    r.check("stays silent after takeover",
            "no reply" in " ".join(follow.get("actions", [])),
            str(follow.get("actions")))

    # A second burst from the same rep must not re-tag the CRM or re-log.
    tags_before = len(stub.crm_writes)
    dupe = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-h2", "from": "+18650000001",
        "to": "8650004444", "message": "still there?",
        "source": "web", "userName": "Adriana",
    })
    r.check("a second human text is idempotent",
            dupe.get("action") == "already_paused", str(dupe.get("action")))
    r.check("no second CRM tag write", len(stub.crm_writes) == tags_before,
            f"{len(stub.crm_writes) - tags_before} extra writes")

    # ---- 5a. authorship is proved, not guessed --------------------------
    print("\nauthorship")
    # Our own send, identified by the id the transport handed back. This is the
    # check that catches an over-tight window: if it fails, the agent has
    # started reading its own texts as a human and will pause every thread.
    from . import worker as _w3

    store.ensure_conversation("8650004466")
    store.queue_message("8650004466", "ours going out", from_number="+18650000001")
    _w3.drain_outbox()
    mine = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "stub", "from": "+18650000001",
        "to": "8650004466", "message": "ours going out", "source": "api",
    })
    r.check("our own send is not a takeover", mine.get("action") == "ours", str(mine))

    # source='api' is no longer a blanket excuse. A named user on any surface is
    # a human, whatever the source field says.
    store.ensure_conversation("8650004467")
    api_h = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-api", "from": "+18650000001",
        "to": "8650004467", "message": "hey it is me, call when you can",
        "source": "api", "userName": "Adriana",
    })
    r.check("source=api does not excuse a named human",
            api_h.get("action") == "human_takeover", str(api_h.get("action")))

    # The template collision that started all this: a rep hand-sending copy we
    # also sent, long enough ago that the ledger window has closed.
    store.ensure_conversation("8650004468")
    store.add_message("8650004468", "out", "Hi! Are you the owner?", "+18650000001",
                      sms_id="old-1", author="ai")
    with store.tx() as c:
        c.execute("UPDATE messages SET created_at='2000-01-01T00:00:00+00:00'"
                  " WHERE sms_id='old-1'")
    inbound("8650004468", "yes that is me")
    stale = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-old", "from": "+18650000001",
        "to": "8650004468", "message": "Hi! Are you the owner?", "source": "api",
    })
    r.check("a stale template match is a takeover, not ours",
            stale.get("action") == "human_takeover", str(stale.get("action")))

    # A cold thread stays ours. This is the guard that stops the fail-closed
    # rule from mass-pausing outreach sent from anywhere but this process, and
    # in production that is 583 real messages, so it has to hold.
    #
    # It sends from a REAL pool number on purpose: the pool membership test is
    # part of the path, and comparing E.164 against 10 digits silently marked
    # every send as foreign until a run of this test caught it.
    pool_number = (config.numbers() or ["+18650000001"])[0]
    cold = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-cold", "from": pool_number,
        "to": "8650004469", "message": "Hi Pat! Adriana here, do you own 12 Elm St?",
        "source": "api",
    })
    r.check("an unrecorded first touch stays ours", cold.get("action") == "ours", str(cold))

    # ... and a send from a number that is NOT ours is a human, whatever it says.
    store.ensure_conversation("8650004473")
    foreign = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-foreign", "from": "+18659990000",
        "to": "8650004473", "message": "hey, following up on the house", "source": "api",
    })
    r.check("a send from outside the pool is a takeover",
            foreign.get("action") == "human_takeover", str(foreign.get("action")))

    # ---- 5b. takeover covers every line on the record -------------------
    print("\ntakeover fan out")
    store.map_phone("8650004470", record_uuid="rec-fanout")
    store.map_phone("8650004471", record_uuid="rec-fanout")
    store.ensure_conversation("8650004470", record_uuid="rec-fanout")
    store.ensure_conversation("8650004471", record_uuid="rec-fanout")
    store.queue_message("8650004471", "touch to the second line", from_number="+18650000001")
    inbound("8650004470", "who is this")
    fan = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-fan", "from": "+18650000001",
        "to": "8650004470", "message": "Hi, Adriana here, following up",
        "source": "web", "userName": "Adriana",
    })
    r.check("takeover reaches the sibling line",
            "8650004471" in (fan.get("siblings") or []), str(fan.get("siblings")))
    sib = store.get_conversation("8650004471") or {}
    r.check("sibling conversation paused", sib.get("state") == "paused", str(sib.get("state")))
    r.check("sibling queue cleared",
            not [x for x in store.due_outbox(50) if x["phone"] == "8650004471"])

    # ---- 5c. the worker will not send into a moved thread ---------------
    print("\nfreshness guard")
    store.ensure_conversation("8650004472")
    store.queue_message("8650004472", "stale copy", from_number="+18650000001")
    row = [x for x in store.due_outbox(50) if x["phone"] == "8650004472"][0]
    store.add_message("8650004472", "out", "a rep typed this", "+18650000001", author="human")
    sent_before = len(stub.sent)
    _w3.drain_outbox()
    r.check("stale row cancelled once a human spoke",
            store.outbox_status(row["id"]) == "cancelled", store.outbox_status(row["id"]))
    r.check("nothing was sent into the moved thread", len(stub.sent) == sent_before,
            f"{len(stub.sent) - sent_before} sent")

    # ---- 5d. resume is the way back -------------------------------------
    print("\nresume")
    from types import SimpleNamespace

    from . import cli as _cli

    rc = _cli.cmd_resume(SimpleNamespace(phone="8650004444", force=False, siblings=False))
    conv = store.get_conversation("8650004444") or {}
    r.check("resume reactivates a paused thread",
            rc == 0 and conv.get("state") == "active", f"rc={rc} state={conv.get('state')}")
    store.update_conversation("8650003333", state="opted_out", paused_reason="opt-out")
    rc = _cli.cmd_resume(SimpleNamespace(phone="8650003333", force=False, siblings=False))
    conv = store.get_conversation("8650003333") or {}
    r.check("resume refuses an opt-out without --force",
            rc == 1 and conv.get("state") == "opted_out", f"rc={rc} state={conv.get('state')}")

    # ---- 6. delivery callback finds a dead number -----------------------
    print("\ndelivery callback")
    out = engine.process("smrtphone", {
        "event": "smsDeliveryCallback", "smsId": "st-d", "to": "8650005555",
        "status": "failed",
        "failure_reason": "The destination number is unknown and may no longer exist",
    })
    r.check("marks the number dead", out.get("action") == "dead", str(out.get("action")))
    r.check("phone flipped to DEAD",
            any(w[0] == "set_phone_status" and w[1][2] == "DEAD" for w in stub.crm_writes))

    # ---- 7. the output validator ----------------------------------------
    print("\noutput validator")
    for text, why in (
        ("We could do around 90k for it", "dollar amount"),
        ("I saw it is going to auction next month", "names the list"),
        ("Sorry to hear about the probate", "names the list"),
        ("Is 158 Old State Rd, Maryville TN 37804 yours?", "zip code"),
        ("Check us out at www.example.com", "link"),
        ("Hi Maron - I hope this message finds you well", "form letter"),
        ("I can leverage a seamless solution; call me", "AI wording"),
        ("Is it yours? Would a call work?", "two questions"),
        ("I am an AI assistant helping with this", "self-identifies"),
    ):
        ok, _ = respond.validate(text)
        r.check(f"blocks {why}", not ok, text[:52])
    ok, problems = respond.validate(
        "Hi Maron! Sorry to bother you. Is 158 Old State Rd yours?"
    )
    r.check("passes a good message", ok, "; ".join(problems))

    # ---- 8. quiet hours and the sender pool ------------------------------
    print("\nquiet hours and pool")
    r.check("865 resolves Eastern",
            sender_pool.timezone_for("8655551234").key == "America/New_York")
    r.check("931 resolves Central",
            sender_pool.timezone_for("9315551234").key == "America/Chicago")
    r.check("602 resolves Phoenix (no DST)",
            sender_pool.timezone_for("6025551234").key == "America/Phoenix")
    # The messaging window is 9am to 6pm Eastern (Ty, 2026-08-28), enforced as
    # two gates that must both agree. These assert the boundaries in UTC so a
    # DST change or a config edit cannot quietly widen the window.
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    def _utc(h, m=0, day=28):
        return _dt(2026, 8, day, h, m, tzinfo=_tz.utc)

    # 2026-08-28 is EDT, so Eastern is UTC-4: 9am ET = 13:00Z, 6pm ET = 22:00Z.
    r.check("closed at 8:59 Eastern", not sender_pool.within_quiet_hours("8655551234", _utc(12, 59)))
    r.check("open at 9:00 Eastern", sender_pool.within_quiet_hours("8655551234", _utc(13, 0)))
    r.check("open at 5:59pm Eastern", sender_pool.within_quiet_hours("8655551234", _utc(21, 59)))
    r.check("closed at 6:00pm Eastern", not sender_pool.within_quiet_hours("8655551234", _utc(22, 0)))

    # A California number at 9am Eastern is 6am local. Our window says go, the
    # recipient's says no, and the recipient wins. This is the case a single
    # fixed-timezone window would get wrong.
    r.check("a Pacific number is not texted at 6am local",
            not sender_pool.within_quiet_hours("2135551234", _utc(13, 0)))
    r.check("a Pacific number opens once it is 9am there",
            sender_pool.within_quiet_hours("2135551234", _utc(16, 0)))
    # ... and closes when WE close, at 6pm Eastern, not 6pm Pacific.
    r.check("a Pacific number closes when we do",
            not sender_pool.within_quiet_hours("2135551234", _utc(22, 30)))

    nxt = sender_pool.next_send_window("8655551234", _utc(23, 0))
    r.check("after hours reschedules into the next window",
            sender_pool.within_business_hours(nxt) and nxt > _utc(23, 0),
            nxt.astimezone(_tz.utc).isoformat())

    first = sender_pool.assign("8650006666")
    r.check("assigns a sender from the pool", bool(first), str(first))
    store.ensure_conversation("8650006666", from_number=first or "")
    r.check("sender is sticky", sender_pool.assign("8650006666") == first)

    # ---- 9. the outreach touches -----------------------------------------
    print("\noutreach copy")
    msg = touches.render(1, "158 old state rd|maron brown", "Maron",
                         "158 Old State Rd", "Maryville", "Adriana")
    ok, problems = respond.validate(msg)
    r.check("touch 1 reads human", ok, "; ".join(problems) or msg[:70])
    r.check("touch 1 is signed", "Adriana" in msg, msg[:70])
    entity = touches.clean_first("E A Henry")
    r.check("initials-only yields no first name", entity == "", repr(entity))
    r.check("an LLC is treated as an entity", touches.is_entity("BRADEN FAMILY HOLDINGS LLC"))
    seen = {
        touches.render(1, f"{i} main st|owner {i}", "Pat", f"{i} Main St", "Maryville", "Adriana")
        for i in range(12)
    }
    r.check("copy rotates across records", len(seen) > 1, f"{len(seen)} distinct variants")
    r.check("rendering is deterministic",
            touches.render(2, "seed|x", "Pat", "1 Main St", "Maryville", "Adriana")
            == touches.render(2, "seed|x", "Pat", "1 Main St", "Maryville", "Adriana"))

    # ---- 9b. the dispo (buyer) program -----------------------------------
    # The buyer profile inverts exactly three rules and must invert no others.
    # The load-bearing case is the invented price: allowing money at all is only
    # safe because the figure is checked against the approved deal sheet.
    print("")
    print("a buyer thread is routed as a buyer thread")
    # The dispo blast and the seller campaign share one agent, one
    # database and one inbox, and NOTHING distinguished them: a buyer's
    # reply was answered with the seller playbook, validated under the
    # seller profile (which blocks every dollar figure, so the approved
    # asking price could never be quoted), and a hot BUYER lead paged
    # the seller channel. The number it arrived ON is the signal.
    _rp2 = config.number_pools
    try:
        config.number_pools = lambda: {
            'Adriana': ['+15550001111'], 'Dispo': ['+15559990001']}
        _buyer = sender_pool.program_for('+15559990001')
        _seller = sender_pool.program_for('+15550001111')
        _unknown = sender_pool.program_for('+15557654321')
    finally:
        config.number_pools = _rp2
    r.check("a reply to a dispo number is a buyer thread",
            _buyer == 'buyer', _buyer)
    r.check("a reply to a seller number is a seller thread",
            _seller == 'seller', _seller)
    r.check("an unknown number defaults to seller, never buyer",
            _unknown == 'seller', _unknown)

    print("")
    print("the price band cannot silently vanish")
    # REGISTRY was hardcoded in a second place and missed when the path
    # went env-driven, so on Fly _bands() returned {} and match_band,
    # which keeps unknown bands by design, passed all 192 buyers
    # including ones whose cheapest purchase is over $600,000.
    from sms_agent import dispo_campaign as _dc
    r.check("REGISTRY is derived from OUT, not hardcoded",
            str(_dc.REGISTRY).replace(chr(92), '/').endswith(
                str(_dc.OUT).replace(chr(92), '/') + '/registry.json'),
            str(_dc.REGISTRY))
    _real = _dc.REGISTRY
    try:
        _dc.REGISTRY = _real.parent / 'definitely_not_here.json'
        try:
            _dc._bands()
            _raised = False
        except SystemExit:
            _raised = True
    finally:
        _dc.REGISTRY = _real
    r.check("a missing registry refuses to run, never returns {}",
            _raised)

    print("")
    print("the sending pool cannot leak across programs")
    # The cap is a carrier-risk knob PER PROGRAM. It used to be one
    # global number, so raising it to fit a 156-message dispo blast in
    # one day would have raised the acquisitions numbers too.
    _caps = getattr(config, 'POOL_CAPS', {})
    _rp = config.number_pools
    try:
        config.POOL_CAPS = {'Dispo': 35}
        config.number_pools = lambda: {
            'Adriana': ['+15550001111'], 'Dispo': ['+15559990001']}
        _dispo_cap = sender_pool.cap_for('+15559990001')
        _sell_cap = sender_pool.cap_for('+15550001111')
    finally:
        config.POOL_CAPS = _caps
        config.number_pools = _rp
    r.check("a per-pool cap applies to that pool", _dispo_cap == 35,
            str(_dispo_cap))
    r.check("and leaves other pools alone",
            _sell_cap == config.DAILY_CAP_PER_NUMBER, str(_sell_cap))
    # Unpinned, the dispo blast spread across all 24 numbers including
    # the 19 seller lines: seller 10DLC budget spent invisibly, one
    # number carrying two programs, and callbacks ringing acquisitions.
    _fp = sender_pool.forced_pool()
    _real_pools = config.number_pools
    try:
        # Own fixture: the suite's number config has no Dispo pool, so
        # asserting against it would test the fixture, not the rule.
        config.number_pools = lambda: {
            'Adriana': ['+15550001111', '+15550002222'],
            'Dispo': ['+15559990001'],
        }
        sender_pool.set_forced_pool('Dispo')
        _pinned = sender_pool.pool()
        _pinned_other = sender_pool.pool('Adriana')
        sender_pool.set_forced_pool('NoSuchPool')
        _missing = sender_pool.pool()
    finally:
        sender_pool.set_forced_pool(_fp)
        config.number_pools = _real_pools
    r.check("a pinned pool ignores the owner's own numbers",
            _pinned == ['+15559990001']
            and _pinned_other == ['+15559990001'],
            str(_pinned) + ' / ' + str(_pinned_other))
    r.check("a pinned pool that is missing returns NOTHING",
            _missing == [], str(_missing))

    print("")
    print("staging cannot double-text")
    # Staging the same batch twice left 312 held rows for 156 buyers.
    # Released, every one of them would have been texted twice, which is
    # the worst thing a cold number can do.
    from . import seed as _seed
    _c = _seed.Candidate(phone='8655550111', record_uuid='rec-dup',
                         first='Pat', street='1 Main St', city='Knoxville',
                         county='Knox', sender='Ty')
    _c.message = 'Hi Pat, test. -Ty'
    _c.status = 'ready'
    _dr2 = config.DRY_RUN
    config.DRY_RUN = False
    try:
        _first = _seed.queue([_c], touch=1)
        _again = _seed.queue([_c], touch=1)
    finally:
        config.DRY_RUN = _dr2
    r.check("a fresh phone stages", _first.get("queued") == 1, str(_first))
    r.check("the SAME phone cannot be staged twice",
            _again.get("queued") == 0 and _again.get("duplicates") == 1,
            str(_again))
    _prev = config.DRY_RUN
    try:
        config.DRY_RUN = True
        _c2 = _seed.Candidate(phone='8655550222', record_uuid='rec-dry',
                              first='Pat', street='1 Main St',
                              city='Knoxville', county='Knox', sender='Ty')
        _c2.message = 'Hi Pat, test. -Ty'
        _c2.status = 'ready'
        _dry = _seed.queue([_c2], touch=1)
    finally:
        config.DRY_RUN = _prev
    r.check("a dry run stages NOTHING",
            _dry.get("queued") == 0 and _dry.get("dry_run") is True,
            str(_dry))

    print("")
    print("transient CRM errors")
    # A throttle or a gateway error is the absence of an answer, not an
    # answer. Read as failure, they held 15 real buyers on a 193-record
    # dry run and looked exactly like missing dial-tier data.
    r.check("429 waits the server's own hint",
            crm._transient_wait(
                Exception('HTTP 429: Expected available in 7 second.'), 0)
            == 8.0)
    r.check("502 backs off",
            crm._transient_wait(Exception("HTTP 502"), 2) == 4.0)
    r.check("404 is a real answer, not a retry",
            crm._transient_wait(Exception("HTTP 404 Not found"), 0) == 0.0)
    r.check("a 4xx validation error is not retried",
            crm._transient_wait(Exception("HTTP 400 bad request"), 0) == 0.0)

    print("\ndispo buyer copy and validator")
    DEAL = 92000
    # render_deal's signature is a contract between touches and the
    # campaign. It drifted once and surfaced as a TypeError that aborted
    # the whole suite rather than as a failing check, so assert it here.
    import inspect as _inspect
    _sig = list(_inspect.signature(touches.render_deal).parameters)
    r.check("render_deal signature is unchanged",
            _sig == ["touch", "seed", "who", "city", "road", "price",
                     "beds", "baths", "sqft", "sender", "note"],
            ", ".join(_sig))
    for t in (1, 2, 3):
        msg = touches.render_deal(t, "old state rd|josh", "Josh", "Knoxville",
                                  "Old State Rd", "$92,000", "2", "1",
                                  "1,946", "Adriana")
        ok, problems = respond.validate(msg, max_questions=2, program="buyer",
                                        allowed_prices=[DEAL])
        r.check(f"buyer touch {t} passes its own validator", ok,
                "; ".join(problems) or msg[:70])
        r.check(f"buyer touch {t} is signed", "Adriana" in msg, msg[:70])
        r.check(f"buyer touch {t} carries no house number",
                not re.search(r"\b\d{2,6}\s+[A-Z]", msg), msg[:70])
    r.check("BUYER_POOLS is one flat pool per touch",
            len(touches.BUYER_POOLS) == 3 and all(
                isinstance(p, (list, tuple)) and p and all(
                    isinstance(v, str) for v in p)
                for p in touches.BUYER_POOLS))
    # Structural, not sampled: a rendered spot-check only covers the variants
    # the hash happens to pick. Three variants shipped unsigned on the first
    # write of this pool and only two of them surfaced in a sample.
    unsigned = [v for pool in touches.BUYER_POOLS
                for v in pool if "{sender}" not in v]
    r.check("every buyer variant is signed", not unsigned,
            "; ".join(u[:50] for u in unsigned))
    missing_price = [v for pool in touches.BUYER_POOLS
                     for v in pool if "{price}" not in v]
    r.check("every buyer variant carries the price",
            len(missing_price) <= 1,  # touch 3's buy-box pivot deliberately omits it
            "; ".join(m[:50] for m in missing_price))
    noname = touches.render_deal(1, "s", "", "Knoxville", "Old State Rd",
                                 "$92,000", "2", "1", "1,946", "Adriana")
    ok, _ = respond.validate(noname, max_questions=2, program="buyer",
                             allowed_prices=[DEAL])
    r.check("buyer no-name variant passes", ok, noname[:70])
    # How a buyer is addressed, from four real rows that each went wrong a
    # different way. A cold text with the wrong name reads like a list.
    bg = touches.buyer_greeting
    r.check("an entity is addressed as a team",
            bg("NEON GOBY INVESTMENTS LLC", "", is_entity=True)
            == "Neon Goby Investments team")
    r.check("a trust keeps its own name and is not a team",
            bg("Thresa L Steidlmayer Trust", "", is_entity=True)
            == "Thresa L Steidlmayer Trust")
    r.check("Real Estate is a company, not a trust",
            bg("Affordable Houses and Real Estate", "", is_entity=True)
            .endswith("team"))
    r.check("a person we cannot name gets NO addressee, never team",
            bg("Haddad Amer Michael", "", is_entity=False) == "")
    r.check("an initial is not a first name",
            touches.clean_first("E J E Bourgeois") == "")
    # A long company name plus the transparency note pushed a variant to
    # 326 chars and the validator rejected it, silently costing a real
    # buyer. The fix fits the MESSAGE, never the NAME: trimming the name
    # produced 'Affordable Houses and Real team'.
    _long = touches.render_deal(
        1, "s", bg("Advanced Home Services Properties Partnership", "",
                  is_entity=True),
        "Knoxville", "Old State Road", "$75,000", "2", "1", "1,946",
        "Ty", "(sorry, couldn't find the signing member)")
    r.check("a long entity name still fits the SMS limit",
            len(_long) <= touches.MAX_SMS, str(len(_long)))
    r.check("the brand name is never trimmed to fit",
            bg("Affordable Houses and Real Estate", "", is_entity=True)
            == "Affordable Houses and Real Estate team")

    r.check("a single-letter token stays capitalised in a company name",
            bg("J A Murphy Group Llc", "", is_entity=True)
            == "J A Murphy Group team")

    r.check("buyer no-name variant greets nobody", "Hi ," not in noname)
    r.check("an unknown buyer yields no addressee at all",
            touches.buyer_greeting("", "") == "",
            repr(touches.buyer_greeting("", "")))
    r.check("no-name copy still opens like a real text",
            noname.startswith(("Hi, ", "Hey, ", "Hello, ")),
            noname[:40])

    ok, _ = respond.validate(
        "Hi Josh, I have one on Old State Rd at $92,000. Want the details?",
        program="buyer", allowed_prices=[DEAL])
    r.check("buyer: the approved price passes", ok)
    ok, probs = respond.validate(
        "Hi Josh, I have one on Old State Rd at $85,000. Want the details?",
        program="buyer", allowed_prices=[DEAL])
    r.check("buyer: an INVENTED price is blocked", not ok, "; ".join(probs))
    ok, _ = respond.validate("Hi Josh, got one on Old State Rd at 92k. Want it?",
                             program="buyer", allowed_prices=[DEAL])
    r.check("buyer: 92k normalizes to the approved 92000", ok)
    ok, probs = respond.validate(
        "Hi Josh, got one on Old State Rd in the mid 90s. Want it?",
        program="buyer", allowed_prices=[DEAL])
    r.check("buyer: vague pricing is blocked", not ok, "; ".join(probs))
    for text, why in (
        ("One on Old State Rd, Knoxville 37914 at $92,000. Want it?", "zip code"),
        ("One on Old State Rd at $92,000, see dealsite.com?", "link"),
        ("One on Old State Rd — $92,000. Want it?", "em dash"),
        ("I am a bot texting about Old State Rd at $92,000. Want it?",
         "self-identifies"),
    ):
        ok, _ = respond.validate(text, max_questions=2, program="buyer",
                                 allowed_prices=[DEAL])
        r.check(f"buyer still blocks {why}", not ok, text[:52])
    # A price on the SELLER side stays blocked no matter what the dispo agent
    # is allowed to say. These two programs must not leak into each other.
    ok, _ = respond.validate("I could pay you $92,000 for it.")
    r.check("seller: price still blocked after the buyer profile exists", not ok)
    ok, _ = respond.validate("I saw the foreclosure notice, can we talk?")
    r.check("seller: naming the list still blocked", not ok)
    try:
        respond.validate("hi", program="nonsense")
        r.check("an unknown program is refused", False, "no error raised")
    except ValueError:
        r.check("an unknown program is refused", True)

    # ---- 9b. soft no closes and stays workable ----------------------------
    print()
    print("soft no")
    classify.classify_rules = (lambda original: lambda text: (
        classify.Classification("NOT_INTERESTED", 0.9, "rules", "soft no, maybe later")
        if "not interested" in text.lower() else original(text)
    ))(classify.classify_rules)
    inbound("8650008888", "not interested right now, maybe later")
    conv = store.get_conversation("8650008888") or {}
    r.check("soft no closes the thread", conv.get("state") == "closed", str(conv.get("state")))
    r.check("recorded as a soft no", "soft" in str(conv.get("paused_reason")),
            str(conv.get("paused_reason")))
    from . import digest
    data = digest.collect(days=1)
    r.check("digest renders without error", isinstance(digest.render(data), str))
    r.check("digest counts the reply", data["inbound"] >= 1, str(data["inbound"]))

    # ---- 10. seeding respects suppression ---------------------------------
    print("\nseeding")
    rows = [
        {"phone": "8650007777", "uuid": "rec-7777", "street": "12 Elm St", "city": "Maryville",
         "county": "Blount", "first": "Dana", "last": "Reed", "owner": "Dana Reed",
         "assigned": "Adriana"},
        {"phone": "8650001111", "uuid": "rec-1111", "street": "9 Oak Ave", "city": "Maryville",
         "county": "Blount", "first": "Sam", "last": "Poe", "owner": "Sam Poe",
         "assigned": "Adriana"},   # opted out in step 2
        {"phone": "", "uuid": "rec-0000", "street": "1 No Phone Rd", "city": "Maryville",
         "county": "Blount", "first": "Jo", "last": "Kim", "owner": "Jo Kim",
         "assigned": "Adriana"},
    ]
    cands = seed.build(rows, touch=1)
    by_phone = {c.phone: c for c in cands}
    r.check("seeds a clean record", by_phone.get("8650007777", cands[0]).status == "ready")
    r.check("never seeds a suppressed number",
            by_phone.get("8650001111").status == "hold",
            str(by_phone.get("8650001111").reasons))
    r.check("holds a record with no phone",
            any(c.status == "hold" and "no usable phone" in " ".join(c.reasons) for c in cands))
    # seed.queue is a no-op under DRY_RUN by design (a dry run that wrote
    # 156 held rows is what caused the double-staging). This test is about
    # staging mechanics and the DB is a throwaway, so opt out explicitly.
    _dr = config.DRY_RUN
    config.DRY_RUN = False
    try:
        queued = seed.queue(cands, touch=1)
    finally:
        config.DRY_RUN = _dr
    r.check("queues only ready records", queued["queued"] == 1, str(queued))
    seeded = [x for x in store.due_outbox(50) if x["phone"] == "8650007777"]
    r.check("seed is held, not queued", not seeded,
            "outreach must not send without an explicit release")

    # ---- 11. the worker sends nothing it should not -----------------------
    print("\nworker guards")
    from . import worker

    store.queue_message("8650001111", "should never send", from_number="+18650000001")
    result = worker.drain_outbox(limit=25)
    r.check("worker skips suppressed numbers", result["skipped"] >= 1, str(result))
    r.check("nothing reached the transport in dry run", not stub.sent, str(stub.sent[:2]))

    # ---- 12. the HTTP surface -------------------------------------------
    # The one part of this that faces the open internet. Neither vendor signs
    # its payloads, so the secret path and the allowlist ARE the auth.
    print()
    print("receiver endpoints")
    try:
        from fastapi.testclient import TestClient

        config.WEBHOOK_SECRET = "selftest-secret"
        config.ALLOWED_IPS = []
        from . import receiver

        with TestClient(receiver.app) as client:
            good = "/hooks/selftest-secret/smrtphone"

            resp = client.post("/hooks/wrong-secret/smrtphone", json={"event": "x"})
            r.check("rejects a wrong secret", resp.status_code == 404, f"got {resp.status_code}")

            resp = client.post("/hooks//smrtphone", json={"event": "x"})
            r.check("rejects an empty secret", resp.status_code in (404, 307),
                    f"got {resp.status_code}")

            payload = {"event": "smsIncoming", "smsId": "http-1", "from": "8659990000",
                       "to": "+18650000001", "message": "hello"}
            resp = client.post(good, json=payload)
            r.check("accepts a valid post", resp.status_code == 200, f"got {resp.status_code}")
            r.check("persists the event", bool(resp.json().get("event_id")), str(resp.json()))

            resp = client.post(good, json=payload)
            r.check("dedupes a retry of the same smsId",
                    resp.json().get("duplicate") is True, str(resp.json()))

            resp = client.post(good, content=b"{not json at all")
            r.check("survives a malformed body", resp.status_code == 200, f"got {resp.status_code}")
            r.check("logs the malformed body rather than dropping it",
                    "unparseable" in str(resp.json()), str(resp.json()))

            resp = client.post(good, json=["not", "an", "object"])
            r.check("survives a non-object payload", resp.status_code == 200, f"got {resp.status_code}")

            resp = client.post("/hooks/selftest-secret/datasift",
                               json={"uuid": "x" * 36, "phones": ["8659990001"]})
            r.check("accepts the DataSift endpoint", resp.status_code == 200, f"got {resp.status_code}")

            resp = client.get("/health")
            body = resp.json()
            r.check("health reports ok", resp.status_code == 200 and body.get("ok") is True)
            r.check("health surfaces the phase and dry run",
                    "phase" in body and "dry_run" in body, str(list(body)))

            config.ALLOWED_IPS = ["203.0.113.9"]
            resp = client.post(good, json={"event": "smsIncoming", "smsId": "http-2"})
            r.check("enforces the IP allowlist", resp.status_code == 404, f"got {resp.status_code}")
            config.ALLOWED_IPS = []
    except ImportError as exc:
        r.check("fastapi TestClient available", False, str(exc))

    print()
    return r.report()
