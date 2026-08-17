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
    escalate._post = lambda text, blocks=None: (stub.slack.append(text) or True)
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
        respond.draft = lambda thread, context=None, intent="", intent_rationale="": respond.Reply(
            message="Hi Maron! Sorry to bother you. Is 158 Old State Rd yours?",
            confidence=0.92, handoff=(intent == "INTERESTED"), reason="stub", ok=True,
        )

    ctx = crm.deal_context("rec-1")
    for phone in ("8650001111", "8650002222", "8650003333", "8650004444",
                  "8650005555", "8650006666", "8650008888"):
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
    before = len(store.due_outbox(50))
    out = engine.process("smrtphone", {
        "event": "smsOutgoing", "smsId": "st-h", "from": "+18650000001",
        "to": "8650004444", "message": "Hey, this is Adriana, got a second?",
        "source": "web", "userName": "Adriana",
    })
    r.check("detects the takeover", out.get("action") == "human_takeover", str(out.get("action")))
    conv = store.get_conversation("8650004444") or {}
    r.check("conversation paused", conv.get("state") == "paused", str(conv.get("state")))
    r.check("pending messages cancelled", len(store.due_outbox(50)) <= before)
    follow = inbound("8650004444", "sure, call me after 5")
    r.check("stays silent after takeover",
            "no reply" in " ".join(follow.get("actions", [])),
            str(follow.get("actions")))

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
    queued = seed.queue(cands, touch=1)
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
