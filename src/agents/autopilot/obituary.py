"""The obituary pipeline, and obituaries matched against probate (build plan 3.6 and 3.7).

An obituary record is a house on DataSift's own Obituary list, inside the buy box: a household
that has had a death. It is NOT known that the owner died. On one measured record (2026-07-29) the
obituary was the owner's husband's and the owner was alive, and an unresearched caller would have
asked a recent widow for her dead husband. So nothing here speaks to a dead owner, by a dead
owner's first name, or about a death. What the pipeline adds is time:

    the first month         nothing goes out, and a call made inside it is reported
    month one to three      one soft letter, to the family, about the house
    three months            research on every one (the research queue ranks them; this stage
                            counts who is due and who has been done)
    after three months      the monthly obituary mail, twelve pieces at most, until research is done
    six months, no probate  the tag "Obituary, No Probate", which the research queue ranks right
                            behind sales, probates and condemned houses

PROBATE RECORDS ARE KEPT OUT: the court named a living representative, and that record has its own
lane. The day a probate filing lands on an obituary record (the same house is the same record),
this pipeline lets go of it: the six-month tag comes off, its staged mail is pulled and it is
counted as moved. When the filing sits on a SECOND record at the same street, the obituary record
is tagged Probate Filed and left alone. It is never put on the Probate list: its owner of record
is not the court's representative, and the gate would then let a cold stage dial a dead owner by
name.

THE HOLD RULES LIVE IN THE GATE, not here. An Obituary-list record is held out of every cold
channel until research has run. A stage of this kind is given the allow word `deceased` in the
playbook, which is what opens the lane's own mail; without it every piece is held and the stage
says so. Timing is the only rule this module adds on top.

CALLS open only after the hold, and by default only once research has run and found the owner
living (`calls: after_research`): that is the widow who should be called, gently, about her own
house. A dead owner with a verified signer belongs to the heir lane, and a dead owner with none
stays on mail. `calls: after_hold` opens the old obituary cold call instead, `off` never assigns.

Mail goes "to the family": the name line is the surname only ("The Example Family"), so a piece
never carries the first name of a person who may have died. Pieces are staged for a person to
release and leave as a file for the mail house, since DataSift's own mail prints the owner's name.

BLOUNT COUNTY has no obituary index, and this stage does not need one: the list is DataSift's in
both counties and every date is read off the record. What differs in Blount is downstream, where
research confirms fewer deaths without the index.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import store
from . import activity, gate, heirlane, lanes, ledger, mail, researchqueue, windows
from .sources import same_street
from .stages import Ctx, addr, assignee_of, balanced_pick, resolve_callers, tag_titles, WORKLOAD_EXCLUDE

log = logging.getLogger(__name__)

KIND_MAIL = "obituary_mail"
FACT = "obituary_facts"           # the few things about an obituary record that decide its window
MATCH_FACT = "obituary_probate"   # a probate filing seen on, or beside, an obituary record
SOFT, MONTHLY = "obituary_soft", "obituary_monthly"

DEFAULTS = {
    "hold_days": 30,                # nothing in the first month (decision D24)
    "research_days": 90,            # research at three months; the soft letter goes before this
    "mail_every_days": 30,
    "mail_pieces": 12,
    "soft_piece": "handwritten_letter",
    "monthly_piece": "letter",
    "soft_copy": None,              # optional body for the soft letter; the one below otherwise
    "monthly_copy": None,           # optional list of bodies for the monthly pieces
    "no_probate_days": 180,
    "no_probate_tag": "Obituary, No Probate",
    "probate_filed_tag": "Probate Filed",
    "calls": "after_research",      # after_research | after_hold | off
    "call_presets": [],             # the stage's presets whose records may be handed to a caller
    "max_assign": 25,
    "max_reads": 150,
    "max_scan": 3000,
    "refresh_days": 14,
    "daily_pieces": 40,
    "max_tag_writes": 50,
    "obituary_date_fields": ["Date of Death"],
    "researched_tags": ["Deep Prospected", "Heir Identified", "Heirs Resolved"],
    "research_workflow": "deep_prospect",
    "match_probate": True,
    "obituary_list": "Obituary",
    "probate_list": "Probate",
    "max_log_calls": 60,
}

SOFT_LETTER = ("Hello, my name is {sender}. I am writing to the family at {street}. If the house is ever more than you "
               "want to look after, I would be glad to talk about buying it as it is, on your timing. There is no "
               "hurry at all. You can reach me at {phone}.")
MONTHLY_LETTERS = [
    "Hello, this is {sender}. I wrote before about the house at {street}. If selling it as it is would ever help your family, I would be glad to talk. My number is {phone}.",
    "Hello from {sender}. I am still interested in the house at {street}, with nothing to fix or clean out. If the time is ever right, please call or text {phone}.",
    "Hello, {sender} here about {street}. I buy houses in the area and can work around your schedule. If you would like to talk, my number is {phone}.",
]


def window(age_days: int, o: dict) -> str:
    """Which part of the timeline a record is in: hold, soft or research."""
    if age_days < int(o["hold_days"]):
        return "hold"
    if age_days < int(o["research_days"]):
        return "soft"
    return "research"


def options(st) -> dict:
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k)
        o[k] = v if got is None else got
    o["calls"] = str(o["calls"]).strip().lower()
    return o


def audit_copy_pool(o: dict) -> list[str]:
    """The letters this stage writes, audited with the heir lane's own rules: no death, no list."""
    out = []
    soft = str(o["soft_copy"] or SOFT_LETTER)
    monthly = [str(b) for b in (o["monthly_copy"] or []) if str(b).strip()] or MONTHLY_LETTERS
    for label, tpl in [("the soft letter", soft)] + [("monthly letter %d" % i, t) for i, t in enumerate(monthly, 1)]:
        try:
            text = " ".join(tpl.format(sender="Dana", street="12 Oak St", phone="865-555-0100").split())
        except (KeyError, IndexError, ValueError) as exc:
            out.append("%s has a blank the stage cannot fill (%s)" % (label, exc))
            continue
        for p in heirlane.audit_copy(text, street="12 Oak St", sms=False):
            out.append("%s %s" % (label, p))
    return out


def family_name(owner: dict) -> str:
    """The name line for a piece to the family: the surname only. '' when the owner is a company or
    a trust, or carries no surname: there is no family to write to."""
    if researchqueue.owner_kind(owner) != "person":
        return ""
    last = " ".join(str((owner or {}).get("last_name") or "").split())
    if len(last) < 2:
        return ""
    shown = last.title() if (last.isupper() or last.islower()) else last
    return "The %s Family" % shown


def facts_of(rec: dict, custom: dict | None, o: dict, gate_cfg, now: datetime) -> dict:
    """What decides an obituary record's window, read once and remembered."""
    tags = {t.lower() for t in tag_titles(rec)}
    marks = tags | {str(x).strip().lower() for x in (rec.get("lists") or []) if isinstance(x, str)}
    low = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    obit = researchqueue.iso_day(rec.get("last_obituary_date"))
    for label in o["obituary_date_fields"]:
        obit = obit or researchqueue.iso_day(low.get(str(label).strip().lower()))
    return {"at": now.isoformat(timespec="seconds"), "obit_date": obit,
            "probate": bool(marks & {m.lower() for m in gate_cfg.probate_marks}),
            "probate_filed": str(o["probate_filed_tag"]).lower() in tags,
            "no_probate_tag": str(o["no_probate_tag"]).lower() in tags,
            "researched": bool(tags & {t.lower() for t in o["researched_tags"]}),
            "lead": lanes.status_of(rec) in lanes.LEAD_STATUSES,
            "owner_uuid": (rec.get("owner") or {}).get("uuid") or ""}


def mail_history(record_uuid: str, stage_id: str) -> dict:
    """What has been mailed to this record: the soft letter, the monthly count, and when the last
    piece from ANY stage went, so two series never run at once."""
    soft, monthly, last = False, 0, ""
    for e in ledger.events(record_uuid, "mail"):
        last = max(last, e["at"])
        if e["stage"] != stage_id:
            continue
        series = e["detail"].get("series")
        soft = soft or series == SOFT
        monthly += 1 if series == MONTHLY else 0
    return {"soft": soft, "monthly": monthly, "last": last}


def staged_keys(stage_id: str) -> set:
    out = set()
    for b in ledger.batches(status="staged", stage=stage_id):
        if b["kind"] != KIND_MAIL:
            continue
        for it in (ledger.batch(b["id"]) or {}).get("items") or []:
            out.add((it.get("record_uuid") or "", it.get("series") or ""))
    return out


def still_due(ctx: Ctx, rec: dict, it: dict) -> str:
    """Why a staged obituary piece may no longer go out, or ''. The lane first: a probate filing, a
    finished research run or a new lead each take the record out of this pipeline. Then the gate."""
    if not rec.get("uuid"):
        return "the record could not be read"
    o = options(ctx.stage)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    f = facts_of(rec, None, o, gate_cfg, ctx.now)
    if f["lead"]:
        return "the record became a lead"
    if f["probate"] or f["probate_filed"]:
        return "a probate was filed: the record left the obituary lane"
    if f["researched"] or researchqueue.research_state(rec["uuid"], str(o["research_workflow"]))["done"]:
        return "research is done: the obituary mail stops"
    v = ctx.verdict(rec, "mail")
    return "" if v.ok else v.reason()


def record_piece(ctx: Ctx, rec: dict, it: dict, ref: str, state: dict) -> None:
    """A released obituary piece, written down in the ledger (as mail, against the outreach budget)
    and on the record: the month tag, the last-mailed date and a note."""
    u = it["record_uuid"]
    today = ledger.day(ctx.now, ctx.policy.tz)
    cost = float(it.get("price") or 0)
    ledger.event(ctx.run_id, ctx.stage.id, u, "mail",
                 {"piece": it.get("piece"), "step": int(it.get("letter") or 1), "vendor": "csv", "ref": ref,
                  "logged": "tag+note", "series": it.get("series")}, usd=round(cost, 4), now=ctx.now, tz=ctx.policy.tz)
    ctx.settle(0, cost, "obituary mail")
    try:
        tag = str(ctx.stage.get("mailed_tag", heirlane.DEFAULTS["mailed_tag"]) or "").format(yyyy=today[:4], mm=today[5:7])
        if tag:
            ctx.crm.add_tags(u, [tag], rec or None)
        if "field" not in state:
            try:
                state["field"] = ctx.crm.custom_field_uuid(str(ctx.stage.get("last_mailed_field",
                                                                         heirlane.DEFAULTS["last_mailed_field"])))
            except Exception:  # noqa: BLE001
                state["field"] = ""
        if state.get("field"):
            ctx.crm.set_custom_field(u, state["field"], today)
        what = "the soft letter to the family" if it.get("series") == SOFT else \
            "obituary mail %d of %d to the family" % (int(it.get("letter") or 1), int(it.get("of") or 1))
        ctx.crm.message(u, "Mailed: %s, %s." % (what, ref), pin=False)
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("the obituary piece for %s is in the ledger, but the record was not marked (%s)" % (
            it.get("property_street") or "a record", str(exc)[:80]))


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, uploaded: bool = False,
            now: datetime | None = None) -> dict:
    """Release a batch of obituary letters (the same two steps as the heir lane's batches)."""
    return heirlane.release_roster(batch_id, (KIND_MAIL,), still_due, record_piece, crm=crm, pol=pol, commit=commit,
                                   uploaded=uploaded, now=now)


def _days_since(day: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(day)).days
    except (TypeError, ValueError):
        return None


def _piece(rec: dict, series: str, number: int, total: int, kind: str, body: str, sender: dict,
           price: float) -> tuple[dict | None, str]:
    """(the staged item, '') or (None, why this record cannot be mailed to the family)."""
    ow = rec.get("owner") or {}
    name = family_name(ow)
    if not name:
        return None, "a company, a trust or an owner with no surname: no family to write to"
    ma = ow.get("address") or {}
    street, city = " ".join(str(ma.get("street") or "").split()), " ".join(str(ma.get("city") or "").split())
    state, zip5 = str(ma.get("state") or "").strip(), str(ma.get("postal_code") or "")[:5]
    if not (street and city and state and len(zip5) == 5):
        return None, "no complete mailing address"
    pa = rec.get("address") or {}
    text = " ".join(body.format(sender=sender.get("name") or "", street=pa.get("street") or "",
                                phone=sender.get("phone") or "").split())
    bad = heirlane.audit_copy(text, street=pa.get("street") or "", sms=False)
    if bad:
        return None, "the letter was refused: it %s" % bad[0]
    return {"kind": "letter", "series": series, "record_uuid": rec["uuid"], "heir": "", "name": name, "first_name": "",
            "piece": kind, "letter": number, "of": total, "street": street, "city": city, "state": state, "zip": zip5,
            "property_street": pa.get("street") or "", "property_city": pa.get("city") or "", "text": text,
            "price": price}, ""


def _match_probate(ctx: Ctx, o: dict, rows: list[dict], known: dict, gone: dict) -> None:
    """3.7: a probate filing against the obituary list, by property address. Same record first (a
    notice at the same house attaches to the same record), then a second record at the same street."""
    obit_id, prob_id = ctx.crm.list_uuid(str(o["obituary_list"])), ctx.crm.list_uuid(str(o["probate_list"]))
    if not obit_id or not prob_id:
        ctx.out.problem("the %s and %s lists are both needed to match filings; one of them is not in this account" % (
            o["obituary_list"], o["probate_list"]))
        return
    cap = int(o["max_scan"])
    both = ctx.crm.rows({"all_lists": [obit_id, prob_id]}, cap=cap)
    for row in both:
        u = row.get("uuid")
        if not u:
            continue
        gone[u] = "a probate was filed"
        if ledger.fact(u, MATCH_FACT) is not None:
            continue
        ctx.out.tally["a probate was filed on an obituary record: moved to the probate lane"] += 1
        ctx.out.line("%s: a probate was filed, the court's representative is the contact now" % addr(row))
        if ctx.live:
            # Said once, the day it lands. A plan run does not remember it: nothing was done about
            # it yet, and a later live run must still see it as new.
            ledger.set_fact(u, MATCH_FACT, {"at": ctx.now.isoformat(timespec="seconds"), "how": "same record"})
    # A second record at the same street: compare house number and zip first, then the street name.
    both_ids = {r.get("uuid") for r in both}
    by_door: dict[tuple, list[dict]] = {}
    for row in rows:
        a = row.get("address") or {}
        street = str(a.get("street") or "")
        door = (street.split(" ")[0] if street else "", str(a.get("postal_code") or "")[:5])
        if door[0].isdigit() and row.get("uuid") not in both_ids:
            by_door.setdefault(door, []).append(row)
    writes = 0
    filed = str(o["probate_filed_tag"])
    for prow in ctx.crm.rows({"any_lists": [prob_id]}, cap=cap):
        a = prow.get("address") or {}
        street = str(a.get("street") or "")
        door = (street.split(" ")[0] if street else "", str(a.get("postal_code") or "")[:5])
        for orow in by_door.get(door, []):
            u = orow["uuid"]
            if u == prow.get("uuid") or not same_street(street, str((orow.get("address") or {}).get("street") or "")):
                continue
            gone[u] = "a probate was filed on a second record"
            if ledger.fact(u, MATCH_FACT) is not None or (known.get(u) or {}).get("probate_filed"):
                continue
            if writes >= int(o["max_tag_writes"]):
                ctx.out.tally["probate matches left for the next run"] += 1
                continue
            writes += 1
            ctx.out.tally["a probate was filed on a second record at the same street: tagged %s" % filed] += 1
            ctx.out.line("%s: a probate record exists for this house; work that one" % addr(orow))
            ctx.change(u, "add_tags", None, [filed])
            ctx.write("probate filed", ctx.crm.add_tags, u, [filed], None)
            if ctx.live:
                # Remembered only once the tag is on the record: a write that failed is tried again.
                ledger.set_fact(u, MATCH_FACT, {"at": ctx.now.isoformat(timespec="seconds"), "how": "second record",
                                               "probate_record": prow.get("uuid")})
            try:
                ctx.write("probate filed note", ctx.crm.message, u,
                          "A probate record exists for this house. The court's representative is on that record; "
                          "this one is left alone and its obituary mail stops.")
            except Exception as exc:  # noqa: BLE001  (the tag is what stops the mail; the note is for the reader)
                ctx.out.problem("%s is tagged %s, but its note did not land (%s)" % (addr(orow), filed, str(exc)[:80]))


def run_obituary(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    if o["calls"] not in ("after_research", "after_hold", "off"):
        ctx.out.problem("calls must be after_research, after_hold or off; %r is read as off" % o["calls"])
        o["calls"] = "off"
    copy_problems = audit_copy_pool(o)
    for p in copy_problems[:3]:
        ctx.out.problem("obituary copy refused: %s. No mail is staged until the copy is fixed." % p)

    seen, rows = set(), []
    for title in st.presets:
        for row in researchqueue._preset_rows(ctx, title, int(o["max_scan"])):
            if row.get("uuid") and row["uuid"] not in seen:
                seen.add(row["uuid"])
                rows.append(row)
    ctx.out.found = len(rows)

    # ---- the few facts that decide a record's window: read once, refreshed every couple of weeks
    refresh = float(o["refresh_days"])
    remembered = researchqueue.facts_by_key(FACT)          # one read, not one per record
    known = {r["uuid"]: (remembered.get(r["uuid"]) if isinstance(remembered.get(r["uuid"]), dict) else None) for r in rows}
    order = sorted(rows, key=lambda r: (known[r["uuid"]] is not None, str((known[r["uuid"]] or {}).get("at") or "")))
    reads = 0
    fresh: dict[str, dict] = {}
    for row in order:
        u = row["uuid"]
        f = known[u]
        if f is not None and researchqueue._age_days(str(f.get("at") or ""), ctx.now) < refresh:
            continue
        if reads >= int(o["max_reads"]):
            break
        reads += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid"):
            continue
        custom = None
        if not rec.get("last_obituary_date"):
            custom = researchqueue.custom_of(ctx, u, max_age=timedelta(days=7))
        known[u] = facts_of(rec, custom, o, gate_cfg, ctx.now)
        ledger.set_fact(u, FACT, known[u])           # knowledge about the record, kept in plan mode too
        fresh[u] = rec
    unread = sum(1 for r in rows if known[r["uuid"]] is None)
    if unread:
        ctx.out.tally["not read yet (read cap, next run)"] = unread

    gone: dict[str, str] = {}
    if o["match_probate"]:
        try:
            _match_probate(ctx, o, rows, known, gone)
        except Exception as exc:  # noqa: BLE001  (the match failing must not stop the timeline)
            ctx.out.problem("could not match probate filings against the obituary list (%s)" % str(exc)[:120])

    # ---- sort every known record into its window
    by_window: Counter = Counter()
    hold, soft_due, monthly_due, tag_due, untag_due = [], [], [], [], []
    research = researchqueue.research_index(str(o["research_workflow"]))      # one read for the whole list
    for row in rows:
        u = row["uuid"]
        f = known[u]
        if f is None:
            continue
        if f["lead"]:
            ctx.out.tally["now a lead: the lead manager has it"] += 1
            gone[u] = "the record became a lead"
            continue
        if f["probate"] or f["probate_filed"] or str(gone.get(u) or "").startswith("a probate"):
            ctx.out.tally["probate: it has its own lane"] += 1
            gone.setdefault(u, "a probate was filed")
            if f["no_probate_tag"]:
                untag_due.append(row)         # six months with no filing is no longer true of this house
            continue
        age = _days_since(f["obit_date"], today)
        if age is None or age < 0:
            ctx.out.tally["no obituary date on the record: left alone"] += 1
            continue
        researched = f["researched"] or bool((research.get(u) or {}).get("done"))
        w = window(age, o)
        by_window[w] += 1
        if w == "hold":
            hold.append((row, f))
            continue
        # Six months with no filing is a fact about the house, whether or not research has run.
        if age >= int(o["no_probate_days"]) and not f["no_probate_tag"]:
            tag_due.append(row)
        if researched:
            ctx.out.tally["research is done: the obituary mail has stopped"] += 1
            gone[u] = "research is done"
            continue
        if w == "soft":
            soft_due.append((row, f, age))
        else:
            ctx.out.tally["due for research (the research queue ranks them)"] += 1
            monthly_due.append((row, f, age))

    # ---- the first month: nothing goes out, and a call inside it is said out loud
    if hold:
        try:
            activity.sync(ctx.crm, "calls", now=ctx.now, max_calls=int(o["max_log_calls"]))
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read the call log (%s); calls inside the first month were not checked" % str(exc)[:80])
        called = []
        for row, f in hold:
            if not f.get("owner_uuid"):
                continue
            since = datetime.fromisoformat(f["obit_date"]).replace(tzinfo=timezone.utc)
            if activity.dial_days(f["owner_uuid"], since=since, tz=ctx.policy.tz):
                called.append(addr(row))
        if called:
            ctx.out.problem("%d obituary record(s) were called inside their first month (for example %s)" % (
                len(called), ", ".join(called[:2])))

    # ---- mail: the one soft letter, then the monthly pieces until research is done
    pieces: list[dict] = []
    usd = 0.0
    if not copy_problems:
        vendor = mail.Vendor(dict(st.get("prices") or {}))
        sender = mail.sender_block(st)
        staged = staged_keys(st.id)
        monthly_bodies = [str(b) for b in (o["monthly_copy"] or []) if str(b).strip()] or MONTHLY_LETTERS
        gap = timedelta(days=float(o["mail_every_days"]))
        budget_used = False
        for series, due in ((SOFT, soft_due), (MONTHLY, monthly_due)):
            for row, f, _age in due:
                u = row["uuid"]
                hist = mail_history(u, st.id)
                if series == SOFT and hist["soft"]:
                    continue
                if series == MONTHLY and hist["monthly"] >= int(o["mail_pieces"]):
                    ctx.out.tally["all %d monthly pieces sent" % int(o["mail_pieces"])] += 1
                    continue
                if hist["last"] and ctx.now - datetime.fromisoformat(hist["last"]) < gap:
                    continue
                if (u, series) in staged:
                    ctx.out.tally["already staged, waiting on release"] += 1
                    continue
                if len(pieces) >= int(o["daily_pieces"]):
                    ctx.out.tally["left for tomorrow (daily cap %d)" % int(o["daily_pieces"])] += 1
                    continue
                if budget_used:
                    ctx.out.tally["left for tomorrow (the day's mail budget is used)"] += 1
                    continue
                rec = fresh.get(u) or ctx.crm.record(u)
                if not ctx.passes(rec, "mail", row=row):
                    continue
                kind = str(o["soft_piece"] if series == SOFT else o["monthly_piece"])
                price = vendor.price(kind)
                ok, why = ctx.budget(usd + price, u, record_usd=0)
                if not ok:
                    budget_used = True
                    ctx.out.tally["left for tomorrow (%s)" % why] += 1
                    continue
                number = 1 if series == SOFT else hist["monthly"] + 1
                body = str(o["soft_copy"] or SOFT_LETTER) if series == SOFT else monthly_bodies[(number - 1) % len(monthly_bodies)]
                item, why = _piece(rec, series, number, 1 if series == SOFT else int(o["mail_pieces"]), kind, body,
                                   sender, price)
                if not item:
                    ctx.out.tally["not mailable: %s" % why] += 1
                    continue
                usd += price
                pieces.append(item)

    # ---- six months with no probate: the tag (3.7), and off again when a filing appears
    tagged = 0
    tag = str(o["no_probate_tag"])
    for row in tag_due[: int(o["max_tag_writes"])]:
        u = row["uuid"]
        rec = fresh.get(u)
        if rec is None and ctx.live:
            rec = ctx.crm.record(u)           # what is remembered can be two weeks old: look before writing
        if rec is not None:
            now_f = facts_of(rec, None, o, gate_cfg, ctx.now) if rec.get("uuid") else None
            if not now_f or now_f["probate"] or now_f["no_probate_tag"] or now_f["lead"]:
                continue
        ctx.change(u, "add_tags", None, [tag])
        ctx.write("no probate", ctx.crm.add_tags, u, [tag], rec)
        tagged += 1
        if ctx.live:
            known[u] = dict(known[u], no_probate_tag=True)
            ledger.set_fact(u, FACT, known[u])
    if len(tag_due) > int(o["max_tag_writes"]):
        ctx.out.tally["six-month tags left for the next run"] = len(tag_due) - int(o["max_tag_writes"])
    for row in untag_due[: int(o["max_tag_writes"])]:
        u = row["uuid"]
        ctx.change(u, "remove_tags", [tag], None)
        ctx.write("probate filed", ctx.crm.remove_tags, u, [tag])
        ctx.out.tally["six-month tag taken off: a probate was filed"] += 1
        if ctx.live:
            known[u] = dict(known[u], no_probate_tag=False)
            ledger.set_fact(u, FACT, known[u])

    # ---- calls: only after the hold, and by default only once research found the owner living
    handed = _assign_calls(ctx, o, known, today) if o["calls"] != "off" and o["call_presets"] else 0

    pulled = heirlane.pull_staged(ctx, (KIND_MAIL,), gone) if gone else 0
    for it in pieces[:4]:
        ctx.out.line("%s: %s to %s" % (it["property_street"], "the soft letter" if it["series"] == SOFT
                                       else "monthly piece %d" % it["letter"], it["name"]))
    done_30 = monthly_researched(30)
    if done_30:
        ctx.out.tally["obituary records researched in the last 30 days"] = done_30
    ctx.out.acted = len(pieces) + tagged + handed + pulled
    mix = "%d inside the first month, %d due a soft letter, %d at three months or more" % (
        by_window["hold"], by_window["soft"], by_window["research"])
    bits = []
    if pieces:
        bits.append("%d piece(s), about $%.2f" % (len(pieces), usd))
    if tagged:
        bits.append("%d tagged %s" % (tagged, tag))
    if handed:
        bits.append("%d handed to a caller" % handed)
    if pulled:
        bits.append("%d staged piece(s) pulled" % pulled)
    if not ctx.live:
        if pieces:
            ctx.reserve(usd)
        ctx.out.head = "%d obituary record(s): %s. %s." % (len(rows), mix, ("Would: " + ", ".join(bits)) if bits
                                                          else "Nothing is due today")
        return
    note = ""
    if pieces:
        bid = ledger.batch_create(st.id, KIND_MAIL, pieces, usd)
        path = heirlane.export(bid, KIND_MAIL, pieces)
        ctx.out.batch = bid
        note = " Staged batch %d for a person: python src/agents/cli.py autopilot release %d (file: %s)." % (bid, bid, path)
    ctx.out.head = "%d obituary record(s): %s. %s.%s" % (len(rows), mix, ("Did: " + ", ".join(bits)) if bits
                                                        else "Nothing is due today", note)


def _assign_calls(ctx: Ctx, o: dict, known: dict, today: date) -> int:
    """Hand obituary call records to a caller, once their hold is over and the rule allows it."""
    st = ctx.stage
    people = resolve_callers(ctx)
    if not people:
        ctx.out.problem("no active caller, so no obituary record is handed out")
        return 0
    load = {p["uuid"]: ctx.crm.count({"assigned_to": p["uuid"], "must_not": {"any_property_status": WORKLOAD_EXCLUDE}})
            for p in people}
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    allow = st.get("gate_allow") or []
    handed = 0
    reads = 0
    seen = set()
    for title in [str(t) for t in o["call_presets"]]:
        try:
            must = ctx.crm.preset_must(title)
        except KeyError as exc:
            ctx.out.problem("%s (no call record is read from it)" % exc)
            continue
        must, _ = windows.resolve(must, ctx.now)
        must = dict(must, assigned_to=None)
        for row in ctx.crm.rows(must, cap=int(o["max_scan"])):
            u = row.get("uuid")
            if not u or u in seen:
                continue
            seen.add(u)
            f = known.get(u)
            if f is None:
                continue
            age = _days_since(f["obit_date"], today)
            if age is None or f["probate"] or f["probate_filed"] or f["lead"]:
                continue
            if age < int(o["hold_days"]):
                ctx.out.tally["calls: inside the first month, not handed out"] += 1
                continue
            if o["calls"] == "after_research":
                state = researchqueue.research_state(u, str(o["research_workflow"]))
                if not (state["done"] or f["researched"]):
                    ctx.out.tally["calls: waits for research"] += 1
                    continue
                res = researchqueue.last_result(u, str(o["research_workflow"])) or {}
                status = str(res.get("owner_status") or "")
                if status != "Living":
                    ctx.out.tally["calls: research %s" % ("did not find the owner living" if status else
                                                          "ran on another box, left for a person")] += 1
                    continue
            if handed >= int(o["max_assign"]) or reads >= int(o["max_reads"]):
                ctx.out.tally["calls: left for the next run"] += 1
                continue
            # What the search row already shows is checked before the record is opened.
            v = gate.check_row(row, "assign", cfg=gate_cfg, allow=allow)
            if not v.ok:
                ctx.out.tally["calls held back: %s" % v.reason()] += 1
                continue
            reads += 1
            rec = ctx.crm.record(u)
            if assignee_of(rec):
                continue
            if not ctx.passes(rec, "assign", row=row):
                continue
            who = balanced_pick(people, load)
            ctx.change(u, "assign", assignee_of(rec), who["uuid"])
            ctx.write("obituary caller", ctx.crm.assign, u, who["uuid"])
            load[who["uuid"]] = load.get(who["uuid"], 0) + 1
            handed += 1
            ctx.out.line("%s -> %s (%s)" % (addr(row), who["name"], title))
            if ctx.live:
                ledger.event(ctx.run_id, st.id, u, "assign", {"to": who["name"], "preset": title}, now=ctx.now,
                             tz=ctx.policy.tz)
    return handed


def waiting_line(stage_id: str) -> str:
    """Obituary mail that waits on a person, for the morning readout."""
    waiting = [b for b in ledger.batches(status="staged", stage=stage_id) if b["kind"] == KIND_MAIL]
    return "" if not waiting else "Obituary mail: %s wait on a person." % ", ".join(
        "batch %d (%d pieces, $%.2f)" % (b["id"], b["count"], b["usd"]) for b in waiting)


def monthly_researched(days: int = 30) -> int:
    """How many obituary records research finished in the last `days`: the plan's monthly proof."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT DISTINCT f.record_uuid FROM ap_facts f JOIN tasks t ON t.record_uuid = f.record_uuid "
                         "WHERE f.key=? AND t.workflow='deep_prospect' AND t.status='done' AND t.finished>=?",
                         (FACT, cutoff)).fetchall()
    return len(rows)
