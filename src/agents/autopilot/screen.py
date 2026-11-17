"""The death screen (build plan 0.21): check for a death before the first touch.

Between a fifth and a third of distressed owners have died, and without this step text 1 goes out
the morning a record lands, addressed to the dead owner by first name. The screen runs ahead of
the assign and text stages on every record it has not seen, and looks in the places a death shows
up before any research has been paid for:

  1. the record itself (the owner flag, a deceased tag, the Obituary list): nothing to do, the
     gate already holds it;
  2. the words of the notice kept in the record's notes ("estate of", "heirs of", "deceased");
  3. the county obituary index, where one exists. Knox County's library indexes the Knoxville News
     Sentinel since 1991 and answers a name with every person of that name and their age. Blount
     has no such index, so a Blount record relies on the notice and, later, on the trace.

A hit is NOT a verdict. The index lists strangers who share a name, and an obituary is often a
spouse's. So a hit only marks the record "Verify Deceased" with what was found; the gate then holds
it out of every cold channel and the trace stage hands it to research, which is the step that
decides who died.

A probate record is skipped: its owner of record is the personal representative the court named.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from . import gate, ledger
from .stages import Ctx, addr, stage_rows, tag_titles

log = logging.getLogger(__name__)

DEATH_WORDS = re.compile(r"\b(deceased|estate of|heirs? of|heirs[- ]at[- ]law|unknown heirs|decedent|"
                         r"surviving spouse|died on|date of death)\b", re.I)
ROW = re.compile(r"^(?P<name>[^|]+)\|\s*age\s*(?P<age>[^|]*)\|(?P<mid>[^|]*)\|\s*obituary date\s*(?P<date>.*)$", re.I)

# Tests swap this for a fake. In production it is the lookup the research engine already uses.
lookup_index = None
INDEX_GAP_S = 0.6
# In plan mode a hit is not tagged, so nothing on the record remembers it. This fact does, for a
# day, so a plan run every 30 minutes does not ask the library about the same name 25 times.
PLAN_HIT = "death_screen_plan_hit"


def _lookup(last: str, first: str) -> str:
    if lookup_index is not None:
        return lookup_index(last, first)
    try:
        from ..research import open_research
    except Exception as exc:  # noqa: BLE001
        return "index lookup failed: %s" % str(exc)[:100]
    time.sleep(INDEX_GAP_S)        # a free public index: one polite request at a time
    return open_research.lookup_obituary_index(last, first)


def _year(text: str) -> int:
    m = re.search(r"(19|20)\d\d", text or "")
    return int(m.group(0)) if m else 0


def index_matches(text: str, first: str, last: str, now: datetime, years: int = 3, owner_age=None) -> list[dict]:
    """The index rows that could be this owner: the same surname, the same first name (an initial is
    not a match), an obituary in the last `years`, and, when the record carries the owner's age, an
    age within five years of it."""
    out = []
    f, l = first.strip().lower(), last.strip().lower()
    if len(f) < 2 or not l:
        return out
    for line in (text or "").splitlines():
        m = ROW.match(line.strip())
        if not m:
            continue
        name = m.group("name").strip()
        if "," not in name:
            continue
        sur, given = [x.strip().lower() for x in name.split(",", 1)]
        if sur.split()[-1:] != [l] and sur != l:
            continue
        g = re.split(r"[\s.]+", given)
        if not g or g[0] != f:
            continue
        yr = _year(m.group("date"))
        if not yr or yr < now.year - years:
            continue
        age = re.sub(r"\D", "", m.group("age") or "")
        if owner_age and age and abs(int(age) - int(owner_age)) > 5:
            continue
        out.append({"name": name, "age": age, "date": m.group("date").strip()})
    return out


def run_screen(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    tag = str(st.get("tag") or "Verify Deceased")
    years = int(st.get("years", 3))
    counties = {str(c).lower() for c in (st.get("index_counties") or ["knox"])}
    on_hit = st.get("on_hit", "tag")
    max_n = int(st.get("max_records", 60))
    recheck = timedelta(days=float(st.get("recheck_days", 90)))
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    done = 0
    index_down = False
    for _, row in rows:
        u = row["uuid"]
        if ledger.fact(u, "death_screen", max_age=recheck) is not None:
            continue
        if not ctx.live:
            seen = ledger.fact(u, PLAN_HIT, max_age=timedelta(hours=24))
            if seen is not None:
                ctx.out.tally["possible death: %s" % ("notice text" if seen.get("hit") == "notice" else "obituary index")] += 1
                continue
        if done >= max_n:
            ctx.out.tally["left for the next run"] += 1
            continue
        if not gate.check_row(row, "assign", cfg=cfg).ok:
            continue
        rec = ctx.crm.record(u)
        if not rec.get("uuid"):
            continue
        done += 1
        ow = rec.get("owner") or {}
        tags = {t.lower() for t in tag_titles(rec)}
        lists = {str(x).lower() for x in rec.get("lists") or [] if isinstance(x, str)}
        result = {"at": ctx.now.isoformat(timespec="seconds"), "hit": "", "detail": ""}
        if (tags | lists) & {m.lower() for m in cfg.probate_marks}:
            ctx.out.tally["probate: the owner of record is the court's representative"] += 1
        elif gate.deceased_signals(rec, cfg):
            ctx.out.tally["already marked on the record"] += 1
            result["hit"] = "record"
        elif ow.get("company") and not (ow.get("first_name") and ow.get("last_name")):
            ctx.out.tally["a company owns it: no person to screen"] += 1
        else:
            first = " ".join(str(ow.get("first_name") or "").split()).split(" ")[0]
            last = " ".join(str(ow.get("last_name") or "").split())
            notes = str(rec.get("notes") or "")
            m = DEATH_WORDS.search(notes)
            county = str((rec.get("address") or {}).get("county") or "").lower()
            if m:
                result["hit"], result["detail"] = "notice", "the notice text says '%s'" % m.group(0).lower()
            elif first and last and any(c in county for c in counties):
                # One failed lookup means the index is down or blocks this box: do not spend forty
                # seconds on every remaining record finding that out again.
                text = "index lookup failed: skipped after an earlier failure" if index_down else _lookup(last, first)
                if text.startswith("index lookup failed"):
                    if not index_down:
                        ctx.out.problem("the obituary index did not answer (%s); those records were not screened and "
                                        "will be tried again" % text[20:80].strip())
                    index_down = True
                    ctx.out.tally["not screened: the index did not answer"] += 1
                    done -= 1
                    continue
                hits = index_matches(text, first, last, ctx.now, years, ow.get("age"))
                if hits:
                    h = hits[0]
                    result["hit"] = "index"
                    result["detail"] = "the county obituary index lists a person of this name (%s%s)" % (
                        h["date"], ", age %s" % h["age"] if h["age"] else "")
                else:
                    ctx.out.tally["screened, nothing found"] += 1
            elif first and last:
                ctx.out.tally["no obituary index for the county (the notice was checked)"] += 1
            else:
                ctx.out.tally["no first and last name to screen"] += 1
        if result["hit"] in ("notice", "index"):
            ctx.out.tally["possible death: %s" % ("notice text" if result["hit"] == "notice" else "obituary index")] += 1
            ctx.out.line("%s: %s" % (addr(rec), result["detail"]))
            if on_hit == "tag" and tag.lower() not in tags:
                ctx.change(u, "add_tags", None, [tag])
                ctx.write("verify deceased", ctx.crm.add_tags, u, [tag], rec)
                ctx.write("verify deceased note", ctx.crm.message, u,
                          "Possible death: %s. Not confirmed. Hold all outreach until research says who died." % result["detail"])
                if ctx.live:
                    ledger.event(ctx.run_id, st.id, u, "screen", {"hit": result["hit"]}, now=ctx.now, tz=ctx.policy.tz)
        # A screen result is knowledge about the record, not an action on it: it is remembered in
        # plan mode too, so the same record is not looked up in the index on every run. A hit is NOT
        # remembered in plan mode, because the tag that makes it stick was not written.
        if ctx.live or not result["hit"] or result["hit"] == "record":
            ledger.set_fact(u, "death_screen", result)
        else:
            ledger.set_fact(u, PLAN_HIT, result)
    hits = sum(n for k, n in ctx.out.tally.items() if k.startswith("possible death"))
    ctx.out.acted = hits
    verb = "marked" if (ctx.live and on_hit == "tag") else "would mark"
    if not done:
        ctx.out.head = "No new record to screen."
    else:
        ctx.out.head = "Screened %d new record(s) for a death: %d possible (%s %s for research), %d already marked." % (
            done, hits, verb, tag, ctx.out.tally.get("already marked on the record", 0))
