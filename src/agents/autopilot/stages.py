"""The stage handlers: assign, native_trace, retrace, score, auction, reactivate, digest (mail lives in
mail.py). Each fills a Summary. Every write goes through Ctx.write, which in plan mode records what
it would have done and touches nothing, so a plan run cannot write by accident.

Rules that cut across stages:
  * Dead statuses are never touched (sold, closed, under contract, not interested, DNC ...). A
    reactivation stage is the only one that reads not-interested records, and it only hands them
    back to a caller.
  * A source counts as a hit only when it found a CALLABLE number (Dial First or Dial Second, not
    a litigator; `callable_tiers` widens it). Lower tiers are still written, tagged, so the caller
    can see them, unless the stage sets `write_tiers`, and the record keeps moving down the waterfall.
  * Money: every paid call checks the stage's daily cap, the lane's own daily cap when the
    playbook gives it one, the autopilot's daily cap, the brain's daily cap and the per-record
    lifetime cap, in that order, before it is made.

Rules added with the second version of the waterfall and the lanes below first to market (build
plan 1.7, 1.13 to 1.15, 1.17, 4.1). Every one of them is switched on by a setting in the stage's
playbook block; a stage whose block does not name the setting behaves exactly as it did before:
  * tranche               a lane worked in fixed sets: the stage acts only on records in the lane's
                          current tranche, and the next is drawn when this one has been worked
  * every_record_sources  that many sources run on EVERY record, not only the ones with no number
  * agreement_tag         a number two sources agree on is marked, on the phone
  * keep_numbers          at most this many numbers land per person from one trace
  * write_tiers           only these tiers land on a record; reserve_tiers land when nothing better did
  * drop_status           a Drop number is marked dead, with the value it replaced recorded
  * fixup_queue           a record with no full name or no mailing address waits for a person
                          (that many at a time) and never reaches a paid source
  * entity: principals    a company or trust goes to the principal finder, not to mail only
  * research_by_list      which lists are researched on arrival and which only once exhausted
  * return_signals        the three ways returned mail is recorded, read by one handoff stage
  * retrace_after_days    DataSift's own trace again on anything still active after that long
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, store
from . import gate, lanes, ledger, windows
from .sources import Subject, digits, line_kind, missing_for_trace, subject_of

log = logging.getLogger(__name__)

DEAD_STATUSES = {"sold", "closed", "close_out", "lost_deal", "listed", "under_contract", "not_interested", "dnc",
                 "opt_out", "dead lead", "buyer", "buyer_found", "buyer_lost", "push_deal", "refer_lead",
                 "transactions", "underwrite"}
WORKLOAD_EXCLUDE = ["sold", "closed", "close_out", "lost_deal", "listed", "under_contract", "not_interested", "dnc",
                    "opt_out", "Dead Lead", "prospecting"]
# Statuses where a record is finished for good (exact DataSift values, for a search's must_not).
GONE_STATUSES = ["sold", "closed", "close_out", "lost_deal", "listed", "dnc", "opt_out", "Dead Lead"]
TIER_NAMES = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")
# What counts as REACHED by a trace. The lessons dial and export only Dial First and Dial Second
# numbers with no phone status (4-05 @ 22:54, 14-13 @ 0:29, 5-03 @ 6:33), so a record holding only
# Third or Fourth numbers keeps going down the waterfall. A stage can widen it with callable_tiers.
CALLABLE_TIERS = {"Dial First", "Dial Second"}
BAD_PHONE = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
LITIGATOR_TAG = "DNC"
MAX_LINES = 12
TIER_RANK = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3, "Drop": 4}
UNSET_PHONE = {"", "UNKNOWN"}                 # a phone status nobody and no dialer has set
# When research may take a record, by the list it is on: the most eager rule a record matches wins.
RESEARCH_RANK = {"arrival": 0, "exhausted": 1, "never": 2}
# The free 180-day trace is logged under its own action so it never counts toward a record's
# lifetime trace cap (plan 1.13): that cap exists to stop paying for the same record twice.
REFRESH_ACTION = "native_refresh"
# How long "reachable, and every stacked source already tried" is trusted before the record is read again.
STACK_DONE_TTL = timedelta(days=7)


# ---------------------------------------------------------------------- summary + context
@dataclass
class Summary:
    stage: str
    kind: str
    owner: str
    mode: str
    live: bool
    found: int = 0
    acted: int = 0
    usd: float = 0.0
    would_usd: float = 0.0
    head: str = ""
    lines: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    tally: Counter = field(default_factory=Counter)
    batch: int = 0
    writes: int = 0

    def line(self, text: str) -> None:
        if len(self.lines) < MAX_LINES:
            self.lines.append(text)
        elif len(self.lines) == MAX_LINES:
            self.lines.append("...")

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["tally"] = dict(self.tally)
        return d


@dataclass
class Ctx:
    policy: object
    stage: object
    crm: object
    run_id: str
    now: datetime
    live: bool
    out: Summary
    sources: dict = field(default_factory=dict)
    scorer: object = None
    mailer: object = None
    task_id: int = 0
    enqueue: object = None          # store.enqueue, injectable
    _reserved: float = 0.0

    # --- writes
    def write(self, what: str, fn, *args, **kw):
        self.out.writes += 1
        if not self.live:
            return None
        return fn(*args, **kw)

    def change(self, record_uuid: str, what: str, before, after) -> None:
        """Remember what a live write replaced, so the run can be put back (plan 0.15). Call it
        BEFORE the write: a write that then fails leaves a change that undo simply re-applies."""
        if self.live:
            ledger.change(self.run_id, self.stage.id, record_uuid, what, before, after)

    # --- the gate
    def passes(self, rec: dict, channel: str, row: dict | None = None, allow=None) -> bool:
        """Run the suppression gate on a full record for this stage. A record that fails is counted
        under its reason ("held back: ...") and the caller skips it."""
        v = self.verdict(rec, channel, row=row, allow=allow)
        if not v.ok:
            self.out.tally["held back: %s" % v.reason()] += 1
        return v.ok

    def verdict(self, rec: dict, channel: str, row: dict | None = None, allow=None):
        cfg = getattr(self.policy, "gate", None) or gate.GateConfig()
        lanes_cfg = getattr(self.policy, "lanes", None)
        allow = set(_list(self.stage.get("gate_allow"))) | set(allow or ())
        ow = (rec or {}).get("owner") or {}
        custom = None
        if cfg.read_fields and (rec or {}).get("uuid") and self.stage.get("read_fields", True):
            custom = ledger.fact(rec["uuid"], "custom_fields", max_age=timedelta(days=7))
            if custom is None:
                try:
                    custom = self.crm.custom_fields(rec["uuid"])
                except Exception:  # noqa: BLE001  (a failed field read must be said out loud, not passed over)
                    custom = {}
                    self.out.problem("could not read the custom fields on a record, so the Owner Deceased field was not checked")
                ledger.set_fact(rec["uuid"], "custom_fields", custom)
        lit = set()
        for p in ow.get("phones") or []:
            n = digits(p.get("number"))
            c = ledger.phone(n) if n else None
            if c and c.get("litigator"):
                lit.add(n)
        return gate.check(rec, channel, cfg=cfg, lane=str(self.stage.get("lane") or ""), allow=allow, row=row,
                          custom=custom, owner=lanes.owner_lane(ow.get("uuid") or ""), litigators=lit,
                          lanes_cfg=lanes_cfg)

    # --- money
    def budget(self, usd: float, record_uuid: str = "", record_usd: float | None = None) -> tuple[bool, str]:
        """May `usd` more be spent today? `record_usd` (default `usd`) is what lands on the one record,
        checked against its lifetime cap; the day caps see the whole amount."""
        if usd <= 0:
            return True, ""
        d = ledger.day(self.now, self.policy.tz)
        stage_cap = self.stage.get("daily_usd")
        pending = self._reserved
        if stage_cap is not None and ledger.spent(d, self.stage.id) + pending + usd > float(stage_cap):
            return False, "stage cap $%.2f/day" % float(stage_cap)
        # A lane's own cap (plan 4.1): what every stage of that lane has spent today, together.
        # Without it the Strong lane's scoring could use up the day before first to market runs.
        lane = str(self.stage.get("lane") or "")
        outreach = self.stage.spend == "outreach"
        lane_cap = lanes.lane_cap(getattr(self.policy, "lanes", None), lane, "outreach_daily_usd" if outreach else "daily_usd")
        if lane_cap is not None:
            ids = [s.id for s in (getattr(self.policy, "stages", None) or {}).values() if str(s.get("lane") or "") == lane]
            used = sum(ledger.spent(d, sid, action="mail") if outreach else ledger.spent(d, sid, exclude_action="mail")
                       for sid in ids)
            if used + pending + usd > lane_cap:
                return False, "%s lane cap $%.2f/day" % (lane.replace("_", " "), lane_cap)
        if self.stage.spend == "outreach":
            # Marketing spend has its own budget: it does not compete with traces and scoring for
            # the data cap, and it never counts toward the brain's research cap.
            if ledger.spent(d, action="mail") + pending + usd > self.policy.outreach_daily_usd:
                return False, "outreach cap $%.2f/day" % self.policy.outreach_daily_usd
            return True, ""
        if ledger.spent(d, exclude_action="mail") + pending + usd > self.policy.daily_usd:
            return False, "autopilot cap $%.2f/day" % self.policy.daily_usd
        if store.spent(day=datetime.now(timezone.utc).isoformat()[:10], research=False) + pending + usd > config.DAILY_CAP_USD:
            return False, "brain cap $%.2f/day" % config.DAILY_CAP_USD
        one = usd if record_usd is None else record_usd
        if record_uuid and ledger.spent(record_uuid=record_uuid, exclude_action=REFRESH_ACTION) + one > self.policy.record_usd:
            return False, "record cap $%.2f lifetime" % self.policy.record_usd
        return True, ""

    def reserve(self, usd: float) -> None:
        self._reserved += usd
        if not self.live:
            self.out.would_usd += usd

    def settle(self, reserved: float, actual: float, what: str) -> None:
        """Turn a reservation into what was actually billed; the brain's ledger gets the real figure."""
        self._reserved = max(0.0, self._reserved - reserved)
        if self.live and actual > 0:
            self.out.usd += actual
            if self.task_id and self.stage.spend != "outreach":
                store.add_spend(self.task_id, actual, "autopilot %s: %s" % (self.stage.id, what))


def addr(x: dict) -> str:
    a = (x or {}).get("address") or {}
    if isinstance(a, str):
        return a
    street = a.get("street") or a.get("full_address") or ""
    city = a.get("city") or ""
    return ("%s, %s" % (street, city)).strip(", ") or (x or {}).get("uuid", "")[:8]


def tag_titles(x: dict) -> list[str]:
    out = []
    for t in (x or {}).get("tags") or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def status_of(x: dict) -> str:
    s = (x or {}).get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title") or ""
    return str(s or "").strip().lower()


def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def assignee_of(rec: dict) -> str:
    a = (rec or {}).get("assigned_to")
    return (a.get("uuid") if isinstance(a, dict) else a) or ""


def marks_of(rec: dict) -> set[str]:
    """Every tag and list title on a record, in lower case: the names a route or a list rule keys
    on. Courthouse records are told apart by their LIST (Liens, Condemned), which is often not a
    tag, so a rule that read tags alone could never see them."""
    out = {t.strip().lower() for t in tag_titles(rec)}
    for x in (rec or {}).get("lists") or []:
        n = (x.get("title") or x.get("name")) if isinstance(x, dict) else x
        if n:
            out.add(str(n).strip().lower())
    return out


def mask(number) -> str:
    """A number as it may be printed or posted: its last four digits. The whole number stays on
    the record and in files that never leave the box."""
    d = digits(number) or "".join(ch for ch in str(number or "") if ch.isdigit())
    return ("ending %s" % d[-4:]) if d else "no number"


def dial_order(numbers: list[dict], agreed=()) -> list[dict]:
    """The order to dial an owner's usable numbers in (plan 1.14): a number a person confirmed,
    then mobiles before landlines, a number two sources agree on before one source's word, then by
    tier. Landlines go last because they reach the owner least often and cannot take the text that
    follows an unanswered dial. `numbers` are the gate's usable numbers ({number, tier, status, type})."""
    agreed = set(agreed or ())

    def key(n: dict):
        confirmed = str(n.get("status") or "").upper() == "CORRECT"
        landline = str(n.get("type") or "").upper() == "LANDLINE"
        return (0 if confirmed else 1, 1 if landline else 0, 0 if n.get("number") in agreed else 1,
                TIER_RANK.get(n.get("tier") or "", 9))
    return sorted(numbers, key=key)


def round_ends_at(ctx: "Ctx", lane: str = "") -> int:
    """The call counter that ends the round for this stage's lane (5 unless the playbook says otherwise)."""
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    tl = cfg.timeline(lane or str(ctx.stage.get("lane") or "")) or cfg.timeline("ftm") or {}
    return int(ctx.stage.get("round_ends_at") or tl.get("round_ends_at", 5))


def exhausted_of(ctx: "Ctx", rec: dict) -> bool:
    """Nothing is left to dial on this record: the call round is over, the record's status says so,
    or every number on it is wrong, dead or do-not-call. A record with no number at all is
    exhausted once its mail lane has run out, since mail is the only channel it ever had."""
    if status_of(rec) == "exhausted":
        return True
    if int(rec.get("predictivecall_attempts") or 0) >= round_ends_at(ctx):
        return True
    phones = (rec.get("owner") or {}).get("phones") or []
    if phones:
        return all(str(p.get("status") or "UNKNOWN").upper() in BAD_PHONE for p in phones)
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    tl = cfg.timeline(str(ctx.stage.get("lane") or "")) or cfg.timeline("ftm") or {}
    return int(rec.get("directmail_attempts") or 0) >= int(tl.get("mail_pieces", 12))


def research_rule(ctx: "Ctx", rec: dict) -> tuple[str, str]:
    """(arrival | exhausted | never, the list that decided it): when research may take this record,
    by the lists and tags it carries (plan 1.17). Liens are the largest courthouse list and are
    dialed and mailed until exhausted before a dollar of research is spent on them; a condemned
    house, a foreclosure and a probate are researched the day they arrive. A record on two lists
    follows the more eager rule. A stage with no research_by_list researches on arrival, as before."""
    table = ctx.stage.get("research_by_list")
    if not isinstance(table, dict) or not table:
        return "arrival", ""
    names = marks_of(rec)
    hits = []
    for k, v in table.items():
        rule = str(v).strip().lower()
        if rule not in RESEARCH_RANK:
            ctx.out.problem("research_by_list: %s is set to %r (use arrival, exhausted or never); those records are not researched" % (k, v))
            rule = "never"
        if str(k).strip().lower() in names:
            hits.append((rule, str(k)))
    if not hits:
        default = str(ctx.stage.get("research_default", "arrival")).strip().lower()
        return (default if default in RESEARCH_RANK else "arrival"), ""
    return min(hits, key=lambda h: RESEARCH_RANK[h[0]])


def research_open(ctx: "Ctx", rec: dict, death: bool = False) -> tuple[bool, str]:
    """(may research take this record now, why not or the list that let it through). A possible
    death is never held by a list rule: a dead owner is held out of every channel until research
    writes back, whichever list the record came from."""
    if death:
        return True, ""
    rule, by = research_rule(ctx, rec)
    if rule == "never":
        return False, "%s records are not researched" % (by or "other")
    if rule == "exhausted" and not exhausted_of(ctx, rec):
        return False, "%s records are researched once they are exhausted" % (by or "other")
    return True, by


def lane_entry(ctx: "Ctx", rec: dict, lane: str) -> datetime:
    """When this record entered its lane: the moment a lane marker (the FTM tag, the Obituary list)
    first landed on it, else the day the record was created. Read once from the record's own log
    and remembered. It is what the timeline counts days from and what the attempt keeper counts
    dials from, so a call made months ago under another list never counts toward this lane."""
    u = rec.get("uuid") or ""
    key = "lane_entry:%s" % (lane or "any")
    v = ledger.fact(u, key)
    if v:
        return datetime.fromisoformat(v)
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    marks = lanes_cfg.markers.get(lane) or {}
    want = {t.lower() for t in (marks.get("tags") or []) + (marks.get("lists") or [])}
    entry = created = None
    try:
        log_rows = ctx.crm.record_logs(u, ["property.created", "property.tags.added", "property.lists.added"])
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read a record's log for its lane entry date (%s)" % str(exc)[:100])
        log_rows = []
    from . import activity
    for e in log_rows:
        at = activity.parse_ts(e.get("timestamp") or "")
        if not at:
            continue
        et = e.get("event_type")
        p = e.get("payload") or {}
        if et == "property.created":
            created = at if created is None else min(created, at)
        else:
            names = {str(x).lower() for x in (p.get("tags") or []) + (p.get("lists") or []) + (p.get("system_lists") or [])}
            if want and names & want:
                entry = at if entry is None else min(entry, at)
    when = entry or created
    if when is None:
        # Older than the log reaches: the record has been here a long time. Use a far-back date
        # (callers clamp to their own look-back) and do not cache a guess as if it were known.
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    ledger.set_fact(u, key, when.isoformat(timespec="seconds"))
    return when


def merge_must(must: dict, extra: dict | None) -> dict:
    m = dict(must)
    for k, v in (extra or {}).items():
        if k in ("all_tags", "any_tags") and isinstance(m.get(k), list):
            m[k] = list(dict.fromkeys(list(m[k]) + list(v)))
        elif k == "must_not" and isinstance(m.get("must_not"), dict):
            mn = dict(m["must_not"])
            for kk, vv in v.items():
                mn[kk] = list(dict.fromkeys(list(mn.get(kk) or []) + list(vv))) if isinstance(vv, list) else vv
            m["must_not"] = mn
        else:
            m[k] = v
    return m


def preset_rows(ctx: Ctx, titles, extra: dict | None = None, cap: int = 2000) -> list[tuple[str, dict]]:
    """(preset title, search row) for every record in the named presets, deduped in preset order.
    The presets are read live by title, so an edit in DataSift changes what a stage works on."""
    seen, out = set(), []
    for title in titles:
        try:
            must = ctx.crm.preset_must(title)
        except KeyError as exc:
            ctx.out.problem("%s (the stage skipped it)" % exc)
            continue
        must, notes = windows.resolve(must, ctx.now)
        for n in notes:
            if "dropped" in n:
                ctx.out.problem("%s: %s" % (title, n))
        must = merge_must(must, extra)
        for row in ctx.crm.rows(must, cap=cap):
            u = row.get("uuid")
            if u and u not in seen:
                seen.add(u)
                out.append((title, row))
    return out


def stage_rows(ctx: Ctx, extra: dict | None = None, cap: int | None = None) -> list[tuple[str, dict]]:
    """(preset title, search row) for every record in the stage's presets, deduped in preset order.
    A stage that works in tranches (`tranche` in its block) gets only the rows in its lane's
    current tranche."""
    cap = int(cap or ctx.stage.get("max_scan", 2000))
    return _in_tranche(ctx, preset_rows(ctx, ctx.stage.presets, extra, cap))


# ---------------------------------------------------------------------- tranches (plan 4.1)
def _tranche_cfg(ctx: Ctx, lane: str) -> dict:
    """The lane's tranche settings: the playbook's lanes.<lane>.tranche block, or, for a stage that
    names only a size (`tranche: 1500`), that size with the default rules."""
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    cfg = dict((getattr(lanes_cfg, "tranches", None) or {}).get(lane) or {})
    want = ctx.stage.get("tranche")
    if not cfg and isinstance(want, int) and not isinstance(want, bool) and want > 0:
        cfg = {"size": int(want), "presets": [], "waiting_presets": [], "carry_over": 0.05, "min_days": 7.0,
               "check_hours": 12.0, "scan": 5000}
    if cfg and not cfg.get("presets"):
        cfg["presets"] = list((getattr(lanes_cfg, "presets", None) or {}).get(lane) or [])
    return cfg


def tranche_done(ctx: Ctx, lane: str, cfg: dict, state: dict) -> bool:
    """Has the lane's current tranche been worked? It has when it has been open for a week and no
    more than a few of its records (carry_over, 5%) still sit in the presets that hold a record
    until it is texted and dialed. Scoring comes first by construction: the text and call lanes use
    only tiered numbers. The check pages those presets, so it runs at most twice a day."""
    try:
        opened = datetime.fromisoformat(state.get("opened") or "")
    except ValueError:
        return False
    if ctx.now - opened < timedelta(days=float(cfg.get("min_days", 7))):
        return False
    checked = state.get("checked") or ""
    if checked:
        try:
            if ctx.now - datetime.fromisoformat(checked) < timedelta(hours=float(cfg.get("check_hours", 12))):
                return False
        except ValueError:
            pass
    members = lanes.tranche_members(lane, int(state["n"]))
    titles = cfg.get("waiting_presets") or cfg.get("presets") or list(ctx.stage.presets)
    waiting = {r["uuid"] for _, r in preset_rows(ctx, titles, None, int(cfg.get("scan", 5000)))} & members
    lanes.tranche_note(lane, checked=ctx.now.isoformat(timespec="seconds"), waiting=len(waiting))
    return len(waiting) <= float(cfg.get("carry_over", 0.05)) * max(1, len(members))


def _next_tranche(ctx: Ctx, lane: str, cfg: dict, rows: list, state: dict | None) -> tuple[dict | None, set]:
    """Draw the next tranche: the first `size` records of the lane's entry presets that were never
    in a tranche before. A live run remembers it; a plan run only works out which records it would
    be, so a plan can never leave a tranche behind for a later live run to inherit."""
    source = preset_rows(ctx, cfg["presets"], None, int(cfg.get("scan", 5000))) if cfg.get("presets") else rows
    drawn = lanes.tranche_members(lane)
    fresh = [r["uuid"] for _, r in source if r["uuid"] not in drawn][: int(cfg["size"])]
    name = lane.replace("_", " ")
    if not fresh:
        # Nothing is left behind the last tranche: the lane's backlog is finished.
        return state, (lanes.tranche_members(lane, int(state["n"])) if state else set())
    n = int((state or {}).get("n") or 0) + 1
    if ctx.live:
        new = lanes.tranche_open(lane, fresh, ctx.now)
        ctx.out.line("Drew tranche %d of the %s lane: %d record(s)." % (n, name, len(fresh)))
        return new, set(fresh)
    ctx.out.tally["would draw tranche %d of the %s lane" % (n, name)] = len(fresh)
    return {"n": n, "opened": "", "size": len(fresh), "planned": True}, set(fresh)


def _in_tranche(ctx: Ctx, rows: list) -> list:
    """Keep only the rows in the lane's current tranche, for a stage whose block sets `tranche`
    (true, or a size). All of Strong and Bulk at once is tens of thousands of texts and a scoring
    bill to match, so those lanes are worked a tranche at a time: the next is drawn only when this one
    has been scored, texted and dialed. A stage that does not ask (the attempt keeper, a
    second-chance stage) sees every row, because a dialed record must be counted whichever tranche
    it came from."""
    want = ctx.stage.get("tranche")
    if not want:
        return rows
    lane = str(ctx.stage.get("lane") or "")
    cfg = _tranche_cfg(ctx, lane) if lane else {}
    if not cfg.get("size"):
        ctx.out.problem("this stage works in tranches, but its lane has no tranche size (lanes.<lane>.tranche in "
                        "the playbook, or tranche: <size> on the stage); it did nothing")
        return []
    state = lanes.tranche_state(lane)
    if state is None or tranche_done(ctx, lane, cfg, state):
        state, members = _next_tranche(ctx, lane, cfg, rows, state)
    else:
        members = lanes.tranche_members(lane, int(state["n"]))
    kept = [(t, r) for t, r in rows if r["uuid"] in members]
    if len(kept) != len(rows):
        ctx.out.tally["behind the current tranche, waiting their turn"] += len(rows) - len(kept)
    return kept


def required_tags(ctx: Ctx) -> dict | None:
    req = _list(ctx.stage.get("require_tags"))
    if not req:
        return None
    ids = []
    for t in req:
        u = ctx.crm.tag_uuid(t)
        if not u:
            ctx.out.problem("tag %r does not exist in this account, so nothing can match require_tags" % t)
            return {"all_tags": ["00000000-0000-0000-0000-000000000000"]}
        ids.append(u)
    return {"all_tags": ids}


# ---------------------------------------------------------------------- people
def resolve_callers(ctx: Ctx, names: list[str] | None = None) -> list[dict]:
    """Callers by DataSift first name, or by {name, uuid}. The team-user list the names resolve
    through is not in the published reference (only the key's own user is), so an install that
    gives uuids never calls it; an install that gives names reads it and says so if it fails."""
    names = names or _list(ctx.stage.get("callers")) or ctx.policy.callers
    direct = [n for n in names if isinstance(n, dict) and n.get("uuid")]
    if direct and len(direct) == len(names):
        return [{"name": str(n.get("name") or n["uuid"][:8]), "uuid": str(n["uuid"])} for n in direct]
    try:
        users = ctx.crm.users()
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the team list (%s); give callers as {name, uuid} in the playbook" % str(exc)[:120])
        return [{"name": str(n.get("name") or n["uuid"][:8]), "uuid": str(n["uuid"])} for n in direct]
    out = [{"name": str(n.get("name") or n["uuid"][:8]), "uuid": str(n["uuid"])} for n in direct]
    for name in [n for n in names if not isinstance(n, dict)]:
        n = str(name).strip().lower()
        matches = [u for u in users if (u.get("first_name") or "").strip().lower() == n
                   or str(u.get("uuid") or "") == str(name)]
        active = [u for u in matches if u.get("is_active", True)]
        if not matches:
            ctx.out.problem("no DataSift user named %s; check people.callers in the playbook" % name)
        elif not active:
            ctx.out.problem("%s is inactive in DataSift, so gets nothing" % name)
        elif len(active) > 1:
            ctx.out.problem("two active users are named %s; list that caller by user uuid instead" % name)
        else:
            out.append({"name": active[0].get("first_name") or str(name), "uuid": active[0].get("uuid") or ""})
    return [p for p in out if p["uuid"]]


def balanced_pick(pool: list[dict], load: dict) -> dict:
    return min(pool, key=lambda p: (load.get(p["uuid"], 0), pool.index(p)))


# ---------------------------------------------------------------------- assign
def run_assign(ctx: Ctx) -> None:
    st = ctx.stage
    people = resolve_callers(ctx)
    rows = stage_rows(ctx, extra={"assigned_to": None})
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No unassigned records in %s." % ", ".join(st.presets)
        return
    if not people:
        ctx.out.problem("no active caller to assign to, so %d records stay unassigned" % len(rows))
        return
    load = {p["uuid"]: ctx.crm.count({"assigned_to": p["uuid"], "must_not": {"any_property_status": WORKLOAD_EXCLUDE}})
            for p in people}
    # A caller past the cap is said out loud every run (plan 1.5). First-to-market records are still
    # assigned: they are the one thing that never waits. Which records leave an over-full queue is
    # the owner's decision (D2), so the stage reports the number and moves nothing.
    cap = int(st.get("caller_cap", 0) or 0)
    if cap:
        for person in people:
            if load.get(person["uuid"], 0) > cap:
                ctx.out.problem("%s holds %d open records, %d over the cap of %d" % (
                    person["name"], load[person["uuid"]], load[person["uuid"]] - cap, cap))
    routes = {str(k).lower(): [str(n).lower() for n in _list(v)] for k, v in (st.get("routes") or {}).items()}
    strategy = st.get("strategy", "balanced")
    rr = int(store.kv_get("autopilot:rr:%s" % st.id) or 0)
    max_n = int(st.get("max_per_run", 200))
    # The backlog rule: the first run (plan or live) draws a line. Records already sitting unassigned
    # at that moment are the owner's call (backlog: true hands them out too); everything that lands
    # after the line is assigned the moment the stage sees it.
    baseline = store.kv_get("autopilot:baseline:%s" % st.id)
    if not baseline:
        baseline = ctx.now.isoformat(timespec="seconds")
        store.kv_set("autopilot:baseline:%s" % st.id, baseline)
    backlog = bool(st.get("backlog", False))
    given: Counter = Counter()
    readback = 0
    for title, row in rows:
        if sum(given.values()) >= max_n:
            ctx.out.tally["left for the next run"] += 1
            continue
        u = row["uuid"]
        if status_of(row) in DEAD_STATUSES:
            continue
        if not backlog and ledger.first_seen(u, st.id, ctx.now).isoformat(timespec="seconds") <= baseline:
            ctx.out.tally["backlog left as is (set backlog: true to hand these out)"] += 1
            continue
        rec = ctx.crm.record(u)
        if not ctx.passes(rec, "assign", row=row):
            continue
        pool = people
        if routes:
            # A route keys on a tag or a list title (plan 1.17): the lien and condemned records
            # are told apart by their list, which is often not a tag.
            tags = marks_of(rec)
            for tag, names in routes.items():
                if tag in tags:
                    pool = [p for p in people if p["name"].lower() in names] or people
                    break
        if strategy == "round_robin":
            who = pool[rr % len(pool)]
            rr += 1
        else:
            who = balanced_pick(pool, load)
        ctx.change(u, "assign", assignee_of(rec), who["uuid"])
        ctx.write("assign", ctx.crm.assign, u, who["uuid"])
        if ctx.live:
            if readback < 2 or sum(given.values()) % 25 == 0:
                back = ctx.crm.record(u).get("assigned_to")
                back = back.get("uuid") if isinstance(back, dict) else back
                readback += 1
                if back != who["uuid"]:
                    ctx.out.problem("assignment did not stick on %s (read back %r); stopped assigning" % (addr(row), back))
                    break
            ledger.event(ctx.run_id, st.id, u, "assign", {"to": who["name"], "preset": title}, now=ctx.now,
                         tz=ctx.policy.tz)
        load[who["uuid"]] = load.get(who["uuid"], 0) + 1
        given[who["name"]] += 1
        ctx.out.line("%s -> %s (%s)" % (addr(row), who["name"], title))
    if strategy == "round_robin" and ctx.live:
        store.kv_set("autopilot:rr:%s" % st.id, str(rr))
    ctx.out.acted = sum(given.values())
    verb = "Assigned" if ctx.live else "Would assign"
    held = ctx.out.tally.get("backlog left as is (set backlog: true to hand these out)", 0)
    if ctx.out.acted:
        ctx.out.head = "%s %d new record(s): %s." % (verb, ctx.out.acted,
                                                     ", ".join("%s %d" % kv for kv in given.most_common()))
    else:
        ctx.out.head = "No new arrivals to assign."
    if held:
        ctx.out.head += " %d unassigned record(s) predate the autopilot and are left for the owner's call." % held


# ---------------------------------------------------------------------- native trace
def _last_traces() -> dict:
    """record uuid -> the newest trace of any kind this autopilot logged for it."""
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, MAX(at) AS at FROM ap_events WHERE action IN ('trace', 'native_trace', ?) "
                         "AND record_uuid<>'' GROUP BY record_uuid", (REFRESH_ACTION,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["record_uuid"]] = datetime.fromisoformat(r["at"])
        except (TypeError, ValueError):
            continue
    return out


def last_traced(ctx: Ctx, row: dict, logged: dict) -> datetime:
    """When this record was last skip traced: the later of the date DataSift shows on the search
    row and the newest trace this autopilot logged. A record with neither is dated from the day the
    stage first saw it, so the wait can only ever be too long, never too short."""
    from . import activity
    seen = []
    at = activity.parse_ts(row.get("last_skip_traced") or "")
    if at:
        seen.append(at)
    if logged.get(row["uuid"]):
        seen.append(logged[row["uuid"]])
    return max(seen) if seen else ledger.first_seen(row["uuid"], ctx.stage.id, ctx.now)


def _native_refresh(ctx: Ctx) -> None:
    """The 180-day re-trace (plan 1.13): anything still active whose last trace is that old gets
    DataSift's own trace again. People change numbers, and a record traced once in the spring is
    being dialed in the autumn on what was true then. The trace is DataSift's (free on the unlimited
    plan), so it sits outside the per-record cap, which exists to stop PAYING twice for one record."""
    st = ctx.stage
    days = float(st.get("retrace_after_days"))
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    logged = _last_traces()
    cutoff = ctx.now - timedelta(days=days)
    due = []
    for _, r in rows:
        v = gate.check_row(r, "trace", cfg=getattr(ctx.policy, "gate", None), allow=_list(st.get("gate_allow")))
        if not v.ok:
            ctx.out.tally["held back: %s" % v.reason()] += 1
            continue
        if last_traced(ctx, r, logged) > cutoff:
            continue
        due.append(r)
    if not due:
        ctx.out.head = "No active record was last traced %d or more days ago." % days
        return
    _native_scoped(ctx, due, refresh_days=days)


def run_native_trace(ctx: Ctx) -> None:
    st = ctx.stage
    if st.get("retrace_after_days"):
        _native_refresh(ctx)
        return
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    # A bulk trace never opens each record, so only the row's own status can be checked here.
    kept = []
    for title, r in rows:
        v = gate.check_row(r, "trace", cfg=getattr(ctx.policy, "gate", None), allow=_list(st.get("gate_allow")))
        if v.ok:
            kept.append((title, r))
        else:
            ctx.out.tally["held back: %s" % v.reason()] += 1
    rows = kept
    if not rows:
        ctx.out.head = "Nothing is waiting on a skip trace."
        return
    min_age = timedelta(hours=float(st.get("min_age_hours", 2)))
    waiting = [r for _, r in rows if ctx.now - ledger.first_seen(r["uuid"], st.id, ctx.now) >= min_age]
    if not waiting:
        ctx.out.head = "%d new record(s) need a trace; giving DataSift %s hours to trace them on its own first." % (
            len(rows), st.get("min_age_hours", 2))
        return
    if st.get("scoped", True):
        # A scoped trace touches only the waiting records, so it may run every slot (the challenge
        # teaches every 30 minutes, C2.2); a record traced in the last day is not sent again.
        recent = ctx.now - timedelta(hours=24)
        waiting = [r for r in waiting if not ledger.events(r["uuid"], "native_trace", since=recent)]
        if not waiting:
            ctx.out.head = "Every record waiting on a trace was sent to DataSift in the last day."
            return
        _native_scoped(ctx, waiting)
        return
    every = timedelta(hours=float(st.get("every_hours", 24)))
    last = store.kv_get("autopilot:native_trace:last")
    if last and ctx.now - datetime.fromisoformat(last) < every:
        ctx.out.head = "%d record(s) still need a trace; DataSift's trace ran at %s and may run again after %s." % (
            len(waiting), last, (datetime.fromisoformat(last) + every).isoformat(timespec="minutes"))
        return
    stats = ctx.crm.skiptrace_stats()
    spent, saved = float(stats.get("value_spent") or 0), float(stats.get("saved_by_unlimited_skiptracing") or 0)
    if spent > 0 or saved <= 0:
        ctx.out.problem("this account looks metered (value spent $%.2f): the account-wide trace would bill every "
                        "record, so it was not run. Use scoped: true, or the retrace stage covers these." % spent)
        return
    ctx.write("native trace", ctx.crm.skiptrace_all)
    if ctx.live:
        store.kv_set("autopilot:native_trace:last", ctx.now.isoformat(timespec="seconds"))
        ledger.event(ctx.run_id, st.id, "", "native_trace", {"waiting": len(waiting), "scope": "account"},
                     now=ctx.now, tz=ctx.policy.tz)
    ctx.out.acted = len(waiting)
    ctx.out.head = "%s DataSift's own skip trace for %d waiting record(s) (account-wide route, free on the unlimited plan)." % (
        "Ran" if ctx.live else "Would run", len(waiting))


def _native_scoped(ctx: Ctx, waiting: list[dict], refresh_days: float = 0) -> None:
    """The documented flow: a dry run on exactly these records, refuse unless it covers exactly
    them, then the real call carrying expected_count so the server refuses any other selection.
    That pair is the guard against the 2026-08-19 class of bug, where an ignored scope traced the
    whole account. The bulk policy treats a skip trace as an ACTIVITY: under 20 records it is not
    allocated properly and repeated small ones count as abuse, so a smaller set waits (DataSift
    traces new uploads on its own, which is where a few stragglers usually get picked up).

    `refresh_days` marks the 180-day re-trace: the same guarded pair, asked to trace records that
    were traced before, and logged under its own action."""
    st = ctx.stage
    floor = int(st.get("min_batch", 20))
    uuids = [r["uuid"] for r in waiting][: min(int(st.get("max_records", 1000)), 1000)]
    again = {"force_refresh": True} if refresh_days else {}
    if len(uuids) < floor:
        if refresh_days:
            ctx.out.head = ("%d active record(s) were last traced %d or more days ago; the bulk policy needs %d or more "
                            "per job, so they wait for more to come due." % (len(uuids), refresh_days, floor))
            return
        ctx.out.head = ("%d record(s) wait on a trace; the bulk policy needs %d or more per job, so they wait for "
                        "more to arrive (DataSift's own upload trace usually takes them first)." % (len(uuids), floor))
        return
    ctx.out.acted = len(uuids)
    if not ctx.live:
        if refresh_days:
            ctx.out.head = ("Would ask DataSift to trace %d active record(s) again, each last traced %d or more days ago, "
                            "scoped to exactly those (dry run first)." % (len(uuids), refresh_days))
            return
        ctx.out.head = "Would ask DataSift to skip trace %d waiting record(s), scoped to exactly those (dry run first)." % len(uuids)
        return
    est = ctx.crm.skiptrace_scoped(uuids, dry_run=True, **again)
    n = None
    if isinstance(est, dict):
        n = est.get("number_of_records", est.get("count", est.get("expected_count")))
    cost = float((est or {}).get("cost") or 0) if isinstance(est, dict) else 0.0
    if n != len(uuids):
        ctx.out.acted = 0
        ctx.out.problem("DataSift's dry run covers %r records, not the %d asked for, so nothing was traced "
                        "(the scope did not take; check the payload before trying again)" % (n, len(uuids)))
        return
    if cost > 0:
        ok, why = ctx.budget(cost)
        if not ok:
            ctx.out.acted = 0
            ctx.out.problem("DataSift quotes $%.2f for this trace, over the %s; not run" % (cost, why))
            return
    ctx.crm.skiptrace_scoped(uuids, dry_run=False, tags=_list(st.get("tags")), **again)
    ctx.settle(0, cost, "native trace x%d" % len(uuids))
    if not refresh_days:
        store.kv_set("autopilot:native_trace:last", ctx.now.isoformat(timespec="seconds"))
    for u in uuids:
        ledger.event(ctx.run_id, st.id, u, REFRESH_ACTION if refresh_days else "native_trace", {"scope": "records"},
                     usd=round(cost / len(uuids), 4), now=ctx.now, tz=ctx.policy.tz)
    if refresh_days:
        ctx.out.head = "Asked DataSift to trace %d active record(s) again, each last traced %d or more days ago%s." % (
            len(uuids), refresh_days, (", $%.2f" % cost) if cost else ", free on this plan")
        return
    ctx.out.head = "Asked DataSift to skip trace %d record(s), scoped and estimated first%s." % (
        len(uuids), (", $%.2f" % cost) if cost else ", free on this plan")


# ---------------------------------------------------------------------- scoring (shared)
def eligible(p: dict) -> bool:
    tags = set(tag_titles(p))
    if tags & set(TIER_NAMES) or LITIGATOR_TAG in tags:
        return False
    if (p.get("status") or "UNKNOWN").upper() in BAD_PHONE:
        return False
    return bool(digits(p.get("number")))


def phone_payload(p: dict, tier: str, extra_tags: list[str] | None = None) -> dict:
    """The full phone object upsert-phones needs, with exactly one tier (mirrors d4d_call_list):
    a union would leave a re-scored phone matching two dialer presets at once."""
    tags = [t for t in tag_titles(p) if t not in TIER_NAMES]
    for t in (extra_tags or []) + ([tier] if tier and tier != "Unknown" else []):
        if t and t not in tags:
            tags.append(t)
    return {"number": digits(p.get("number")) or p.get("number"), "type": p.get("type") or "UNKNOWN",
            "tags": sorted(tags), "status": (p.get("status") or "UNKNOWN").upper(),
            "is_connected": p.get("is_connected", True), "verified": p.get("verified", False)}


def score_numbers(ctx: Ctx, numbers: list[str], record_uuid: str = "") -> dict:
    """number -> {"tier", "score", ...}. Cached numbers cost nothing; new ones go to Trestle inside
    the caps. In plan mode the new ones are priced, not scored."""
    out, need = {}, []
    for n in dict.fromkeys(numbers):
        c = ledger.phone(n)
        if c and c.get("tier"):
            out[n] = {"tier": c["tier"], "score": c.get("score"), "cached": True, "litigator": bool(c.get("litigator")),
                      "line_type": c.get("line_type") or ""}
        else:
            need.append(n)
    if not need:
        return out
    if not ctx.scorer or not ctx.scorer.configured():
        ctx.out.problem("no Trestle key on this box, so new numbers land unscored (set TRESTLE_PAID_API_KEY)")
        return out
    unit = ctx.scorer.unit_usd
    allowed = []
    for n in need:
        ok, why = ctx.budget(unit * (len(allowed) + 1), record_uuid)
        if not ok:
            ctx.out.tally["numbers left unscored (%s)" % why] += len(need) - len(allowed)
            break
        allowed.append(n)
    if not allowed:
        return out
    cost = round(unit * len(allowed), 4)
    ctx.reserve(cost)
    if not ctx.live:
        ctx.out.tally["numbers to score"] += len(allowed)
        ctx.settle(cost, 0, "score")
        return out
    res = ctx.scorer.score(allowed)
    paid = 0
    for n, r in res.items():
        if r.get("error"):
            ctx.out.tally["scoring errors"] += 1
            continue
        paid += 1
        ledger.set_phone(n, r.get("score"), r.get("tier") or "", r.get("line_type") or "", bool(r.get("litigator")))
        out[n] = r
    ctx.settle(cost, round(paid * unit, 4), "scored %d numbers" % paid)
    if record_uuid and paid:
        ledger.event(ctx.run_id, ctx.stage.id, record_uuid, "score", {"numbers": paid}, usd=round(paid * unit, 4),
                     now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["numbers scored"] += paid
    return out


def phone_tier(p: dict) -> str:
    return next((t for t in tag_titles(p) if t in TIER_NAMES), "")


def litigator_tag(ctx: Ctx) -> str:
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    return (cfg.litigator_tags or ["Litigator"])[0]


def suppress_litigator(ctx: Ctx, rec: dict) -> None:
    """A litigator's number is a RECORD problem, not a phone problem (plan 0.3): the record, and
    every other record the same owner holds, is tagged Litigator and Do Not Market for good, with
    a note. Marking only the one phone left the owner's other numbers, the mail lane and the
    owner's other houses open."""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    add = [(cfg.litigator_tags or ["Litigator"])[0], (cfg.do_not_market_tags or ["Do Not Market"])[0]]
    u = rec.get("uuid") or ""
    targets = [(u, rec)]
    owner_uuid = (rec.get("owner") or {}).get("uuid") or ""
    if owner_uuid:
        try:
            for r in ctx.crm.owner_records(owner_uuid):
                if r.get("uuid") and r["uuid"] != u:
                    targets.append((r["uuid"], None))
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not list a litigator's other records (%s); only the one record was tagged" % str(exc)[:100])
    for uu, full in targets:
        have = set(tag_titles(full)) if full else set()
        need = [t for t in add if t not in have]
        if not need:
            continue
        ctx.change(uu, "add_tags", None, need)
        ctx.write("litigator", ctx.crm.add_tags, uu, need, full)
        ctx.write("litigator note", ctx.crm.message, uu,
                  "A number on this owner belongs to a known TCPA litigator. Do not call, text or mail, on any record this owner holds.")
        if ctx.live:
            ledger.event(ctx.run_id, ctx.stage.id, uu, "litigator", {"tags": need}, now=ctx.now, tz=ctx.policy.tz)
        ctx.out.tally["records suppressed for a litigator"] += 1
    ctx.out.line("%s: a litigator's number, record%s tagged %s" % (addr(rec), "s" if len(targets) > 1 else "", " and ".join(add)))


def _unset(p: dict) -> bool:
    return str(p.get("status") or "UNKNOWN").upper() in UNSET_PHONE


def _tier_rules(ctx: Ctx, payload: list[dict], elig: list[dict], tiers: dict, old_drops: list[dict],
                old_types: list[tuple], drop_status: str, write_type: bool) -> list[dict]:
    """Tier discipline on the same upsert that writes the tiers (plan 1.14, decision D12).

    A Drop number is marked dead, so no dialer preset, export or text list can pick it up again and
    the "every number is wrong or dead" preset can see the record for what it is. Only a status
    nobody has set is replaced: a number a person marked, correct or wrong, keeps that mark.

    The scorer's line type is written where DataSift has none, because two rules read it: a text
    may only use a mobile, and landlines are dialed last. A number left at UNKNOWN is invisible to
    both."""
    by_num = {digits(p.get("number")): p for p in elig}
    for ph in payload:
        res = tiers.get(ph["number"]) or {}
        if drop_status and "Drop" in ph["tags"] and ph["status"] in UNSET_PHONE and not res.get("litigator"):
            ph["status"] = drop_status
            ctx.out.tally["Drop numbers marked %s" % drop_status.lower()] += 1
        src = by_num.get(ph["number"]) or {}
        if write_type and str(src.get("type") or "UNKNOWN").upper() == "UNKNOWN" and line_kind(res.get("line_type")):
            ph["type"] = line_kind(res.get("line_type"))
            ctx.out.tally["line types written"] += 1
    # Numbers tiered before these rules existed are never "eligible" again, so they are swept here.
    older: dict[str, dict] = {}
    for p in old_drops:
        ph = phone_payload(p, "Drop")
        ph["status"] = drop_status
        older[ph["number"]] = ph
        ctx.out.tally["Drop numbers marked %s" % drop_status.lower()] += 1
    for p, kind in old_types:
        n = digits(p.get("number"))
        ph = older.get(n) or phone_payload(p, phone_tier(p))
        ph["type"] = kind
        older[n] = ph
        ctx.out.tally["line types written"] += 1
    return payload + list(older.values())


def run_score(ctx: Ctx) -> None:
    st = ctx.stage
    rows = stage_rows(ctx, extra=required_tags(ctx))
    ctx.out.found = len(rows)
    recheck = timedelta(days=float(st.get("recheck_days", 7)))
    max_records = int(st.get("max_records", 200))
    drop_status = str(st.get("drop_status") or "").strip().upper()      # "DEAD" marks Drop numbers dead (D12)
    write_type = bool(st.get("write_line_type", False))
    done = 0
    checked = 0
    # Finding a record's unscored numbers means reading the record, and a lane of 568 records is
    # about 750 requests: twenty minutes of this app's share that the next assignment run then waits
    # behind. So one run reads at most max_reads records, the ones never read first and then the
    # ones read longest ago, and says how many it left. In plan mode nothing is ever marked scored,
    # so without the cap every run would read the whole lane again.
    max_reads = int(st.get("max_reads", 250))
    reads = 0
    read_at = {r["uuid"]: ledger.fact(r["uuid"], "score_read") for _, r in rows}
    rows = sorted(rows, key=lambda tr: str(read_at.get(tr[1]["uuid"]) or ""))
    for _, row in rows:
        if done >= max_records:
            ctx.out.tally["left for the next run"] += 1
            continue
        u = row["uuid"]
        if ledger.fact(u, "scored", max_age=recheck):
            continue
        if reads >= max_reads:
            ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
            continue
        reads += 1
        rec = ctx.crm.record(u)
        ledger.set_fact(u, "score_read", ctx.now.isoformat(timespec="seconds"))   # the autopilot's own note, kept in plan mode too
        if not ctx.passes(rec, "score", row=row):
            continue
        ow = rec.get("owner") or {}
        elig = [p for p in (ow.get("phones") or []) if eligible(p)]
        # Already tiered, so never eligible again: Drop numbers still unmarked, and tiered numbers
        # whose line type the scoring cache knows and DataSift does not (plan 1.14).
        old_drops = [p for p in (ow.get("phones") or []) if drop_status and phone_tier(p) == "Drop" and _unset(p)
                     and digits(p.get("number"))]
        old_types = []
        if write_type:
            for p in ow.get("phones") or []:
                n = digits(p.get("number"))
                if n and phone_tier(p) and str(p.get("type") or "UNKNOWN").upper() == "UNKNOWN":
                    kind = line_kind((ledger.phone(n) or {}).get("line_type"))
                    if kind:
                        old_types.append((p, kind))
        older = old_drops + [p for p, _ in old_types if p not in old_drops]
        if not elig and not older:
            if ctx.live:
                ledger.set_fact(u, "scored", ctx.now.isoformat(timespec="seconds"))
            continue
        tiers = score_numbers(ctx, [digits(p["number"]) for p in elig], u) if elig else {}
        lit_tag = litigator_tag(ctx)
        payload = [phone_payload(p, (tiers.get(digits(p["number"])) or {}).get("tier", ""),
                                 [lit_tag] if (tiers.get(digits(p["number"])) or {}).get("litigator") else None)
                   for p in elig if (tiers.get(digits(p["number"])) or {}).get("tier") not in (None, "", "Unknown")]
        if drop_status or write_type:
            payload = _tier_rules(ctx, payload, elig, tiers, old_drops, old_types, drop_status, write_type)
        done += 1
        if any((tiers.get(digits(p["number"])) or {}).get("litigator") for p in elig):
            suppress_litigator(ctx, rec)
        if payload and ow.get("uuid"):
            ctx.change(u, "phones", [phone_payload(p, phone_tier(p)) for p in elig + older], [ph["number"] for ph in payload])
            ctx.write("tier phones", ctx.crm.upsert_phones, ow["uuid"], payload)
            if ctx.live and checked < 1:
                back = {digits(p.get("number")): p for p in ((ctx.crm.record(u).get("owner") or {}).get("phones") or [])}
                checked += 1
                for ph in payload:
                    got = [t for t in tag_titles(back.get(ph["number"]) or {}) if t in TIER_NAMES or t == LITIGATOR_TAG]
                    want = [t for t in ph["tags"] if t in TIER_NAMES or t == LITIGATOR_TAG]
                    if got != want:
                        ctx.out.problem("tier read-back mismatch on %s: wrote %s, read %s" % (addr(rec), want, got))
                        break
        if ctx.live:
            ledger.set_fact(u, "scored", ctx.now.isoformat(timespec="seconds"))
        if elig:
            ctx.out.line("%s: %d number(s)%s" % (addr(rec), len(elig), "" if ctx.live else " to score"))
        else:
            ctx.out.line("%s: %d older number(s) %s" % (addr(rec), len(older), "tidied (marked dead or given a line type)"
                                                        if ctx.live else "to tidy (mark dead or give a line type)"))
    ctx.out.acted = done
    n = ctx.out.tally.get("numbers scored", 0) or ctx.out.tally.get("numbers to score", 0)
    ctx.out.head = "%s %d number(s) on %d record(s)%s." % (
        "Scored" if ctx.live else "Would score", n, done,
        (", $%.2f" % ctx.out.usd) if ctx.live else (", about $%.2f" % ctx.out.would_usd))


# ---------------------------------------------------------------------- retrace (the waterfall)
def _handoff(ctx: Ctx, rec: dict, subj: Subject, why: str, death: bool = False) -> None:
    """Hand one record to Diggs for a deep prospect. `death` says the reason is a possible death,
    which no list rule may hold back (plan 1.17): every other reason follows research_by_list."""
    st = ctx.stage
    u = subj.record_uuid
    if ledger.handed_off(u, "deep_prospect"):
        return
    if not ctx.passes(rec, "research"):
        return
    ok, by = research_open(ctx, rec, death=death)
    if not ok:
        ctx.out.tally["research waits: %s" % by] += 1
        return
    d = ledger.day(ctx.now, ctx.policy.tz)
    today_n = sum(1 for e in ledger.events(action="handoff", stage=st.id)
                  if e["day"] == d and e["detail"].get("to") == "deep_prospect")
    cap = int(st.get("deep_prospect_per_day", 3))
    if today_n + ctx.out.tally.get("to Diggs for a deep prospect", 0) >= cap:
        ctx.out.tally["deep prospect waiting (cap %d/day)" % cap] += 1
        return
    enqueue = ctx.enqueue or store.enqueue
    tid = ctx.write("handoff", enqueue, "research", "deep_prospect", u, addr(rec), source="autopilot:%s" % st.id)
    if ctx.live:
        ledger.event(ctx.run_id, st.id, u, "handoff", {"to": "deep_prospect", "why": why,
                                                       "task": tid[0] if tid else 0}, now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["to Diggs for a deep prospect"] += 1
    if st.get("research_by_list"):
        ctx.out.tally["to research, by list: %s" % (by or ("a possible death" if death else "other"))] += 1
    ctx.out.line("%s: %s, to Diggs for a deep prospect" % (addr(rec), why))


def _after(ctx: Ctx, rec: dict, subj: Subject, why: str) -> None:
    after = ctx.stage.get("after_sources", "mail_only")
    if after == "deep_prospect":
        _handoff(ctx, rec, subj, why)
    elif after == "mail_only":
        tag = ctx.stage.get("mail_only_tag", "Mail Only")
        if tag in tag_titles(rec):
            return
        ctx.change(subj.record_uuid, "add_tags", None, [tag])
        ctx.write("mail only", ctx.crm.add_tags, subj.record_uuid, [tag], rec)
        if ctx.live:
            ledger.event(ctx.run_id, ctx.stage.id, subj.record_uuid, "after", {"to": "mail_only", "why": why},
                         now=ctx.now, tz=ctx.policy.tz)
        ctx.out.tally["moved to mail only"] += 1
        ctx.out.line("%s: %s, tagged %s" % (addr(rec), why, tag))
    else:
        ctx.out.tally["left in place (%s)" % why] += 1


def _to_principals(ctx: Ctx, rec: dict, subj: Subject) -> None:
    """A company or a trust owns the house (plan 1.15): hand the record to the principal finder
    (the free reverse-address check, then the business search) instead of parking it as Mail Only.
    A letter to "Acme Holdings LLC" reaches nobody who can sell. The record stays out of the paid
    trace and out of Mail Only until a person is named on it; the next run after that traces the
    person like any other owner."""
    st = ctx.stage
    u = subj.record_uuid
    if ledger.fact(u, "entity_pending") is None:
        # The autopilot's own note that this record waits on a named person, kept in plan mode too.
        ledger.set_fact(u, "entity_pending", {"company": subj.company or " ".join([subj.first, subj.last]).strip(),
                                              "since": ctx.now.isoformat(timespec="seconds")})
    if ledger.handed_off(u, "entity_principals"):
        ctx.out.tally["company owners waiting on a named person"] += 1
        return
    if not ctx.passes(rec, "research"):
        return
    d = ledger.day(ctx.now, ctx.policy.tz)
    today_n = sum(1 for e in ledger.events(action="handoff", stage=st.id)
                  if e["day"] == d and e["detail"].get("to") == "entity_principals")
    cap = int(st.get("principals_per_day", 10))
    if today_n + ctx.out.tally.get("to the principal finder", 0) >= cap:
        ctx.out.tally["principal finder waiting (cap %d/day)" % cap] += 1
        return
    enqueue = ctx.enqueue or store.enqueue
    tid = ctx.write("handoff", enqueue, "research", "entity_principals", u, addr(rec), source="autopilot:%s" % st.id)
    if ctx.live:
        ledger.event(ctx.run_id, st.id, u, "handoff", {"to": "entity_principals", "why": "company or trust owner",
                                                       "task": tid[0] if tid else 0}, now=ctx.now, tz=ctx.policy.tz)
    ctx.out.tally["to the principal finder"] += 1
    ctx.out.line("%s: a company or trust owns it, to the principal finder" % addr(rec))


def _fixup_open(now: datetime, stale_days: float = 14) -> dict:
    """record uuid -> its fix-up note, for every record waiting in the fix-up queue. A record no
    run has seen for two weeks has left the lane (sold, suppressed, out of the presets) and no
    longer holds one of the queue's places: without that, a hundred records that went away would
    block the queue for good."""
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, value FROM ap_facts WHERE key='fixup'").fetchall()
    out = {}
    for r in rows:
        try:
            v = json.loads(r["value"])
        except ValueError:
            continue
        if not (isinstance(v, dict) and v.get("since") and not v.get("fixed")):
            continue
        try:
            seen = datetime.fromisoformat(v.get("seen") or v["since"])
        except ValueError:
            continue
        if now - seen <= timedelta(days=stale_days):
            out[r["record_uuid"]] = v
    return out


def _fixup(ctx: Ctx, rec: dict, subj: Subject, missing: list[str], size: int, queue: dict, row: dict | None = None) -> None:
    """An incomplete record goes to the fix-up queue, never to a paid source (plan 1.13). A trace on
    half a name bills for a stranger's numbers, and a record with no mailing address cannot be
    matched at all. The queue is the autopilot's own memory first; it holds `size` records at a
    time so a person can clear it, and a tag is written only when the playbook names one."""
    u = subj.record_uuid
    why = " and ".join(missing)
    if u not in queue:
        if not ctx.passes(rec, "trace", row=row):
            return
        if len(queue) >= size:
            ctx.out.tally["incomplete, waiting for room in the fix-up queue (%d at a time)" % size] += 1
            return
        note = {"why": missing, "since": ctx.now.isoformat(timespec="seconds"), "seen": ctx.now.isoformat(timespec="seconds")}
        ledger.set_fact(u, "fixup", note)
        queue[u] = note
        ctx.out.line("%s: %s, to the fix-up queue before any paid trace" % (addr(rec), why))
    else:
        note = queue[u]
        try:
            fresh = ctx.now - datetime.fromisoformat(note.get("seen") or note["since"]) < timedelta(days=1)
        except ValueError:
            fresh = False
        if not fresh or note.get("why") != missing:
            # Still here and still incomplete: keep its place in the queue (see _fixup_open).
            note = dict(note, why=missing, seen=ctx.now.isoformat(timespec="seconds"))
            ledger.set_fact(u, "fixup", note)
            queue[u] = note
    ctx.out.tally["in the fix-up queue: %s" % why] += 1
    tag = str(ctx.stage.get("fixup_tag") or "")
    if tag and tag.lower() not in marks_of(rec):
        ctx.change(u, "add_tags", None, [tag])
        ctx.write("fix-up tag", ctx.crm.add_tags, u, [tag], rec)


def _fixup_done(ctx: Ctx, rec: dict, queue: dict) -> None:
    """A record that was in the fix-up queue and is complete now leaves it, and its tag comes off.
    The tag also comes off a complete record whose place in the queue had already lapsed, so the
    tag never outlives the reason for it."""
    u = rec.get("uuid") or ""
    was = queue.pop(u, None)
    if was is not None:
        ledger.set_fact(u, "fixup", dict(was, fixed=ctx.now.isoformat(timespec="seconds")))
        ctx.out.tally["fixed up, back in the trace"] += 1
    tag = str(ctx.stage.get("fixup_tag") or "")
    if tag and tag.lower() in marks_of(rec):
        ctx.change(u, "remove_tags", [tag], None)
        ctx.write("fix-up tag off", ctx.crm.remove_tags, u, [tag])


def _numbers_to_keep(ctx: Ctx, rec: dict, nums: list[dict], tiers: dict, written: dict | None = None) -> list[dict]:
    """Which of a source's new numbers land on the record (plan 1.13 and 1.14, decision D12).
    With `write_tiers` only those tiers land (a number that could not be scored is kept, since it
    cannot be judged); a `reserve_tiers` number lands only when the record has nothing better; the
    rest stay off the record, where no dialer preset can pick them up. With `keep_numbers` a person
    keeps at most that many usable numbers: what the record already has counts, and the best new
    ones fill the room that is left. A caller with nine numbers for one owner dials the first two
    and never reaches the rest, so the rest are noise. A litigator's number always lands, so the
    record carries the reason it was suppressed. A stage with neither setting writes every number,
    as before."""
    st = ctx.stage
    write = set(_list(st.get("write_tiers")))
    keep_n = int(st.get("keep_numbers") or 0)
    if not write and not keep_n:
        return nums
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()

    def tier_of(n: dict) -> str:
        return (tiers.get(n["number"]) or {}).get("tier", "") or ""

    lits = [n for n in nums if (tiers.get(n["number"]) or {}).get("litigator")]
    rest = [n for n in nums if n not in lits]
    if write:
        reserve = set(_list(st.get("reserve_tiers")))
        good = [n for n in rest if tier_of(n) in write or tier_of(n) in ("", "Unknown")]
        spare = [n for n in rest if tier_of(n) in reserve and n not in good]
        low = len(rest) - len(good) - len(spare)
        if spare and not good and not gate.usable_numbers(rec, "call", cfg):
            good = spare
        else:
            low += len(spare)
        if low:
            ctx.out.tally["low-tier numbers kept off the record"] += low
        rest = good
    if keep_n:
        on_record = {digits(p.get("number")) for p in ((rec.get("owner") or {}).get("phones") or [])}
        have = len(gate.usable_numbers(rec, "call", cfg))
        have += sum(1 for n, p in (written or {}).items() if n not in on_record and phone_tier(p) in cfg.callable_tiers)
        room = max(0, keep_n - have)
        if len(rest) > room:
            best = sorted(rest, key=lambda n: (TIER_RANK.get(tier_of(n), 5), 1 if str(n.get("type") or "").upper() == "LANDLINE" else 0))
            ctx.out.tally["numbers past the %d kept per person" % keep_n] += len(rest) - room
            rest = best[:room]
    return rest + lits


def _mark_agreed(ctx: Ctx, rec: dict, subj: Subject, nums: list[dict], source: str, written: dict) -> None:
    """A paid source returned a number the record already had: two sources agree, and that is the
    number to dial first (plan 1.13). The agreement is the autopilot's own fact first; the phone is
    tagged only when the playbook names the tag (`agreement_tag`)."""
    u = subj.record_uuid
    known = ledger.fact(u, "agreed_numbers") or {}
    fresh = False
    for n in nums:
        by = known.setdefault(n["number"], [])
        if source not in by:
            by.append(source)
            fresh = True
    if fresh:
        ledger.set_fact(u, "agreed_numbers", known)
    ctx.out.tally["numbers two sources agree on"] += len(nums)
    tag = str(ctx.stage.get("agreement_tag") or "")
    if not tag or not subj.owner_uuid:
        return
    on_record = {digits(p.get("number")): p for p in ((rec.get("owner") or {}).get("phones") or [])}
    before, payload = [], []
    for n in nums:
        p = written.get(n["number"]) or on_record.get(n["number"])
        if not p or tag in tag_titles(p):
            continue
        before.append(phone_payload(p, phone_tier(p)))
        payload.append(phone_payload(p, phone_tier(p), [tag]))
        written[n["number"]] = payload[-1]
    if payload:
        ctx.change(u, "phones", before, [ph["number"] for ph in payload])
        ctx.write("agreement tag", ctx.crm.upsert_phones, subj.owner_uuid, payload)


def agreed_numbers(record_uuid: str) -> set[str]:
    """The numbers on this record that two sources agree on (the autopilot's own fact)."""
    return set(ledger.fact(record_uuid, "agreed_numbers") or {})


def _write_numbers(ctx: Ctx, rec: dict, subj: Subject, nums: list[dict], source: str, written: dict | None = None) -> int:
    """Score, then add the new numbers once, tiered and tagged with their source. Returns how many
    are callable (the measure of a hit)."""
    tiers = score_numbers(ctx, [n["number"] for n in nums], subj.record_uuid)
    tag = {"smartskip": "SmartSkip"}.get(source, source.capitalize())
    reached = set(_list(ctx.stage.get("callable_tiers"))) or CALLABLE_TIERS
    lit_tag = litigator_tag(ctx)
    drop_status = str(ctx.stage.get("drop_status") or "").strip().upper()
    write_type = bool(ctx.stage.get("write_line_type", False))
    nums = _numbers_to_keep(ctx, rec, nums, tiers, written)
    phones, callable_n, litigator = [], 0, False
    for n in nums:
        res = tiers.get(n["number"]) or {}
        t = res.get("tier", "")
        is_lit = bool(res.get("litigator"))
        litigator = litigator or is_lit
        phones.append(phone_payload({"number": n["number"], "type": n.get("type") or "UNKNOWN", "tags": []}, t,
                                    [tag] + ([lit_tag] if is_lit else [])))
        if drop_status and t == "Drop" and not is_lit:
            phones[-1]["status"] = drop_status          # a Drop number is dead on arrival (plan 1.14)
        if write_type and phones[-1]["type"] == "UNKNOWN" and line_kind(res.get("line_type")):
            phones[-1]["type"] = line_kind(res.get("line_type"))
        # Unscored (no Trestle key, an Unknown score) cannot be judged, so it counts as a hit: the
        # waterfall must not keep paying the next source for a record that already has numbers.
        if not is_lit and (t in reached or t in ("", "Unknown")):
            callable_n += 1
    if written is not None:
        written.update({p["number"]: p for p in phones})
    if subj.owner_uuid and phones:
        ctx.change(subj.record_uuid, "add_numbers", None, [p["number"] for p in phones])
        ctx.write("add numbers", ctx.crm.upsert_phones, subj.owner_uuid, phones)
        if ctx.stage.get("count_as_traced", True):
            # Numbers from an outside source land through the phone route, which DataSift does not
            # count as a skip trace: the record keeps reading "never skip traced" and stays out of
            # every Ready to Call preset until the owner's counter says otherwise (plan 0.10).
            total = int((rec.get("owner") or {}).get("skiptrace_attempts") or 0) + 1
            ctx.change(subj.record_uuid, "skiptrace_attempts", total - 1, total)
            ctx.write("skip trace counter", ctx.crm.skiptrace_attempts, subj.owner_uuid, total)
    if litigator:
        suppress_litigator(ctx, rec)
    return callable_n


def _stack_done(note, owed: list, now: datetime) -> bool:
    """Is the note "reachable, and every stacked source already tried" still good? Only for the
    same stacked sources (a source added to the stack is owed at once) and only for a week, timed
    against the run's own clock so a plan run and a test read it the way a live run does."""
    if not isinstance(note, dict) or list(note.get("sources") or []) != list(owed):
        return False
    try:
        return now - datetime.fromisoformat(str(note.get("at") or "")) < STACK_DONE_TTL
    except (TypeError, ValueError):
        return False


def run_retrace(ctx: Ctx) -> None:
    st = ctx.stage
    rows = stage_rows(ctx, extra=required_tags(ctx), cap=int(st.get("max_scan", 500)))
    ctx.out.found = len(rows)
    order = [s for s in _list(st.get("sources"))]
    max_records = int(st.get("max_records", 50))
    deceased_to = st.get("deceased", "deep_prospect")
    entity_to = st.get("entity", "after")
    new_tag = st.get("new_numbers_tag", "")
    # The second version of the waterfall (plan 1.13, decision D11): the first `stack` sources run
    # on EVERY record, with or without a number, because a number two sources agree on is the one
    # to dial first; the sources after them run only on a record still without a callable number.
    stack = max(0, int(st.get("every_record_sources") or 0))
    done_key = "stack_done:%s" % st.id
    fixup_size = max(0, int(st.get("fixup_queue") or 0))
    fixups = _fixup_open(ctx.now) if fixup_size else {}
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    queue: dict[str, dict] = {}
    for _, row in rows:
        if len(queue) + ctx.out.tally.get("to Diggs for a deep prospect", 0) >= max_records:
            ctx.out.tally["left for the next run"] += 1
            continue
        u = row["uuid"]
        if status_of(row) in DEAD_STATUSES:
            continue
        if stack and _stack_done(ledger.fact(u, done_key), order[:stack], ctx.now):
            # Known to be reachable with every stacked source behind it: not read again this week.
            # Stacking puts the call presets in front of this stage, and without the note every
            # record in them would be opened on every run only to be told there is nothing to do.
            ctx.out.tally["already has a callable number and every stacked source"] += 1
            continue
        rec = ctx.crm.record(u)
        if status_of(rec) in DEAD_STATUSES:
            continue
        subj = subject_of(rec)
        if subj.kind == "entity" and entity_to == "principals":
            from ..workflows.entity_principals import as_principal_record
            named = as_principal_record(rec)
            if named is not None:
                rec, subj = named, subject_of(named)   # plan 1.15: the named person is traced; numbers land on the company's owner
        if subj.kind in ("deceased", "probate") and deceased_to == "deep_prospect":
            _handoff(ctx, rec, subj, "%s owner" % subj.kind, death=True)
            continue
        if fixup_size and not subj.org:
            # The clean-record check, before anything is paid for. A company is not incomplete, it
            # is a company: it goes to the principal finder below.
            missing = missing_for_trace(subj)
            if missing:
                _fixup(ctx, rec, subj, missing, fixup_size, fixups, row)
                continue
            _fixup_done(ctx, rec, fixups)
        if subj.kind == "entity":
            if entity_to == "principals":
                _to_principals(ctx, rec, subj)
            elif entity_to == "deep_prospect":
                _handoff(ctx, rec, subj, "entity owner, needs its principal")
            else:
                ctx.out.tally["entity owners (need a principal)"] += 1
                if ctx.live:
                    _after(ctx, rec, subj, "entity owner, no person to trace")
            continue
        v = ctx.verdict(rec, "trace", row=row, allow={"deceased"} if deceased_to == "retrace" else None)
        if not v.ok:
            if v.code == "deceased" and deceased_to == "deep_prospect":
                # The gate reads death from more places than the owner flag (the Obituary list, the
                # Owner Deceased field), and a possible death goes to research, never to a blind trace.
                _handoff(ctx, rec, subj, "the owner may have died (%s)" % v.reasons[0][1], death=True)
            else:
                ctx.out.tally["held back: %s" % v.reason()] += 1
            continue
        rem = [s for s in order if s not in ledger.tried_sources(u)]
        reached = bool(stack) and bool(gate.usable_numbers(rec, "call", gate_cfg, row))
        if reached:
            # Already reachable: only the sources that run on every record are still owed.
            rem = [s for s in rem if order.index(s) < stack]
            if not rem:
                ledger.set_fact(u, done_key, {"at": ctx.now.isoformat(timespec="seconds"), "sources": order[:stack]})
                ctx.out.tally["already has a callable number and every stacked source"] += 1
                continue
        if not rem:
            if ctx.live:
                _after(ctx, rec, subj, "every source already tried")
            else:
                ctx.out.tally["every source already tried"] += 1
            continue
        queue[u] = {"rec": rec, "subj": subj, "rem": rem, "hit": False, "reached": reached, "written": {}}
    plan_round = 0
    for _ in range(len(order)):
        groups: dict[str, list[str]] = defaultdict(list)
        for u, q in queue.items():
            if q["rem"] and (not q["hit"] or order.index(q["rem"][0]) < stack):
                groups[q["rem"][0]].append(u)
        if not groups:
            break
        for name, us in groups.items():
            src = ctx.sources.get(name)
            if not src or not src.configured():
                ctx.out.problem("source %s has no key on this box, skipped" % name)
                for u in us:
                    queue[u]["rem"].pop(0)
                continue
            allowed, capped = [], ""
            for u in us:
                ok, why = ctx.budget(src.unit_usd * (len(allowed) + 1), u, record_usd=src.unit_usd)
                if not ok:
                    if why.startswith("record cap"):
                        ctx.out.tally["at the per-record cap"] += 1
                        queue[u]["rem"] = []
                        continue
                    capped = why
                    break
                allowed.append(u)
            if capped:
                left = [u for u in us if u not in allowed and queue[u]["rem"]]
                ctx.out.tally["not traced today (%s)" % capped] += len(left)
                for u in left:
                    queue[u]["rem"] = []
                    queue[u]["capped"] = True
            if not allowed:
                continue
            worst = round(src.unit_usd * len(allowed), 2)
            ctx.reserve(worst)
            if not ctx.live:
                ctx.out.line("%s %d record(s) via %s (up to $%.2f)" % (
                    "would trace" if plan_round == 0 else "misses would then go to", len(allowed), name, worst))
                ctx.out.tally["traces planned via %s" % name] += len(allowed)
                ctx.settle(worst, 0, name)
                for u in allowed:
                    queue[u]["rem"].pop(0)
                continue
            subjects = [queue[u]["subj"] for u in allowed]
            try:
                found, billed = src.trace(subjects)
            except Exception as exc:  # noqa: BLE001
                ctx.settle(worst, 0, name)
                ctx.out.problem("%s failed, nothing written: %s" % (name, str(exc)[:200]))
                for u in allowed:
                    queue[u]["rem"].pop(0)
                    queue[u]["failed"] = True
                continue
            ctx.settle(worst, billed, "%s x%d" % (name, len(subjects)))
            per = (billed / len(subjects)) if subjects else 0.0
            costs = getattr(src, "costs", None) or {}
            for u in allowed:
                q = queue[u]
                rec, subj = q["rec"], q["subj"]
                # A number an earlier source returned and the rules kept off the record is neither
                # new nor agreed: agreement is only ever said about a number the record holds.
                left_off = q.setdefault("left_off", set())
                nums = [n for n in found.get(u, []) if n["number"] not in subj.existing and n["number"] not in left_off]
                same = [n for n in found.get(u, []) if n["number"] in subj.existing]
                usd_u = costs.get(u, 0.0) if (costs and src.per == "match") else per
                ledger.event(ctx.run_id, st.id, u, "trace", {"source": name, "found": len(found.get(u, [])),
                                                            "new": len(nums)}, usd=round(usd_u, 4), now=ctx.now,
                             tz=ctx.policy.tz)
                q["rem"].pop(0)
                if same:
                    _mark_agreed(ctx, rec, subj, same, name, q["written"])
                if not nums:
                    continue
                good = _write_numbers(ctx, rec, subj, nums, name, q["written"])
                landed = {n["number"] for n in nums if (digits(n["number"]) or n["number"]) in q["written"]}
                subj.existing |= landed
                left_off |= {n["number"] for n in nums} - landed
                if good:
                    ctx.out.tally["found callable numbers via %s" % name] += 1
                    ctx.out.line("%s: %d new number(s) via %s, %d callable" % (addr(rec), len(nums), name, good))
                    if new_tag and subj.attempts > 0:
                        ctx.write("new numbers tag", ctx.crm.add_tags, u, [new_tag], None)
                        ledger.set_fact(u, "new_numbers_at_attempts", subj.attempts)
                    q["hit"] = True
        plan_round += 1
    # A record that already had a callable number is not a miss when a stacked source adds nothing.
    misses = [q for q in queue.values() if not q["hit"] and not q["rem"] and not q.get("capped") and not q.get("failed")
              and not q.get("reached")]
    if ctx.live:
        for q in misses:
            _after(ctx, q["rec"], q["subj"], "no source found a callable number")
    elif misses:
        ctx.out.tally["if every source misses: %s" % st.get("after_sources", "mail_only")] += len(misses)
    hits = sum(1 for q in queue.values() if q["hit"])
    if st.get("lookback_days"):
        _trace_lookback(ctx)
    ctx.out.acted = (hits + ctx.out.tally.get("to Diggs for a deep prospect", 0) + ctx.out.tally.get("moved to mail only", 0)
                     + ctx.out.tally.get("to the principal finder", 0))
    if ctx.live:
        ctx.out.head = "Retraced %d record(s): %d found callable numbers, %d to Diggs, %d to mail only, $%.2f." % (
            len(queue), hits, ctx.out.tally.get("to Diggs for a deep prospect", 0), ctx.out.tally.get("moved to mail only", 0),
            ctx.out.usd)
    else:
        ctx.out.head = "Would retrace %d record(s) (up to $%.2f with scoring), %d to Diggs, %d entity owners." % (
            len(queue), ctx.out.would_usd, ctx.out.tally.get("to Diggs for a deep prospect", 0),
            ctx.out.tally.get("entity owners (need a principal)", 0))
    extra = []
    finder = ctx.out.tally.get("to the principal finder", 0)
    if finder:
        extra.append("%d company owner(s) %s the principal finder" % (finder, "went to" if ctx.live else "would go to"))
    waiting = sum(n for k, n in ctx.out.tally.items() if k.startswith("in the fix-up queue"))
    if waiting:
        extra.append("%d incomplete record(s) wait in the fix-up queue and were not traced" % waiting)
    if extra:
        ctx.out.head += " " + "; ".join(extra) + "."


def _trace_lookback(ctx: Ctx) -> None:
    """The 14-day look-back (plan 1.13): judge each trace pass by what the callers found, not by
    what the source returned. A trace "succeeds" the day it lands numbers; whether any of them is
    the owner's is only known once somebody has dialed or texted them. So two weeks after each
    pass the record is read once and the pass is scored: did it have a number, and did a person or
    a dialer confirm one as correct. Each (record, source) is judged once. The first trace is
    DataSift's own, and when it finds numbers on under nine records in ten the data is the
    problem, not the callers."""
    st = ctx.stage
    days = float(st.get("lookback_days"))
    hi = ctx.now - timedelta(days=days)
    lo = hi - timedelta(days=float(st.get("lookback_window_days", 7)))
    max_reads = int(st.get("lookback_max_reads", 60))
    due: dict[tuple, None] = {}
    for action in ("native_trace", REFRESH_ACTION, "trace"):
        for e in ledger.events(action=action, since=lo):
            if e["record_uuid"] and datetime.fromisoformat(e["at"]) <= hi:
                due[(e["record_uuid"], e["detail"].get("source") or "datasift")] = None
    key = "autopilot:lookback:%s" % st.id
    try:
        totals = json.loads(store.kv_get(key) or "{}")
    except ValueError:
        totals = {}
    reads = 0
    for u, source in due:
        seen = ledger.fact(u, "trace_judged") or {}
        if source in seen:
            continue
        if reads >= max_reads:
            ctx.out.tally["trace passes not judged yet (read cap %d)" % max_reads] += 1
            continue
        reads += 1
        phones = (ctx.crm.record(u).get("owner") or {}).get("phones") or []
        res = {"numbers": bool(phones), "at": ctx.now.isoformat(timespec="seconds"),
               "correct": any(str(p.get("status") or "").upper() in ("CORRECT", "CORRECT_DNC") for p in phones)}
        seen[source] = res
        ledger.set_fact(u, "trace_judged", seen)          # what the look-back found: knowledge, kept in plan mode too
        t = totals.setdefault(source, {"judged": 0, "numbers": 0, "correct": 0})
        t["judged"] += 1
        t["numbers"] += int(res["numbers"])
        t["correct"] += int(res["correct"])
    if reads:
        store.kv_set(key, json.dumps(totals))
    for source, t in sorted(totals.items()):
        if t.get("judged"):
            ctx.out.line("%s, %d days on: %d of %d record(s) have a number, %d a confirmed correct one (%d%%)" % (
                source, days, t["numbers"], t["judged"], t["correct"], round(100.0 * t["correct"] / t["judged"])))
            ctx.out.tally["%s: share with a confirmed correct number, %d days on (percent)" % (source, days)] = round(
                100.0 * t["correct"] / t["judged"])
    first = totals.get("datasift") or {}
    floor = float(st.get("first_trace_floor", 0.9))
    if first.get("judged", 0) >= int(st.get("lookback_min_records", 20)) and first["numbers"] / first["judged"] < floor:
        ctx.out.problem("DataSift's own trace found a number on %d%% of %d record(s), under the %d%% it should; check the "
                        "trace before paying for more passes" % (round(100.0 * first["numbers"] / first["judged"]),
                                                                 first["judged"], round(100 * floor)))


# ---------------------------------------------------------------------- auction
def _custom_dates(ctx: Ctx, uuid: str, labels: list[str]) -> dict:
    r = ctx.crm.call("/api/internal/property/%s/custom-field/" % uuid)
    items = r if isinstance(r, list) else ((r or {}).get("results") or (r or {}).get("data") or [])
    want = {x.lower() for x in labels}
    out = {}
    for it in items:
        cf = it.get("custom_field") if isinstance(it.get("custom_field"), dict) else {}
        label = (cf.get("label") or cf.get("name") or cf.get("title") or "").strip()
        if label.lower() in want and it.get("value"):
            out[label] = str(it["value"])[:10]
    return out


def _as_date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def run_auction(ctx: Ctx) -> None:
    st = ctx.stage
    tag = st.get("tag", "Auction Soon")
    soon = int(st.get("soon_days", 21))
    fields = _list(st.get("date_fields")) or ["auction_date", "tax_auction_date"]
    custom = _list(st.get("custom_date_fields")) or ["Foreclosure Date"]
    trust_builtin_fc = bool(st.get("trust_builtin_foreclosure_date", False))
    refresh = timedelta(days=float(st.get("refresh_days", 3)))
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    tag_uuid = ctx.crm.tag_uuid(tag)
    if not tag_uuid:
        ctx.out.problem("there is no %r tag in this account; create it (the Hot Auction preset keys on it)" % tag)
    rows = stage_rows(ctx) if ctx.stage.presets else []
    cands = {r["uuid"]: r for _, r in rows}
    scope = _list(st.get("scope_tags"))
    if scope:
        ids = [ctx.crm.tag_uuid(t) for t in scope]
        if all(ids):
            # A record already in negotiation still has its sale date: the scope is the tag, not a
            # calling preset (those drop every record with a lead status).
            for r in ctx.crm.rows({"all_tags": ids, "must_not": {"any_property_status": GONE_STATUSES}},
                                  cap=int(st.get("max_scan", 3000))):
                cands.setdefault(r["uuid"], r)
        else:
            ctx.out.problem("scope_tags %s: one of those tags does not exist" % scope)
    tagged = {r["uuid"]: r for r in ctx.crm.rows({"all_tags": [tag_uuid]}, cap=2000)} if tag_uuid else {}
    for u, r in tagged.items():
        cands.setdefault(u, r)
    # A second, tighter tier (plan 1.8): hot inside soon_days, critical inside critical_days. The
    # critical tag is what a priority package or a door-knock route keys on.
    crit_tag = str(st.get("critical_tag") or "")
    crit_days = int(st.get("critical_days", 14))
    crit_uuid = ctx.crm.tag_uuid(crit_tag) if crit_tag else ""
    if crit_tag and not crit_uuid:
        ctx.out.problem("there is no %r tag in this account yet; create it and the critical tier starts" % crit_tag)
    crit_tagged = {r["uuid"] for r in ctx.crm.rows({"all_tags": [crit_uuid]}, cap=2000)} if crit_uuid else set()
    # The day after the sale every channel stops (decision D39): the record takes this status, which
    # the gate treats as finished. Unset, the stage only untags.
    after_status = str(st.get("after_sale_status") or "")
    after_window = int(st.get("after_sale_window_days", 60))
    ctx.out.found = len(cands)
    hot = []
    # Reading a sale date costs two requests a record (the record, then its custom fields), so one
    # run reads at most max_reads of them: records never read come first, then the stalest. The rest
    # are judged on the date already known, and a record whose date is not known yet waits a run.
    max_reads = int(st.get("max_reads", 400))
    reads = 0
    known = {u: ledger.fact(u, "auction_dates") for u in cands}
    order = sorted(cands, key=lambda u: (known[u] is not None, u in tagged))
    for u in order:
        row = cands[u]
        dates = ledger.fact(u, "auction_dates", max_age=refresh) if known[u] is not None else None
        if dates is None:
            if reads >= max_reads:
                if known[u] is None:
                    ctx.out.tally["sale date not read yet (read cap, next run)"] += 1
                    continue
                dates = known[u]
            else:
                reads += 1
                rec = ctx.crm.record(u)
                dates = {f: rec.get(f) for f in fields if rec.get(f)}
                if trust_builtin_fc and rec.get("foreclosure_date"):
                    dates["foreclosure_date"] = rec["foreclosure_date"]
                dates.update(_custom_dates(ctx, u, custom))
                ledger.set_fact(u, "auction_dates", dates)
        upcoming = sorted(d for d in (_as_date(v) for v in dates.values()) if d and d >= today)
        nxt = upcoming[0] if upcoming else None
        want = nxt is not None and (nxt - today).days <= soon
        has = u in tagged
        if want and not has:
            ctx.change(u, "add_tags", None, [tag])
            ctx.write("tag auction", ctx.crm.add_tags, u, [tag], None)
            ctx.out.tally["tagged %s" % tag] += 1
        elif has and not want:
            ctx.change(u, "remove_tags", [tag], None)
            ctx.write("untag auction", ctx.crm.remove_tags, u, [tag])
            ctx.out.tally["untagged (date passed or moved out)"] += 1
        if want:
            hot.append((nxt, row))
        if ctx.live and (want != has):
            ledger.event(ctx.run_id, st.id, u, "auction_tag", {"add": want, "date": str(nxt) if nxt else ""},
                         now=ctx.now, tz=ctx.policy.tz)
        if crit_uuid:
            want_c = nxt is not None and (nxt - today).days <= crit_days
            has_c = u in crit_tagged
            if want_c and not has_c:
                ctx.change(u, "add_tags", None, [crit_tag])
                ctx.write("tag critical", ctx.crm.add_tags, u, [crit_tag], None)
                ctx.out.tally["tagged %s" % crit_tag] += 1
            elif has_c and not want_c:
                ctx.change(u, "remove_tags", [crit_tag], None)
                ctx.write("untag critical", ctx.crm.remove_tags, u, [crit_tag])
                ctx.out.tally["untagged %s" % crit_tag] += 1
        if after_status and nxt is None:
            past = sorted(d for d in (_as_date(v) for v in dates.values()) if d and d < today)
            cur = status_of(row)
            # A record the 45-day deed check brought back (the borrower still owns the house) keeps
            # its past sale date, and must not be closed out a second time for that same sale.
            kept = ledger.fact(u, "deed_check") or {}
            if past and kept.get("verdict") == "still_owned" and kept.get("sale") == str(past[-1]):
                ctx.out.tally["sale passed, borrower still owns it: left in the lane"] += 1
            elif past and 1 <= (today - past[-1]).days <= after_window and cur in ("", "prospecting", "exhausted"):
                ctx.change(u, "status", cur or None, after_status)
                ctx.write("after sale", ctx.crm.set_status, u, after_status)
                ctx.out.tally["sale date passed: set %s" % after_status] += 1
                if ctx.live:
                    ledger.event(ctx.run_id, st.id, u, "after_sale", {"date": str(past[-1]), "status": after_status},
                                 now=ctx.now, tz=ctx.policy.tz)
    hot.sort(key=lambda x: x[0])
    for d, row in hot:
        ctx.out.line("%s (%d days): %s" % (d.strftime("%b %d"), (d - today).days, addr(row)))
    ctx.out.acted = sum(n for k, n in ctx.out.tally.items() if k.startswith(("tagged ", "untagged", "sale date passed")))
    critical = sum(1 for d, _ in hot if (d - today).days <= crit_days) if crit_tag else 0
    ctx.out.head = "%d sale(s) inside %d days%s%s." % (
        len(hot), soon, (", %d of them inside %d" % (critical, crit_days)) if crit_tag else "",
        "" if not ctx.out.acted else ", %s %d change(s)" % ("made" if ctx.live else "would make", ctx.out.acted))


# ---------------------------------------------------------------------- reactivate
def run_reactivate(ctx: Ctx) -> None:
    st = ctx.stage
    people = resolve_callers(ctx)
    rows = stage_rows(ctx)
    ctx.out.found = len(rows)
    active = {p["uuid"] for p in people}
    by_preset: Counter = Counter(t for t, _ in rows)
    moved = 0
    if st.get("assign", True) and people:
        load = {p["uuid"]: ctx.crm.count({"assigned_to": p["uuid"], "must_not": {"any_property_status": WORKLOAD_EXCLUDE}})
                for p in people}
        for title, row in rows[: int(st.get("max_per_run", 100))]:
            u = row["uuid"]
            if ledger.events(u, "reactivate", since=ctx.now - timedelta(days=float(st.get("every_days", 30)))):
                continue
            rec = ctx.crm.record(u)
            cur = assignee_of(rec)
            if cur in active:
                continue
            # The second-chance lane is the one place a Not Interested record may be touched.
            if not ctx.passes(rec, "assign", row=row, allow={"not_interested"}):
                continue
            who = balanced_pick(people, load)
            ctx.change(u, "assign", cur, who["uuid"])
            ctx.write("reassign", ctx.crm.assign, u, who["uuid"])
            if ctx.live:
                ledger.event(ctx.run_id, st.id, u, "reactivate", {"to": who["name"], "preset": title}, now=ctx.now,
                             tz=ctx.policy.tz)
            load[who["uuid"]] += 1
            moved += 1
            ctx.out.line("%s -> %s (%s)" % (addr(row), who["name"], title))
    ctx.out.acted = moved
    ctx.out.head = "%d record(s) are back from Not Interested (%s); %s %d to a caller." % (
        len(rows), ", ".join("%s %d" % kv for kv in by_preset.most_common()) or "none",
        "handed" if ctx.live else "would hand", moved)


# ---------------------------------------------------------------------- handoff
def run_handoff(ctx: Ctx) -> None:
    """Records in the stage's presets go to a teammate's workflow, a few a day, each record once.
    The workflow carries its own caps (a deep prospect spends inside AGENTS_RUN_CAP_USD and runs dry
    on a dry box), so this stage only decides who is next."""
    st = ctx.stage
    to, agent = str(st.get("workflow")), str(st.get("agent"))
    per_day = int(st.get("per_day", 3))
    rows = stage_rows(ctx, extra=required_tags(ctx))
    # Returned mail is recorded three ways (plan 1.7); a stage that names them reads all three.
    signals: dict[str, str] = {}
    if st.get("return_signals"):
        rows, signals = _return_rows(ctx, rows)
    ctx.out.found = len(rows)
    d = ledger.day(ctx.now, ctx.policy.tz)
    today_n = sum(1 for e in ledger.events(action="handoff", stage=st.id) if e["day"] == d)
    enqueue = ctx.enqueue or store.enqueue
    by_list = bool(st.get("research_by_list"))
    max_reads = int(st.get("max_reads", 100))
    reads = 0
    sent = 0
    for _, row in rows:
        u = row["uuid"]
        if status_of(row) in DEAD_STATUSES or ledger.handed_off(u, to):
            continue
        if today_n + sent >= per_day:
            ctx.out.tally["waiting for a later day (%d a day)" % per_day] += 1
            continue
        if by_list:
            # A list rule can leave a record waiting for weeks (a lien, until it is exhausted), so
            # the answer is remembered for a day and the reads are capped: a stage must stop reading
            # when it can no longer act.
            if ledger.fact(u, "research_wait", max_age=timedelta(hours=20)) is not None:
                ctx.out.tally["research waits (checked earlier today)"] += 1
                continue
            if reads >= max_reads:
                ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
                continue
            reads += 1
        rec = ctx.crm.record(u)
        if not ctx.passes(rec, "research", row=row):
            continue
        by, death = "", False
        if by_list:
            # A possible death is never held by a list rule, whichever stage sees the record first.
            gcfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
            marks = {str(t).lower() for t in list(getattr(gcfg, "deceased_tags", []) or []) + list(getattr(gcfg, "deceased_lists", []) or [])}
            death = subject_of(rec).kind in ("deceased", "probate") or bool(marks_of(rec) & marks)
            ok, by = research_open(ctx, rec, death=death)
            if not ok:
                ledger.set_fact(u, "research_wait", by)     # the autopilot's own note, kept in plan mode too
                ctx.out.tally["research waits: %s" % by] += 1
                continue
        tid = ctx.write("handoff", enqueue, agent, to, u, addr(row), source="autopilot:%s" % st.id)
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, "handoff", {"to": to, "task": tid[0] if tid else 0}, now=ctx.now,
                         tz=ctx.policy.tz)
        sent += 1
        if by_list:
            ctx.out.tally["to research, by list: %s" % (by or ("a possible death" if death else "other"))] += 1
        if u in signals:
            ctx.out.tally["mail came back: %s" % signals[u]] += 1
            ctx.out.line("%s -> %s (mail came back: %s)" % (addr(row), to.replace("_", " "), signals[u]))
        else:
            ctx.out.line("%s -> %s" % (addr(row), to.replace("_", " ")))
    if st.get("returns_scan"):
        _returns_scan_task(ctx)
    ctx.out.acted = sent
    ctx.out.head = "%s %d record(s) to %s; %d in the presets%s." % (
        "Handed" if ctx.live else "Would hand", sent, to.replace("_", " "), len(rows),
        " and the return signals" if st.get("return_signals") else "")


RETURN_LABELS = {"status": "the Return Mail status", "tag": "a return tag", "flag": "the do-not-mail flag"}


def _flag_is_return(ctx: Ctx, row: dict, seen: dict) -> bool:
    """A do-not-mail flag cannot say whether mail came back or the owner asked not to be mailed
    (plan 1.7). The two are told apart by a tag written at the time: an opt-out carries its own tag
    (or Do Not Market, or the owner's opt-out flag), and a flag with none of those is a return,
    which is marked with the return tag the day it is first seen so nobody has to guess later."""
    st = ctx.stage
    u = row["uuid"]
    known = ledger.fact(u, "mail_flag")
    if known == "opt_out":
        ctx.out.tally["do-not-mail set by an opt-out, not a return"] += 1
        return False
    if known == "return":
        return True
    if seen["reads"] >= int(st.get("max_reads", 100)):
        ctx.out.tally["do-not-mail flags not read this run (read cap)"] += 1
        return False
    seen["reads"] += 1
    rec = ctx.crm.record(u)
    names = marks_of(rec)
    ow = rec.get("owner") or {}
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    opt_tags = {str(t).strip().lower() for t in (_list(st.get("opt_out_tags")) or ["Mail Opt Out", "Do Not Mail"])}
    opt_tags |= {t.lower() for t in cfg.do_not_market_tags}
    if names & opt_tags or ow.get("opt_out") or ow.get("dnc"):
        ledger.set_fact(u, "mail_flag", "opt_out")          # what the record says: knowledge, kept in plan mode too
        ctx.out.tally["do-not-mail set by an opt-out, not a return"] += 1
        return False
    if st.get("flag_requires_mailed", False) and not ledger.events(u, "mail"):
        ctx.out.tally["do-not-mail on a record the autopilot never mailed (left alone)"] += 1
        return False
    tag = str(st.get("return_tag", "Return Mail") or "")
    if tag and tag.lower() not in names:
        ctx.change(u, "add_tags", None, [tag])
        ctx.write("return mail tag", ctx.crm.add_tags, u, [tag], rec)
        ctx.out.tally["returns marked with the %s tag" % tag] += 1
    if ctx.live:
        ledger.set_fact(u, "mail_flag", "return")
    return True


def _return_rows(ctx: Ctx, rows: list) -> tuple[list, dict]:
    """The stage's preset rows plus every record whose mail came back, however that was recorded
    (plan 1.7): the Return Mail status somebody set by hand, a return tag an outside mail house
    wrote, or the do-not-mail flag DataSift's own mail sets on a return. Returns (rows, record uuid
    -> how the return was recorded). The flag is looked for on one lane's records, never the whole
    account, and it rides on the search row, so finding it costs no record read."""
    st = ctx.stage
    sig = st.get("return_signals")
    if not isinstance(sig, dict):
        ctx.out.problem("return_signals must be a mapping (statuses, tags, flag, flag_scope_tags)")
        return rows, {}
    cap = int(st.get("max_scan", 2000))
    extra = required_tags(ctx) or {}
    have = {r["uuid"] for _, r in rows}
    out, how = list(rows), {}

    def add(row: dict, kind: str) -> None:
        u = row.get("uuid")
        if u and u not in have:
            have.add(u)
            out.append((RETURN_LABELS[kind], row))
            how[u] = RETURN_LABELS[kind]

    statuses = [str(s) for s in _list(sig.get("statuses"))]
    if statuses:
        for row in ctx.crm.rows(merge_must({"any_property_status": statuses}, extra), cap=cap):
            add(row, "status")
    tag_ids = []
    for t in _list(sig.get("tags")):
        tid = ctx.crm.tag_uuid(str(t))
        if tid:
            tag_ids.append(tid)
        else:
            ctx.out.tally["no tag named %s in this account yet" % t] = 1
    if tag_ids:
        for row in ctx.crm.rows(merge_must({"any_tags": tag_ids}, extra), cap=cap):
            add(row, "tag")
    if sig.get("flag"):
        scope = [ctx.crm.tag_uuid(str(t)) for t in (_list(sig.get("flag_scope_tags")) or _list(st.get("require_tags")))]
        if not scope or not all(scope):
            ctx.out.problem("return_signals.flag needs flag_scope_tags (or require_tags) that exist in this account, "
                            "so the do-not-mail flag is looked for on one lane and not on every record")
        else:
            seen = {"reads": 0}
            for row in ctx.crm.rows({"all_tags": scope, "must_not": {"any_property_status": GONE_STATUSES}}, cap=cap):
                flagged = row.get("do_not_mail_ever") or row.get("owner_do_not_mail_ever")
                if flagged and row.get("uuid") not in have and _flag_is_return(ctx, row, seen):
                    add(row, "flag")
    return out, how


def _returns_scan_task(ctx: Ctx) -> None:
    """Handwritten letters carry no tracking, so nothing in DataSift ever says one came back
    (plan 1.7). Once a week a person goes through the returned envelopes and marks each record;
    this creates that task, once per week, for whoever the playbook names."""
    st = ctx.stage
    cfg = st.get("returns_scan")
    cfg = cfg if isinstance(cfg, dict) else {}
    local = ctx.now.astimezone(ZoneInfo(ctx.policy.tz))
    week = "%d-W%02d" % (local.isocalendar()[0], local.isocalendar()[1])
    key = "autopilot:returns_scan:%s" % st.id
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    start = str(cfg.get("weekday", "mon")).lower()[:3]
    if store.kv_get(key) == week or local.weekday() < (days.index(start) if start in days else 0):
        return
    who = cfg.get("assignee")
    people = resolve_callers(ctx, [who]) if who else []
    if not people:
        ctx.out.line("The weekly scan of returned handwritten letters has nobody named yet (returns_scan.assignee).")
        return
    from . import laneroutes
    title = str(cfg.get("title") or "Go through this week's returned letters")
    notes = str(cfg.get("notes") or "Handwritten letters carry no tracking. For each envelope that came back, set the "
                                    "record's status to Return Mail so it goes to research and stops being mailed.")
    ctx.write("returns scan task", laneroutes.create_task, ctx.crm, title,
              laneroutes.end_of_day(local.date(), ctx.policy.tz), notes, people[0]["uuid"])
    ctx.out.line("%s the weekly returned-letters task for %s." % ("Created" if ctx.live else "Would create", people[0]["name"]))
    ctx.out.tally["weekly returned-letters task"] += 1
    if ctx.live:
        store.kv_set(key, week)
        ledger.event(ctx.run_id, st.id, "", "returns_scan", {"to": people[0]["name"], "week": week}, now=ctx.now,
                     tz=ctx.policy.tz)


# ---------------------------------------------------------------------- digest
def run_digest(ctx: Ctx) -> None:
    st = ctx.stage
    counts = {}
    for title in st.presets:
        try:
            must, _ = windows.resolve(ctx.crm.preset_must(title), ctx.now)
            counts[title] = ctx.crm.count(must)
        except KeyError as exc:
            ctx.out.problem(str(exc))
    prev = {}
    raw = store.kv_get("autopilot:digest:counts")
    if raw:
        try:
            prev = json.loads(raw)
        except ValueError:
            prev = {}
    if st.get("remember", True):
        store.kv_set("autopilot:digest:counts", json.dumps(counts))
    since = ctx.now - timedelta(hours=24)
    acts = ledger.counts_since(since)
    spent = sum(v[1] for v in acts.values())
    by = Counter()
    for (stage, action), (n, _) in acts.items():
        by[action] += n
    staged = ledger.batches(status="staged")
    ctx.out.found = sum(counts.values())
    parts = []
    for k, label in (("assign", "assigned"), ("trace", "retraced"), ("handoff", "to Diggs"),
                     ("auction_tag", "auction tags"), ("mail", "mailed"), ("score", "scored"), ("text", "texted")):
        if by.get(k):
            parts.append("%d %s" % (by[k], label))
    ctx.out.head = "Last 24h: %s, $%.2f." % (", ".join(parts) or "nothing moved", spent)
    for title, n in counts.items():
        delta = n - prev[title] if title in prev else None
        ctx.out.line("%s: %d%s" % (title, n, "" if delta in (None, 0) else " (%+d)" % delta))
    if staged:
        from .. import release as release_mod
        ctx.out.line("Waiting on a person: %s" % ", ".join("batch %d (%d %s, $%.2f)" % (
            b["id"], b["count"], release_mod.label(b["kind"]), b["usd"]) for b in staged[:4]))
    try:
        from . import budget, capture
        for ln in capture.recent_lines(1)[:4]:
            ctx.out.line(ln)
        use = budget.usage()
        if use["minute_waits_today"] or use["hour_refusals_today"]:
            ctx.out.line("Request budget today: waited for a free minute %d time(s), refused for the hour %d time(s)" % (
                use["minute_waits_today"], use["hour_refusals_today"]))
    except Exception:  # noqa: BLE001  (the digest must post even when an extra line cannot be built)
        log.exception("digest extras failed")
    try:
        for ln in lane_lines(ctx):
            ctx.out.line(ln)
    except Exception:  # noqa: BLE001
        log.exception("digest lane lines failed")
    digest_extras(ctx, counts)
    ctx.out.acted = 0


def _stage_ids(ctx: Ctx, kind: str) -> list[str]:
    return [s.id for s in (getattr(ctx.policy, "stages", None) or {}).values() if s.kind == kind and s.mode != "off"]


def digest_extras(ctx: Ctx, counts: dict) -> None:
    """The lines the stages built after the first digest add: speed to lead, the outreach limits
    broken, the registry scrub, research, heirs, obituaries, the KPI alarms and the pacing rule
    about to be broken, and what each stage in plan mode would do. Each is built on its own, so one
    that fails costs its line and never the digest."""
    def add(label, fn, problem=False, limit=3):
        try:
            for ln in [x for x in (fn() or []) if x][:limit]:
                (ctx.out.problem if problem else ctx.out.line)(ln)
        except Exception:  # noqa: BLE001
            log.exception("digest %s line failed", label)

    from . import alarms, heirlane, leadclock, limits, obituary, pacing, researchqueue, scrub
    add("speed", lambda: leadclock.speed_lines(ctx.now, ctx.policy.tz), limit=2)
    add("limits", lambda: limits.recent_lines(ctx.now, ctx.policy.tz), problem=True, limit=3)
    add("scrub", lambda: [scrub.recent_line(sid) for sid in _stage_ids(ctx, "scrub")], limit=2)
    add("research", lambda: [researchqueue.digest_line()], limit=1)
    add("heirs", lambda: [heirlane.digest_line(sid) for sid in _stage_ids(ctx, "heirlane")], limit=2)
    add("obituary", lambda: [obituary.waiting_line(sid) for sid in _stage_ids(ctx, "obituary")], limit=2)
    add("alarms", lambda: alarms.digest_lines(now=ctx.now, tz=ctx.policy.tz, limit=2), problem=True, limit=2)
    add("pacing", lambda: pacing.digest_lines(ctx.policy, now=ctx.now, counts=counts, limit=1,
                                              remember=bool(ctx.task_id)), problem=True, limit=1)
    add("plans", lambda: plan_lines(ctx), limit=7)


def plan_lines(ctx: Ctx, hours: int = 24, limit: int = 6) -> list[str]:
    """What each stage in plan mode would have done, from its last run in the last day. Plan-mode
    stages no longer post their plans one by one (config.PLAN_POSTS), so this is where they are read."""
    since = ctx.now - timedelta(hours=hours)
    rows = []
    for s in (getattr(ctx.policy, "stages", None) or {}).values():
        if s.mode != "plan" or s.kind == "digest":
            continue
        try:
            last = json.loads(store.kv_get("autopilot:last:%s" % s.id) or "{}")
            at = datetime.fromisoformat(last.get("at") or "")
        except ValueError:
            continue
        if at < since or not (last.get("acted") or last.get("problems")):
            continue
        head = (last.get("head") or "").strip()
        rows.append((s.id.replace("_", " "), head if len(head) <= 150 else head[:147] + "..."))
    if not rows:
        return []
    out = ["In plan mode, nothing written. What %d stage(s) would do:" % len(rows)]
    out += ["%s: %s" % r for r in rows[:limit]]
    if len(rows) > limit:
        out.append("and %d more (cli.py autopilot last)" % (len(rows) - limit))
    return out


def lane_lines(ctx: Ctx) -> list[str]:
    """One line for each lane that is worked in tranches (plan 4.1): the tranche's size, what it
    has cost so far and what is left, in this tranche and behind it."""
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    out = []
    for lane in lanes.ORDER:
        tcfg = (getattr(cfg, "tranches", None) or {}).get(lane)
        state = lanes.tranche_state(lane)
        if not tcfg and not state:
            continue
        name = lane.replace("_", " ").capitalize()
        if not state:
            out.append("%s lane: no tranche drawn yet (the first will be %d records)." % (name, tcfg["size"]))
            continue
        try:
            opened = datetime.fromisoformat(state.get("opened") or "")
        except ValueError:
            continue
        ids = {s.id for s in (getattr(ctx.policy, "stages", None) or {}).values() if str(s.get("lane") or "") == lane}
        usd = sum(v[1] for (stage, _action), v in ledger.counts_since(opened).items() if stage in ids)
        waiting = int(state.get("waiting") or 0)
        behind = ""
        source = (tcfg or {}).get("presets") or (getattr(cfg, "presets", None) or {}).get(lane) or []
        try:
            total = sum(ctx.crm.count(windows.resolve(ctx.crm.preset_must(t), ctx.now)[0]) for t in source)
            if source:
                behind = ", %d behind it" % max(0, total - waiting)
        except KeyError:
            behind = ""
        out.append("%s lane: tranche %d, %d record(s), opened %s; $%.2f spent on it; %d still waiting%s." % (
            name, int(state["n"]), int(state.get("size") or 0),
            opened.astimezone(ZoneInfo(ctx.policy.tz)).strftime("%b %d").replace(" 0", " "), usd, waiting, behind))
    return out
