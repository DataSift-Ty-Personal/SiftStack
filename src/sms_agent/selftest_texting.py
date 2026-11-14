"""Selftest for the build plan's texting packages (0.1, 0.4, 0.14, 0.17, 0.22, 0.23, 2.2, 2.3, 4.7).

Run by selftest.py as its last section, on the same throwaway database, with the same stubs and
with the network already switched off. Every behavior here sits behind a switch that is off by
default, so each part turns one switch on, checks what changes, and checks what the app does
with it off (which is what it did before any of this was written).

Zero network. Every name, street, number and record id in this file is invented.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

SELLER = "+18650000001"         # a number in the seller pool
SELLER2 = "+18650000002"
BUYER = "+18650000091"          # a number in the Dispo pool

CONFIG_KEYS = (
    "PHASE", "DRY_RUN", "GATE_MODE", "GATE_READ_FIELDS", "GATE_SETTINGS_RAW", "OWNER_REVOKE", "SEND_RECHECK",
    "DISPOSITION_TABLE", "NOT_NOW_STATUS", "TAG_DEATH_REPORTED", "TAG_CONFIRM_CALL", "TAG_RELATIVE",
    "SC_EXTRA_LABELS", "SC_LABEL_CORRECT", "SC_LABEL_WRONG_DNC", "SC_LABEL_RELATIVE", "SC_LABEL_NURTURE",
    "HAND_RAISE_NEW_LEAD", "NEW_LEAD_STATUS", "NEW_LEAD_PROTECTED", "TAG_REPLY_ON_LEAD", "OVER_VALUE",
    "COLD_LEAD_STATUS", "TAG_OVER_VALUE", "LISTED_STATUS", "SPEED_TO_LEAD", "SPEED_TO_LEAD_DELAY",
    "SPEED_TO_LEAD_SEQUENCES", "LEAD_CALLER_NAME", "RATE_BUDGET", "RATE_PER_MIN", "RATE_PER_HOUR", "RATE_QUIET",
    "RATE_QUIET_PER_MIN", "RATE_MAX_WAIT", "RATE_ENFORCE", "RATE_BUDGET_MODE", "TOUCH_FEED", "REST_FLAGGED",
    "REST_DAYS", "CANARY_TO",
    "SC_AUTO_REPLY", "SC_AUTO_LABEL", "SC_REPLY_DELAY_MIN", "SC_REPLY_DELAY_MAX", "SMRTPHONE_NUMBERS_RAW",
    "MIN_SEND_GAP_SECONDS", "DAILY_CAP_PER_NUMBER", "POOL_CAPS", "POOL_GAPS", "SENSITIVE_SLACK_LEGAL_ONLY",
    "HANDOFF_NAME", "HANDOFF_ASSIGNEE_UUID", "SC_NI_OVERWRITE", "SC_NI_STATUS", "SC_PRICE_CEILING", "SC_LISTED_MLS",
    "ESCALATE_INTENTS", "WEBHOOK_SECRET", "ALLOWED_IPS", "SLACK_INTERESTED_ONLY", "ANSWER_WHO", "LIST_DNC",
    "TAG_OPT_OUT", "TAG_MAIL_ONLY", "REQUIRE_VISIBLE_DNC", "BUSINESS_TZ",
)


def _phone(number: str, status: str = "UNKNOWN", kind: str = "MOBILE", tags=("Dial First",)) -> dict:
    return {"number": number, "status": status, "type": kind, "tags": [{"title": t} for t in tags],
            "is_connected": True}


def _record(uuid: str, *phones, status=None, tags=("FTM",), lists=("Foreclosure",), owner="", **extra) -> dict:
    rec = {"uuid": uuid, "status": status, "tags": list(tags), "lists": list(lists), "mls": "Off Market",
           "estimate_value": "200000.00",
           "address": {"street": "12 Elm St", "city": "Maryville", "state": "TN", "postal_code": "37801",
                       "county": "Blount"},
           "owner": {"uuid": owner or "own-" + uuid, "first_name": "Dana", "last_name": "Reed",
                     "phones": [p if isinstance(p, dict) else _phone(p) for p in phones]}}
    rec.update(extra)
    return rec


class Kit:
    """The fake account this section runs against, and the handful of things every part needs."""

    def __init__(self, stub, mods):
        self.stub, self.m = stub, mods
        self.recs: dict[str, dict] = {}
        self.reads: list[str] = []
        self.sent: list[tuple] = []
        self.n = 0

    # ---- the fake DataSift
    def get_record(self, uuid, fresh=False):
        self.reads.append(uuid)
        rec = self.recs.get(uuid)
        return copy.deepcopy(rec) if rec else None

    def put(self, rec: dict) -> dict:
        self.recs[rec["uuid"]] = rec
        return rec

    def _rec(self, uuid) -> dict:
        return self.recs.setdefault(uuid, {"uuid": uuid, "status": None, "tags": [], "lists": [], "owner": {"phones": []}})

    def _write(self, name, args) -> dict:
        self.stub.crm_writes.append((name, args))
        return {"ok": True}

    def set_status(self, uuid, status):
        self._rec(uuid)["status"] = status
        return self._write("set_status", (uuid, status))

    def add_tags(self, uuid, tags):
        self._rec(uuid)["tags"] = list(self._rec(uuid).get("tags") or []) + [t for t in tags]
        return self._write("add_tags", (uuid, list(tags)))

    def add_list(self, uuid, title):
        self._rec(uuid)["lists"] = list(self._rec(uuid).get("lists") or []) + [title]
        return self._write("add_list", (uuid, title))

    def set_phone_status(self, uuid, phone, status):
        for p in (self._rec(uuid).get("owner") or {}).get("phones") or []:
            if p.get("number") == phone:
                p["status"] = status
        return self._write("set_phone_status", (uuid, phone, status))

    def add_phone_tag(self, uuid, phone, tag):
        for p in (self._rec(uuid).get("owner") or {}).get("phones") or []:
            if p.get("number") == phone:
                p["tags"] = list(p.get("tags") or []) + [{"title": tag}]
        return self._write("add_phone_tag", (uuid, phone, tag))

    def post_note(self, uuid, message, pinned=False):
        return self._write("post_note", (uuid, message))

    def assign(self, uuid, user):
        return self._write("assign", (uuid, user))

    # ---- replies
    def lead(self, phone: str, record: str, to: str = SELLER, **rec_kw) -> str:
        """A number we have texted, on a record the fake account holds."""
        store, crm = self.m.store, self.m.crm
        if record not in self.recs:
            self.put(_record(record, phone, **rec_kw))
        store.map_phone(phone, record_uuid=record, context=crm.deal_context(record))
        store.ensure_conversation(phone, from_number=to, record_uuid=record)
        return phone

    def sm(self, phone: str, text: str, to: str = SELLER) -> dict:
        """One reply to a smrtPhone text, through the real engine."""
        self.n += 1
        return self.m.engine.process("smrtphone", {"event": "smsIncoming", "smsId": f"bp-{self.n}", "from": phone,
                                                    "to": to, "message": text})

    def sc(self, phone: str, text: str, contact: str) -> dict:
        """One reply to a Smarter Contact campaign, through the real handler."""
        self.n += 1
        self.stub.sc_threads.setdefault(contact, []).append(
            {"id": f"bp{self.n}", "dir": "in", "body": text, "created_at": "z", "raw_direction": "INCOMING",
             "manual": True})
        return self.m.engine.process("smartercontact", {
            "id": f"evt_bp{self.n}", "event": "message.received.v1",
            "data": {"message_id": f"bpm{self.n}", "contact_id": contact, "from_number": "+1" + phone,
                     "to_number": "+18655550100", "content": text}})

    def sc_rows(self, phone: str, kind: str = "") -> list[dict]:
        q = "SELECT * FROM sc_outbox WHERE phone=?" + (" AND kind=?" if kind else "") + " ORDER BY id"
        return [dict(x) for x in self.m.store._conn().execute(q, (phone, kind) if kind else (phone,))]

    def rows(self, phone: str) -> list[dict]:
        return [dict(x) for x in self.m.store._conn().execute(
            "SELECT * FROM outbox WHERE phone=? ORDER BY id", (phone,))]

    def writes_since(self, mark: int) -> list[tuple]:
        return self.stub.crm_writes[mark:]

    def shape(self, writes) -> set:
        """What a run of CRM writes amounts to, with the record, the number and the wording of
        the note taken out, so the same reply on two paths can be compared."""
        out = set()
        for name, args in writes:
            if name == "set_phone_status":
                out.add(("phone", args[2]))
            elif name == "set_status":
                out.add(("status", args[1]))
            elif name == "add_tags":
                for tag in args[1]:
                    out.add(("tag", tag))
            elif name == "add_list":
                out.add(("list", args[1]))
            elif name == "add_phone_tag":
                out.add(("phone tag", args[2]))
            elif name == "post_note":
                out.add(("note",))
            elif name == "assign":
                out.add(("assign",))
        return out

    def drain(self, live: bool = False) -> dict:
        """One pass of the worker's send loop, with the send window open."""
        config, worker = self.m.config, self.m.worker
        was = config.DRY_RUN
        config.DRY_RUN = not live
        try:
            return worker.drain_outbox(limit=50)
        finally:
            config.DRY_RUN = was

    def clear_queue(self) -> None:
        self.m.store.cancel_queued_all("selftest: cleared between parts")

    def clear_sc(self) -> None:
        """Nothing left waiting in the Smarter Contact queue: an earlier part's unsent answer
        would otherwise go out in a later part's send pass and be counted as its own."""
        with self.m.store.tx() as c:
            c.execute("UPDATE sc_outbox SET status='cancelled', detail='selftest: cleared between parts'"
                      " WHERE status IN ('queued','held')")


def run(r, stub) -> None:
    from . import (classify, config, crm, digest, dispositions, engine, escalate, gate_rules, gatekeeper,
                   numberhealth, ratebudget, receiver, respond, revocation, sc_client, sc_inbound, seed,
                   sender_pool, smrtphone, speedtolead, store, touchlog, transport, worker)
    from .knowledge import lead_texts, sc_replies

    mods = SimpleNamespace(**{k: v for k, v in locals().items() if k not in ("r", "stub")})
    kit = Kit(stub, mods)

    saved_cfg = {k: getattr(config, k) for k in CONFIG_KEYS}
    patched = [(crm, n) for n in ("get_record", "add_tags", "add_list", "set_status", "post_note", "assign",
                                  "set_phone_status", "add_phone_tag", "phone_is_dnc", "resolve_preset",
                                  "fetch_cohort", "dial_tier_uuids", "custom_fields", "deal_context")]
    patched += [(classify, "classify_rules"), (classify, "classify_llm"), (sender_pool, "within_quiet_hours"),
                (sender_pool, "next_send_window"), (worker, "time"), (sc_client, "contact_id_for"),
                (transport, "send"), (sc_inbound, "listing_status")]
    saved_fn = [(mod, name, getattr(mod, name)) for mod, name in patched]

    try:
        # ---- one known install, whatever this machine's .env says ----
        config.PHASE, config.DRY_RUN = 2, True
        for key in ("GATE_READ_FIELDS", "OWNER_REVOKE", "DISPOSITION_TABLE", "SC_EXTRA_LABELS", "HAND_RAISE_NEW_LEAD",
                    "RATE_BUDGET", "TOUCH_FEED", "REST_FLAGGED", "REQUIRE_VISIBLE_DNC"):
            setattr(config, key, False)
        config.GATE_MODE, config.SEND_RECHECK, config.SPEED_TO_LEAD = "off", "off", "off"
        config.OVER_VALUE, config.LISTED_STATUS, config.GATE_SETTINGS_RAW = "not_interested", "", ""
        config.TAG_DEATH_REPORTED, config.NOT_NOW_STATUS = "", "follow_up"
        config.TAG_CONFIRM_CALL, config.TAG_RELATIVE, config.TAG_REPLY_ON_LEAD = "Confirm By Call", "Relative", "Reply On Lead"
        config.TAG_OVER_VALUE, config.COLD_LEAD_STATUS, config.NEW_LEAD_STATUS = "Ask Over Value", "Cold Lead", "new_lead"
        config.NEW_LEAD_PROTECTED = {"sold", "closed", "close_out", "dnc", "opt_out", "buyer", "buyer_found", "buyer_lost"}
        config.SC_LABEL_CORRECT, config.SC_LABEL_WRONG_DNC = "Correct Number", "Wrong DNC"
        config.SC_LABEL_RELATIVE, config.SC_LABEL_NURTURE = "Relative", "Nurture"
        config.SC_AUTO_REPLY = config.SC_AUTO_LABEL = True
        config.SC_REPLY_DELAY_MIN = config.SC_REPLY_DELAY_MAX = 0
        config.SMRTPHONE_NUMBERS_RAW = json.dumps({"Adriana": [SELLER, SELLER2], "Dispo": [BUYER, "+18650000092"]})
        config.MIN_SEND_GAP_SECONDS, config.DAILY_CAP_PER_NUMBER, config.POOL_CAPS, config.POOL_GAPS = 0, 100000, {}, {}
        config.SENSITIVE_SLACK_LEGAL_ONLY, config.SLACK_INTERESTED_ONLY, config.ANSWER_WHO = True, True, True
        config.HANDOFF_NAME, config.LEAD_CALLER_NAME, config.HANDOFF_ASSIGNEE_UUID = "Adriana", "Adriana", ""
        config.SC_NI_STATUS, config.SC_NI_OVERWRITE = "not_interested", {"", "prospecting"}
        config.SC_PRICE_CEILING, config.SC_LISTED_MLS = 1.25, {"listed", "pending", "active"}
        config.ESCALATE_INTENTS, config.LIST_DNC = ["INTERESTED"], "DNC"
        config.TAG_OPT_OUT, config.TAG_MAIL_ONLY, config.BUSINESS_TZ = "Do Not Market", "Mail Only", "America/New_York"
        config.RATE_PER_MIN, config.RATE_PER_HOUR, config.RATE_QUIET, config.RATE_QUIET_PER_MIN = 10, 0, "06:25-07:15", 3
        config.RATE_MAX_WAIT, config.REST_DAYS, config.CANARY_TO, config.SPEED_TO_LEAD_DELAY = 180, 5, "", 180
        config.SPEED_TO_LEAD_SEQUENCES = ["call new lead", "new lead"]

        # The fake account: reads come from kit.recs, and a write changes what the next read sees.
        crm.get_record = kit.get_record
        for name in ("add_tags", "add_list", "set_status", "post_note", "assign", "set_phone_status", "add_phone_tag"):
            setattr(crm, name, getattr(kit, name))
        crm.phone_is_dnc = lambda number: None
        crm.custom_fields = lambda uuid: {}
        crm.deal_context = lambda uuid: {"owner_first": "Dana", "street": "12 Elm St", "city": "Maryville",
                                         "county": "Blount", "assigned_name": "Adriana", "record_uuid": uuid}
        sc_inbound.listing_status = lambda rec: ""
        # An earlier section replaces the rules with a test double and leaves it in. The real
        # rules are what these replies have to be read by.
        rules = classify.classify_rules
        while getattr(rules, "__closure__", None):
            inner = [c.cell_contents for c in rules.__closure__ if callable(c.cell_contents)]
            if not inner:
                break
            rules = inner[0]
        classify.classify_rules = rules
        classify.classify_llm = lambda text, history=None: classify.Classification("OTHER", 0.0, "fallback", "stubbed")
        sender_pool.within_quiet_hours = lambda phone, at=None: True
        worker.time = SimpleNamespace(sleep=lambda s: None, time=time.time)

        def send(to, body, frm=""):
            kit.sent.append((to, body, frm))
            return smrtphone.SendResult(True, sms_id=f"bp-sent-{len(kit.sent)}")

        transport.send = send
        kit.clear_queue()

        _switches(r, kit)
        _gate(r, kit)
        _registry(r, kit)
        _seeder_gate(r, kit)
        _table(r, kit)
        _revocation(r, kit)
        _budget(r, kit)
        _touch_feed(r, kit)
        _hand_raise(r, kit)
        _speed_to_lead(r, kit)
        _number_health(r, kit)
    finally:
        for key, value in saved_cfg.items():
            setattr(config, key, value)
        for mod, name, value in saved_fn:
            setattr(mod, name, value)
        gatekeeper._settings.update(raw=None, cfg=None)


# ====================================================================== switches
def _switches(r, kit) -> None:
    config = kit.m.config
    print("\nbuild plan: every new behavior is behind a switch that is off")
    names = [s[0] for s in config.SWITCHES]
    r.check("every switch is listed once, with its default and what turning it on changes",
            len(names) == len(set(names)) == 13 and all(len(s) == 4 and s[2] and s[3] for s in config.SWITCHES),
            str(names))
    r.check("every switch defaults to what the app did before it existed",
            all(s[1] in ("0", "off", "", "not_interested") for s in config.SWITCHES),
            str([(s[0], s[1]) for s in config.SWITCHES if s[1] not in ("0", "off", "", "not_interested")]))
    os.environ["SMS_AGENT_SELFTEST_CHOICE"] = "enforced"
    r.check("a mistyped mode is the default, never a guess",
            config._choice("SMS_AGENT_SELFTEST_CHOICE", "off", ("off", "report", "enforce")) == "off")
    os.environ["SMS_AGENT_SELFTEST_CHOICE"] = " Enforce \r"
    r.check("a mode with stray spaces and a carriage return still reads",
            config._choice("SMS_AGENT_SELFTEST_CHOICE", "off", ("off", "report", "enforce")) == "enforce")
    os.environ.pop("SMS_AGENT_SELFTEST_CHOICE", None)
    r.check("a flag reads 1, true, yes and on as on, and anything else as off",
            all(config._flag("SMS_AGENT_NO_SUCH_FLAG", v) for v in ("1", "true", "YES", "on"))
            and not any(config._flag("SMS_AGENT_NO_SUCH_FLAG", v) for v in ("0", "", "off", "maybe")))


# ====================================================================== the gate (0.1)
def _gate(r, kit) -> None:
    G = kit.m.gate_rules
    print("\nbuild plan 0.1: the suppression gate, one rule at a time")
    m1 = _phone("8650007000")
    land = _phone("8650007001", kind="LANDLINE")

    def clean() -> dict:
        # Whole copies: a check that tags one phone must not tag it for every check after it.
        return _record("bp-g0", copy.deepcopy(m1), copy.deepcopy(land))

    def held(change, channel="text", **kw):
        rec = clean()
        change(rec)
        return G.check(rec, channel, **kw)

    ok = G.check(clean(), "call")
    r.check("a clean record passes, and a call may use its top-tier numbers",
            ok.ok and [n["number"] for n in ok.numbers] == ["8650007000", "8650007001"], str(ok.reasons))
    r.check("a text may use only a mobile",
            [n["number"] for n in G.check(clean(), "text").numbers] == ["8650007000"])
    r.check("Do Not Market stops a text and a call",
            held(lambda x: x["tags"].append("Do Not Market")).code == "do_not_market"
            and held(lambda x: x["tags"].append("do not market"), "call").code == "do_not_market")
    r.check("nothing a caller is allowed lifts Do Not Market",
            not held(lambda x: x["tags"].append("Do Not Market"), allow=tuple(G.RULES) + ("heir", "deceased")).ok)
    r.check("a Litigator tag on the record, or on one phone, holds the whole record",
            held(lambda x: x["tags"].append("Litigator")).code == "litigator"
            and held(lambda x: x["owner"]["phones"][1]["tags"].append({"title": "Litigator"})).code == "litigator")
    r.check("a number this app already knows as a litigator's holds the record",
            G.check(clean(), "text", litigators={"8650007001"}).code == "litigator")
    r.check("the owner's opt-out flag, do-not-call flag, the DNC list and an opt-out status all hold it",
            [held(lambda x: x["owner"].update(opt_out=True)).code, held(lambda x: x["owner"].update(dnc=True)).code,
             held(lambda x: x["lists"].append("DNC")).code, held(lambda x: x.update(status="opt_out")).code]
            == ["opted_out"] * 4)
    r.check("a sold tag and a sold status both hold it",
            held(lambda x: x["tags"].append("recently sold")).code == "sold"
            and held(lambda x: x.update(status="sold"), "call").code == "sold")
    deaths = [held(lambda x: x["owner"].update(deceased=True)), held(lambda x: x["tags"].append("deceased")),
              held(lambda x: x["lists"].append("Obituary")), G.check(clean(), "text", custom={"Owner Deceased": "Yes"})]
    r.check("a death is read from all four places: the owner flag, a tag, the Obituary list, the Owner Deceased field",
            all(v.code == "deceased" for v in deaths), str([v.code for v in deaths]))
    r.check("the reason says where the death was recorded, and to research first",
            "Obituary list" in deaths[2].reason() and "research first" in deaths[2].reason(), deaths[2].reason())
    r.check("a probate record is not held by the Obituary list (its owner is the court's representative)",
            held(lambda x: x["lists"].extend(["Obituary", "Probate"])).ok)
    r.check("an explicit deceased flag still holds a probate record",
            not held(lambda x: (x["lists"].append("Probate"), x["owner"].update(deceased=True))).ok)
    r.check("the heir lane may text a dead owner's record only once research tagged a verified heir",
            not held(lambda x: x["owner"].update(deceased=True), allow=("heir",)).ok
            and held(lambda x: (x["owner"].update(deceased=True), x["tags"].append("Heir Identified")), allow=("heir",)).ok)
    r.check("a lead is never texted by a prospecting batch",
            held(lambda x: x.update(status="Hot Lead")).code == "lead"
            and held(lambda x: x.update(status="follow_up"), "call").code == "lead")
    r.check("a lead-management text may go to a lead", held(lambda x: x.update(status="new_lead"), allow=("lead",)).ok)
    r.check("Not Interested is finished, and only the second-chance allowance lifts it",
            held(lambda x: x.update(status="not_interested")).code == "finished"
            and held(lambda x: x.update(status="not_interested"), allow=("not_interested",)).ok
            and not held(lambda x: x.update(status="Dead Lead"), allow=("not_interested",)).ok)
    r.check("a status the gate has never seen holds the record",
            held(lambda x: x.update(status="Brand New Status")).code == "unknown_status")
    r.check("an install can name its own working statuses",
            held(lambda x: x.update(status="Brand New Status"), cfg=G.GateConfig(working_statuses=["Brand New Status"])).ok)
    r.check("a record held for research is not texted",
            held(lambda x: x.update(status="Deep Prospecting")).code == "in_research")
    r.check("a listed house gets no cold text, and an MLS value of Sold alone holds nothing",
            held(lambda x: x.update(mls="Listed")).code == "listed" and held(lambda x: x.update(mls="Sold")).ok)
    r.check("Mail Only stops a text and a call",
            held(lambda x: x["tags"].append("Mail Only")).code == "mail_only"
            and held(lambda x: x["tags"].append("Mail Only"), "call").code == "mail_only")
    r.check("Not Single Family holds the record", held(lambda x: x["tags"].append("Not Single Family")).code == "not_single_family")
    r.check("a record whose only number is Drop tier, wrong or do-not-call has nothing to text",
            [held(lambda x: x["owner"].update(phones=[_phone("8650007002", tags=("Drop",))])).code,
             held(lambda x: x["owner"].update(phones=[_phone("8650007000", status="WRONG")])).code,
             held(lambda x: x["owner"].update(phones=[_phone("8650007000", status="CORRECT_DNC")])).code]
            == ["no_number"] * 3)
    r.check("a number a person confirmed is usable whatever its tier",
            held(lambda x: x["owner"].update(phones=[_phone("8650007002", status="CORRECT", tags=("Drop",))])).ok)
    r.check("a landline may be called and is never texted",
            held(lambda x: x["owner"].update(phones=[dict(land)]), "call").ok
            and held(lambda x: x["owner"].update(phones=[dict(land)])).code == "no_number")
    flagged = {"phone": {"number": "8650007000", "doNotCall": True}}
    r.check("the number the do-not-call registry flagged is left out, where the search row shows the flag",
            G.check(clean(), "text", row=flagged).code == "no_number"
            and [n["number"] for n in G.check(clean(), "call", row=flagged).numbers] == ["8650007001"])
    r.check("an exhausted call round stops calls and not texts",
            held(lambda x: x.update(status="Exhausted"), "call").code == "exhausted"
            and held(lambda x: x.update(status="Exhausted")).ok)
    r.check("do-not-mail and returned mail do not stop a text",
            held(lambda x: x.update(do_not_mail_ever=True)).ok and held(lambda x: x.update(status="Return Mail")).ok)
    owner = {"lane": "lead", "records": {"other": "lead"}}
    r.check("an owner who is a lead on another record is held here",
            G.check(clean(), "text", lane="ftm", owner=owner).code == "owner_lane")
    r.check("a record that cannot be read is held", G.check({}, "text").code == "unreadable" and not G.check(None, "call").ok)
    try:
        G.check(clean(), "mail")
        r.check("the texting app's gate refuses a channel it does not answer for", False, "no error raised")
    except ValueError:
        r.check("the texting app's gate refuses a channel it does not answer for", True)
    r.check("the most serious reason is the one reported",
            held(lambda x: (x["tags"].extend(["Mail Only", "Do Not Market"]), x.update(status="sold"))).code == "do_not_market")

    # ---- held to the brain's own gate
    root = Path(__file__).resolve().parents[2]
    parity = root / "tests" / "test_sms_gate_parity.py"
    if not parity.exists():
        # The cloud image carries only this package: neither the test nor the brain's gate is there.
        r.check("gate parity is checked in the repository (this copy has no tests folder)", True)
        return
    spec = importlib.util.spec_from_file_location("test_sms_gate_parity", parity)
    module = importlib.util.module_from_spec(spec)
    failed, total = [], [0]

    def check(cond, msg):
        total[0] += 1
        if not cond:
            failed.append(msg)

    try:
        os.environ.setdefault("AGENTS_DATA_DIR", str(Path(str(kit.m.config.DB_PATH)).parent / "brain"))
        spec.loader.exec_module(module)
        module.run(check, rounds=1500)
    except Exception as exc:  # noqa: BLE001 - a parity run that cannot start is a failure, not a skip
        failed.append("the parity test could not run: %s" % str(exc)[:200])
    r.check("the texting app's gate and the brain's gate give the same verdict on every rule (%d comparisons)" % total[0],
            not failed and total[0] > 200, "; ".join(failed[:3]))


# ====================================================================== the registry flag (0.22)
def _registry(r, kit) -> None:
    G, config, crm, seed, gatekeeper = kit.m.gate_rules, kit.m.config, kit.m.crm, kit.m.seed, kit.m.gatekeeper
    print("\nbuild plan 0.22: one rule for the do-not-call registry flag")
    r.check("a phone carrying the registry flag is never textable",
            G.do_not_text({"number": "8650007003", "status": "UNKNOWN", "doNotCall": True}) == G.REGISTRY)
    r.check("a number seen flagged on a search row is refused wherever it turns up",
            G.do_not_text({"number": "8650007003", "status": "CORRECT"}, {"8650007003"}) == G.REGISTRY)
    r.check("a phone status of wrong, dead or any do-not-call variant is refused",
            all(G.do_not_text({"number": "8650007004", "status": s}) for s in ("WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC")))
    r.check("a clean, confirmed or unanswered number is not refused",
            not any(G.do_not_text({"number": "8650007005", "status": s}) for s in ("UNKNOWN", "", "CORRECT", "NO_ANSWER", None)))
    r.check("the seeder's skip list is the gate's own set, not a copy of it", seed.SKIP_PHONE_STATUSES is G.BAD_PHONE)
    rows = [{"phone": {"number": "+1 (865) 000-7003", "doNotCall": True}}, {"phone": {"number": "8650007005"}},
            {"phone": {"number": "8650007006", "doNotCall": False}}, {"phone": None}, {}]
    r.check("the flagged numbers are collected off a page of search rows", G.flagged_numbers(rows) == {"8650007003"})

    def row(uuid, number, **phone):
        return {"uuid": uuid, "phone": dict({"number": number, "type": "MOBILE", "status": "UNKNOWN", "tags": []}, **phone),
                "address": {"street": "9 Oak Ave", "city": "Maryville"}, "owner": {"first_name": "Sam", "last_name": "Poe"}}

    cohort = [row("bp-c1", "8650007003", doNotCall=True), row("bp-c2", "8650007004", status="CORRECT_DNC"),
              row("bp-c3", "8650007005"), row("bp-c4", "8650007006", status="DEAD", doNotCall=True)]
    crm.resolve_preset = lambda title: ({"any_tags": ["x"]}, title)
    crm.fetch_cohort = lambda must, limit=0: copy.deepcopy(cohort)
    crm.dial_tier_uuids = lambda *a, **k: {}
    stats: dict = {}
    got, _ = seed.from_preset("Selftest preset", stats=stats)
    r.check("a preset sweep keeps the clean number and drops the flagged and the do-not-call ones",
            [x["uuid"] for x in got] == ["bp-c3"], str([x["uuid"] for x in got]))
    r.check("the sweep counts each drop under the reason it always used",
            stats.get("phone flagged do-not-call") == 2 and stats.get("phone status DNC/WRONG/DEAD") == 1
            and stats.get("kept") == 1 and stats.get("cohort") == 4, str({k: v for k, v in stats.items() if k != "_dnc_numbers"}))
    r.check("a flagged row is dropped as flagged whatever its status says, and its number is carried forward",
            stats.get("_dnc_numbers") == {"8650007003", "8650007006"}, str(stats.get("_dnc_numbers")))
    deep_stats: dict = {}
    deep, _ = seed.from_preset("Selftest preset", keep_unresolved=True, stats=deep_stats)
    r.check("a deep source keeps a do-not-call STATUS row for a look at the whole record, never a flagged one",
            sorted(x["uuid"] for x in deep) == ["bp-c2", "bp-c3"] and deep[0].get("_needs_best_phone"),
            str([(x["uuid"], x.get("_needs_best_phone")) for x in deep]))
    r.check("with the gate off, nothing is remembered between sweeps (today's behavior)",
            not gatekeeper.is_flagged("8650007003"))

    kit.put(_record("bp-best", _phone("8650007003"), _phone("8650007007", tags=("Dial Second",))))
    crm.phone_is_dnc = lambda number: False
    best, why = seed.resolve_best_phone({"uuid": "bp-best", "phone": "8650007007"}, set())
    r.check("with the gate off, a number flagged in an EARLIER sweep is invisible and gets picked",
            bool(best) and best["phone"] == "8650007003", f"{best and best.get('phone')} ({why})")
    config.GATE_MODE = "enforce"
    seed.from_preset("Selftest preset", stats={})
    r.check("with the gate on, the flagged numbers a sweep sees are remembered",
            gatekeeper.is_flagged("8650007003") and gatekeeper.is_flagged("+1 865-000-7006")
            and not gatekeeper.is_flagged("8650007005"))
    best, why = seed.resolve_best_phone({"uuid": "bp-best", "phone": "8650007007"}, set())
    r.check("and a remembered number is never the one picked, even as the record's best tier",
            bool(best) and best["phone"] == "8650007007", f"{best and best.get('phone')} ({why})")
    r.check("the gate refuses the remembered number by name when asked about it",
            kit.m.gate_rules.REGISTRY in gatekeeper.reasons(kit.recs["bp-best"], phone="8650007003"),
            str(gatekeeper.reasons(kit.recs["bp-best"], phone="8650007003")))
    crm.phone_is_dnc = lambda number: True
    kit.put(_record("bp-probe", _phone("8650007008")))
    best, why = seed.resolve_best_phone({"uuid": "bp-probe", "phone": "8650007008"}, set())
    r.check("a number the live probe says is flagged is refused and remembered",
            best is None and "do-not-call" in why and gatekeeper.is_flagged("8650007008"), str(why))
    crm.phone_is_dnc = lambda number: None
    config.GATE_MODE = "off"


# ====================================================================== the gate in front of the seeder (0.1)
def _seeder_gate(r, kit) -> None:
    config, seed, store, gatekeeper = kit.m.config, kit.m.seed, kit.m.store, kit.m.gatekeeper
    print("\nbuild plan 0.1: the gate in front of the seeder, when a record is loaded and again at release")
    cases = {
        "clean": {}, "dnm": {"tags": ("FTM", "Do Not Market")}, "dead": {"owner_flag": True},
        "obit": {"lists": ("Obituary",)}, "lead": {"status": "Hot Lead"}, "sold": {"tags": ("FTM", "recently sold")},
        "listed": {"mls": "Listed"}, "dnc": {"lists": ("Foreclosure", "DNC")}, "ni": {"status": "not_interested"},
        "mail": {"tags": ("FTM", "Mail Only")}, "buyer": {"status": "buyer_found", "tags": ("recently sold",)},
    }
    rows = []
    for i, (name, kw) in enumerate(cases.items()):
        kw = dict(kw)
        dead = kw.pop("owner_flag", False)
        number = f"86500104{i:02d}"
        rec = kit.put(_record("bp-s-" + name, number, **kw))
        rec["owner"]["deceased"] = dead
        rows.append({"phone": number, "uuid": rec["uuid"], "street": f"{10 + i} Elm St", "city": "Maryville",
                     "county": "Blount", "first": "Dana", "last": "Reed", "owner": "Dana Reed", "assigned": "Adriana"})
    kit.put(_record("bp-s-lit", _phone("8650007009", tags=("Dial First", "Litigator"))))
    rows.append(dict(rows[0], phone="8650007009", uuid="bp-s-lit", street="30 Elm St"))

    def by_name(cands):
        return {c.record_uuid.replace("bp-s-", ""): c for c in cands}

    reads = len(kit.reads)
    off = by_name(seed.build(copy.deepcopy(rows), touch=2))
    r.check("with the gate off, the seeder holds nobody the gate would (today's behavior)",
            all(c.status == "ready" and not c.gate for c in off.values()), str({k: c.reasons for k, c in off.items() if c.reasons}))
    r.check("and with it off, the gate does not read a single record", len(kit.reads) == reads, f"{len(kit.reads) - reads} reads")

    config.GATE_MODE = "report"
    rep = seed.build(copy.deepcopy(rows), touch=2)
    said = by_name(rep)
    r.check("in report mode nobody is held, and each record says what the gate would do",
            all(c.status == "ready" for c in rep) and not said["clean"].gate
            and said["dnm"].gate == "tagged Do Not Market" and "research first" in said["dead"].gate, str({k: c.gate for k, c in said.items()}))
    summary = seed.summary(rep)
    r.check("the batch summary counts what enforcing the gate would take out",
            summary["gate"]["mode"] == "report" and sum(summary["gate"]["reasons"].values()) == len(rows) - 1,
            str(summary.get("gate")))

    config.GATE_MODE = "enforce"
    enf = by_name(seed.build(copy.deepcopy(rows), touch=2))
    want = {"dnm": "tagged Do Not Market", "dead": "the owner has died or may have (owner flag); research first",
            "obit": "the owner has died or may have (Obituary list); research first", "lead": "already a lead (hot lead)",
            "sold": "the house sold", "listed": "listed on the MLS", "dnc": "the owner opted out (DNC list)",
            "ni": "status not_interested", "mail": "mail only: the owner is not to be called or texted",
            "lit": "a litigator's number is on the record", "buyer": "the house sold"}
    wrong = {k: (enf[k].status, enf[k].reasons) for k, v in want.items() if enf[k].status != "hold" or enf[k].reasons != [v]}
    r.check("enforced, each record is held with the gate's own reason", not wrong, str(wrong))
    r.check("and the clean record is still ready", enf["clean"].status == "ready" and not enf["clean"].reasons)
    bare = seed.build([dict(rows[0], uuid="", phone="8650007018")], touch=2)
    r.check("enforced, a number with no record behind it is held: no answer is not a yes",
            bare[0].status == "hold" and "no record" in bare[0].reasons[0], str(bare[0].reasons))
    gone = seed.build([dict(rows[0], uuid="bp-s-missing", phone="8650007019")], touch=2)
    r.check("a record that cannot be read is held", gone[0].status == "hold", str(gone[0].reasons))

    buyers = by_name(seed.build(copy.deepcopy(rows), touch=2, new_deal=True))
    r.check("a deal blast holds a buyer only on the person-level rules: sold and buyer_found are what make a buyer",
            buyers["buyer"].status == "ready" and buyers["sold"].status == "ready" and buyers["ni"].status == "ready",
            str({k: buyers[k].reasons for k in ("buyer", "sold", "ni")}))
    r.check("a deal blast still never reaches Do Not Market, an opt-out, a litigator or a dead owner",
            all(buyers[k].status == "hold" for k in ("dnm", "dnc", "lit", "dead")), str({k: buyers[k].status for k in ("dnm", "dnc", "lit", "dead")}))

    heir = dict(rows[0], phone="8650007010", uuid="bp-s-heir", street="40 Elm St")
    rec = kit.put(_record("bp-s-heir", "8650007010"))
    rec["owner"]["deceased"] = True
    r.check("the heir campaign is held on a dead owner's record until research tags a verified heir",
            seed.build([dict(heir)], touch=2, gate_allow=("heir",))[0].status == "hold")
    rec["tags"].append("Heir Identified")
    r.check("and may text it once the heir is verified",
            seed.build([dict(heir)], touch=2, gate_allow=("heir",))[0].status == "ready")

    # ---- the same question again at release
    kit.clear_queue()
    kit.put(_record("bp-s-r1", "8650007011"))
    kit.put(_record("bp-s-r2", "8650007012"))
    kit.put(_record("bp-s-r3", "8650007013"))
    batch = seed.build([dict(rows[0], phone=f"865000701{i}", uuid=f"bp-s-r{i}", street=f"5{i} Elm St") for i in (1, 2, 3)], touch=2)
    config.DRY_RUN = False
    try:
        staged = seed.queue(batch, touch=2)
    finally:
        config.DRY_RUN = True
    r.check("three clean records are staged, held for a person to release", staged["queued"] == 3
            and all(kit.rows(f"865000701{i}")[-1]["status"] == "held" for i in (1, 2, 3)), str(staged))
    kit.recs["bp-s-r2"]["tags"].append("Do Not Market")         # the owner opted out after staging
    del kit.recs["bp-s-r3"]                                      # and one record cannot be read today
    store.queue_message("8650007017", "staged before the gate, with no record behind it", from_number=SELLER,
                        status="held", intent="seed_touch_2")
    released = seed.release(2)
    last = dict(seed.LAST_RELEASE)
    r.check("at release the gate asks again: the clean text goes",
            released == 1 and kit.rows("8650007011")[-1]["status"] == "queued", str(last))
    row2 = kit.rows("8650007012")[-1]
    r.check("the one whose owner opted out after staging is cancelled, with the reason",
            row2["status"] == "cancelled" and row2["error"] == "held at release: tagged Do Not Market", str(row2["error"]))
    r.check("and a record that could not be re-read stays staged for the next release, never sent on no answer",
            kit.rows("8650007013")[-1]["status"] == "held" and last["cancelled"] == 1, str(last))
    r.check("a staged number with no record behind it stays staged too, as it would have been held at loading",
            kit.rows("8650007017")[-1]["status"] == "held" and last["left_staged"] == 2, str(last))
    kit.clear_queue()
    kit.put(_record("bp-s-r5", "8650007015"))
    kit.put(_record("bp-s-r6", "8650007016"))
    second = seed.build([dict(rows[0], phone=f"865000701{i}", uuid=f"bp-s-r{i}", street=f"5{i} Elm St") for i in (5, 6)],
                        touch=2)
    config.DRY_RUN = False
    try:
        seed.queue(second, touch=2)
    finally:
        config.DRY_RUN = True
    kit.recs["bp-s-r6"]["tags"].append("Do Not Market")
    config.GATE_MODE = "report"
    released = seed.release(2)
    last = dict(seed.LAST_RELEASE)
    r.check("in report mode release lets every staged text go, and says what enforcing would have held",
            released == 2 and last["held_by_gate"] == {"tagged Do Not Market": 1} and last["cancelled"] == 0
            and kit.rows("8650007016")[-1]["status"] == "queued", str(last))
    kit.clear_queue()
    config.GATE_MODE = "off"
    kit.put(_record("bp-s-r4", "8650007014", tags=("FTM", "Do Not Market")))
    store.map_phone("8650007014", record_uuid="bp-s-r4")
    store.ensure_conversation("8650007014", from_number=SELLER, record_uuid="bp-s-r4")
    store.queue_message("8650007014", "staged before the gate existed", from_number=SELLER, status="held",
                        intent="seed_touch_2")
    reads = len(kit.reads)
    r.check("with the gate off, release moves every staged text and reads nothing (today's behavior)",
            seed.release(2) == 1 and len(kit.reads) == reads and seed.LAST_RELEASE["gate"] == "off")
    kit.clear_queue()
    gatekeeper._settings.update(raw=None, cfg=None)
    config.GATE_SETTINGS_RAW = '{"working_statuses": ["Brand New Status"], "no_such_key": 1}'
    r.check("a gate setting it does not know is refused whole, and the strictest defaults apply",
            gatekeeper.settings().working_statuses == [])
    config.GATE_SETTINGS_RAW = '{"working_statuses": ["Brand New Status"]}'
    r.check("a valid gate setting is read", gatekeeper.settings().working_statuses == ["Brand New Status"])
    config.GATE_SETTINGS_RAW = ""
    gatekeeper._settings.update(raw=None, cfg=None)


# ====================================================================== one table for every no (0.4)
def _table(r, kit) -> None:
    config, store, D, crm = kit.m.config, kit.m.store, kit.m.dispositions, kit.m.crm
    classify, stub, sc_replies = kit.m.classify, kit.stub, kit.m.sc_replies
    print("\nbuild plan 0.4: one table for every no, on both texting paths")

    r.check("every kind of no has one row, and every row says what it writes in a sentence",
            set(D.TABLE) >= {"stop_all", "wrong_stop", "wrong_number", "relative", "legal_stop", "leave_alone",
                             "bereavement", "crisis", "plain_no", "hostile_no", "bare_no", "not_now", "hand_raise"}
            and all(row.what and row.kind == k for k, row in D.TABLE.items()))
    reads = {
        "Please stop texting me": ("OPT_OUT", "stop_all"),
        "wrong number, stop texting me": ("OPT_OUT", "wrong_stop"),
        "wrong number, I don't own any property": ("WRONG_NUMBER", "wrong_number"),
        "That's my dad's house, I'm not the owner": ("WRONG_NUMBER", "relative"),
        "my attorney will be in touch about this harassment": ("ESCALATE", "legal_stop"),
        "Leave this family alone, my father died and the house is not for sale": ("ESCALATE", "leave_alone"),
        "Judy died in 2022": ("ESCALATE", "bereavement"),
        "my father died. Im blowing my brains out Wednesday": ("ESCALATE", "crisis"),
        "not interested": ("NOT_INTERESTED", "plain_no"),
        "this is a scam, not interested": ("NOT_INTERESTED", "hostile_no"),
        "No": ("OTHER", "bare_no"),
        "not right now, maybe next year": ("NOT_INTERESTED", "not_now"),
        "How much would you pay for it?": ("INTERESTED", "hand_raise"),
        "who is this?": ("ASKING_WHO", ""),
    }
    got = {t: (classify.classify(t).intent, D.decide(classify.classify(t).intent, t)) for t in reads}
    r.check("each reply is read as its kind", got == reads, str({t: v for t, v in got.items() if v != reads[t]}))
    r.check("a bare No is never a wrong number, however sure the read",
            D.decide("WRONG_NUMBER", "No") == "bare_no" and D.decide("NOT_INTERESTED", "nope.") == "bare_no")
    r.check("'not now, not ever' is a no, not a later lead",
            D.decide("NOT_INTERESTED", "not now, not ever") == "plain_no")
    r.check("a relative is read only when the sender says who they are to the owner",
            D.is_relative("this is his daughter") and D.is_relative("that was my mom's house")
            and not D.is_relative("not my house") and not D.is_relative("my house is not for sale"))

    # ---- today's behavior, with the table off
    kit.lead("8650007020", "bp-t-off")
    mark = len(stub.crm_writes)
    out = kit.sm("8650007020", "not interested")
    r.check("with the table off, a smrtPhone 'not interested' writes nothing at all (the gap this closes)",
            out.get("action") == "classified" and not kit.writes_since(mark), str(kit.writes_since(mark)))

    config.DISPOSITION_TABLE = True
    texts = {kind: text for text, (_, kind) in reads.items() if kind and kind not in ("hand_raise", "crisis")}
    same, binds, labels = {}, {}, {}
    for i, (kind, text) in enumerate(sorted(texts.items())):
        row = D.TABLE[kind]
        a, b = f"86500151{i:02d}", f"86500152{i:02d}"
        kit.lead(a, "bp-t-sm-" + kind)
        kit.lead(b, "bp-t-sc-" + kind, to="")
        mark = len(stub.crm_writes)
        first = kit.sm(a, text)
        on_smrtphone = kit.shape(kit.writes_since(mark))
        mark = len(stub.crm_writes)
        second = kit.sc(b, text, "c-t-" + kind)
        on_sc = kit.shape(kit.writes_since(mark))
        if on_smrtphone != on_sc or first.get("action") != second.get("action"):
            same[kind] = (sorted(on_smrtphone ^ on_sc), first.get("action"), second.get("action"))
        # the row is the written statement of what was written
        missing = []
        for phone, shape in ((a, on_smrtphone), (b, on_sc)):
            conv = store.get_conversation(phone) or {}
            if row.phone and ("phone", row.phone) not in shape:
                missing.append(f"phone {row.phone}")
            if not row.phone and any(x[0] == "phone" for x in shape):
                missing.append("the phone status was changed")
            if row.status and ("status", D.status_value(kind)) not in shape:
                missing.append(f"status {row.status}")
            if not row.status and any(x[0] == "status" for x in shape):
                missing.append("the record status was changed")
            missing += [f"tag {t}" for t in D.tag_names(row.tags) if ("tag", t) not in shape]
            if row.lists and ("list", "DNC") not in shape:
                missing.append("the DNC list")
            if row.phone_tags and ("phone tag", "Relative") not in shape:
                missing.append("the Relative phone tag")
            if row.suppress and not (store.is_suppressed(phone) or "").startswith(row.suppress):
                missing.append(f"suppressed as {row.suppress} (is {store.is_suppressed(phone)})")
            if not row.suppress and store.is_suppressed(phone):
                missing.append(f"suppressed ({store.is_suppressed(phone)})")
            if row.thread and conv.get("state") != row.thread:
                missing.append(f"thread {row.thread} (is {conv.get('state')})")
            if row.stops_mail != (("tag", "Do Not Market") in shape):
                missing.append("Do Not Market, which is what stops the mail")
        if missing:
            binds[kind] = sorted(set(missing))
        queued = {x["kind"]: x["body"] for x in kit.sc_rows(b)}
        want_label = D.sc_label(kind)
        if queued.get("label", "") != want_label or bool(queued.get("dnc")) != row.sc_dnc:
            labels[kind] = (queued, want_label, row.sc_dnc)
    r.check("the same reply writes the same things to Sift on the smrtPhone path and the Smarter Contact path",
            not same, str(same))
    r.check("and what each kind writes is exactly what its row in the table says", not binds, str(binds))
    r.check("on Smarter Contact each kind sets the label and the do-not-contact its row names", not labels, str(labels))
    r.check("a plain no on smrtPhone now sets Not Interested and marks the number Correct",
            ("set_status", ("bp-t-sm-plain_no", "not_interested")) in stub.crm_writes
            and any(w[0] == "set_phone_status" and w[1][0] == "bp-t-sm-plain_no" and w[1][2] == "CORRECT" for w in stub.crm_writes))
    r.check("a wrong number marks only the phone: no record status, no tag, no list",
            not any(w[1][0] == "bp-t-sm-wrong_number" and w[0] in ("set_status", "add_tags", "add_list") for w in stub.crm_writes))
    r.check("a hostile no does not mark the number Correct",
            not any(w[0] == "set_phone_status" and w[1][0] == "bp-t-sm-hostile_no" for w in stub.crm_writes))
    r.check("a bare No is tagged for one confirmation call, and the number stays unverified",
            any(w[0] == "add_tags" and w[1][0] == "bp-t-sm-bare_no" and "Confirm By Call" in w[1][1] for w in stub.crm_writes)
            and not any(w[0] == "set_phone_status" and w[1][0] == "bp-t-sm-bare_no" for w in stub.crm_writes))
    r.check("not now goes to Follow Up, not to Not Interested",
            ("set_status", ("bp-t-sm-not_now", "follow_up")) in stub.crm_writes
            and ("set_status", ("bp-t-sm-not_now", "not_interested")) not in stub.crm_writes)
    r.check("a relative's number is kept and tagged, never marked wrong, and a person is told once",
            any(w[0] == "add_phone_tag" and w[1][0] == "bp-t-sm-relative" for w in stub.crm_writes)
            and sum("a relative of the owner replied" in s for s in stub.slack) == 2, str(stub.slack[-2:])[:200])
    r.check("a family that says leave us alone gets the phone marked do-not-call and the DNC list, like any stop",
            any(w[0] == "set_phone_status" and w[1][0] == "bp-t-sm-leave_alone" and "DNC" in w[1][2] for w in stub.crm_writes)
            and ("add_list", ("bp-t-sm-leave_alone", "DNC")) in stub.crm_writes)
    r.check("a reported death is marked Mail Only and the number is NOT suppressed",
            any(w[0] == "add_tags" and w[1][0] == "bp-t-sm-bereavement" and "Mail Only" in w[1][1] for w in stub.crm_writes))
    r.check("on Smarter Contact a no cancels an answer that was still waiting to go out",
            not [x for x in kit.sc_rows("8650007021") if x["kind"] == "reply" and x["status"] == "queued"])
    apology = [x for x in kit.sc_rows(next(f"86500152{i:02d}" for i, k in enumerate(sorted(texts)) if k == "wrong_number"))]
    order = {x["kind"]: x["id"] for x in apology}
    r.check("a wrong number still gets the one apology, queued before the do-not-contact",
            "reply" in order and "dnc" in order and order["reply"] < order["dnc"], str(order))

    # ---- the rules around the table
    kit.lead("8650007022", "bp-t-buyer", to=BUYER)
    mark = len(stub.crm_writes)
    out = kit.sm("8650007022", "not interested", to=BUYER)
    conv = store.get_conversation("8650007022") or {}
    r.check("a buyer who passes on one deal stays on the buyer list: no status, no close, nothing written",
            out.get("action") == "buyer_passed" and not kit.writes_since(mark) and conv.get("state") == "active",
            f"{out.get('action')} {kit.writes_since(mark)} {conv.get('state')}")
    kit.lead("8650007023", "bp-t-buyer2", to=BUYER)
    out = kit.sm("8650007023", "STOP", to=BUYER)
    r.check("a buyer's stop is still a full stop", out.get("action") == "opted_out" and store.is_suppressed("8650007023") == "opt_out")

    kit.lead("8650007024", "bp-t-worked", status="Warm Lead")
    mark = len(stub.crm_writes)
    kit.sm("8650007024", "not interested")
    r.check("a no never overwrites the status of a record somebody is working",
            not any(w[0] == "set_status" for w in kit.writes_since(mark))
            and any(w[0] == "post_note" and "status left as warm lead" in w[1][1] for w in kit.writes_since(mark)),
            str([w for w in kit.writes_since(mark) if w[0] in ("set_status", "post_note")])[:300])
    kit.lead("8650007025", "bp-t-stopped")
    kit.sm("8650007025", "STOP")
    kit.sm("8650007025", "it is not for sale anyway, not interested")
    r.check("a later no from someone who already said stop does not reopen the thread",
            (store.get_conversation("8650007025") or {}).get("state") == "opted_out")
    config.PHASE = 1
    kit.lead("8650007026", "bp-t-phase1")
    mark = len(stub.crm_writes)
    kit.sm("8650007026", "not interested")
    r.check("at phase 1 the table still marks the number but may not flip a record status",
            any(w[0] == "set_phone_status" for w in kit.writes_since(mark))
            and not any(w[0] == "set_status" for w in kit.writes_since(mark)))
    config.PHASE = 2

    # ---- the labels the plan adds
    r.check("until the new labels exist in Smarter Contact, the table sets only the three the account has",
            {D.sc_label(k) for k in D.TABLE} - {""} == set(sc_replies.LABELS.values()), str({k: D.sc_label(k) for k in D.TABLE}))
    config.SC_EXTRA_LABELS = True
    r.check("with them switched on: Wrong DNC, Relative and Nurture",
            (D.sc_label("wrong_stop"), D.sc_label("relative"), D.sc_label("not_now")) == ("Wrong DNC", "Relative", "Nurture"))
    kit.lead("8650007027", "bp-t-label", to="")
    kit.sc("8650007027", "not right now, maybe next year", "c-t-nurture")
    r.check("a Smarter Contact 'not now' is labeled Nurture, not Not interested",
            [x["body"] for x in kit.sc_rows("8650007027", "label")] == ["Nurture"], str(kit.sc_rows("8650007027", "label")))
    kit.lead("8650007028", "bp-t-yes", to="")
    stub.sc_threads["c-t-yes"] = [{"id": "t", "dir": "out", "body": "Hi Dana, is 12 Elm St yours?",
                                   "created_at": "a", "raw_direction": "OUTGOING", "manual": False}]
    kit.sc("8650007028", "Yes it is", "c-t-yes")
    r.check("an owner who confirms the house is labeled Correct Number",
            [x["body"] for x in kit.sc_rows("8650007028", "label")] == ["Correct Number"], str(kit.sc_rows("8650007028", "label")))
    config.SC_EXTRA_LABELS = False
    config.TAG_DEATH_REPORTED = "Verify Deceased"
    kit.lead("8650007029", "bp-t-death")
    kit.sm("8650007029", "my husband passed away last month")
    r.check("with a tag named for it, a reported death also tags the record for research",
            any(w[0] == "add_tags" and w[1] == ("bp-t-death", ["Verify Deceased"]) for w in stub.crm_writes))
    config.TAG_DEATH_REPORTED = ""
    r.check("the table can be printed for a person to read", len(D.describe()) == 2 * len(D.TABLE)
            and any("mail stops too" in line for line in D.describe()))
    config.DISPOSITION_TABLE = False


# ====================================================================== a no stops what is in motion (0.17)
def _revocation(r, kit) -> None:
    config, store, engine, rev = kit.m.config, kit.m.store, kit.m.engine, kit.m.revocation
    sc_client, stub, gatekeeper = kit.m.sc_client, kit.stub, kit.m.gatekeeper
    print("\nbuild plan 0.17: a no stops what is already in motion, for every number of that owner")

    def trio(tag: str, n: int, to: str = SELLER, base: str = "8650016") -> tuple[str, str, str]:
        """An owner with two numbers on one record and a third on another of their records."""
        a, b, c = (f"{base}{n}{i}0" for i in (1, 2, 3))
        kit.put(_record(f"bp-v-{tag}-1", a, b, owner="own-" + tag))
        kit.put(_record(f"bp-v-{tag}-2", c, owner="own-" + tag))
        for phone, record in ((a, 1), (b, 1), (c, 2)):
            store.map_phone(phone, record_uuid=f"bp-v-{tag}-{record}", owner_uuid="own-" + tag,
                            context=kit.m.crm.deal_context("x"))
            store.ensure_conversation(phone, from_number=to, record_uuid=f"bp-v-{tag}-{record}")
        store.queue_message(b, "touch to the second number", from_number=to, intent="seed_touch_2")
        store.queue_message(c, "touch on the other house", from_number=to, status="held", intent="seed_touch_2")
        return a, b, c

    a, b, c = trio("off", 0)
    kit.sm(a, "STOP")
    r.check("with the switch off, a stop clears only the number that said it (today's gap)",
            kit.rows(b)[-1]["status"] == "queued" and kit.rows(c)[-1]["status"] == "held" and not store.is_suppressed(b))
    kit.clear_queue()

    config.OWNER_REVOKE = True
    r.check("an owner's other numbers are found across both of their records, from this app's store alone",
            rev.siblings(a) == sorted([b, c]), str(rev.siblings(a)))
    a, b, c = trio("stop", 1)
    reads = len(kit.reads)
    out = kit.sm(a, "STOP")
    r.check("a stop pulls the queued and the held text to every other number of that owner",
            kit.rows(b)[-1]["status"] == "cancelled" and kit.rows(c)[-1]["status"] == "cancelled"
            and kit.rows(b)[-1]["error"] == "pulled: the owner stopped", str((kit.rows(b)[-1]["error"], kit.rows(c)[-1]["status"])))
    r.check("the other numbers are suppressed as the owner's stop, so one person is one opt-out in the digest",
            store.is_suppressed(b) == store.is_suppressed(c) == "owner stopped" and store.is_suppressed(a) == "opt_out")
    r.check("and their threads read opted out, so no later batch seeds them",
            all((store.get_conversation(x) or {}).get("state") == "opted_out" for x in (b, c)))
    r.check("the reply says what it reached", any("owner-wide: 2 other number(s)" in x for x in out.get("actions", [])),
            str(out.get("actions"))[-200:])

    a, b, c = trio("wrong", 2)
    kit.sm(a, "wrong number, I don't own any property")
    r.check("a wrong number stays on the one phone: the owner's other numbers are untouched",
            kit.rows(b)[-1]["status"] == "queued" and kit.rows(c)[-1]["status"] == "held" and not store.is_suppressed(b))
    kit.clear_queue()

    a, b, c = trio("hand", 3)
    kit.sm(a, "How much would you pay for it?")
    conv = store.get_conversation(b) or {}
    r.check("a hand raise pauses the owner's other numbers and pulls their queued texts",
            conv.get("state") == "paused" and str(conv.get("paused_reason")).startswith("handed to Adriana")
            and kit.rows(b)[-1]["status"] == "cancelled" and kit.rows(c)[-1]["status"] == "cancelled", str(conv))

    a, b, c = trio("no", 4)
    engine._close_not_interested(a, "bp-v-no-1", "soft no by text")
    conv = store.get_conversation(c) or {}
    r.check("a no closes the owner's other numbers too",
            conv.get("state") == "closed" and kit.rows(b)[-1]["status"] == "cancelled", str(conv))
    r.check("without counting the same no twice in the digest's soft-no list", "soft" not in str(conv.get("paused_reason")))

    a, b, c = trio("buyer", 5, to=BUYER)
    engine._close_not_interested(a, "bp-v-buyer-1", "soft no by text")
    r.check("a buyer who passes keeps every number on the buyer list",
            kit.rows(b)[-1]["status"] == "queued" and (store.get_conversation(b) or {}).get("state") == "active")
    kit.clear_queue()

    a, b, c = trio("death", 6)
    kit.sm(a, "my husband passed away last month")
    conv = store.get_conversation(b) or {}
    r.check("a reported death pauses the owner's other numbers, so nothing greets the dead owner by name",
            conv.get("state") == "paused" and kit.rows(b)[-1]["status"] == "cancelled" and not store.is_suppressed(b), str(conv))

    a, b, c = trio("sc", 7)
    sc_client.contact_id_for = lambda phone: "c-" + phone
    pulled = len(stub.sc_pulled)
    kit.sm(a, "STOP")
    r.check("a stop also takes the owner's other numbers out of Smarter Contact's pending campaigns",
            ("c-" + b, "grp-t2") in stub.sc_pulled[pulled:] and ("c-" + c, "grp-t2") in stub.sc_pulled[pulled:],
            str(stub.sc_pulled[pulled:]))
    r.check("and queues each for Smarter Contact's own do-not-contact list",
            [x["intent"] for x in kit.sc_rows(b, "dnc")] == ["OWNER_STOP"], str(kit.sc_rows(b, "dnc")))
    sc_client.contact_id_for = lambda phone: ""

    a, b, c = trio("direct", 9, base="8650014")
    reads, writes = len(kit.reads), len(stub.crm_writes)
    acts = rev.on_event("stop", a, "bp-v-direct-1")
    r.check("the owner-wide step works from this app's own store: it reads no record and writes nothing to DataSift",
            len(kit.reads) == reads and len(stub.crm_writes) == writes and store.is_suppressed(b) == "owner stopped"
            and kit.rows(c)[-1]["status"] == "cancelled", str(acts))
    try:
        rev.on_event("maybe", a, "bp-v-direct-1")
        r.check("a kind of event nobody defined is a loud error, never a silent no-op", False, "no error raised")
    except ValueError:
        r.check("a kind of event nobody defined is a loud error, never a silent no-op", True)
    config.OWNER_REVOKE = False
    a, b, c = trio("direct-off", 0, base="8650014")
    r.check("called with the switch off it does nothing at all",
            rev.on_event("stop", a, "bp-v-direct-off-1") == [] and kit.rows(b)[-1]["status"] == "queued"
            and not store.is_suppressed(b))
    config.OWNER_REVOKE = True
    kit.clear_queue()

    # ---- the last look before a text leaves
    print("\nbuild plan 0.17: the same check again, right before each send")
    a, b, c = trio("late", 8)
    kit.sm(a, "STOP")
    late, later = "8650007030", "8650007031"
    for phone in (late, later):                     # mapped to the owner AFTER the stop
        store.map_phone(phone, record_uuid="bp-v-late-1", owner_uuid="own-late")
        store.ensure_conversation(phone, from_number=SELLER, record_uuid="bp-v-late-1")
    store.queue_message(late, "staged after the stop", from_number=SELLER, intent="seed_touch_2")
    r.check("with the last look off, a text to a number mapped after the stop still goes (today's gap)",
            kit.drain()["sent"] == 1 and kit.rows(late)[-1]["status"] == "sent")
    config.SEND_RECHECK = "local"
    store.queue_message(later, "staged after the stop", from_number=SELLER, intent="seed_touch_2")
    reads = len(kit.reads)
    result = kit.drain()
    row = kit.rows(later)[-1]
    r.check("the local last look cancels it: the owner stopped on another number",
            row["status"] == "cancelled" and row["error"].startswith("pulled: the owner stopped on another number"), str(row["error"]))
    r.check("and the local look reads nothing from DataSift", len(kit.reads) == reads and result["sent"] == 0)

    def other_line(tag: str, n: int, state: str, reason: str, suppress: str = "", to: str = SELLER) -> str:
        """An owner whose FIRST number is in the given state, and a touch queued to their second."""
        first, second = f"86500148{n}1", f"86500148{n}2"
        for phone in (first, second):
            store.map_phone(phone, record_uuid="bp-v-line-" + tag, owner_uuid="own-line-" + tag)
            store.ensure_conversation(phone, from_number=to, record_uuid="bp-v-line-" + tag)
        store.update_conversation(first, state=state, paused_reason=reason)
        if suppress:
            store.suppress(first, suppress)
        store.queue_message(second, "a touch to the owner's other line", from_number=to, intent="seed_touch_2")
        return second

    lines = {
        "no": other_line("no", 0, "closed", "soft no"),
        "handed": other_line("handed", 1, "paused", "handed to Adriana"),
        "typed": other_line("typed", 2, "paused", "a person replied in Smarter Contact"),
        "called": other_line("called", 3, "paused", "human called (Adriana, 95s)"),
        "wrong": other_line("wrong", 4, "closed", "wrong number", suppress="wrong_number"),
        "dead-line": other_line("dead-line", 5, "closed", "", suppress="dead"),
        "turn-cap": other_line("turn-cap", 6, "paused", "turn cap reached"),
        "buyer-no": other_line("buyer-no", 7, "closed", "soft no", to=BUYER),
        "buyer-stop": other_line("buyer-stop", 8, "opted_out", "opt-out", suppress="opt_out", to=BUYER),
    }
    kit.drain()
    got = {tag: (kit.rows(p)[-1]["status"], kit.rows(p)[-1]["error"] or "") for tag, p in lines.items()}
    r.check("a no, a handoff, a person typing and a person calling on one line each hold the owner's other line",
            all(got[t][0] == "cancelled" for t in ("no", "handed", "typed", "called"))
            and got["no"][1] == "pulled: the owner said no on another number"
            and got["typed"][1] == "pulled: a person is working this owner on another number", str(got))
    r.check("a wrong number, a dead line and a thread the agent paused on its own are about ONE line: the other still gets its text",
            all(got[t][0] == "sent" for t in ("wrong", "dead-line", "turn-cap")), str({t: got[t] for t in ("wrong", "dead-line", "turn-cap")}))
    r.check("a buyer who passed on the last deal still gets the next one on their other line, and a buyer who said stop does not",
            got["buyer-no"][0] == "sent" and got["buyer-stop"][0] == "cancelled", str((got["buyer-no"], got["buyer-stop"])))

    def staged(tag: str, n: int, intent: str = "seed_touch_2", **rec_kw) -> str:
        phone = f"86500169{n:02d}"
        kit.lead(phone, "bp-v-" + tag, **rec_kw)
        store.queue_message(phone, "a staged touch", from_number=SELLER, intent=intent)
        return phone

    config.SEND_RECHECK = "full"
    changes = {"lead": (lambda x: x.update(status="Hot Lead"), "pulled: already a lead (hot lead)"),
               "sold": (lambda x: x["tags"].append("recently sold"), "pulled: the house sold"),
               "dead": (lambda x: x["owner"].update(deceased=True), "pulled: the owner has died or may have (owner flag); research first"),
               "dnm": (lambda x: x["tags"].append("Do Not Market"), "pulled: tagged Do Not Market")}
    phones = {}
    for i, (tag, (change, _)) in enumerate(changes.items()):
        phones[tag] = staged("full-" + tag, 10 + i)
        change(kit.recs["bp-v-full-" + tag])        # the record changed in DataSift after staging
    clean = staged("full-clean", 20)
    result = kit.drain()
    wrong = {t: kit.rows(p)[-1]["error"] for t, p in phones.items() if kit.rows(p)[-1]["error"] != changes[t][1]}
    r.check("the full last look reads the record as it is now: a new lead, a sale, a death and Do Not Market each pull the text",
            not wrong and all(kit.rows(p)[-1]["status"] == "cancelled" for p in phones.values()), str(wrong))
    r.check("and a record nothing changed on still gets its text", kit.rows(clean)[-1]["status"] == "sent", str(result))

    lost = staged("full-lost", 21)
    del kit.recs["bp-v-full-lost"]
    kit.drain()
    row = kit.rows(lost)[-1]
    r.check("a record that cannot be re-read holds the text: nothing leaves on no answer",
            row["status"] == "queued" and row["not_before"] > store.now(), str((row["status"], row["not_before"])))
    kit.clear_queue()

    answer = staged("full-answer", 22, intent="ASKING_WHO")
    kit.recs["bp-v-full-answer"]["status"] = "Hot Lead"
    refused = staged("full-refused", 23, intent="ASKING_WHO")
    kit.recs["bp-v-full-refused"]["tags"].append("Do Not Market")
    kit.drain()
    r.check("an answer to someone who just wrote is held only by the hard rules: a lead still gets it, Do Not Market does not",
            kit.rows(answer)[-1]["status"] == "sent" and kit.rows(refused)[-1]["status"] == "cancelled",
            str((kit.rows(answer)[-1]["status"], kit.rows(refused)[-1]["error"])))

    flag = staged("full-flag", 24)
    gatekeeper.remember_flagged([flag])
    kit.drain()
    r.check("a number that carries the do-not-call registry flag never gets outreach, even once queued",
            kit.rows(flag)[-1]["error"] == "pulled: this number carries the do-not-call registry flag", str(kit.rows(flag)[-1]["error"]))

    asked = staged("full-asked", 28, intent="ASKING_WHO")
    gatekeeper.remember_flagged([asked])
    kit.recs["bp-v-full-asked"]["owner"]["phones"][0]["status"] = "WRONG"
    kit.drain()
    r.check("the rules about one phone are for texts we start: an answer to a question still goes to a flagged number",
            kit.rows(asked)[-1]["status"] == "sent", str(kit.rows(asked)[-1]["error"]))
    kit.put(_record("bp-v-full-sorry", _phone("8650007033", status="WRONG"), tags=("FTM", "Do Not Market")))
    r.check("the one apology to a stranger is never held by anything about the owner's record",
            rev.send_block({"phone": "8650007033", "intent": "WRONG_NUMBER"}, {"record_uuid": "bp-v-full-sorry"}) == ("", ""))
    for n, (tag, change) in enumerate({"said-no": lambda x: x.update(status="not_interested"),
                                       "now-dnm": lambda x: x["tags"].append("Do Not Market")}.items()):
        phone = staged("full-lead-" + tag, 30 + n, intent="SPEED_TO_LEAD")
        change(kit.recs["bp-v-full-lead-" + tag])
    flagged_lead = staged("full-lead-flag", 32, intent="SPEED_TO_LEAD")
    gatekeeper.remember_flagged([flagged_lead])
    kit.drain()
    lead_rows = {t: kit.rows(f"86500169{30 + i}")[-1] for i, t in enumerate(("said-no", "now-dnm", "flag"))}
    r.check("the call-is-coming text is owed to someone who raised a hand: a Not Interested status does not stop it",
            lead_rows["said-no"]["status"] == "sent", str(lead_rows["said-no"]["error"]))
    r.check("but Do Not Market does, and so does the registry flag on that number",
            lead_rows["now-dnm"]["error"] == "pulled: tagged Do Not Market"
            and lead_rows["flag"]["error"] == "pulled: this number carries the do-not-call registry flag",
            str((lead_rows["now-dnm"]["error"], lead_rows["flag"]["error"])))

    owner = staged("full-owner", 25)
    other = "8650007032"
    kit.recs["bp-v-full-owner"]["owner"]["phones"].append(_phone(other))
    store.map_phone(other, record_uuid="bp-v-elsewhere")
    kit.drain()
    mapped = store.lookup_phone(other) or {}
    r.check("reading a record teaches the store who else shares its owner, without moving where a number routes",
            mapped.get("owner_uuid") == "own-bp-v-full-owner" and mapped.get("record_uuid") == "bp-v-elsewhere", str(mapped))

    config.SEND_RECHECK = "off"
    quiet = staged("full-off", 27)
    kit.recs["bp-v-full-off"]["tags"].append("Do Not Market")
    reads = len(kit.reads)
    kit.drain()
    r.check("with the last look off the worker reads no record and sends what is queued (today's behavior)",
            kit.rows(quiet)[-1]["status"] == "sent" and len(kit.reads) == reads)
    config.OWNER_REVOKE = False
    r.check("with both switches off the digest says nothing about pulled texts (today's digest)",
            kit.m.digest.collect(days=1).get("extras", {}).get("pulled") is None)
    config.OWNER_REVOKE = True
    config.SEND_RECHECK = "local"
    r.check("everything this pulled says so the same way, so with a switch on the digest carries the count",
            int(kit.m.digest.collect(days=1)["extras"].get("pulled") or 0) >= 6
            and "Pulled before sending" in kit.m.digest.render(kit.m.digest.collect(days=1)))
    config.OWNER_REVOKE, config.SEND_RECHECK = False, "off"
    kit.clear_queue()


# ====================================================================== the request budget (0.14)
def _budget(r, kit) -> None:
    config, B, crm = kit.m.config, kit.m.ratebudget, kit.m.crm
    print("\nbuild plan 0.14: this app's share of the DataSift request budget")
    base = datetime(2026, 11, 13, 15, 0, 5, tzinfo=timezone.utc).timestamp()      # 10:00:05 in the morning, Eastern

    class Clock:
        def __init__(self, t):
            self.t, self.slept = t, []

        def __call__(self):
            return self.t

        def sleep(self, seconds):
            self.slept.append(seconds)
            self.t += seconds

    class Session:
        def __init__(self):
            self.calls = 0

        def request(self, *a, **k):
            self.calls += 1
            return "ok"

    class Client:
        def __init__(self):
            self._session = Session()

        def _request(self, path):
            return self._session.request("GET", path)

    plain = Client()
    r.check("with the budget off the client is handed back untouched and nothing is counted",
            crm._budgeted(plain) is plain and not getattr(plain, "_rate_budgeted", False) and B.acquire() == 0.0)
    config.RATE_BUDGET, config.RATE_ENFORCE = True, False
    clock = Clock(base - 7200)
    took = [B.acquire(is_priority=False, clock=clock, sleep=clock.sleep) for _ in range(14)]
    use = B.usage(clock.t)
    r.check("in count mode every request is counted and none is ever delayed or refused",
            took == [0.0] * 14 and not clock.slept and use["minute"] == 14 and not use["enforcing"], str(use))
    r.check("and it notes each request the limit would have delayed, so the share can be set from a measured week",
            use["would_wait_today"] == 6 and use["minute_waits_today"] == 0
            and "counting only" in B.line(clock.t) and "6 request(s) today would have waited" in B.line(clock.t),
            B.line(clock.t))
    try:
        with B.nowait():
            B.acquire(clock=clock, sleep=clock.sleep)
        r.check("count mode never tells the send loop the share is used", True)
    except B.BudgetExhausted:
        r.check("count mode never tells the send loop the share is used", False, "it refused a request")
    config.RATE_ENFORCE = True
    clock = Clock(base)
    took = [B.acquire(is_priority=True, clock=clock, sleep=clock.sleep) for _ in range(10)]
    r.check("reply work may use the whole ten a minute without waiting", took == [0.0] * 10 and not clock.slept)
    B.acquire(is_priority=True, clock=clock, sleep=clock.sleep)
    r.check("the eleventh reply write waits for the next minute, and is never refused",
            len(clock.slept) == 1 and 54 < clock.slept[0] <= 56, str(clock.slept))
    use = B.usage(clock.t)
    r.check("the count is per minute and per hour", use["minute"] == 1 and use["hour"] == 11 and use["minute_cap"] == 10
            and use["hour_cap"] == 600, str(use))

    clock = Clock(base + 3600)                       # a fresh hour
    for _ in range(8):
        B.acquire(is_priority=False, clock=clock, sleep=clock.sleep)
    r.check("a scan stops at eight of the ten, so two are always left for a reply", not clock.slept)
    try:
        with B.nowait():
            B.acquire(clock=clock, sleep=clock.sleep)
        r.check("the worker's send loop is told the share is used at once, and does not sleep", False, "no refusal")
    except B.BudgetBusy:
        r.check("the worker's send loop is told the share is used at once, and does not sleep", not clock.slept)
    with B.priority():
        B.acquire(clock=clock, sleep=clock.sleep)
        B.acquire(clock=clock, sleep=clock.sleep)
    r.check("the reserve is there for a reply: two more go through in the same minute", not clock.slept
            and B.usage(clock.t)["minute"] == 10)
    B.acquire(is_priority=False, clock=clock, sleep=clock.sleep)
    r.check("a scan that may wait does wait for the next minute", len(clock.slept) == 1)

    clock = Clock(base + 7200)
    for _ in range(10):
        B.acquire(is_priority=True, clock=clock, sleep=clock.sleep)
    B.acquire(is_priority=True, clock=clock, sleep=clock.sleep, max_wait=0)
    use = B.usage(clock.t)
    r.check("a reply write is delayed, never dropped: past the wait limit it goes, and the overrun is counted",
            not clock.slept and use["minute"] == 11 and use["overruns_today"] == 1, str(use))
    try:
        B.acquire(is_priority=False, clock=clock, sleep=clock.sleep, max_wait=0)
        r.check("a scan past the wait limit is refused, so the caller can stop and say so", False, "no refusal")
    except B.BudgetExhausted as exc:
        r.check("a scan past the wait limit is refused, so the caller can stop and say so",
                crm._transient_wait(exc, 0) == 0.0 and not any(ch.isdigit() for ch in str(exc)), str(exc))

    config.RATE_PER_HOUR = 12
    clock = Clock(base + 4 * 3600)
    for _ in range(12):
        B.acquire(is_priority=True, clock=clock, sleep=clock.sleep)      # the minute cap makes it wait once
    slept = len(clock.slept)
    try:
        B.acquire(is_priority=False, clock=clock, sleep=clock.sleep)
        r.check("an hour cap, when one is set, refuses a scan until the next hour", False, "no refusal")
    except B.BudgetExhausted:
        r.check("an hour cap, when one is set, refuses a scan until the next hour", len(clock.slept) == slept)
    config.RATE_PER_HOUR = 0

    quiet = datetime(2026, 11, 13, 11, 40, tzinfo=timezone.utc).timestamp()       # 6:40 in the morning, Eastern
    r.check("while the first-to-market box makes its run, scans drop to a trickle and replies do not",
            B.is_quiet(quiet) and B.per_min(quiet) == 3 and B.per_min(quiet, True) == 10 and not B.is_quiet(base))
    config.RATE_QUIET = "off"
    r.check("the quiet window can be switched off", not B.is_quiet(quiet))
    # Left off for the rest of this part: the checks below count against the real clock, and a
    # run started inside the real window would otherwise wait a real minute.

    other = sqlite3.connect(str(config.DB_PATH))
    key = time.strftime("h:%Y-%m-%dT%H", time.gmtime(base))
    seen = other.execute("SELECT n FROM rate_budget WHERE app=? AND win=?", (B.APP, key)).fetchone()
    other.close()
    r.check("the count lives in the app's own database, where another process reads the same number",
            bool(seen) and seen[0] == 11, str(seen))
    clock = Clock(base + 6 * 3600)
    threads = [threading.Thread(target=lambda: B.acquire(is_priority=True, clock=clock, sleep=clock.sleep)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    r.check("six threads asking at once are counted as six", B.usage(clock.t)["minute"] == 6, str(B.usage(clock.t)))

    def counted() -> int:
        # Every hour window together, so a run that crosses the top of a real hour still adds up.
        return int(B._conn().execute("SELECT COALESCE(SUM(n), 0) AS n FROM rate_budget WHERE win LIKE 'h:%'").fetchone()["n"])

    client, second = Client(), Client()
    wrapped = crm._budgeted(client)
    before = counted()
    for _ in range(3):
        wrapped._request("/api/internal/property/x/")
    crm._budgeted(wrapped)._request("/api/internal/property/y/")
    second._request("/api/internal/property/z/")
    r.check("every request the client puts on the wire is counted once, however often it is wrapped",
            wrapped is client and client._session.calls == 4 and counted() - before == 4, str(B.usage()))
    r.check("only that one client is counted: the shared class is not changed",
            second._session.calls == 1 and not getattr(second, "_rate_budgeted", False))

    class Bare:
        def __init__(self):
            self.calls = 0

        def _request(self, path):
            self.calls += 1

    bare = B.wrap(Bare())
    before = counted()
    bare._request("/x")
    r.check("a client with no session is counted once per call", bare.calls == 1 and counted() - before == 1)
    with B.priority():
        with B.nowait():
            inner = (getattr(B._local, "priority", False), getattr(B._local, "nowait", False))
    r.check("the priority and no-wait marks end with their block",
            inner == (True, True) and not getattr(B._local, "priority", False) and not getattr(B._local, "nowait", False))
    r.check("the digest carries the app's own use of the budget",
            "DataSift requests:" in kit.m.digest.render(kit.m.digest.collect(days=1)))
    config.RATE_BUDGET = False
    r.check("and says nothing about it while the budget is off",
            "DataSift requests:" not in kit.m.digest.render(kit.m.digest.collect(days=1)))


# ====================================================================== the touch feed (0.23)
def _touch_feed(r, kit) -> None:
    config, store, T = kit.m.config, kit.m.store, kit.m.touchlog
    print("\nbuild plan 0.23: the day's sent texts, for the touch audit")
    day = "2026-10-05"                               # a Monday, daylight time: Eastern is four hours behind UTC
    store.map_phone("8650007034", record_uuid="bp-f-1", owner_uuid="own-f-1")
    store.map_phone("8650007035", record_uuid="bp-f-2")
    with store.tx() as c:
        add = ("INSERT INTO outbox(phone, from_number, body, status, intent, error, created_at, sent_at)"
               " VALUES (?,?,?,?,?,?,?,?)")
        c.execute(add, ("8650007034", SELLER, "SECRET BODY ONE", "sent", "seed_touch_1", None,
                        "2026-10-05T13:00:00+00:00", "2026-10-05T14:30:00+00:00"))
        c.execute(add, ("8650007034", SELLER, "a dry run", "sent", "seed_touch_2", "dry run",
                        "2026-10-05T13:00:00+00:00", "2026-10-05T15:00:00+00:00"))
        c.execute(add, ("8650007035", SELLER, "late evening", "sent", "ASKING_WHO", None,
                        "2026-10-06T02:00:00+00:00", "2026-10-06T03:30:00+00:00"))     # 11:30pm Eastern on the 5th
        c.execute(add, ("8650007035", SELLER, "the next morning", "sent", "seed_touch_1", None,
                        "2026-10-06T13:00:00+00:00", "2026-10-06T14:00:00+00:00"))
        c.execute(add, ("8650007034", SELLER, "never released", "held", "seed_touch_3", None,
                        "2026-10-05T13:00:00+00:00", None))
        c.execute("INSERT INTO messages(phone, direction, body, from_number, author, created_at) VALUES (?,?,?,?,?,?)",
                  ("8650007036", "out", "typed by a person", SELLER2, "human", "2026-10-05T16:00:00+00:00"))
        c.execute("INSERT INTO sc_outbox(phone, contact_id, kind, body, intent, status, created_at, done_at)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  ("8650007037", "c-f", "reply", "an auto reply", "ASKING_WHO", "sent",
                   "2026-10-05T17:00:00+00:00", "2026-10-05T17:02:00+00:00"))
        c.execute("INSERT INTO sc_outbox(phone, contact_id, kind, body, intent, status, created_at, done_at)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  ("8650007037", "c-f", "label", "Not interested", "NOT_INTERESTED", "sent",
                   "2026-10-05T17:00:00+00:00", "2026-10-05T17:03:00+00:00"))
    rows = T.sent_texts(day)
    r.check("the feed holds every text that left on that day, oldest first",
            [(x["system"], x["author"], x["last4"]) for x in rows]
            == [("smrtphone", "agent", "7034"), ("smrtphone", "person", "7036"), ("smartercontact", "agent", "7037"),
                ("smrtphone", "agent", "7035")], str([(x["system"], x["author"], x["last4"], x["at"]) for x in rows]))
    r.check("a dry run, a text never released and a label are not touches", len(rows) == 4)
    r.check("the day is the business day: 11:30pm Eastern belongs to it, the next morning does not",
            rows[-1]["at"].startswith("2026-10-06T03:30") and len(T.sent_texts("2026-10-06")) == 1)
    r.check("each row names the record and the owner where they are known, the time and the system",
            rows[0]["record_uuid"] == "bp-f-1" and rows[0]["owner_uuid"] == "own-f-1" and rows[0]["kind"] == "seed_touch_1"
            and rows[0]["from_last4"] == "0001" and rows[1]["record_uuid"] == "")
    blob = json.dumps(T.feed(day))
    r.check("the feed never carries a whole number or a word of any message",
            not any(x in blob for x in ("8650007034", "8650007035", "8650007036", "8650007037", "SECRET", "typed by")), blob[:200])
    feed = T.feed(day)
    r.check("it counts by system, and says what it cannot see (the campaigns Smarter Contact sends itself)",
            feed["count"] == 4 and feed["by_system"] == {"smrtphone by agent": 2, "smrtphone by person": 1,
                                                         "smartercontact by agent": 1}
            and "Smarter Contact" in feed["not_seen"], str(feed["by_system"]))
    r.check("with no day given it reads yesterday, the morning-after audit's day",
            T.day_bounds()[0] == (datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(config.BUSINESS_TZ))
                                  - timedelta(days=1)).date().isoformat())
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:
        r.check("fastapi TestClient available", False, str(exc))
        return
    config.WEBHOOK_SECRET, config.ALLOWED_IPS = "selftest-secret", []
    with TestClient(kit.m.receiver.app) as client:
        r.check("with the switch off there is no such route (today's behavior)",
                client.get(f"/hooks/selftest-secret/touches?day={day}").status_code == 404)
        config.TOUCH_FEED = True
        resp = client.get(f"/hooks/selftest-secret/touches?day={day}")
        r.check("switched on, the audit can fetch the day over the same secret path as the webhooks",
                resp.status_code == 200 and resp.json()["count"] == 4, f"{resp.status_code} {resp.text[:120]}")
        r.check("a wrong secret gets nothing", client.get(f"/hooks/wrong/touches?day={day}").status_code == 404)
        r.check("a day that is not a date is refused, not guessed",
                client.get("/hooks/selftest-secret/touches?day=last-tuesday").status_code == 400)
        config.TOUCH_FEED = False


# ====================================================================== a hand raise becomes a New Lead (2.3)
def _hand_raise(r, kit) -> None:
    config, store, stub, classify, worker = kit.m.config, kit.m.store, kit.stub, kit.m.classify, kit.m.worker
    print("\nbuild plan 2.3: a hand raise becomes a New Lead")
    kit.lead("8650007038", "bp-h-off")
    mark = len(stub.crm_writes)
    kit.sm("8650007038", "How much would you pay for it?")
    r.check("with the switch off, an interested reply is tagged and handed off and the status is not touched (today's behavior)",
            not any(w[0] == "set_status" for w in kit.writes_since(mark))
            and any(w[0] == "add_tags" and "sys_escalated" in w[1][1] for w in kit.writes_since(mark)))

    config.HAND_RAISE_NEW_LEAD = True
    kit.lead("8650007039", "bp-h-new")
    mark = len(stub.crm_writes)
    kit.sm("8650007039", "How much would you pay for it?")
    kit.sm("8650007039", "call me this afternoon")
    sets = [w for w in kit.writes_since(mark) if w[0] == "set_status"]
    r.check("switched on, an interested reply sets New Lead, once, however many texts the burst has",
            sets == [("set_status", ("bp-h-new", "new_lead"))], str(sets))
    r.check("and the handoff still tags the record and marks the number Correct",
            any(w[0] == "set_phone_status" and w[1][2] == "CORRECT" for w in kit.writes_since(mark)))

    kit.lead("8650007040", "bp-h-lead", status="Warm Lead")
    mark, slack = len(stub.crm_writes), len(stub.slack)
    kit.sm("8650007040", "How much would you pay for it?")
    kit.sm("8650007040", "are you still interested? call me")
    r.check("a record that already holds a lead status is never set back to New Lead",
            not any(w[0] == "set_status" for w in kit.writes_since(mark)), str([w for w in kit.writes_since(mark) if w[0] == "set_status"]))
    r.check("it is tagged, so the card can be moved back",
            any(w[0] == "add_tags" and w[1] == ("bp-h-lead", ["Reply On Lead"]) for w in kit.writes_since(mark)))
    told = [s for s in stub.slack[slack:] if "a lead texted again" in s]
    r.check("and the lead manager is told once, by name, with the status and what to do",
            len(told) == 1 and "Adriana" in told[0] and "warm lead" in told[0] and "Move its card back" in told[0], str(told)[:300])

    kit.lead("8650007041", "bp-h-sold", status="sold")
    mark = len(stub.crm_writes)
    kit.sm("8650007041", "How much would you pay for it?")
    r.check("a sold or closed record is left for a person to look at, with a note",
            not any(w[0] == "set_status" for w in kit.writes_since(mark))
            and any(w[0] == "post_note" and "reads sold" in w[1][1] for w in kit.writes_since(mark)))

    kit.lead("8650007042", "bp-h-buyer", to=BUYER)
    mark = len(stub.crm_writes)
    kit.sm("8650007042", "How much would you pay for it?", to=BUYER)
    r.check("a buyer's interest never sets a seller's New Lead",
            not any(w[0] == "set_status" for w in kit.writes_since(mark)))

    kit.lead("8650007043", "bp-h-sc", to="")
    mark = len(stub.crm_writes)
    out = kit.sc("8650007043", "How much would you pay for it?", "c-h-sc")
    r.check("the Smarter Contact path sets New Lead the same way",
            out.get("action") == "handoff" and ("set_status", ("bp-h-sc", "new_lead")) in kit.writes_since(mark))
    config.PHASE = 1
    kit.lead("8650007044", "bp-h-phase1")
    mark = len(stub.crm_writes)
    kit.sm("8650007044", "How much would you pay for it?")
    r.check("at phase 1 nothing is escalated and no status is set", not any(w[0] == "set_status" for w in kit.writes_since(mark)))
    config.PHASE = 2
    config.HAND_RAISE_NEW_LEAD = False

    # ---- an ask over value, and a listed house (decision D14 is not made: today's rule is the default)
    said = lambda text: classify.Classification("INTERESTED", 0.9, "llm", "stub")       # noqa: E731
    kit.put(_record("bp-h-over", "8650007045", estimate_value="599000.00"))
    kit.lead("8650007045", "bp-h-over", to="")
    classify.classify_llm = lambda text, history=None: said(text)
    mark = len(stub.crm_writes)
    out = kit.sc("8650007045", "I want 2 million", "c-h-over")
    r.check("by default an ask far over value is closed Not Interested (today's rule)",
            out.get("action") == "not_interested" and ("set_status", ("bp-h-over", "not_interested")) in kit.writes_since(mark))
    config.OVER_VALUE = "cold_lead"
    kit.put(_record("bp-h-cold", "8650007046", estimate_value="599000.00"))
    kit.lead("8650007046", "bp-h-cold", to="")
    mark, slack = len(stub.crm_writes), len(stub.slack)
    out = kit.sc("8650007046", "I want 2 million", "c-h-cold")
    writes = kit.writes_since(mark)
    r.check("with decision D14 switched to its recommendation, it is passed as a Cold lead with an ask-over-value tag",
            out.get("action") == "cold_lead" and ("set_status", ("bp-h-cold", "Cold Lead")) in writes
            and any(w[0] == "add_tags" and "Ask Over Value" in w[1][1] for w in writes), f"{out.get('action')} {writes}"[:400])
    r.check("on a confirmed number, with nobody paged and no Not interested label",
            any(w[0] == "set_phone_status" and w[1][2] == "CORRECT" for w in writes) and len(stub.slack) == slack
            and not kit.sc_rows("8650007046", "label") and not store.due_escalations(), str(kit.sc_rows("8650007046")))
    config.OVER_VALUE = "not_interested"

    config.LISTED_STATUS = "follow_up"
    kit.put(_record("bp-h-listed", "8650007047", mls="Listed"))
    kit.lead("8650007047", "bp-h-listed", to="")
    mark = len(stub.crm_writes)
    out = kit.sc("8650007047", "Yes, make me a bid", "c-h-listed")
    r.check("with a status named for it, an interested reply on a listed house goes to nurture",
            out.get("action") == "nurture" and ("set_status", ("bp-h-listed", "follow_up")) in kit.writes_since(mark),
            f"{out.get('action')} {out.get('actions')}"[:300])
    config.LISTED_STATUS = ""

    config.DISPOSITION_TABLE = True
    kit.put(_record("bp-h-sm-over", "8650007048", estimate_value="599000.00"))
    kit.lead("8650007048", "bp-h-sm-over")
    mark = len(stub.crm_writes)
    out = kit.sm("8650007048", "I want 2 million")
    r.check("with the table on, the same ask is the same no on smrtPhone as on Smarter Contact, and nobody is paged",
            out.get("action") == "not_interested" and out.get("disposition") == "ask_over_value"
            and ("set_status", ("bp-h-sm-over", "not_interested")) in kit.writes_since(mark)
            and not [e for e in store.due_escalations() if e["phone"] == "8650007048"], f"{out.get('action')} {out.get('actions')}"[:300])
    kit.put(_record("bp-h-sm-fair", "8650007049", estimate_value="599000.00"))
    kit.lead("8650007049", "bp-h-sm-fair")
    out = kit.sm("8650007049", "I would take 450k")
    r.check("and a fair ask is still handed straight to a person", out.get("action") == "handoff", str(out.get("action")))
    config.DISPOSITION_TABLE = False
    classify.classify_llm = lambda text, history=None: classify.Classification("OTHER", 0.0, "fallback", "stubbed")
    with store.tx() as c:
        c.execute("DELETE FROM escalations")


# ====================================================================== the call-is-coming text (2.2)
def _speed_to_lead(r, kit) -> None:
    config, store, stub, worker = kit.m.config, kit.m.store, kit.stub, kit.m.worker
    S, sender_pool, respond, lead_texts = kit.m.speedtolead, kit.m.sender_pool, kit.m.respond, kit.m.lead_texts
    sc_replies, sc_inbound, engine, receiver = kit.m.sc_replies, kit.m.sc_inbound, kit.m.engine, kit.m.receiver
    print("\nbuild plan 2.2: the text that says a call is coming")

    def raise_hand(phone: str, record: str) -> int:
        """An interested reply, the burst settled, the handoff posted."""
        kit.lead(phone, record)
        kit.sm(phone, "How much would you pay for it?")
        with store.tx() as c:
            c.execute("UPDATE escalations SET due_at='2000-01-01T00:00:00+00:00' WHERE phone=?", (phone,))
        return worker.flush_escalations()

    def lead_rows(phone: str) -> list[dict]:
        return [x for x in kit.rows(phone) if x["intent"] == S.INTENT]

    bad = []
    for name, pool in lead_texts.ALL_POOLS.items():
        for template in pool:
            text = template.format(first="Dana", caller="Jordan")
            ok, problems = respond.validate(text, max_questions=0)
            if not ok or sc_replies.problems(text) or "{" in text:
                bad.append(f"{name}: {text} {problems} {sc_replies.problems(text)}")
    r.check("every call-is-coming text and the line check pass both validators, with no question in them", not bad, "; ".join(bad)[:300])
    r.check("the copy never names a price, a company or the list",
            not any(w in " ".join(" ".join(p) for p in lead_texts.ALL_POOLS.values()).lower()
                    for w in ("$", "cash", "offer", "llc", "foreclos", "probate", "auction", " now")))

    posted = raise_hand("8650007050", "bp-l-off")
    r.check("with the switch off, an interested owner gets no text from the agent (today's behavior)",
            posted == 1 and not lead_rows("8650007050"))

    config.SPEED_TO_LEAD = "held"
    before = datetime.now(timezone.utc)
    raise_hand("8650007051", "bp-l-held")
    rows = lead_rows("8650007051")
    r.check("switched on, the text is staged once the handoff has posted, held for a person",
            len(rows) == 1 and rows[0]["status"] == "held" and rows[0]["from_number"] == SELLER, str(rows))
    due = datetime.fromisoformat(rows[0]["not_before"]) if rows else before
    r.check("timed about three minutes after the hand was raised",
            170 <= (due - before).total_seconds() <= 190, str((due - before).total_seconds()))
    body = rows[0]["body"] if rows else ""
    r.check("it greets the owner by first name, promises the call, and asks nothing",
            "Dana" in body and "call" in body and "?" not in body and respond.validate(body, max_questions=0)[0], body)
    r.check("staging it again the same day does nothing", not S.stage("bp-l-held", phone="8650007051").get("staged"))

    r.check("nothing leaves while it is only staged", kit.drain(live=True)["sent"] == 0 and not kit.sent)
    with store.tx() as c:
        c.execute("UPDATE outbox SET not_before=NULL WHERE phone='8650007051'")
    r.check("a person releases it", S.release("8650007051") == 1)
    sent = len(kit.sent)
    kit.drain(live=True)
    r.check("released, it goes out on the thread's own number even though the thread is paused for the handoff",
            len(kit.sent) == sent + 1 and kit.sent[-1][0] == "8650007051" and kit.sent[-1][2] == SELLER, str(kit.sent[-1:]))

    raise_hand("8650007052", "bp-l-taken")
    store.pause_conversation("8650007052", "human took over (Adriana)")
    S.release("8650007052")
    with store.tx() as c:
        c.execute("UPDATE outbox SET not_before=NULL WHERE phone='8650007052'")
    sent = len(kit.sent)
    kit.drain(live=True)
    r.check("if a person texts or calls first, the promise is already kept and the text is dropped",
            len(kit.sent) == sent and lead_rows("8650007052")[0]["status"] == "cancelled", str(lead_rows("8650007052")))

    raise_hand("8650007053", "bp-l-moved")
    S.release("8650007053")
    with store.tx() as c:
        c.execute("UPDATE outbox SET not_before=NULL, created_at='2000-01-01T00:00:00+00:00' WHERE phone='8650007053'")
    kit.sm("8650007053", "actually I am at work until five")
    sent = len(kit.sent)
    kit.drain(live=True)
    r.check("if the owner writes again first, the canned text is dropped rather than talk past them",
            len(kit.sent) == sent and lead_rows("8650007053")[0]["error"] == "they replied", str(lead_rows("8650007053")))

    morning = datetime(2026, 11, 14, 14, 5, tzinfo=timezone.utc)                  # 9:05 the next morning, Eastern
    sender_pool.within_quiet_hours = lambda phone, at=None: False
    sender_pool.next_send_window = lambda phone, at=None: morning
    raise_hand("8650007054", "bp-l-night")
    sender_pool.within_quiet_hours = lambda phone, at=None: True
    rows = lead_rows("8650007054")
    r.check("a lead that arrives at night gets it when the window opens, and the text says this morning",
            len(rows) == 1 and rows[0]["not_before"] == morning.isoformat(timespec="seconds")
            and rows[0]["body"].startswith("Good morning") and "this morning" in rows[0]["body"], str(rows))

    kit.lead("8650007055", "bp-l-dnm", tags=("FTM", "Do Not Market"))
    out = S.stage("bp-l-dnm", phone="8650007055")
    r.check("the gate still holds it: nobody tagged Do Not Market is texted, hand raise or not",
            not out.get("staged") and "Do Not Market" in out.get("skipped", ""), str(out))
    kit.lead("8650007056", "bp-l-gone")
    del kit.recs["bp-l-gone"]
    r.check("a record that cannot be read gets no text", not S.stage("bp-l-gone", phone="8650007056").get("staged"))
    kit.lead("8650007057", "bp-l-supp")
    store.suppress("8650007057", "opt_out")
    r.check("a suppressed number gets no text", not S.stage("bp-l-supp", phone="8650007057").get("staged"))

    config.LEAD_CALLER_NAME = "Jordan"
    kit.lead("8650007058", "bp-l-other")
    out = S.stage("bp-l-other", phone="8650007058")
    r.check("when somebody else makes the call, the text names them", out.get("staged") and "Jordan" in out["message"], str(out))
    config.LEAD_CALLER_NAME = ""
    kit.lead("8650007059", "bp-l-nobody")
    r.check("with nobody named to make the call, nothing is promised",
            not S.stage("bp-l-nobody", phone="8650007059").get("staged"))
    config.LEAD_CALLER_NAME = "Adriana"

    config.SPEED_TO_LEAD = "auto"
    kit.lead("8650007060", "bp-l-auto")
    out = S.stage("bp-l-auto", phone="8650007060")
    r.check("in auto it is queued to send on its own, still timed and still through the same queue",
            out.get("staged") and out["status"] == "queued" and lead_rows("8650007060")[0]["status"] == "queued", str(out))
    r.check("each staged text carries the moment it stops being true: an hour after its send time",
            (datetime.fromisoformat(out["good_until"]) - datetime.fromisoformat(out["at"])).total_seconds() == 3600, str(out))
    late_morning = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc)            # 11:30 in the morning, Eastern
    r.check("and 'this morning' is never good past noon",
            S.good_until(late_morning, True) == datetime(2026, 11, 14, 17, 0, tzinfo=timezone.utc)
            and S.good_until(late_morning, False) == datetime(2026, 11, 14, 17, 30, tzinfo=timezone.utc))
    with store.tx() as c:
        c.execute("UPDATE outbox SET not_before=NULL WHERE phone='8650007060'")
    store.set_meta("lead_text_until:8650007060", "2000-01-01T00:00:00+00:00")       # the worker was down for an hour
    sent = len(kit.sent)
    kit.drain(live=True)
    r.check("a text that is more than an hour late is dropped, not sent: the promise in it is no longer true",
            len(kit.sent) == sent and lead_rows("8650007060")[0]["error"] == S.TOO_LATE, str(lead_rows("8650007060")))
    config.SPEED_TO_LEAD = "held"
    kit.lead("8650007065", "bp-l-stale")
    S.stage("bp-l-stale", phone="8650007065")
    store.set_meta("lead_text_until:8650007065", "2000-01-01T00:00:00+00:00")
    r.check("a person cannot release one that is already too late: it is cancelled instead",
            S.release("8650007065") == 0 and lead_rows("8650007065")[0]["status"] == "cancelled", str(lead_rows("8650007065")))
    r.check("a text with no deadline on record is never dropped by this rule", not S.expired("8650007067"))
    kit.clear_queue()

    # ---- a Smarter Contact thread is answered through Smarter Contact
    kit.clear_sc()
    kit.lead("8650007061", "bp-l-sc", to="")
    kit.sc("8650007061", "How much would you pay for it?", "c-l-sc")
    with store.tx() as c:
        c.execute("UPDATE escalations SET due_at='2000-01-01T00:00:00+00:00' WHERE phone='8650007061'")
    worker.flush_escalations()
    held = [x for x in kit.sc_rows("8650007061", "reply") if x["intent"] == S.INTENT]
    r.check("a reply to a Smarter Contact campaign is staged in Smarter Contact's queue, never the smrtPhone one",
            len(held) == 1 and held[0]["status"] == "held" and held[0]["contact_id"] == "c-l-sc" and not lead_rows("8650007061"),
            str(held))
    was = config.DRY_RUN
    config.DRY_RUN = False
    try:
        sent = len(stub.sc_sent)
        sc_inbound.drain()
        r.check("held, it does not send", len(stub.sc_sent) == sent)
        S.release("8650007061")
        with store.tx() as c:
            c.execute("UPDATE sc_outbox SET not_before='2000-01-01' WHERE phone='8650007061'")
        sc_inbound.drain()
        r.check("released, it goes out on the campaign's own thread though that thread is paused for the handoff",
                len(stub.sc_sent) == sent + 1 and stub.sc_sent[-1][0] == "c-l-sc", str(stub.sc_sent[-1:]))

        kit.lead("8650007066", "bp-l-sc-late", to="")
        kit.sc("8650007066", "How much would you pay for it?", "c-l-sc-late")
        with store.tx() as c:
            c.execute("UPDATE escalations SET due_at='2000-01-01T00:00:00+00:00' WHERE phone='8650007066'")
        worker.flush_escalations()
        S.release("8650007066")
        with store.tx() as c:
            c.execute("UPDATE sc_outbox SET not_before='2000-01-01' WHERE phone='8650007066'")
        store.set_meta("lead_text_until:8650007066", "2000-01-01T00:00:00+00:00")
        sent = len(stub.sc_sent)
        sc_inbound.drain()
        late = [x for x in kit.sc_rows("8650007066", "reply") if x["intent"] == S.INTENT]
        r.check("on Smarter Contact too, a call-is-coming text that is an hour late is dropped",
                len(stub.sc_sent) == sent and len(late) == 1 and late[0]["status"] == "cancelled"
                and late[0]["detail"] == S.TOO_LATE, str(late))
    finally:
        config.DRY_RUN = was

    # ---- a lead this app only hears about from DataSift
    def hook(record: str, sequence: str = "Call New Lead", event: str = "sequence.action.triggered", n: str = "1") -> dict:
        return engine.process("datasift", {"event_type": event, "event_id": "evt-" + record + n,
                                           "sequence": {"uuid": "seq-1", "name": sequence},
                                           "property": {"uuid": record, "address": {}, "tags": [], "lists": []}})

    kit.lead("8650007062", "bp-l-hook", status="new_lead")
    store.add_message("8650007062", "in", "please call me about the house", author="owner")
    out = hook("bp-l-hook")
    r.check("a New Lead sequence's webhook stages the text for a number that owner has texted from",
            (out.get("speed_to_lead") or {}).get("staged") and out["speed_to_lead"]["phone"] == "8650007062", str(out))
    kit.lead("8650007063", "bp-l-cold")
    store.add_message("8650007063", "in", "hello", author="owner")
    out = hook("bp-l-cold")
    r.check("a webhook for a record that is not a lead texts nobody",
            not (out.get("speed_to_lead") or {}).get("staged") and "not a lead" in out["speed_to_lead"]["skipped"], str(out))
    kit.lead("8650007064", "bp-l-seq", status="new_lead")
    store.add_message("8650007064", "in", "hello", author="owner")
    r.check("a webhook from any other sequence is not a New Lead signal",
            "speed_to_lead" not in hook("bp-l-seq", sequence="Sold Property Cleanup"))
    r.check("DataSift's own test delivery texts nobody", "speed_to_lead" not in hook("bp-l-seq", event="webhook.test"))
    one = receiver._dedupe_key("datasift", {"event_type": "sequence.action.triggered", "event_id": "a", "property": {"uuid": "x"}})
    two = receiver._dedupe_key("datasift", {"event_type": "sequence.action.triggered", "event_id": "b", "property": {"uuid": "x"}})
    r.check("with the switch on, each DataSift delivery keeps its own id and is not dropped as a repeat", one != two, one)
    config.SPEED_TO_LEAD = "off"
    r.check("with it off, the receiver keys a DataSift delivery exactly as it always did",
            receiver._dedupe_key("datasift", {"event_type": "x", "event_id": "a", "property": {"uuid": "x"}}) == "datasift:")
    r.check("and a DataSift webhook stages nothing", "speed_to_lead" not in hook("bp-l-hook", n="2"))
    r.check("a smrtPhone event is keyed as before, whatever the switch says",
            receiver._dedupe_key("smrtphone", {"event": "smsIncoming", "smsId": "s1"}) == "smrtphone:smsIncoming:s1")
    kit.clear_queue()
    with store.tx() as c:
        c.execute("DELETE FROM escalations")


# ====================================================================== number health (4.7)
def _number_health(r, kit) -> None:
    config, store, N, sender_pool, seed = kit.m.config, kit.m.store, kit.m.numberhealth, kit.m.sender_pool, kit.m.seed
    receiver = kit.m.receiver
    print("\nbuild plan 4.7: number health: rest a flagged number, and a canary before a campaign day")
    out = N.rest(SELLER, "carrier flagged it")
    r.check("a flagged number goes on the rest list for about five days",
            4.9 < (datetime.fromisoformat(out["until"]) - datetime.now(timezone.utc)).total_seconds() / 86400 < 5.1
            and "8650000001" in N.resting())
    r.check("with the switch off the list is a note and nothing more: the number is not treated as resting",
            not N.is_resting(SELLER) and N.usable([SELLER, SELLER2]) == [SELLER, SELLER2])
    config.REST_FLAGGED = True
    r.check("switched on, the number is resting", N.is_resting(SELLER) and not N.is_resting(SELLER2))
    r.check("a new conversation never starts on a resting number", sender_pool.assign("8650007068", "Adriana") == SELLER2)
    store.ensure_conversation("8650007069", from_number=SELLER)
    r.check("a thread already on it keeps its number: an owner has one texting number",
            sender_pool.assign("8650007069", "Adriana") == SELLER)

    def cand(phone):
        c = seed.Candidate(phone=phone, record_uuid="bp-n", street="1 Test St", city="Knoxville", county="Knox",
                           owner_full="Test Owner")
        c.first, c.sender, c.message, c.status = "Test", "Adriana", "hi", "ready"
        return c

    plan = {c.phone: number for c, number, _ in seed.schedule([cand("8650007070"), cand("8650007069")])}
    r.check("a batch gives a new owner a rested-free number and leaves an existing thread on its own",
            plan == {"8650007070": SELLER2, "8650007069": SELLER}, str(plan))

    store.ensure_conversation("8650007071", from_number=SELLER)
    store.queue_message("8650007071", "an outreach touch", from_number=SELLER, intent="seed_touch_2")
    store.ensure_conversation("8650007072", from_number=SELLER)
    store.queue_message("8650007072", "an answer to their question", from_number=SELLER, intent="ASKING_WHO")
    result = kit.drain()
    r.check("the worker holds outreach from a resting number, and does not cancel it or move it",
            kit.rows("8650007071")[-1]["status"] == "queued" and result["held"] >= 1, str(result))
    r.check("an answer to someone who just wrote still goes: silence is worse for the owner than one text is for the number",
            kit.rows("8650007072")[-1]["status"] == "sent")
    longer = N.rest(SELLER, "still flagged", days=9)
    shorter = N.rest(SELLER, "a second look", days=1)
    r.check("resting a number again extends the rest and never shortens it", shorter["until"] == longer["until"])
    r.check("a rest ends on its own date", not N.is_resting(SELLER, datetime.now(timezone.utc) + timedelta(days=10)))
    r.check("and a person can end it early", N.unrest(SELLER) and not N.is_resting(SELLER) and not N.unrest(SELLER))
    kit.drain()
    r.check("once the rest is over the held outreach goes out, from the same number",
            kit.rows("8650007071")[-1]["status"] == "sent")
    N.rest(SELLER, "every number resting")
    N.rest(SELLER2, "every number resting")
    r.check("if every number is resting the list is handed back whole, so the failure is a held text and not no number at all",
            N.usable([SELLER, SELLER2]) == [SELLER, SELLER2])
    N.unrest(SELLER)
    N.unrest(SELLER2)

    # ---- who is flagged, from the carriers' own answers
    def ev(event: str, p: dict) -> None:
        p = dict(p, event=event)
        store.record_event("smrtphone", event, receiver._dedupe_key("smrtphone", p), p)

    for i in range(4):
        ev("smsOutgoing", {"smsId": f"nh-a{i}", "from": SELLER2, "callerIdName": "Adriana - 2"})
    for i in range(2):
        ev("smsDeliveryCallback", {"smsId": f"nh-a{i}", "status": "undelivered",
                                   "failure_reason": "Your message was flagged as objectionable by the carrier."})
    ev("smsOutgoing", {"smsId": "nh-d0", "from": BUYER, "callerIdName": "Selftest - Dispo"})
    ev("smsDeliveryCallback", {"smsId": "nh-d0", "status": "undelivered",
                               "failure_reason": "a US 10DLC number that is not associated with an approved A2P 10DLC Campaign."})
    flagged = N.flagged(days=1)
    r.check("a number the carriers refused as spam twice is flagged, with the reason",
            "8650000002" in flagged and "refused as spam" in flagged["8650000002"]["why"][0], str(flagged.get("8650000002")))
    r.check("a number that left the pool long ago is not on the list", all(k in {"8650000001", "8650000002", "8650000091", "8650000092"}
                                                                           for k in flagged), str(sorted(flagged)))
    plan = N.suggest(days=1)
    r.check("an unregistered number is told apart: resting will not fix registration",
            [v["number"] for v in plan["rest"]] == [SELLER2] and [v["number"] for v in plan["register"]] == [BUYER], str(plan)[:300])
    text = N.report(days=1)
    r.check("the readout says who to rest, that they are rested and not replaced, and when it is more than a quarter",
            "Rest these" in text and "do not replace" in text and "More than a quarter" in text and SELLER2 in text, text[:400])
    r.check("and says plainly when resting is switched off", "Resting is switched off" not in text)
    config.REST_FLAGGED = False
    r.check("with the switch off the readout says the list changes nothing", "Resting is switched off" in N.report(days=1))

    # ---- the canary
    kit.clear_queue()
    r.check("with no handset of our own there is no canary", N.canary_stage()["staged"] == 0 and "handset" in N.canary_stage()["refused"])
    store.suppress("8650007074", "opt_out")
    r.check("a suppressed number is never the canary's handset", N.canary_stage(to="8650007074")["staged"] == 0)
    config.CANARY_TO = "8650007073"
    store.ensure_conversation("8650007073", from_number=SELLER)      # the handset already has a sticky number
    out = N.canary_stage()
    rows = kit.rows("8650007073")
    r.check("the canary stages one line check per sending number to our own handset, held",
            out["staged"] == 4 and [x["status"] for x in rows] == ["held"] * 4
            and sorted(x["from_number"] for x in rows) == sorted([SELLER, SELLER2, BUYER, "+18650000092"]), str(out))
    r.check("staging it twice does not double it", N.canary_stage()["staged"] == 0)
    r.check("one pool can be checked on its own", N.canary_stage(pool="nope")["staged"] == 0)
    sent = len(kit.sent)
    kit.drain(live=True)
    r.check("nothing leaves until a person releases it", len(kit.sent) == sent)
    r.check("a person releases it", N.canary_release() == 4)
    kit.drain(live=True)
    froms = [x[2] for x in kit.sent[sent:]]
    r.check("each line check leaves from its OWN number, through the same send loop as every other text",
            sorted(froms) == sorted([SELLER, SELLER2, BUYER, "+18650000092"]) and all(x[0] == "8650007073" for x in kit.sent[sent:]),
            str(froms))
    r.check("and the handset's thread does not get moved to the last number that texted it",
            (store.get_conversation("8650007073") or {}).get("from_number") == SELLER)
    first_id = store._conn().execute(
        "SELECT sms_id FROM messages WHERE phone='8650007073' AND from_number=? AND intent='canary'", (SELLER,)).fetchone()
    ev("smsDeliveryCallback", {"smsId": first_id["sms_id"], "status": "delivered"})
    bad_id = store._conn().execute(
        "SELECT sms_id FROM messages WHERE phone='8650007073' AND from_number=? AND intent='canary'", (BUYER,)).fetchone()
    ev("smsDeliveryCallback", {"smsId": bad_id["sms_id"], "status": "undelivered",
                               "failure_reason": "not associated with an approved A2P 10DLC Campaign"})
    results = {x["number"]: x for x in N.canary_results()}
    r.check("the readout joins each line check to the carrier's verdict",
            results[SELLER]["result"] == "delivered" and results[BUYER]["result"] == "undelivered"
            and "10DLC" in results[BUYER]["detail"] and results[SELLER2]["result"] == "sent, no carrier verdict yet", str(results))
    r.check("and the day's line check is part of the number readout", "Today's line check" in N.report(days=1))
    config.CANARY_TO = ""
    kit.clear_queue()
