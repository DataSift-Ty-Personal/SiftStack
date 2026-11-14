"""entity_principals: the person behind a company or trust that owns a house (build plan 1.15).

Two ways in. The autopilot's retrace stage hands a company-owned record here instead of parking
it as Mail Only (`entity: principals` in team/autopilot.yaml, stages._to_principals: the record
waits, out of the paid trace, until a person is named on it), and Diggs runs it when asked in
Slack. Two steps, the free one first:

  1. FREE, the reverse-address check (buyer_sweep.reverse_address, the Harper move): an LLC's mail
     usually goes to somebody's house, and the person who owns that house is usually the person
     behind the LLC. SiftMap is already paid for, and the trace address is by construction the
     person's own, which is why these skip trace at 95%.
  2. PAID, Enformion BusinessV2 ($0.10 a match, a miss is free), for a COMPANY only: a trust is
     not on the Secretary of State's rolls, so its trustee is a deep prospect's job. BusinessV2
     matches company names nationally, so an officer outside the company's own state is a
     different person and is dropped (measured: 2 of 12 entities had any Tennessee officer). An
     officer titled registered agent whose name has nothing to do with the company is usually its
     lawyer, so that name is reported and never written (principal_confidence "low").
     The officer is traced at the officer's own filing address or not at all: traced at the
     company's address a BusinessV2 principal hit 4%, at their own 77%.

Where the person is written, live only, every write read back:
  * the Decision Maker and DM Relationship custom fields (what every caller already reads for who
    signs), only when the field is empty or already names the same person: a name a person or the
    research put there is never overwritten;
  * one note on the record (the documented add-notes route) saying how the name was found. No
    phone number ever goes in it;
  * the autopilot's own memory: the fact `entity_principal` (the name, how it was found, the
    first and last name to trace, the address to trace them at) and `entity_pending.named`.
The record's OWNER stays the company: the deed is in its name, and the research write-back never
swaps a company owner either. `as_principal_record()` shows the record with the named person in
the owner's place, which is all the retrace's own code needs to trace that person like any other
owner; the numbers land on the company's owner, tagged with their source, and the caller reads
whom to ask for in Decision Maker.

Money: the paid step runs only on a live box, inside AGENTS_PRINCIPALS_RUN_USD for the run and the
brain's daily cap. A record the gate holds (Do Not Market, a litigator, an opt-out, a sold house)
is never sent to it. A name found before (the fact, AGENTS_PRINCIPALS_CACHE_DAYS) is not bought
or looked up again.
"""
from __future__ import annotations

import copy
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..autopilot import gate, ledger, stages
from ..autopilot.sources import Subject, same_street, subject_of
from . import skip_trace as common

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import buyer_sweep  # noqa: E402
import enformion_business as eb  # noqa: E402

WORKFLOW = "entity_principals"
DM_FIELD, REL_FIELD = "Decision Maker", "DM Relationship"
TRUST_RX = re.compile(r"\bTRUST(?:EE|EES)?\b|\bTTEE\b", re.I)
TRUST_WORDS = {"REVOCABLE", "IRREVOCABLE", "LIVING", "FAMILY", "TRUST", "TRUSTEE", "TRUSTEES", "TTEE", "THE",
               "OF", "DATED", "DTD", "AGREEMENT", "U", "A", "UA", "UAD", "ET", "AL", "TR"}
SOURCE_WORDS = {"siftmap-reverse-address": "owns the house the company's mail goes to",
                "siftmap-secondary-owner": "is on the title of the house the company's mail goes to",
                "enformion-businessv2": "is in the company's state filing"}


def _clean(name: str) -> str:
    return " ".join(str(name or "").split())


def display_name(raw: str, company: str = "") -> str:
    """The name as the record will show it: title case, and for a two-word name whose first word is
    also in the company's name, turned around (Smith Homes LLC -> "SMITH KENNETH" is Kenneth Smith).
    A longer name is left in the order its source printed it: guessing wrong reads worse than a
    county-style name a caller already knows how to read."""
    toks = _clean(raw).split()
    if len(toks) == 2:
        first, last = eb.first_last_for(raw, company)
        if first and last and first.lower() == toks[1].lower() and last.lower() == toks[0].lower():
            toks = [toks[1], toks[0]]
    return " ".join(t if any(c.islower() for c in t) else t.title() for t in toks)


def trust_person(company: str) -> str:
    """The person a trust is named after ("PAT EXAMPLE REVOCABLE TRUST" -> Pat Example), or ''.
    Said, never written: a trust's grantor may well have died, and then the successor trustee signs."""
    toks = [t for t in re.sub(r"[^A-Za-z ]", " ", company or "").upper().split() if t not in TRUST_WORDS]
    name = " ".join(toks)
    return name.title() if eb._is_human(name) else ""


def mail_parts(owner: dict) -> dict:
    a = (owner or {}).get("address") or {}
    return {"street": _clean(a.get("street")), "city": _clean(a.get("city")),
            "state": _clean(a.get("state")).upper(), "zip": str(a.get("postal_code") or "")[:5]}


def mail_line(parts: dict) -> str:
    tail = " ".join(x for x in (parts.get("state"), parts.get("zip")) if x)
    return ", ".join(x for x in (parts.get("street"), parts.get("city"), tail) if x)


# ------------------------------------------------------------------------- the two steps
def free_step(company: str, mail: dict, prop_street: str, map_client) -> dict:
    """The reverse-address check. {"person", "source", "trace"} on a hit, {"miss"} otherwise."""
    if not mail.get("street"):
        return {"miss": "the company has no mailing address on the record"}
    if prop_street and same_street(mail["street"], prop_street):
        return {"miss": "the company's mail goes to the house itself, so its address names nobody else"}
    line = mail_line(mail)
    got = buyer_sweep.reverse_address(buyer_sweep.drop_unit(line), map_client)
    if got["miss"] == "no property at the mailing address" and buyer_sweep.drop_unit(line) != line:
        got = buyer_sweep.reverse_address(line, map_client)
    if not got["principal"]:
        return {"miss": got["miss"]}
    # The person lives where the company's mail goes: that address is theirs to be traced at.
    return {"person": got["principal"], "source": got["source"], "title": "",
            "trace": {"street": buyer_sweep.drop_unit(mail["street"]), "city": mail["city"], "state": mail["state"],
                      "zip": mail["zip"]}}


def paid_step(company: str, mail: dict, officer_search=None) -> dict:
    """The business search. {"person", "source", "title", "trace", "billed"} on a hit; otherwise
    {"miss", "billed"} and, for an officer who is probably the company's lawyer, "low"."""
    state = mail.get("state") or "TN"
    city_state = ", ".join(x for x in (mail.get("city"), state) if x)
    officers, billed = eb.search_officers(company, city_state, search=officer_search)
    humans = [o for o in officers if eb._is_human(o.get("name") or "")]
    ranked = eb.rank_officers(company, humans, state)
    if not ranked:
        return {"billed": billed, "miss": ("the business search named %d officer(s), none in %s: a same-named company "
                                           "elsewhere is not this one" % (len(humans), state)) if humans
                else "the business search named no person"}
    best = ranked[0]
    title = _clean(best.get("title"))
    if eb.principal_confidence(company, best.get("name") or "", title) == "low":
        return {"billed": billed, "low": {"person": _clean(best.get("name")), "title": title},
                "miss": "the only %s officer is titled %s and shares no name with the company, which usually means "
                        "its lawyer, so the name is not written" % (state, (title or "agent").lower())}
    street, city, st, zip_ = eb.split_mail(best.get("address") or "")
    trace = {"street": street, "city": city, "state": st, "zip": zip_} if street and re.search(r"\d", street) else {}
    return {"billed": billed, "person": _clean(best.get("name")), "source": "enformion-businessv2", "title": title,
            "trace": trace}


# ------------------------------------------------------------------------- the retrace's side
def principal_subject(rec: dict, subj: Subject | None = None) -> Subject | None:
    """The named person behind a company-owned record as the subject a trace source looks for, or
    None while no person is named ON the record (a live write that read back) with a first name, a
    last name and an address of their own to trace them at."""
    subj = subj or subject_of(rec)
    f = ledger.fact(subj.record_uuid, "entity_principal") or {}
    t = f.get("trace") or {}
    if not (f.get("named") and f.get("first") and f.get("last") and t.get("street")
            and (t.get("zip") or (t.get("city") and t.get("state")))):
        return None
    return Subject(record_uuid=subj.record_uuid, owner_uuid=subj.owner_uuid, kind="person", first=f["first"],
                   last=f["last"], company="", street=subj.street, city=subj.city, state=subj.state, zip=subj.zip,
                   mail_street=t["street"], mail_city=t.get("city") or "", mail_state=t.get("state") or "",
                   mail_zip=t.get("zip") or "", attempts=subj.attempts, existing=set(subj.existing),
                   tags=list(subj.tags), org=False)


def as_principal_record(rec: dict) -> dict | None:
    """A copy of the record with the named person shown in the owner's place (their first and last
    name, their own address), or None while nobody is named. Only the owner's NAME and ADDRESS
    change: the owner's uuid, phones, flags and counters are the company owner's, so every number a
    trace finds lands on the record's real owner, and the gate reads the record exactly as it is."""
    s = principal_subject(rec)
    if s is None:
        return None
    r = copy.deepcopy(rec)
    ow = r.setdefault("owner", {})
    ow.update({"first_name": s.first, "last_name": s.last, "company": "",
               "address": {"street": s.mail_street, "city": s.mail_city, "state": s.mail_state, "postal_code": s.mail_zip}})
    return r


# ------------------------------------------------------------------------- the workflow
def _note(company: str, person: str, title: str, source: str) -> str:
    who = "%s (%s)" % (person, title.lower()) if title else person
    return ("Person behind %s: %s, who %s. Found by the principal finder; not skip traced yet. The record's owner is "
            "still the company, so ask for %s by name." % (company, who, SOURCE_WORDS.get(source, "was found"),
                                                          person.split()[0] if person else "them"))


def _write(crm, u: str, rec: dict, company: str, found: dict, display: str, run_id: str) -> dict:
    """The live writes, each recorded with the value it replaced, then read back."""
    out = {"fields": [], "kept": "", "note": False, "read_back": "", "problems": []}
    custom = crm.custom_fields(u) or {}
    current = _clean(custom.get(DM_FIELD))
    rel = ("%s of %s" % (found["title"].title(), company)) if found.get("title") else ("Principal of %s" % company)
    if current and eb.norm_name(current) != eb.norm_name(display):
        out["kept"] = current          # a decision maker a person or the research named stays
    else:
        for label, value in ((DM_FIELD, display), (REL_FIELD, rel)):
            fid = crm.custom_field_uuid(label)
            if not fid:
                out["problems"].append("this account has no %s field; the name is in the note only" % label)
                continue
            before = _clean(custom.get(label))
            if before == value:
                continue
            ledger.change(run_id, WORKFLOW, u, "custom_field", {"field_uuid": fid, "value": before, "label": label},
                          {"field_uuid": fid, "value": value, "label": label})
            crm.set_custom_field(u, fid, value)
            out["fields"].append(label)
    text = _note(company, display, found.get("title") or "", found.get("source") or "")
    if text.split(":")[0] not in str(rec.get("notes") or ""):
        ledger.change(run_id, WORKFLOW, u, "note", None, text)
        crm.message(u, text)
        out["note"] = True
    if out["fields"]:
        back = _clean((crm.custom_fields(u) or {}).get(DM_FIELD))
        out["read_back"] = "ok" if eb.norm_name(back) == eb.norm_name(display) else "mismatch"
        if out["read_back"] == "mismatch":
            out["problems"].append("Decision Maker read back as %r, not %r" % (back or "empty", display))
    return out


def run(task: dict, *, crm=None, pol=None, map_client=None, officer_search=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if pol is None:
        pol, note = common.load_policy()
    else:
        note = ""
    u = task.get("record_uuid") or ""
    if not u:
        return {"ok": False, "say": "Name the record a company or trust owns (its uuid or street line)."}
    crm = crm or common.default_crm()
    rec = crm.record(u) or {}
    if not rec.get("uuid"):
        return {"ok": False, "say": "No record with uuid %s." % u}
    live = config.live()
    res = {"ok": True, "live": live, "record": u, "street": stages.addr(rec), "source_task": task.get("source") or "",
           "problems": [note] if note else [], "misses": [], "named": False}
    subj = subject_of(rec)
    if not subj.org:
        res["head"] = "The owner of record is not a company or a trust, so there is nobody behind it to find."
        return res
    company = subj.company or " ".join(x for x in (subj.first, subj.last) if x)
    res["company"] = company
    v = gate.check(rec, "research", cfg=getattr(pol, "gate", None) or gate.GateConfig())
    if not v.ok:
        res.update(held=v.reason(), head="Held back (%s); nothing looked up or bought." % v.reason())
        return res
    mail = mail_parts(rec.get("owner") or {})
    money = common.Money(int(task.get("id") or 0), common.setting("AGENTS_PRINCIPALS_RUN_USD", 0.50))
    fact = ledger.fact(u, "entity_principal") or {}
    fresh = False
    try:
        at = datetime.fromisoformat(str(fact.get("at") or ""))
        fresh = now - at < timedelta(days=common.setting("AGENTS_PRINCIPALS_CACHE_DAYS", 90))
    except ValueError:
        pass
    found: dict = {}
    if fact.get("named") and fact.get("display"):
        res.update(person=fact["display"], how=SOURCE_WORDS.get(fact.get("source"), ""), title=fact.get("title") or "",
                   traceable=bool(fact.get("first") and fact.get("last") and (fact.get("trace") or {}).get("street")),
                   named=True, head="%s was named as the person behind %s on %s; nothing to do." % (
                       fact["display"], company, str(fact.get("at") or "")[:10]))
        return res
    if fresh and fact.get("person"):
        found = {k: fact.get(k) for k in ("person", "source", "title", "trace")}
        res["misses"].append("found before, on %s; not looked up again" % str(fact.get("at"))[:10])
    else:
        try:
            free = free_step(company, mail, subj.street, map_client or buyer_sweep.MapClient())
        except Exception as exc:  # noqa: BLE001  (a SiftMap failure is said, and the paid step may still run)
            free = {"miss": "the map lookup failed: %s" % str(exc)[:120]}
            res["problems"].append(free["miss"])
        if free.get("person"):
            found = free
        else:
            res["misses"].append(free["miss"])
            if TRUST_RX.search(company):
                tp = trust_person(company)
                res["misses"].append("a trust is not on the Secretary of State's rolls, so its trustee is a deep prospect's "
                                     "job" + ("; the trust carries the name %s, whose own status research settles" % tp
                                              if tp else ""))
            elif officer_search is None and not eb.is_configured():
                res["misses"].append("the business search has no key on this box (ENFORMION_AP_NAME), so it was not tried")
            elif not live:
                res["would_usd"] = eb.UNIT_USD
                res["misses"].append("the business search would cost up to $%.2f (plan: not bought)" % eb.UNIT_USD)
            elif not money.allows(eb.UNIT_USD, "the business search"):
                res["misses"].extend(money.refused)
            else:
                paid = paid_step(company, mail, officer_search)
                if paid.get("billed"):
                    money.charge(eb.UNIT_USD, "entity principals: business search for %s" % company)
                if paid.get("person"):
                    found = paid
                else:
                    res["misses"].append(paid["miss"])
                    if paid.get("low"):
                        res["low_confidence"] = paid["low"]
    res["usd"] = money.spent
    if not found:
        res["head"] = "No person found behind %s." % company
        if live:
            ledger.event("%s-%s" % (WORKFLOW, uuid.uuid4().hex[:8]), WORKFLOW, u, "principal",
                         {"found": False, "why": res["misses"][-1:]}, usd=money.spent, now=now, tz=pol.tz)
        return res
    display = display_name(found["person"], company)
    first, last = eb.first_last_for(found["person"], company)
    res.update(person=display, how=SOURCE_WORDS.get(found.get("source"), ""), title=found.get("title") or "",
               traceable=bool(first and last and (found.get("trace") or {}).get("street")))
    memory = {"person": found["person"], "display": display, "source": found.get("source"), "title": found.get("title") or "",
              "first": first, "last": last, "trace": found.get("trace") or {}, "company": company,
              "at": str(fact.get("at")) if fresh else now.isoformat(timespec="seconds"),
              "named": bool(fact.get("named")) if fresh else False}
    if not live:
        # What the free step found is the autopilot's own knowledge, kept in plan mode too; nothing
        # is written to the record, and `named` stays false until a live write reads back.
        ledger.set_fact(u, "entity_principal", memory)
        res["head"] = "Would name %s as the person behind %s." % (display, company)
        return res
    run_id = "%s-%s" % (WORKFLOW, uuid.uuid4().hex[:8])
    w = _write(crm, u, rec, company, found, display, run_id)
    res["problems"] += w["problems"]
    res.update(fields=w["fields"], note_added=w["note"], read_back=w["read_back"], run_id=run_id)
    if w["kept"]:
        res["kept"] = w["kept"]
        res["head"] = "%s is behind %s, but the record already names %s as the decision maker; left as it is." % (
            display, company, w["kept"])
    else:
        memory["named"] = w["read_back"] in ("ok", "") and not w["problems"]
        res["named"] = memory["named"]
        res["head"] = "%s is behind %s; %s." % (display, company, "named on the record" if res["named"]
                                                 else "the write did not read back")
    ledger.set_fact(u, "entity_principal", memory)
    pending = ledger.fact(u, "entity_pending") or {}
    if res["named"]:
        ledger.set_fact(u, "entity_pending", dict(pending, named=now.isoformat(timespec="seconds"), person=display))
    ledger.event(run_id, WORKFLOW, u, "principal", {"found": True, "person": display, "source": found.get("source"),
                                                    "named": res["named"]}, usd=money.spent, now=now, tz=pol.tz)
    return res


def render(task: dict, result: dict) -> str:
    if not result.get("ok"):
        return result.get("say") or ""
    # A handoff from the autopilot that named nobody is a quiet miss: the stage's own digest counts
    # them, and up to ten a day in the channel would bury the ones a person has to act on.
    if (result.get("source_task") or "").startswith("autopilot") and not result.get("person") and not result.get("problems"):
        return ""
    plan = "" if result.get("live") else " _(plan: nothing written or bought)_"
    icon = ":warning:" if result.get("problems") else ":white_check_mark:"
    out = ["%s *entity principals* %s%s %s" % (icon, result.get("street") or "", plan, result.get("head") or "")]
    if result.get("person") and result.get("how"):
        out.append("  %s %s%s." % (result["person"], result["how"], " (%s)" % result["title"].lower() if result.get("title") else ""))
    if result.get("person") and not result.get("traceable"):
        out.append("  Not traceable as printed (no first and last name, or no address of their own); a person fixes the name first.")
    for m in (result.get("misses") or [])[:3]:
        out.append("  %s" % m)
    if result.get("usd"):
        out.append("  Spent $%.2f." % result["usd"])
    for p in (result.get("problems") or [])[:3]:
        out.append(":small_orange_diamond: %s" % p)
    return "\n".join(out)
