"""Budget and pacing rules (build plan 5.7): the page the team reads and the line the digest quotes.

A playbook can be switched on faster than a business can work it. Nothing stopped a second list from
starting while the first sat half worked, a strategy from being swapped after two quiet weeks, or AI
data from being bought on a budget that cannot carry it for six months. The teaching has a rule for
each, and until now the rules lived in people's heads.

They are written once, in the `pacing:` block of team/kpi.yaml, in the playbook's own format. This
module does two things with them:

  * `page_lines()` is the page itself: every rule in order, with its figures read from the catalog
    (a rule and its number cannot drift apart) and where the rule comes from.
  * `digest_lines()` is what the morning digest quotes. Each rule that names a `check` is tested
    against the playbook and the last calling day, and a rule that is about to be broken comes back
    as one plain line. At most one line a morning, the first rule in page order that has something
    to say, because the digest is a glance and a list of five warnings is read by nobody.

A rule that keeps being broken is repeated only every `pacing.repeat_days`; a rule that is about a
single moment (a strategy changed early, mail switched on for a new lane) is said once.

Nothing here changes the playbook or the account. The page is advice; the stages keep their own modes.

A SiftMap value is the one rule with nothing to test: `as_is_note()` gives any report that shows
that value the sentence that belongs beside it.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .. import store
from . import activity, kpi, lanes

log = logging.getLogger(__name__)

KV_STRATEGY = "autopilot:pacing:strategy"
KV_SAID = "autopilot:pacing:said:%s"
# Stages that work a first-to-market record under another lane's name still belong to that list.
FOLD = {"hot_auction": "ftm", "heirs": "ftm"}
LADDER = ("ftm", "obituary", "priority1", "strong", "bulk")
NAMES = {"ftm": "first-to-market", "obituary": "obituary", "priority1": "Priority 1", "strong": "Strong", "bulk": "Bulk"}


def lane_name(lane: str) -> str:
    return NAMES.get(lane, lane.replace("_", " "))


def _names(lane_list) -> str:
    items = [lane_name(x) for x in lane_list]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def stage_lane(cat: kpi.Catalog, st) -> str:
    """The list a stage works: its `lane`, else the lane whose preset titles its presets start like.
    A stage that works no list (the digest, the audit) answers ""."""
    lane = str(st.get("lane") or "")
    if not lane:
        titles = [str(t) for t in getattr(st, "presets", [])]
        titles += [str(p.get("preset") or "") for p in (st.get("pieces") or []) if isinstance(p, dict)]
        for ln, words in (cat.pacing.get("preset_prefixes") or {}).items():
            if any(t.startswith(w) for t in titles for w in words):
                lane = ln
                break
    lane = FOLD.get(lane, lane)
    return lane if lane in LADDER else ""


@dataclass
class Env:
    cat: kpi.Catalog
    policy: object
    now: datetime
    counts: dict = field(default_factory=dict)

    @property
    def tz(self) -> str:
        return getattr(self.policy, "tz", "") or "America/New_York"

    @property
    def today(self) -> date:
        return self.now.astimezone(kpi.zone(self.tz)).date()

    def by_lane(self) -> dict:
        """lane -> [(stage id, kind, mode)] for every stage that works a list."""
        out: dict[str, list] = defaultdict(list)
        for sid, st in (getattr(self.policy, "stages", None) or {}).items():
            lane = stage_lane(self.cat, st)
            if lane:
                out[lane].append((sid, st.kind, st.mode))
        return out

    def lanes_on(self) -> list[str]:
        by = self.by_lane()
        return [ln for ln in LADDER if any(mode != "off" for _, _, mode in by.get(ln, []))]

    def rule(self, rule_id: str) -> str:
        for r in self.cat.pacing["rules"]:
            if r["id"] == rule_id:
                return self.cat.fill(r["says"])
        return ""


# ---------------------------------------------------------------------- the checks
def lane_map_built(policy) -> bool:
    """Whether the nightly owner-lane stage has ever run here. Without its map every owner reads as
    bulk, so nothing may be concluded from an owner's lane."""
    for sid, st in (getattr(policy, "stages", None) or {}).items():
        if st.kind == "lanes" and store.kv_get("autopilot:last:%s" % sid):
            return True
    return False


def dial_mix(env: Env):
    """(share of the last calling day's dialed owners who sit on a lane below first to market, that
    day), or None when it cannot be told: no dials in the log, a day too small to judge, or the
    nightly owner-lane map has never been built (every owner would read as bulk)."""
    if not lane_map_built(env.policy):
        return None
    by_day: dict[str, list] = defaultdict(list)
    for e in activity.events(activity.DIAL, since=env.now - timedelta(days=7), until=env.now):
        if e["data"].get("direction") != "inbound" and e["resource"]:
            by_day[kpi.local_day(e["when"], env.tz)].append(e["resource"])
    if not by_day:
        return None
    day = max(by_day)
    if len(by_day[day]) < env.cat.value("alarm.min_dials_to_judge"):
        return None
    owners = set(by_day[day])
    lower = set(env.cat.pacing["ai_lanes"])
    n = 0
    for o in owners:
        held = lanes.owner_lane(o)
        if (held["lane"] if held else "bulk") in lower:
            n += 1
    return n / len(owners), day


def _waiting(env: Env):
    title = env.cat.pacing.get("first_to_market_ready") or ""
    if not title:
        return None
    if title in env.counts:
        return int(env.counts[title])
    try:
        return int(json.loads(store.kv_get("autopilot:digest:counts") or "{}")[title])
    except (KeyError, TypeError, ValueError):
        return None


def _lower_lane(env: Env):
    """First to market first. Broken when more first-to-market records wait for a first call than one
    caller dials in a day, while a lower list is switched on or is where the dials are going."""
    waiting = _waiting(env)
    if waiting is None or waiting <= env.cat.value("caller.dials_per_day"):
        return None
    lower_on = [ln for ln in env.lanes_on() if ln in env.cat.pacing["ai_lanes"]]
    if lower_on:
        what = "the %s stages are switched on" % _names(lower_on)
    else:
        mix = dial_mix(env)
        if mix is None or mix[0] <= 0.5:
            return None
        what = "%d%% of the owners dialed on %s sit on a lower list" % (round(mix[0] * 100), kpi.fmt_day(mix[1]))
    line = "%s first-to-market records are waiting for a first call and %s. %s" % (
        kpi.num(waiting), what, env.rule("first_to_market_first"))
    return line, "%s|%s" % (",".join(lower_on), "dials" if not lower_on else "")


def _second_lane(env: Env):
    """One list end to end before the next. Broken when a later list has a stage switched on while
    the first list still has stages that are not live."""
    by, on = env.by_lane(), env.lanes_on()
    if len(on) < 2:
        return None
    first, later = on[0], on[1:]
    not_live = [sid for sid, _, mode in by[first] if mode != "live"]
    if not not_live:
        return None
    live_later = [ln for ln in later if any(mode == "live" for _, _, mode in by[ln])]
    behind = ("the one %s stage is" % lane_name(first)) if len(by[first]) == 1 else (
        "%d of the %d %s stages are" % (len(not_live), len(by[first]), lane_name(first)))
    line = "The %s stages are %s while %s not live yet. %s" % (
        _names(live_later or later), "live" if live_later else "switched on", behind, env.rule("one_list_end_to_end"))
    return line, "%s|%s|%s|%d" % (first, ",".join(later), ",".join(live_later), len(not_live))


def _strategy(env: Env):
    """Hold a strategy about three months. The lists with a stage switched on are remembered with
    the day they last changed; a change sooner than the hold is said, unless the last 30 days brought
    no lead at all, which is the one reason the teaching gives for changing early."""
    on = env.lanes_on()
    try:
        mem = json.loads(store.kv_get(KV_STRATEGY) or "{}")
    except ValueError:
        mem = {}
    today = env.today
    if not mem.get("since"):
        store.kv_set(KV_STRATEGY, json.dumps({"lanes": on, "since": today.isoformat()}))
        return None
    if mem.get("lanes") != on:
        held = (today - date.fromisoformat(mem["since"])).days
        new = {"lanes": on, "since": today.isoformat()}
        quiet_days = int(env.cat.value("pacing.no_lead_days_to_change"))
        knows_leads = kpi.log_reach("property.status.updated") is not None
        no_lead = knows_leads and not kpi.new_leads(since=env.now - timedelta(days=quiet_days), until=env.now)
        if held < env.cat.value("pacing.strategy_hold_days") and not no_lead:
            new["early"] = {"at": today.isoformat(), "was": mem.get("lanes") or [], "held": held}
        store.kv_set(KV_STRATEGY, json.dumps(new))
        mem = new
    early = mem.get("early")
    if not early or (today - date.fromisoformat(early["at"])).days > env.cat.value("pacing.repeat_days"):
        return None
    line = "The lists being worked changed after %d days (%s before, %s now). %s" % (
        early["held"], _names(early["was"]) if early["was"] else "none", _names(on) if on else "none",
        env.rule("hold_a_strategy"))
    return line, "early|%s" % early["at"]


def _ai_budget(env: Env):
    """AI data only on a budget that can carry it. Broken when an AI lane is switched on and the
    owner's budget and runway are not written down, or are written down under the figures."""
    on = [ln for ln in env.lanes_on() if ln in env.cat.pacing["ai_lanes"]]
    if not on:
        return None
    budget_in, runway_in = env.cat.input("monthly_marketing_budget"), env.cat.input("runway_months")
    if budget_in is None or runway_in is None:
        what = "no monthly budget or runway is written down in team/kpi.yaml"
    elif budget_in < env.cat.low("pacing.ai_data_monthly_spend"):
        what = "the budget written down is %s a month" % kpi.money(budget_in)
    elif runway_in < env.cat.value("pacing.runway_months"):
        what = "the runway written down is %s months" % kpi.num(runway_in)
    else:
        return None
    line = "The %s stages are switched on and %s. %s" % (_names(on), what, env.rule("ai_data_budget"))
    return line, "%s|%s|%s" % (",".join(on), budget_in, runway_in)


def _mail_ai(env: Env):
    """Mail past the buy box. Said once when mail is first switched on for an AI lane: that is the
    moment someone chooses which records get a letter."""
    by = env.by_lane()
    stages = sorted(sid for ln in env.cat.pacing["ai_lanes"] for sid, kind, mode in by.get(ln, [])
                    if kind == "mail" and mode != "off")
    if not stages:
        return None
    lanes_ = [ln for ln in LADDER if ln in env.cat.pacing["ai_lanes"]
              and any(kind == "mail" and mode != "off" for _, kind, mode in by.get(ln, []))]
    line = "Mail is switched on for the %s list. %s" % (_names(lanes_), env.rule("mail_past_the_buy_box"))
    return line, ",".join(stages)


# check name -> (the function, whether the line repeats while the rule stays broken)
CHECKS = {
    "lower_lane_while_first_to_market_waits": (_lower_lane, True),
    "second_lane_before_first_is_live": (_second_lane, True),
    "strategy_changed_early": (_strategy, False),
    "ai_lane_without_budget": (_ai_budget, True),
    "mail_on_ai_lane": (_mail_ai, False),
}


# ---------------------------------------------------------------------- the page and the digest
def page_lines(cat: kpi.Catalog | None = None) -> list[str]:
    """The pacing page: every rule in order, figures filled in, with its source."""
    cat = cat or kpi.load()
    out = ["Budget and pacing rules (team/kpi.yaml)"]
    for i, r in enumerate(cat.pacing["rules"], 1):
        out.append("%d. %s (%s)%s" % (i, cat.fill(r["says"]), ", ".join(r["source"]),
                                     "" if r["check"] else " A rule for people: nothing tests it."))
    return out


def page_text(cat: kpi.Catalog | None = None) -> str:
    return "\n".join(page_lines(cat))


def breaches(policy, now: datetime | None = None, counts: dict | None = None, cat: kpi.Catalog | None = None) -> list[dict]:
    """Every rule that is about to be broken right now, in page order, said or not:
    [{"id", "line", "sig", "repeats"}]. One check failing never hides the others."""
    cat = cat or kpi.load()
    env = Env(cat=cat, policy=policy, now=now or datetime.now(timezone.utc), counts=dict(counts or {}))
    out = []
    for r in cat.pacing["rules"]:
        if not r["check"]:
            continue
        fn, repeats = CHECKS[r["check"]]
        try:
            hit = fn(env)
        except kpi.KpiError:
            raise
        except Exception:  # noqa: BLE001  (a check that cannot run says nothing; the digest must still post)
            log.exception("pacing check %s failed", r["check"])
            continue
        if hit:
            out.append({"id": r["id"], "line": hit[0], "sig": hit[1], "repeats": repeats})
    return out


def digest_lines(policy, now: datetime | None = None, counts: dict | None = None, limit: int = 1,
                 remember: bool = True) -> list[str]:
    """Ready lines for the morning digest: the rules about to be broken that have not been said
    lately, at most `limit`. `counts` is the digest's own preset counts (title -> records).

    `remember` marks each line as said, so it is not repeated tomorrow. A run a person starts from
    the command line should pass False: it then shows the line and the channel still gets it."""
    now = now or datetime.now(timezone.utc)
    try:
        cat = kpi.load()
        found = breaches(policy, now=now, counts=counts, cat=cat)
        repeat_days = cat.value("pacing.repeat_days")
    except kpi.KpiError as exc:
        return ["Pacing rules could not be read: the KPI catalog has %d problem(s)." % len(exc.problems)]
    today = now.astimezone(kpi.zone(getattr(policy, "tz", ""))).date()
    out = []
    for b in found:
        if len(out) >= limit:
            break
        try:
            said = json.loads(store.kv_get(KV_SAID % b["id"]) or "{}")
        except ValueError:
            said = {}
        if said.get("sig") == b["sig"]:
            if not b["repeats"]:
                continue
            try:
                age = (today - date.fromisoformat(str(said.get("day")))).days
            except ValueError:         # a mark with no readable day is treated as never said
                age = None
            if age is not None and age < repeat_days:
                continue
        out.append("Pacing: %s" % b["line"])
        if remember:
            store.kv_set(KV_SAID % b["id"], json.dumps({"sig": b["sig"], "day": today.isoformat()}))
    return out


def as_is_note(value, cat: kpi.Catalog | None = None) -> str:
    """The sentence that belongs beside any SiftMap value a report shows: it is an as-is number, and
    the repaired value runs higher by the catalog's range."""
    cat = cat or kpi.load()
    v = float(value or 0)
    if v <= 0:
        return ""
    lo, hi = cat.low("pacing.as_is_to_arv") / 100.0, cat.high("pacing.as_is_to_arv") / 100.0
    step = 1000.0
    return "SiftMap's %s is an as-is value. Repaired, expect about %s to %s." % (
        kpi.money(v), kpi.money(round(v * (1 + lo) / step) * step), kpi.money(round(v * (1 + hi) / step) * step))
