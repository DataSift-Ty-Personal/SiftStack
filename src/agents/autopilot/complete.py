"""Complete the incomplete records (Ty, 2026-11-15): "completing all the incomplete records in the
account that are individuals and not entities, because I think those are going to be big
opportunities to add to the pipeline. Having a process to resolve those."

The Challenge teaches it as a person's job and says why it pays: "incomplete records has a ton of
deals in them" (C2.7). C2.7 holds an incomplete record out of skip tracing and mail until it is
fixed; C2.27 fixes them 100 at a time (find the owner's name, catch the ones that sold, skip trace
again; about an hour per 100 by hand, and 13 to 14 in every 100 had sold); C11.28 rewrites a life
estate owner to the named person; C14.20 tags each completed record "Incomplete Data" so its later
lead or deal can still be traced back to the cleanup.

What "incomplete" means, measured on prod-account 2026-11-15 (11,533 of 64,874 records): DataSift calls a
record incomplete when its owner is not a person with a first and a last name. 9,342 were
companies and are left alone (people only). The people were mostly a filing problem, not missing
data: 1,733 carried the person's name in the COMPANY field, county style ("Tolliver Milton M & Esther
Maria"); 370 had both names but a bare initial for a first name (a record reading "J" Ransom is J
Marcus Ransom); 76 had only a last name. None of 80 read in full was missing an address.

Proven on one record the same day: the name moved into first and last, the company cleared and the
owner's type set to person turn the record clean at once (the search tabs follow within about a
minute). PATCH /api/internal/owner/{uuid}/, all four fields in the published UpdateOwner schema.

Each run:
  1. the incomplete tab is read in created-date windows (the search refuses past 10,000 rows), and
     each owner is classified from the row alone: entity, trust, estate, junk, life estate, person
     in the company field, initials only, last name only, or both names;
  2. a person's name is split using how often each word is a first name and how often a last name
     across the account's own clean records (county style puts the surname first, "Drake Michelle
     C"; natural order puts it last), and an order it cannot tell is held, never guessed;
  3. the full record is read and gated: a finished status, the sold tag, a sale recorded after the
     record was added, a deceased or researched owner, and the suppression gate's own rules;
  4. a first name that is only initials, or missing, is looked up in SiftMap (the record is already
     in the account), which also says whether the owner of record is still the same person;
  5. live: the owner is renamed through the documented route, the record tagged Incomplete Data
     (Life Estate as well where it is one), a co-owner put in a note and every old value kept for
     undo; and once 20 or more are done they go through DataSift's own free skip trace, scoped to
     exactly those records.
Anything it cannot settle goes to for_a_person.csv with the reason.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, store
from . import ledger

log = logging.getLogger(__name__)

TAG = "Incomplete Data"
LIFE_TAG = "Life Estate"
VERDICT = "complete_verdict"
QUEUE_KEY = "autopilot:complete:trace_queue"
MISS_KEY = "autopilot:complete:trace_misses"   # uuid -> nights its trace batch found it off the clean tab
MISS_LIMIT = 3
STILL_INCOMPLETE = "still incomplete after the name was completed (an address part is missing)"
WALL = 9900
DEFAULTS = {"per_run": 300, "read_cap": 400, "siftmap_cap": 150, "trusts": "hold", "life_estates": "complete",
            "trace": True, "min_batch": 20, "trace_cap": 1000, "names_rows": 30000, "names_max_age_days": 30,
            "recheck_days": 30, "min_first_count": 2, "margin": 1.0}
FINISHED = {"sold", "closed", "close_out", "lost_deal", "listed", "under_contract", "dnc", "opt_out", "dead lead",
            "not_interested"}
HOLD_TAGS = {"recently sold", "do not market", "litigator", "dnc", "verify deceased", "deep prospected", "researched",
             "wrong house", "estate sold"}

# Words that make an owner a company. deep_prospect_batch.ENTITY_RX is the shared core; these are the
# ones the live sample showed it missing (a housing agency, a swim club, a church, an equipment dealer,
# a rental company, "... Co").
ORG_RX = re.compile(
    r"\b(agency|club|methodist|baptist|presbyterian|lutheran|catholic|episcopal|ministry|temple|chapel|congregation|"
    r"assn|assoc|authority|housing|development|sales|rentals?|property|investment|homes|realty|realtors?|"
    r"partnership|corporation|incorporated|ltd|plc|pllc|credit union|county|city of|state of|town of|schools?|"
    r"board|district|utilit(?:y|ies)|cemetery|lodge|hoa|condominiums?|condo|owners|fund|capital|enterprises?|"
    r"services?|construction|builders|building|farms|ranch|center|hospital|university|college|academy|federal|"
    r"national|mortgage|financial|finance|equity|ventures?|solutions|management|mgmt|systems|industries|"
    r"international|estates|apartments|apts|villas|bros|brothers|sons|l l c)\b|(?:^|\s)co\.?$", re.I)
TRUST_RX = re.compile(r"\b(trust|trus|tru|trs|trustees?|ttees?|revocable|irrevocable|irrev|living|livin|fbo|tr)\b", re.I)
LIFE_RX = re.compile(r"\blife\s+est(?:ate)?\b|\bl/e\b", re.I)
ESTATE_RX = re.compile(r"\b(estate of|est of|heirs?|deceased|decd|unknown heirs)\b|(?<!life )\b(?:estate|est)\b", re.I)
JUNK_RX = re.compile(r"not available|data source|unknown owner|current owner|occupant|record owner|owner of record|"
                     r"^\W*(?:n/?a|none|unknown)\W*$", re.I)
ROLE_RX = re.compile(r"\b(life\s+estate|life\s+est|l/e|etal|et\s+al|et\s+ux|et\s+vir|jtrs|jt\s+ten|jtwros|tic|h/w|"
                     r"his\s+wife|her\s+husband|rem|remaindermen|remainderman)\b", re.I)
SPLIT_RX = re.compile(r"\s*(?:&|\band\b|;|/|\+)\s*", re.I)
WORD_RX = re.compile(r"[A-Za-z][A-Za-z'\-]*")
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def _entity_rx():
    from deep_prospect_batch import ENTITY_RX
    return ENTITY_RX


# ------------------------------------------------------------------------- reading an owner
def classify(owner: dict) -> str:
    """What an incomplete record's owner is, from the search row alone."""
    first, last, company = ((owner.get(k) or "").strip() for k in ("first_name", "last_name", "company"))
    name = " ".join(p for p in (first, last) if p)
    text = company or name
    if not text:
        return "none"
    if JUNK_RX.search(text):
        return "junk"
    if LIFE_RX.search(text):
        return "life_estate"
    if TRUST_RX.search(text):
        # A corporate trust (a custodian account "Equity Trust Company FBO ...", a bank's land trust) is
        # a company; only a trust named for people can be rewritten to its person (C11.28).
        rest = TRUST_RX.sub(" ", text)
        if re.search(r"\bfbo\b", text, re.I) or _entity_rx().search(rest) or ORG_RX.search(rest):
            return "entity"
        return "trust"
    if _entity_rx().search(text) or ORG_RX.search(text):
        return "entity"
    if ESTATE_RX.search(text):
        return "estate"
    if company and not name:
        return "person_company"
    if first and last:
        return "initials" if len(re.sub(r"[^A-Za-z]", "", first)) < 2 else "first_last"
    if last:
        return "last_only"
    return "first_only"


def _cap(word: str) -> str:
    return "-".join(p[:1].upper() + p[1:].lower() for p in (word or "").split("-"))


def _words(text: str) -> list[str]:
    return [w for w in WORD_RX.findall(text or "") if w.lower() not in SUFFIX]


def pick_first(given: list[str]) -> str:
    """The name a person goes by: the first given word longer than an initial ("J Marcus" -> Marcus)."""
    for w in given:
        if len(w) >= 2:
            return w
    return ""


def _score(counts: dict, side: str, word: str) -> float:
    return math.log1p((counts.get(side) or {}).get((word or "").lower(), 0))


def name_order(toks: list[str], counts: dict, margin: float = 1.0) -> str:
    """"county" (surname first, "Drake Michelle C") or "natural" (surname last), or "" when the
    counts cannot tell them apart by a clear margin. A name ending in an initial is county style."""
    if len(toks) < 2:
        return ""
    county = _score(counts, "last", toks[0]) + _score(counts, "first", pick_first(toks[1:]))
    natural = _score(counts, "first", pick_first(toks[:-1])) + _score(counts, "last", toks[-1])
    if len(toks[-1]) == 1:
        county += 3.0
    if county - natural >= margin:
        return "county"
    if natural - county >= margin:
        return "natural"
    return ""


def parse(raw: str, counts: dict, margin: float = 1.0, min_first: int = 2, trust: bool = False) -> dict:
    """A person's name out of one owner string: {"first", "last", "co_owner", "order"} or {"hold": why}.
    trust=True first takes the trust's own words out ("Hesketh John E & Mary S Rev Tr")."""
    s = ROLE_RX.sub(" ", raw or "")
    if trust:
        s = re.sub(r"\b(rev|family|the)\b", " ", TRUST_RX.sub(" ", s), flags=re.I)
    s = re.sub(r"[,.]", " ", s)
    parts = [p for p in SPLIT_RX.split(s) if p and p.strip()]
    if not parts:
        return {"hold": "no name to work from"}
    toks = _words(parts[0])
    if len(toks) == 1 and len(parts) > 1 and len(_words(parts[1])) >= 2:
        # "John & Mary Smith": the surname comes once, at the very end
        rest = _words(parts[1])
        first, last = toks[0], rest[-1]
        co = "%s %s" % (_cap(pick_first(rest[:-1]) or rest[0]), _cap(last))
        order = "natural"
    else:
        if len(toks) < 2:
            return {"hold": "only one word in the name"}
        order = name_order(toks, counts, margin)
        if not order:
            return {"hold": "cannot tell which word is the surname"}
        if order == "county":
            last, given = toks[0], toks[1:]
            again = [i for i, w in enumerate(given) if w.lower() == last.lower()]
            co_given = given[again[0] + 1:] if again else []      # "Varner Mary Ellery Varner Phillip Edward"
            given = given[:again[0]] if again else given
        else:
            last, given, co_given = toks[-1], toks[:-1], []
        first = pick_first(given)
        co = ""
        if co_given:
            co = "%s %s" % (_cap(pick_first(co_given)), _cap(last)) if pick_first(co_given) else ""
        elif len(parts) > 1:
            ct = _words(parts[1])
            if order == "county" and ct and ct[0].lower() == last.lower():
                ct = ct[1:]
            cf = pick_first(ct[:-1] if order == "natural" and len(ct) > 1 else ct)
            if cf:
                co = "%s %s" % (_cap(cf), _cap(ct[-1] if order == "natural" and len(ct) > 1 else last))
    if not first:
        return {"hold": "initials only"}
    if (counts.get("first") or {}).get(first.lower(), 0) < min_first:
        return {"hold": "the first name is not one the account has seen (%s order)" % order}
    return {"first": _cap(first), "last": _cap(last), "co_owner": co, "order": order}


def from_siftmap(owner_name: str, last: str) -> str:
    """A usable first name from SiftMap's owner name ("J Marcus Ransom"), only when its surname is the
    record's own; "" when SiftMap names someone else or has initials too."""
    toks = _words(owner_name)
    if len(toks) < 2 or toks[-1].lower() != (last or "").strip(" ,.").lower():
        return ""
    return pick_first(toks[:-1])


# ------------------------------------------------------------------------- the data it reads
def _dir() -> Path:
    d = Path(config.DATA_DIR) / "autopilot" / "complete"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _windows(crm, must: dict, lo: datetime, hi: datetime, cap: int, depth: int = 0) -> list:
    """Every row matching `must`, read in created windows small enough for the 10,000-row wall."""
    span = [lo.isoformat(timespec="seconds"), hi.isoformat(timespec="seconds")]
    n = crm.count(dict(must, created=span))
    if not n:
        return []
    if n <= WALL or depth >= 16 or hi - lo <= timedelta(minutes=10):
        return crm.rows(dict(must, created=span), cap=min(cap, WALL))
    mid = lo + (hi - lo) / 2
    left = _windows(crm, must, lo, mid, cap, depth + 1)
    if len(left) >= cap:
        return left[:cap]
    return left + _windows(crm, must, mid, hi, cap - len(left), depth + 1)


def _row(r: dict) -> dict:
    o = r.get("owner") or {}
    return {"uuid": r.get("uuid"), "owner": {k: o.get(k) for k in ("uuid", "first_name", "last_name", "company")},
            "status": r.get("status"), "list_count": r.get("list_count") or 0, "phones": bool(r.get("has_phones"))}


def pool(ctx) -> list[dict]:
    """The incomplete tab, read once a day and kept on disk (the rows change as records complete)."""
    day = ctx.now.date().isoformat()
    f = _dir() / "pool.json"
    try:
        kept = json.loads(f.read_text(encoding="utf-8"))
        if kept.get("day") == day:
            return kept["rows"]
    except (OSError, ValueError):
        pass
    lo = datetime(2000, 1, 1, tzinfo=timezone.utc)
    hi = ctx.now.astimezone(timezone.utc) + timedelta(days=1)
    rows = [_row(r) for r in _windows(ctx.crm, {"property_type": "incomplete"}, lo, hi, 10 ** 6)]
    f.write_text(json.dumps({"day": day, "rows": rows}), encoding="utf-8")
    return rows


def name_counts(ctx, st: dict) -> dict:
    """How often each word is a first name and a last name among the account's clean records: the
    split's only evidence. Built from search rows (no record reads), kept for a month."""
    f = _dir() / "names.json"
    try:
        kept = json.loads(f.read_text(encoding="utf-8"))
        built = datetime.fromisoformat(kept["built"])
        if ctx.now - built < timedelta(days=int(st["names_max_age_days"])):
            return kept
    except (OSError, ValueError, KeyError, TypeError):
        pass
    lo = datetime(2000, 1, 1, tzinfo=timezone.utc)
    hi = ctx.now.astimezone(timezone.utc) + timedelta(days=1)
    first, last = Counter(), Counter()
    for r in _windows(ctx.crm, {"property_type": "clean"}, lo, hi, int(st["names_rows"])):
        o = r.get("owner") or {}
        f_, l_ = (o.get("first_name") or "").strip(), (o.get("last_name") or "").strip()
        if len(f_) >= 2 and l_ and not (o.get("company") or "").strip():
            first[f_.split()[0].lower()] += 1
            last[l_.split()[-1].strip(",.").lower()] += 1
    kept = {"built": ctx.now.isoformat(timespec="seconds"), "first": dict(first), "last": dict(last)}
    f.write_text(json.dumps(kept), encoding="utf-8")
    return kept


# ------------------------------------------------------------------------- the record gate
def _tags(rec: dict) -> set[str]:
    return {str((t.get("title") if isinstance(t, dict) else t) or "").strip().lower() for t in rec.get("tags") or []}


def _day(v) -> str:
    return str(v or "")[:10]


SOLD_AFTER = "a sale recorded after the record was added (the owner may have sold)"
BASE_DAY = "2000-01-01"


def _first_made(ctx, cache: dict) -> str:
    """The day the oldest incomplete record was added, found by halving the created window (about 14
    counts, once a run). A sale before that day came before every incomplete record was added, so it
    needs no count of its own."""
    if "day" not in cache:
        lo = datetime.strptime(BASE_DAY, "%Y-%m-%d").date()
        hi = ctx.now.date() + timedelta(days=1)
        if not ctx.crm.count({"property_type": "incomplete", "created": [BASE_DAY, hi.isoformat()]}):
            cache["day"] = hi.isoformat()
            return cache["day"]
        while (hi - lo).days > 1:                      # some record was added before hi, none before lo
            mid = lo + timedelta(days=(hi - lo).days // 2)
            if ctx.crm.count({"property_type": "incomplete", "created": [BASE_DAY, mid.isoformat()]}):
                hi = mid
            else:
                lo = mid
        cache["day"] = lo.isoformat()
    return cache["day"]


def added_before(ctx, uuid: str, day: str) -> bool:
    """True when the record was added before `day`. A full record carries no created date (read on prod-account
    2026-11-15: only last_sold and owned_since), but the records search filters one record id by a
    created window: the end of a window is the start of that day, so a record added on the sale day
    itself does not count."""
    return bool(ctx.crm.count({"properties": [uuid], "created": [BASE_DAY, day]}))


def record_hold(rec: dict) -> str:
    """Why a full record must not be completed now, or "". A sale after the record was added is judged
    here only when the record carries a created date, which DataSift does not send today: the run asks
    the search instead (added_before)."""
    if (rec.get("status") or "").strip().lower() in FINISHED:
        return "a finished status"
    hit = _tags(rec) & HOLD_TAGS
    if hit:
        return "tagged %s" % sorted(hit)[0]
    owner = rec.get("owner") or {}
    if owner.get("deceased"):
        return "the owner is marked deceased: research first"
    if rec.get("type") == "clean":
        return "already complete"
    sold, made = _day(rec.get("last_sold")), _day(rec.get("created"))
    if sold and made and sold > made:
        return SOLD_AFTER
    return ""


def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


# ------------------------------------------------------------------------- the run
def _siftmap(ctx):
    sm = (ctx.sources or {}).get("siftmap")
    if sm is None:
        from siftmap_standalone import SiftMapClient
        sm = SiftMapClient()
        ctx.sources["siftmap"] = sm
    return sm


def _hold(holds: list, row: dict, why: str) -> None:
    o = row.get("owner") or {}
    holds.append({"record": row.get("uuid"), "owner": " ".join(p for p in (o.get("first_name"), o.get("last_name"), o.get("company")) if p),
                  "why": why})


def run_complete(ctx) -> None:
    st = settings(ctx.stage)
    rows = pool(ctx)
    kinds = Counter(classify(r["owner"]) for r in rows)
    people = {"person_company", "first_last", "initials", "last_only", "life_estate"}
    if st["trusts"] == "complete":
        people.add("trust")
    todo = [r for r in rows if classify(r["owner"]) in people]
    todo.sort(key=lambda r: -(r.get("list_count") or 0))      # records on more lists first
    counts = name_counts(ctx, st)
    ctx.out.found = len(rows)
    recheck = timedelta(days=int(st["recheck_days"]))
    reads = sm_reads = done = 0
    holds, why_held, made = [], Counter(), {}
    for row in todo:
        if done >= int(st["per_run"]) or reads >= int(st["read_cap"]):
            break
        u, o = row["uuid"], row["owner"]
        seen = ledger.fact(u, VERDICT, max_age=recheck)
        if seen:
            ctx.out.tally["decided on an earlier night"] += 1
            continue
        kind = classify(o)
        first = last = co = ""
        if kind in ("person_company", "life_estate", "trust"):
            got = parse(o.get("company") or " ".join(p for p in (o.get("first_name"), o.get("last_name")) if p),
                        counts, float(st["margin"]), int(st["min_first_count"]), trust=(kind == "trust"))
            if got.get("hold"):
                why_held[got["hold"]] += 1
                _hold(holds, row, got["hold"])
                ledger.set_fact(u, VERDICT, {"held": got["hold"]})
                continue
            first, last, co = got["first"], got["last"], got.get("co_owner") or ""
        else:
            first = (o.get("first_name") or "").strip()
            last = (o.get("last_name") or "").strip(" ,.")
        reads += 1
        try:
            rec = ctx.crm.record(u)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", 0) == 404 or "HTTP 404" in str(exc):
                # Deleted or merged away in DataSift; the search index still lists it for a while.
                # Remembered like a verdict, so it is not read and reported every night (two such
                # records posted "could not read" to the issues channel on 2026-11-18).
                ctx.out.tally["gone from DataSift (the search still lists it)"] += 1
                ledger.set_fact(u, VERDICT, {"gone": True})
                continue
            ctx.out.problem("could not read an incomplete record (%s)" % str(exc)[:100])
            continue
        hold = record_hold(rec)
        if hold:
            why_held[hold] += 1
            _hold(holds, row, hold)
            if hold != "already complete":
                ledger.set_fact(u, VERDICT, {"held": hold})
            continue
        if not ctx.passes(rec, "trace"):
            continue
        sold = _day(rec.get("last_sold"))
        if sold and not rec.get("created") and sold >= _first_made(ctx, made):
            ctx.out.tally["sale dates checked against the day the record was added"] += 1
            try:
                after_add = added_before(ctx, u, sold)
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("could not check when an incomplete record was added (%s)" % str(exc)[:100])
                continue
            if after_add:
                why_held[SOLD_AFTER] += 1
                _hold(holds, row, SOLD_AFTER)
                ledger.set_fact(u, VERDICT, {"held": SOLD_AFTER})
                continue
        if kind in ("initials", "last_only", "first_last"):
            if sm_reads >= int(st["siftmap_cap"]):
                ctx.out.tally["waiting for a SiftMap read (cap, next run)"] += 1
                continue
            sm_reads += 1
            try:
                detail = _siftmap(ctx).get_detail(rec.get("dataflik_id")) if rec.get("dataflik_id") else {}
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("SiftMap did not answer for an incomplete record (%s)" % str(exc)[:80])
                continue
            sm_name = ((detail or {}).get("owner_info") or {}).get("owner_name") or ""
            sm_toks = _words(sm_name)
            same_surname = bool(sm_toks) and sm_toks[-1].lower() == last.lower()
            if kind == "first_last":
                # Both names are there; SiftMap must name the same person, or the house may have changed hands.
                better = first if same_surname and first.lower() in {w.lower() for w in sm_toks[:-1]} else ""
            else:
                better = from_siftmap(sm_name, last)
                if kind == "initials" and better and sm_toks[0][:1].lower() != first[:1].lower():
                    better = ""                       # "J" on the record, "K Marcus Ransom" on SiftMap: not the same person
            if not better:
                why = ("SiftMap names someone else as the owner" if sm_toks and not same_surname
                       else "SiftMap has a different first name" if kind == "first_last" and sm_toks
                       else "initials only")
                why_held[why] += 1
                _hold(holds, row, why)
                ledger.set_fact(u, VERDICT, {"held": why})
                continue
            first = better
            if (counts.get("first") or {}).get(first.lower(), 0) < int(st["min_first_count"]):
                why = "the first name is not one the account has seen"
                why_held[why] += 1
                _hold(holds, row, why)
                ledger.set_fact(u, VERDICT, {"held": why})
                continue
        first, last = _cap(first), _cap(last)
        owner = rec.get("owner") or {}
        before = {"owner_uuid": owner.get("uuid"), "first_name": owner.get("first_name"),
                  "last_name": owner.get("last_name"), "company": owner.get("company"),
                  "type_override": owner.get("type_override")}
        after = {"first_name": first, "last_name": last, "company": None, "type_override": "person"}
        tags = [TAG] + ([LIFE_TAG] if kind == "life_estate" else [])
        ctx.change(u, "owner", before, after)
        ctx.write("owner name", ctx.crm.update_owner, owner.get("uuid"), after)
        ctx.change(u, "add_tags", [], tags)
        ctx.write("tag", ctx.crm.add_tags, u, tags, rec)
        if co:
            ctx.write("note", ctx.crm.message, u, "Owner name completed from the record (%s order). Co-owner: %s." % (
                "county" if (kind != "first_last") else "record", co))
        done += 1
        if ctx.live:
            ledger.set_fact(u, VERDICT, {"completed": ctx.now.date().isoformat()})
            ledger.event(ctx.run_id, ctx.stage.id, u, "complete", {"kind": kind}, now=ctx.now, tz=ctx.policy.tz)
            _queue(u)
    ctx.out.acted = done
    _write_holds(holds)
    traced = _trace(ctx, st)
    left = max(0, len(todo) - done - sum(why_held.values()) - ctx.out.tally.get("decided on an earlier night", 0))
    ctx.out.head = ("%s %d of %d people-owned incomplete records%s; %d held for a person; %d companies and %d trusts left alone." % (
        "Completed" if ctx.live else "Would complete", done, len(todo),
        (" (%d more on later nights)" % left) if left else "", sum(why_held.values()),
        kinds.get("entity", 0), kinds.get("trust", 0) if st["trusts"] != "complete" else 0))
    ctx.out.line("Incomplete records: %d. People: %d in the company field, %d with initials, %d with a last name only, "
                 "%d with both names, %d life estates." % (len(rows), kinds.get("person_company", 0), kinds.get("initials", 0),
                                                          kinds.get("last_only", 0), kinds.get("first_last", 0),
                                                          kinds.get("life_estate", 0)))
    if st["trusts"] != "complete" and kinds.get("trust"):
        ctx.out.line("%d trusts held: the course rewrites a trust named for a person to that person (C11.28); "
                     "set trusts: complete to include them." % kinds["trust"])
    for why, n in why_held.most_common(5):
        ctx.out.line("Held, %s: %d" % (why, n))
    if kinds.get("estate"):
        ctx.out.line("%d owners are an estate (the owner died): left for research." % kinds["estate"])
    if traced:
        ctx.out.line(traced)


def _queue(u: str) -> None:
    try:
        q = json.loads(store.kv_get(QUEUE_KEY) or "[]")
    except ValueError:
        q = []
    if u not in q:
        q.append(u)
    store.kv_set(QUEUE_KEY, json.dumps(q))


def _trace(ctx, st: dict) -> str:
    """DataSift's own skip trace on the records this stage completed, scoped to exactly them, dry run
    first (the native trace stage's own contract), once 20 or more are waiting."""
    if not st.get("trace"):
        return ""
    try:
        q = json.loads(store.kv_get(QUEUE_KEY) or "[]")
    except ValueError:
        q = []
    if len(q) < int(st["min_batch"]):
        return ("%d completed record(s) wait for a skip trace (it runs at %d)." % (len(q), int(st["min_batch"]))) if q else ""
    batch = q[:int(st["trace_cap"])]
    if not ctx.live:
        return "Would skip trace %d completed record(s), scoped to exactly those." % len(batch)
    # The trace scope is the clean tab. A record can stay incomplete after its name is completed (a
    # missing mailing street or ZIP, measured 10 of 100 on 2026-11-20) or reach the clean tab a minute
    # late, and one such record made the dry run disagree and stopped the whole batch every night. So
    # only the records the search counts clean go; the rest wait, and after MISS_LIMIT nights go to a person.
    clean = {r.get("uuid") for r in ctx.crm.rows({"properties": batch, "property_type": "clean"}, cap=len(batch))}
    ready = [u for u in batch if u in clean]
    try:
        misses = json.loads(store.kv_get(MISS_KEY) or "{}")
    except ValueError:
        misses = {}
    dropped, holds = set(), []
    for u in batch:
        if u in clean:
            misses.pop(u, None)
            continue
        misses[u] = int(misses.get(u, 0)) + 1
        if misses[u] >= MISS_LIMIT:
            dropped.add(u)
            misses.pop(u, None)
            holds.append({"record": u, "owner": "", "why": STILL_INCOMPLETE})
    store.kv_set(MISS_KEY, json.dumps(misses))
    _write_holds(holds)
    rest = [u for u in q if u not in dropped]
    store.kv_set(QUEUE_KEY, json.dumps(rest))
    note = ""
    if len(batch) - len(ready):
        note = " %d completed record(s) are not on the clean tab yet, so they wait%s." % (
            len(batch) - len(ready), (" (%d still incomplete after %d nights went to a person)" % (len(dropped), MISS_LIMIT))
            if dropped else "")
    if len(ready) < int(st["min_batch"]):
        return ("%d completed record(s) on the clean tab wait for a skip trace (it runs at %d).%s" % (
            len(ready), int(st["min_batch"]), note)) if (ready or note) else ""
    est = ctx.crm.skiptrace_scoped(ready, dry_run=True)
    n = est.get("number_of_records", est.get("count", est.get("expected_count"))) if isinstance(est, dict) else None
    if n != len(ready):
        ctx.out.problem("DataSift's dry run covers %r records, not the %d completed ones, so nothing was traced" % (n, len(ready)))
        return note.strip()
    ctx.write("skip trace", ctx.crm.skiptrace_scoped, ready, False, [TAG])
    sent = set(ready)
    store.kv_set(QUEUE_KEY, json.dumps([u for u in rest if u not in sent]))
    for u in ready:
        ledger.event(ctx.run_id, ctx.stage.id, u, "native_trace", {"scope": "completed"}, now=ctx.now, tz=ctx.policy.tz)
    return "Asked DataSift to skip trace %d completed record(s), free on this plan.%s" % (len(ready), note)


def _write_holds(holds: list) -> None:
    if not holds:
        return
    f = _dir() / "for_a_person.csv"
    seen = set()
    old = []
    if f.exists():
        with f.open(encoding="utf-8", newline="") as fh:
            old = [r for r in csv.DictReader(fh)]
    out = []
    for r in holds + old:
        if r.get("record") in seen:
            continue
        seen.add(r.get("record"))
        out.append(r)
    with f.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["record", "owner", "why"])
        w.writeheader()
        w.writerows(out)
