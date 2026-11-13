"""How the texting app asks the gate, and what it remembers for it (build plan 0.1, 0.22).

gate_rules.py is the rules, with no dependencies, so it can be held to the brain's copy by a
test. This file is the other half: the parts that need this app's own store and CRM client. It
answers one question in three places (when the seeder vets a record, when a Smarter Contact
loader does, and right before a queued text leaves): may THIS number on THIS record be texted,
and if not, why, in words a person reading the digest understands.

Three things the gate cannot know on its own are supplied here:

  * The numbers this app already suppressed as litigators (the Trestle pass writes them to the
    local suppression table). Passed in, a litigator's number holds the whole record, not just
    that one phone.
  * The do-not-call registry flag. DataSift shows it only on a search row's representative
    phone, so a number seen flagged during one preset sweep would be invisible the next time it
    turns up as some other record's second phone. The flagged numbers are remembered here.
  * Which program is texting. A deal blast to a buyer is judged on the person-level rules only
    (Do Not Market, a litigator, an opt-out, a death): a buyer's own record reading sold or
    buyer_found is what makes them a buyer, not a reason to hold them.

Nothing here runs unless SMS_AGENT_GATE or SMS_AGENT_SEND_RECHECK is switched on.
"""
from __future__ import annotations

import json
import logging

from . import config, crm, gate_rules, store

log = logging.getLogger(__name__)

# What stops a REPLY to somebody who just texted us. Answering a direct question is not
# prospecting, so the status rules (a lead, Not Interested, held for research) do not apply to
# it. These do: nobody answers a litigator, an opt-out, a dead owner's number or a sold house.
HARD_RULES = gate_rules.PERSON_RULES | {"sold"}

_settings: dict = {"raw": None, "cfg": None}

FLAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_flags (
    number TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
"""


def settings() -> gate_rules.GateConfig:
    """The gate's settings: the defaults, or SMS_AGENT_GATE_SETTINGS over them."""
    raw = config.GATE_SETTINGS_RAW or ""
    if _settings["raw"] == raw and _settings["cfg"] is not None:
        return _settings["cfg"]
    block, problems = None, []
    if raw.strip():
        try:
            block = json.loads(raw)
        except ValueError:
            problems.append("SMS_AGENT_GATE_SETTINGS is not JSON")
            block = None
        if block is not None and not isinstance(block, dict):
            problems.append("SMS_AGENT_GATE_SETTINGS must be an object")
            block = None
    cfg = gate_rules.parse(block, problems)
    if problems:
        # A bad setting must not open the gate. Say so and run on the defaults, which are the
        # strictest reading of every rule.
        log.error("gate settings ignored (%s); the defaults apply", "; ".join(problems))
        cfg = gate_rules.GateConfig()
    cfg.read_fields = cfg.read_fields or bool(config.GATE_READ_FIELDS)
    _settings["raw"], _settings["cfg"] = raw, cfg
    return cfg


def litigators() -> set[str]:
    """Numbers this app suppressed as litigators."""
    rows = store._conn().execute(
        "SELECT phone FROM suppression WHERE reason=?", (config.LITIGATOR_SUPPRESSION_REASON,)).fetchall()
    return {r["phone"] for r in rows if r["phone"]}


# ---------------------------------------------------------------------- the registry flag, remembered
def _flags():
    c = store._conn()
    c.executescript(FLAGS_SCHEMA)
    return c


def remember_flagged(numbers) -> int:
    """Keep the numbers a sweep saw carrying the do-not-call registry flag. Returns how many."""
    clean = sorted({gate_rules.digits(n) for n in numbers or []} - {""})
    if not clean:
        return 0
    _flags()
    with store.tx() as c:
        for n in clean:
            c.execute("INSERT INTO registry_flags(number, seen_at) VALUES (?,?)"
                      " ON CONFLICT(number) DO UPDATE SET seen_at=excluded.seen_at", (n, store.now()))
    return len(clean)


def flagged() -> set[str]:
    """Every number ever seen carrying the registry flag."""
    return {r["number"] for r in _flags().execute("SELECT number FROM registry_flags")}


def is_flagged(number) -> bool:
    n = gate_rules.digits(number)
    if not n:
        return False
    return _flags().execute("SELECT 1 FROM registry_flags WHERE number=? LIMIT 1", (n,)).fetchone() is not None


# ---------------------------------------------------------------------- the question
def _phone_on(rec: dict, phone: str) -> dict | None:
    want = gate_rules.digits(phone)
    for p in ((rec or {}).get("owner") or {}).get("phones") or []:
        if isinstance(p, dict) and gate_rules.digits(p.get("number")) == want:
            return p
    return None


def reasons(rec: dict, *, phone: str = "", channel: str = "text", row: dict | None = None, allow=(),
            program: str = "seller", hard_only: bool = False, custom: dict | None = None) -> list[str]:
    """Why this record (and this number on it) may not be texted. Empty means it may.

    The record-level rules come from the gate. "No usable number" is left out: the caller has
    already chosen a number by its own line-type rule (a mobile, or an unknown type that Trestle
    scored Dial First or Second), and the question here is whether that number is ALLOWED, which
    is the do-not-text rule below.
    """
    cfg = settings()
    verdict = gate_rules.check(rec, channel, cfg=cfg, allow=allow, row=row, custom=custom,
                               litigators=litigators())
    found = [(c, d) for c, d in verdict.reasons if c != "no_number"]
    if program in ("buyer", "dispo"):
        found = [x for x in found if x[0] in gate_rules.PERSON_RULES]
    if hard_only:
        found = [x for x in found if x[0] in HARD_RULES]
    out = [gate_rules.words(c, d) for c, d in found]
    if phone and rec and rec.get("uuid"):
        obj = _phone_on(rec, phone) or {"number": phone}
        why = gate_rules.do_not_text(obj, gate_rules.flagged_numbers([row] if row else []))
        if not why and is_flagged(phone):
            why = gate_rules.REGISTRY           # flagged in an earlier sweep, invisible in this one
        if why:
            out.append(why)
        elif gate_rules.litigator_marked(obj, litigators(), cfg) and not any("litigator" in x for x in out):
            out.append(gate_rules.words("litigator"))
    return out


def read(record_uuid: str, fresh: bool = False) -> tuple[dict | None, dict | None]:
    """(the record, its custom fields or None). The custom fields are read only when asked for."""
    rec = crm.get_record(record_uuid, fresh=fresh) if record_uuid else None
    custom = crm.custom_fields(record_uuid) if rec and settings().read_fields else None
    return rec, custom


def hold_reason(record_uuid: str, phone: str = "", *, row: dict | None = None, allow=(), program: str = "seller",
                hard_only: bool = False, fresh: bool = False) -> str:
    """The first reason this record may not be texted, "" when it may. A record that cannot be
    read is held: no answer from DataSift is never a yes."""
    rec, custom = read(record_uuid, fresh=fresh)
    if not rec:
        return gate_rules.words("unreadable")
    found = reasons(rec, phone=phone, row=row, allow=allow, program=program, hard_only=hard_only, custom=custom)
    return found[0] if found else ""
