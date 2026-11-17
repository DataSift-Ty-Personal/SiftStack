"""Neighborhood suppression suggestions (Ty, 2026-11-15): "neighborhoods that keep producing bad
leads, suggested for you to approve rather than changed automatically."

The teaching (C10.34): make neighborhood suppression iterative; when the team reports enough bad
leads from an area, look up that property's neighborhood and add it to the suppression. The account
carries a suppression list (the marketing presets skip a fixed set of neighborhoods under
must_not.any_neighborhood), but it was written once and never revisited, so a neighborhood that
keeps wasting the callers' time stays in every dial queue and every mail drop.

WHAT COUNTS. Each record worked to an outcome in the window (`window_days`, from the brain's copy of
the account log): a status a person changed to a bad status (Dead Lead or Not Interested by
default), or a bad-area tag (`bad_tags`), is a bad outcome; a status changed into a lead status is
a good one. The record's last outcome in the window is the one that counts. A status set by
automation is not a person's judgment of the lead and is left out (`ignore_sources`): a sequence, a
bulk action, DataSift's own refresh, and the autopilot's own key. The sold guard puts a record's old
status back through that key, which would otherwise read as a caller marking it Not Interested. A
record on a buyer list (`buyer_list_prefixes`, the dispo program) is left out too: a buyer saying no
to a deal blast says nothing about the neighborhood as a place to find sellers.

SIZE IS NOT EVIDENCE. A big neighborhood produces many bad outcomes because it is big, and a
handful of bad leads from a small one can be chance. So each neighborhood is judged against the
whole account: its share of bad outcomes must be at least `min_lift` above the account's share, it
needs `min_bad` bad outcomes out of at least `min_worked` worked records, and the count must be
unlikely by chance at the account's own rate (`max_chance`, the binomial tail). When nearly every
outcome everywhere is a no, only a neighborhood with real volume can stand out, and the report names
the neighborhoods closest to the line instead of pretending.

NOTHING IS CHANGED. A neighborhood the presets already skip is not suggested again. Each new one is
proven by a count first (the records search with and without the exclusion: a filter counts only
when a count moves, because the search ignores a key or a spelling it does not know), then written
into a change file in the preset tool's own plan format. A person applies it with preset_patch.py,
which reads each preset back for an exact match and stops on the first difference; there is no
write in this module. By default the change goes to the presets that already skip neighborhoods
(the owner chose those); `apply_to: marketing` adds it to every call, mail and pre-call preset.

Reads, bounded: one record read per worked record whose neighborhood is not known yet, up to
`read_cap` a run, newest outcomes first, each remembered (a house does not change neighborhood); the
preset listing; and one count per suggestion plus one base count.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .. import config, store
from . import activity, budget, kpi, lanes, ledger
from .stages import Ctx

log = logging.getLogger(__name__)

HOOD_FACT = "suppression_hood"            # ap_facts: the neighborhood (and value) a record sits in
SUGGESTED_KEY = "autopilot:suppression:suggested"
HOOD_KEY = "any_neighborhood"
DEFAULTS = {"window_days": 180, "bad_statuses": ["Dead Lead", "not_interested"], "bad_tags": ["Bad Area"],
            "ignore_sources": ["sequence", "bulk", "auto-update", "data-enriching", "internal-api"],
            "read_cap": 400, "min_bad": 5, "min_worked": 8, "min_lift": 0.15, "max_chance": 0.05, "max_suggest": 8,
            "suggest_lines": 3, "watch_lines": 3, "apply_to": "excluding", "prove": True, "sync": True,
            "buyer_list_prefixes": ["Dispo"]}
INT_KEYS = ("window_days", "read_cap", "min_bad", "min_worked", "max_suggest", "suggest_lines", "watch_lines")
SHARE_KEYS = ("min_lift", "max_chance")
LIST_KEYS = ("bad_statuses", "bad_tags", "ignore_sources", "buyer_list_prefixes")
APPLY_TO = ("excluding", "marketing")
# The preset tool's roles a marketing preset has: the call steps, the step before the first call,
# the mail steps and the hot auction lane (preset_patch.ROLES).
MARKETING_ROLES = ("call_step", "pre_call", "mail_step", "hot_auction")
PRESET_TOOL = "src/agents/autopilot/preset_patch.py"


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with a suppression stage's block, for the playbook checker."""
    out = []
    for k in INT_KEYS:
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            out.append("stage %s: %s must be a whole number" % (st.id, k))
    for k in SHARE_KEYS:
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1):
            out.append("stage %s: %s must be a share from 0 to 1 (0.15 is 15 points)" % (st.id, k))
    for k in LIST_KEYS:
        v = st.get(k)
        if v is not None and not isinstance(v, (list, tuple)):
            out.append("stage %s: %s must be a list" % (st.id, k))
    a = st.get("apply_to")
    if a is not None and str(a) not in APPLY_TO:
        out.append("stage %s: apply_to must be %s, not %r" % (st.id, " or ".join(APPLY_TO), a))
    return out


# ---------------------------------------------------------------------- the outcomes
def flat(s) -> str:
    """A status the way two spellings of it agree: "Dead Lead" and dead_lead are one status."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _word(status: str) -> str:
    return str(status or "").replace("_", " ").strip().lower()


def outcomes(st: dict, since: datetime, until: datetime) -> dict:
    """record -> {"bad", "why", "when"}: the last outcome a person gave the record in the window."""
    bad = {flat(s): _word(s) for s in st["bad_statuses"]}
    good = {flat(s) for s in lanes.LEAD_STATUS_VALUES} - set(bad)
    tags = {str(t).strip().lower() for t in st["bad_tags"]}
    ignore = {str(s).strip().lower() for s in st["ignore_sources"]}
    out: dict[str, dict] = {}
    for e in activity.events(("property.status.updated", "property.tags.added"), since=since, until=until):
        u = e.get("resource") or ""
        src = str(e.get("source") or "").strip().lower()
        if not u or e.get("author") == "self":
            continue                     # our own key (the sold guard puts old statuses back through it)
        if src in ignore and not (src == "internal-api" and e.get("author") == "person"):
            continue                     # a person working in the app is logged as internal-api too
        if e["event_type"] == "property.status.updated":
            to = flat(e["data"].get("to"))
            if to in bad:
                out[u] = {"bad": True, "why": bad[to], "when": e["when"]}
            elif to in good:
                out[u] = {"bad": False, "why": "a lead", "when": e["when"]}
            continue
        hit = [t for t in e["data"].get("tags") or [] if str(t).strip().lower() in tags]
        if hit:
            out[u] = {"bad": True, "why": "tagged %s" % hit[0], "when": e["when"]}
    return out


def hood_of(rec: dict) -> str:
    v = (rec or {}).get("neighborhood")
    if isinstance(v, dict):
        v = v.get("title") or v.get("name") or ""
    return " ".join(str(v or "").split())


def is_buyer(rec: dict, prefixes) -> bool:
    """A record on a buyer list (the dispo program) holds an investor we sell deals TO. A buyer saying no
    to a deal blast says nothing about the neighborhood as a place to find sellers."""
    want = [str(p).strip().lower() for p in prefixes or [] if str(p).strip()]
    for t in (rec or {}).get("lists") or []:
        n = str((t.get("title") or t.get("name")) if isinstance(t, dict) else t or "").strip().lower()
        if n and any(n.startswith(p) for p in want):
            return True
    return False


def _money(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------- the judgment
def chance_at_least(k: int, n: int, p: float) -> float:
    """The chance of k or more bad outcomes out of n when each is bad with probability p (the account's
    own rate). Small means the neighborhood is surprising, whatever its size. Summed in logs so a
    neighborhood of a thousand records cannot overflow a float."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    top = math.lgamma(n + 1)
    total = sum(math.exp(top - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq) for i in range(k, n + 1))
    return min(1.0, total)


def odds_text(p: float) -> str:
    """A chance as a person says it: "a 1 in 40 chance"."""
    if p >= 0.5:
        return "about an even chance"
    if p < 0.001:
        return "under a 1 in 1,000 chance"
    return "a 1 in %s chance" % kpi.num(round(1.0 / p))


def table(res: dict, hoods: dict) -> tuple[dict, dict]:
    """(neighborhood -> its counts, the account's counts) over the records whose neighborhood is known."""
    by: dict[str, dict] = {}
    tot = {"worked": 0, "bad": 0, "blank": 0, "buyers": 0, "values": []}
    for u, o in res.items():
        f = hoods.get(u)
        if f is None:
            continue
        if f.get("buyer"):
            tot["buyers"] += 1
            continue
        tot["worked"] += 1
        tot["bad"] += 1 if o["bad"] else 0
        if f.get("value"):
            tot["values"].append(f["value"])
        name = f.get("hood") or ""
        if not name:
            tot["blank"] += 1
            continue
        row = by.setdefault(name.lower(), {"name": name, "worked": 0, "bad": 0, "why": Counter(), "values": []})
        row["worked"] += 1
        if o["bad"]:
            row["bad"] += 1
            row["why"][o["why"]] += 1
        if f.get("value"):
            row["values"].append(f["value"])
    return by, tot


def judge(by: dict, p0: float, st: dict) -> list[dict]:
    """Every neighborhood with its rate, its chance and whether it is flagged, most surprising first."""
    out = []
    for row in by.values():
        n, k = row["worked"], row["bad"]
        row["rate"] = k / float(n) if n else 0.0
        row["chance"] = chance_at_least(k, n, p0)
        row["flagged"] = (k >= int(st["min_bad"]) and n >= int(st["min_worked"])
                          and row["rate"] - p0 >= float(st["min_lift"]) and row["chance"] <= float(st["max_chance"]))
        out.append(row)
    return sorted(out, key=lambda r: (r["chance"], -r["rate"], -r["worked"], r["name"]))


# ---------------------------------------------------------------------- the presets
def _pp():
    from . import preset_patch
    return preset_patch


def read_presets(ctx: Ctx) -> list[dict]:
    """Every saved preset with its filter exactly as saved (not reshaped for the search: the change
    file is checked against the saved filter, byte for byte, when a person applies it)."""
    out = []
    for title, p in (ctx.crm.presets() or {}).items():
        must = p.get("must")
        if must is None:
            d = ctx.crm.call("/api/internal/filter-preset/%s/" % p["uuid"]) or {}
            must = (d.get("filters") or {}).get("must") or {}
        out.append({"uuid": p.get("uuid"), "title": title, "folder": p.get("folder") or "", "must": must or {}})
    return out


def skipped(must: dict) -> list[str]:
    return [str(x) for x in ((must or {}).get("must_not") or {}).get(HOOD_KEY) or [] if str(x).strip()]


def targets(presets: list[dict], apply_to: str) -> list[dict]:
    """The presets a suppression goes into: those that already skip neighborhoods, or every marketing
    preset as the preset tool classifies it."""
    if apply_to == "marketing":
        pp = _pp()
        return [p for p in presets if pp.classify(p["folder"], p["title"])["role"] in MARKETING_ROLES]
    return [p for p in presets if skipped(p["must"])]


def prove(ctx: Ctx, names: list[str]) -> dict:
    """name -> records with a phone the exclusion takes out of the search (0: the name matched
    nothing; None: it could not be counted). The preset tool's own proof base, so both prove a rule
    the same way."""
    pp = _pp()
    out: dict = {n: None for n in names}
    try:
        base = int(ctx.crm.count(copy.deepcopy(pp.PROOF_BASE)))
        for n in names:
            without = int(ctx.crm.count(dict(copy.deepcopy(pp.PROOF_BASE), must_not={HOOD_KEY: [n]})))
            out[n] = max(0, base - without)
    except budget.BudgetExhausted as exc:
        ctx.out.problem("stopped counting the suggested neighborhoods: %s. They are counted next run" % exc)
    except Exception as exc:  # noqa: BLE001  (an uncounted neighborhood is left out of the file, never assumed)
        ctx.out.problem("could not count a suggested neighborhood in the records search (%s)" % str(exc)[:100])
    return out


def plan_entries(chosen: list[dict], names: list[str], why: str) -> list[dict]:
    """The change file's entries, in the preset tool's plan shape: each preset's saved filter, its
    fingerprint, and the filter with the new neighborhoods added after the ones it already skips."""
    pp = _pp()
    out = []
    for p in chosen:
        must = copy.deepcopy(p["must"] or {})
        mn = must.setdefault("must_not", {})
        have = list(mn.get(HOOD_KEY) or [])
        low = {str(x).strip().lower() for x in have}
        add = [n for n in names if n.strip().lower() not in low]
        if not add:
            continue
        mn[HOOD_KEY] = have + add
        out.append({"uuid": p["uuid"], "title": p["title"], "folder": p["folder"], "action": "change",
                    "before_sha": pp.sha(p["must"] or {}), "must_after": must, "after_sha": pp.sha(must),
                    "changes": [{"pack": "neighborhood_suppression", "say": "Skip records in %s" % _and(add), "why": why}]})
    return out


# ---------------------------------------------------------------------- the run's file
def files_dir(ctx: Ctx) -> Path:
    """RUNS_DIR/<stage>-<task id>/files, which the brain serves by a signed link (Summary.run_dir)."""
    d = Path(config.RUNS_DIR) / ("%s-%s" % (ctx.stage.id, ctx.task_id or "cli")) / "files"
    d.mkdir(parents=True, exist_ok=True)
    ctx.out.run_dir = str(d.parent)
    return d


def _link(ctx: Ctx, path: Path) -> str:
    if ctx.task_id:
        from .. import slack
        url = slack.file_url(int(ctx.task_id), path.name)
        if url:
            return url
    return str(path)


def command(path: Path) -> str:
    """The command that sends the change, run from the app folder. Without --commit (or without the
    switch) the same command only shows what it would patch."""
    return "PRESET_PATCH_ALLOW=1 python %s apply --plan %s --commit" % (PRESET_TOOL, path)


# ---------------------------------------------------------------------- the report
def _and(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _pct(x: float) -> str:
    return "%d%%" % round(100.0 * x)


def _remember(names: list[str], day: str) -> dict:
    """name -> the day it was first suggested. The autopilot's own memory, kept in plan mode too."""
    try:
        seen = json.loads(store.kv_get(SUGGESTED_KEY) or "{}")
    except ValueError:
        seen = {}
    for n in names:
        row = seen.setdefault(n.lower(), {"name": n, "first": day})
        row["last"] = day
    store.kv_set(SUGGESTED_KEY, json.dumps(seen))
    return seen


def run_suppression(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    tz = ctx.policy.tz
    if st["sync"]:
        kpi.sync_log(ctx, ("state",))
    days = int(st["window_days"])
    res = outcomes(st, ctx.now - timedelta(days=days), ctx.now)

    # ---- the neighborhood of every worked record, newest outcomes first, inside the read cap
    hoods: dict[str, dict] = {}
    reads = waiting = errors = 0
    stopped = False
    for u, _ in sorted(res.items(), key=lambda kv: kv[1]["when"], reverse=True):
        f = ledger.fact(u, HOOD_FACT)
        if isinstance(f, dict):
            hoods[u] = f
            continue
        if stopped or reads >= int(st["read_cap"]):
            waiting += 1
            continue
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted as exc:
            ctx.out.problem("stopped reading the worked records: %s. The rest are read next run" % exc)
            stopped = True
            waiting += 1
            continue
        except Exception:  # noqa: BLE001  (counted below and tried again next run)
            errors += 1
            continue
        reads += 1
        f = {"hood": hood_of(rec), "value": _money(rec.get("estimate_value")),
             "buyer": is_buyer(rec, st["buyer_list_prefixes"])}
        ledger.set_fact(u, HOOD_FACT, f)        # what the stage learned: kept in plan mode too
        hoods[u] = f
    if errors:
        ctx.out.problem("could not read some worked records (%d this run); they are tried again next run" % errors)

    by, tot = table(res, hoods)
    p0 = tot["bad"] / float(tot["worked"]) if tot["worked"] else 0.0
    rows = judge(by, p0, st)

    # ---- what the presets already skip, and where a new suppression would go
    try:
        presets = read_presets(ctx)
    except budget.BudgetExhausted as exc:
        ctx.out.problem("stopped before reading the presets: %s. Nothing is suggested this run" % exc)
        presets = None
    except Exception as exc:  # noqa: BLE001  (without the presets, "already skipped" cannot be known)
        ctx.out.problem("could not read the presets, so nothing is suggested this run (%s)" % str(exc)[:100])
        presets = None
    if presets is None:
        ctx.out.found = len(res)
        ctx.out.head = "Neighborhood check not finished: the presets could not be read, so nothing is suggested this run."
        return
    chosen = targets(presets, str(st["apply_to"]))
    already = {n.strip().lower(): n for p in chosen for n in skipped(p["must"])}
    flagged = [r for r in rows if r["flagged"]]
    old = [r for r in flagged if r["name"].strip().lower() in already]
    new = [r for r in flagged if r["name"].strip().lower() not in already][:int(st["max_suggest"])]
    counted = prove(ctx, [r["name"] for r in new]) if (new and st["prove"]) else {r["name"]: None for r in new}
    proven = [r for r in new if counted.get(r["name"])] if st["prove"] else list(new)
    for r in new:
        r["records"] = counted.get(r["name"])

    # ---- the change file, for a person to apply
    day = kpi.local_day(ctx.now, tz)
    path = None
    entries: list[dict] = []
    if proven and chosen:
        why = ("Over %d days these neighborhoods produced far more bad outcomes than the account as a whole (%s bad "
               "across %d worked records); suggested by the suppression check for the owner to approve." % (
                   days, _pct(p0), tot["worked"]))
        entries = plan_entries(chosen, [r["name"] for r in proven], why)
        if entries:
            path = files_dir(ctx) / ("suppression_plan_%s.json" % day)
            path.write_text(json.dumps({
                "tool": "suppression", "plan_date": day, "generated_at": ctx.now.isoformat(timespec="seconds"),
                "apply_to": st["apply_to"], "window_days": days, "account_bad_share": round(p0, 4),
                "neighborhoods": [{"name": r["name"], "worked": r["worked"], "bad": r["bad"], "rate": round(r["rate"], 4),
                                   "chance": r["chance"], "records_with_phone": r.get("records")} for r in proven],
                "presets": entries, "creates": []}, indent=1, default=str), encoding="utf-8")
    seen = _remember([r["name"] for r in proven], day) if proven else {}

    # ---- the report
    ctx.out.found = len(res)
    ctx.out.acted = len(proven)
    acct_value = statistics.median(tot["values"]) if tot["values"] else 0.0
    if proven:
        ctx.out.head = "Neighborhoods to suppress, for you to approve: %s, against %s across the account. Nothing was changed." % (
            _and(["%s (%d of %d worked records went bad)" % (r["name"], r["bad"], r["worked"]) for r in proven[:3]]
                 + (["%d more" % (len(proven) - 3)] if len(proven) > 3 else [])), _pct(p0))
    elif new:
        ctx.out.head = ("Neighborhoods to suppress: %s stand%s out, but none could be proven by a count in the records "
                        "search, so there is nothing to apply yet." % (_and([r["name"] for r in new]), "s" if len(new) == 1 else ""))
    elif tot["worked"]:
        ctx.out.head = ("No neighborhood to suppress: %d records were worked to an outcome in the last %d days, %s of them "
                        "went bad, and none stands far enough above that." % (tot["worked"], days, _pct(p0)))
    else:
        ctx.out.head = "No neighborhood to suppress: no record was worked to an outcome in the last %d days that the log copy holds." % days
    for r in new[:int(st["suggest_lines"])]:
        mix = _and(["%d %s" % (n, w) for w, n in r["why"].most_common()])
        line = "%s: %d of %d worked records went bad (%s), against %s across the account, %s." % (
            r["name"], r["bad"], r["worked"], mix, _pct(p0), odds_text(r["chance"]))
        if r.get("records"):
            line += " %s record%s with a phone sit there." % (kpi.num(r["records"]), "" if r["records"] == 1 else "s")
        elif r.get("records") == 0:
            line += " It matched no record in the records search, so it is not in the change file (it may be spelled another way there)."
        else:
            line += " It could not be counted in the records search, so it is not in the change file yet."
        if r["values"] and acct_value:
            line += " Median value %s, against %s across the account." % (kpi.money(statistics.median(r["values"])),
                                                                           kpi.money(acct_value))
        first = (seen.get(r["name"].lower()) or {}).get("first")
        if first and first != day:
            line += " First suggested %s." % kpi.fmt_day(first)
        ctx.out.line(line)
    if len(new) > int(st["suggest_lines"]):
        ctx.out.line("and %d more in the change file." % (len(new) - int(st["suggest_lines"])))
    # The channel shows the head and the first six lines (voice.report_text): the evidence, then the
    # command that applies it, then the detail.
    if path is not None:
        ctx.out.line("The change for %d preset(s) is in %s (%s). To apply it, from the app folder on the brain's box: "
                     "`%s`; without --commit it only shows the change." % (len(entries), path.name, _link(ctx, path),
                                                                          command(path)))
    elif proven and not chosen:
        ctx.out.line("No preset skips a neighborhood yet, so there is nowhere to add these; set apply_to: marketing to "
                     "add them to every call, mail and pre-call preset.")
    elif proven:
        ctx.out.line("Every preset that would take these already skips them.")
    if old:
        ctx.out.line("Already skipped by the presets: %s." % _and(["%s (%d of %d bad)" % (r["name"], r["bad"], r["worked"])
                                                                  for r in old[:4]]))
    # Close to the line means above the account's own rate and short only on volume or chance; a
    # neighborhood doing better than the account is not close to anything.
    near = [r for r in rows if not r["flagged"] and r["bad"] >= int(st["min_bad"]) and r["rate"] > p0
            and r["name"].strip().lower() not in already][:int(st["watch_lines"])]
    if near:
        ctx.out.line("Closest to the line: %s." % "; ".join(
            "%s %d of %d bad (%s), %s" % (r["name"], r["bad"], r["worked"], _pct(r["rate"]), odds_text(r["chance"])) for r in near))
    reach = kpi.log_reach(("property.status.updated",))
    cover = ("Worked to an outcome in the last %d days: %d record(s); judged here %d, %d of them bad (%s). Neighborhood "
             "known for %d, blank on %d" % (days, len(res), tot["worked"], tot["bad"], _pct(p0),
                                            tot["worked"] - tot["blank"], tot["blank"]))
    if tot["buyers"]:
        cover += ", %d buyer record(s) left out" % tot["buyers"]
    if waiting:
        cover += ", %d wait for the next run (%d record reads a run)" % (waiting, int(st["read_cap"]))
    cover += (". The status log copy starts %s." % kpi.fmt_day(kpi.local_day(reach, tz))) if reach else ". The status log copy is empty."
    ctx.out.line(cover)
    ctx.out.line("Nothing was changed: the presets are yours to change.")
    kpi.save_report("suppression", ctx.now, {"day": day, "window_days": days, "account_bad_share": p0,
                                              "worked": tot["worked"], "bad": tot["bad"], "blank": tot["blank"],
                                              "waiting": waiting, "neighborhoods": [
                                                  {k: (dict(v) if isinstance(v, Counter) else v) for k, v in r.items()
                                                   if k != "values"} for r in rows],
                                              "suggested": [r["name"] for r in proven],
                                              "plan": str(path) if path else ""})
