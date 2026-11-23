"""The cold email program's four jobs, run by the brain instead of Ty's PC (2026-11-20).

    cold_email_replies  every 5 minutes  Instantly replies read and acted on (src/cold_email/replies.py). A
                                         hand raise still goes to the TEXT LEADS channel through
                                         SMS_AGENT_SLACK_WEBHOOK, the same handoff a text gets (Ty, 2026-11-16).
    cold_email_topup    nightly          about three sending days of checked leads kept queued (daily.topup_run:
                                         the list build, ZeroBounce, the load into Instantly)
    cold_email_health   weekday mornings inboxes, warmup, bounces, the campaign, the reply watcher (daily.health_run)
    cold_email_stats    Mondays          last week's numbers (daily.stats_text)

The code is the cold email program's own: this module calls it and turns what it says into a stage
summary, so the board and #issues carry cold email like any other stage. It copies none of the rules.

ONE install runs these jobs (src/cold_email/coldstate.py). A stage acts only where the state folder's
home.json names this install; anywhere else it does nothing at all, not even a read. So the brain can
be deployed before the cutover and changes nothing until deploy/cold_email_cutover.py stops the PC's
scheduled tasks, copies the state to /data/cold_email and names this box the home, in one step.

Spend classes (agents/autopilot/registry.py): reading replies and writing notes is free; the top-up loads
people into a campaign that emails them (outreach), so on a box in AGENTS_MODE=free it acts only when
its stage id is in AGENTS_LIVE_STAGES, and plans (reads the counts, builds nothing) until then.
"""
from __future__ import annotations

import sys
from pathlib import Path

_COLD_EMAIL = Path(__file__).resolve().parents[2] / "cold_email"     # src/cold_email


def mods():
    """(coldstate, daily, replies): the cold email modules, imported the way their own command lines
    and tests import them (by folder), so there is one copy of each in the process."""
    if str(_COLD_EMAIL) not in sys.path:
        sys.path.insert(0, str(_COLD_EMAIL))
    import coldstate
    import daily
    import replies
    return coldstate, daily, replies


def _int(block, key: str, default: int, lo: int, hi: int, out: list) -> None:
    v = block.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        out.append("stage %s: %s must be a whole number from %d to %d" % (getattr(block, "id", "?"), key, lo, hi))


def block_problems(block) -> list[str]:
    """A mistyped block is refused when the playbook loads."""
    out: list[str] = []
    kind = getattr(block, "kind", "") or ""
    if kind == "coldtopup":
        for key in ("campaign", "preset"):
            v = block.get(key, "x")
            if not isinstance(v, str) or not v.strip():
                out.append("stage %s: %s must be a title" % (getattr(block, "id", "?"), key))
        _int(block, "days_queued", 3, 1, 14, out)
        _int(block, "min_batch", 100, 1, 5000, out)
        _int(block, "build_timeout_min", 120, 10, 600, out)
    if kind in ("coldhealth", "coldstats"):
        v = block.get("campaign", "x")
        if not isinstance(v, str) or not v.strip():
            out.append("stage %s: campaign must be a title" % getattr(block, "id", "?"))
    return out


def _not_home(ctx, coldstate) -> bool:
    """True when this install is not cold email's home: the stage does nothing at all and posts nothing.
    The reason is the run's head (never a problem: before the cutover this is the expected state), and
    a report stage leaves even that out, because a report with a head is posted."""
    ok, why = coldstate.owns()
    if ok:
        return False
    ctx.out.tally["not this install's job"] += 1
    if ctx.stage.kind != "coldstats":
        ctx.out.head = why[:1].upper() + why[1:] + "."
    return True


def _campaign(ctx, daily) -> str:
    return str(ctx.stage.get("campaign") or daily.CAMPAIGN)


# What each kind of reply is called in a summary line: (one, more than one).
KIND_WORDS = {"interested": ("hand raise", "hand raises"), "needs_reply": ("needs an answer", "need an answer"),
              "sensitive": ("for a person", "for a person"), "stop": ("asked to stop", "asked to stop"),
              "not_interested": ("said no", "said no"), "wrong_person": ("wrong person", "wrong people")}


def _plural(n: int, word: str) -> str:
    if n == 1:
        return "1 %s" % word
    if word.endswith("y") and not word.endswith("ay"):
        return "%d %sies" % (n, word[:-1])
    return "%d %ss" % (n, word)


def _kind_count(kind: str, n: int) -> str:
    one, many = KIND_WORDS.get(kind, (kind.replace("_", " "), kind.replace("_", " ")))
    return "%d %s" % (n, one if n == 1 else many)


# ---------------------------------------------------------------------- the four stages
def run_coldreplies(ctx) -> None:
    coldstate, daily, replies = mods()
    if _not_home(ctx, coldstate):
        return
    if not ctx.live:
        # A plan reads the new replies' count and classifies nothing: the classifier is a model call,
        # and a plan every five minutes would pay for the same replies over and over.
        p = replies.peek()
        ctx.out.found = p["new"]
        if p["new"]:
            ctx.out.head = "Would read %s." % _plural(p["new"], "new cold email reply")
        if not p["state"]:
            ctx.out.problem("there is no replies_state.json here, so a live pass would refuse to start (copy it from "
                            "the install that ran cold email before, or run replies.py --init-state on a new install)")
        return
    r = replies.run_pass(commit=True)
    ctx.out.found, ctx.out.acted = r["new"], r["handled"] + r["gave_up"]
    for p in r["problems"]:
        ctx.out.problem(p)
    if r["skipped"] and not r["problems"]:
        ctx.out.tally["skipped: %s" % r["skipped"]] += 1
        return
    kinds = r.get("kinds") or {}
    said = [_kind_count(k, n) for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))]
    if r["handled"] or r["gave_up"]:
        ctx.out.head = "%s handled%s." % (_plural(r["handled"] + r["gave_up"], "cold email reply"),
                                         (": " + ", ".join(said)) if said else "")
        if kinds.get("interested"):
            ctx.out.line("%s posted to the text leads channel for a call." % _plural(kinds["interested"], "hand raise"))
        if r["gave_up"]:
            ctx.out.line("%s could not be handled on its own and went to a person." % _plural(r["gave_up"], "reply"))
    if r["retry"]:
        ctx.out.line("%s failed this pass and %s read again in five minutes." % (
            _plural(r["retry"], "reply"), "is" if r["retry"] == 1 else "are"))


def run_coldtopup(ctx) -> None:
    coldstate, daily, _ = mods()
    if _not_home(ctx, coldstate):
        return
    st = ctx.stage
    try:
        r = daily.topup_run(ctx.live, name=_campaign(ctx, daily), preset=str(st.get("preset") or daily.PRESET),
                            days_queued=int(st.get("days_queued", daily.DAYS_QUEUED)),
                            min_batch=int(st.get("min_batch", daily.MIN_BATCH)),
                            build_timeout_s=60 * int(st.get("build_timeout_min", 120)))
    except SystemExit as exc:              # campaign() stops a command line when the campaign is not found
        ctx.out.problem("could not top up the cold email list: %s" % exc)
        return
    ctx.out.found = r["want"]
    for p in r["problems"]:
        ctx.out.problem(p)
    days = (r["left"] / r["daily"]) if r["daily"] else 0
    pace = "%.1f sending days at %s a day" % (days, format(r["daily"], ","))
    action = r["action"]
    if action == "busy":
        ctx.out.tally["skipped: another top-up is running"] += 1
    elif action == "enough":
        ctx.out.head = "Enough cold email leads queued: %s, or %s." % (format(r["left"], ","), pace)
    elif action == "would_build":
        ctx.out.head = "Would build about %s leads from %s and load them; %s are queued (%s)." % (
            format(r["want"], ","), st.get("preset") or daily.PRESET, format(r["left"], ","), pace)
    elif action == "built":
        ctx.out.acted = r["loaded"]
        ctx.out.head = "%s loaded into the cold email campaign; %s were queued before tonight (%s)." % (
            "1 new lead" if r["loaded"] == 1 else "%s new leads" % format(r["loaded"], ","), format(r["left"], ","), pace)
        ctx.out.line("ZeroBounce had %s credits before the build." % format(r["credits"], ","))


def run_coldhealth(ctx) -> None:
    coldstate, daily, _ = mods()
    if _not_home(ctx, coldstate):
        return
    try:
        problems = daily.health_run(_campaign(ctx, daily))
    except SystemExit as exc:
        ctx.out.problem("could not check cold email: %s" % exc)
        return
    for p in problems:
        ctx.out.problem(p)
    ctx.out.found = len(problems)
    ctx.out.head = "Cold email needs a look." if problems else "Cold email is healthy."


def run_coldstats(ctx) -> None:
    coldstate, daily, _ = mods()
    if _not_home(ctx, coldstate):
        return
    try:
        text = daily.stats_text(_campaign(ctx, daily))
    except SystemExit as exc:
        ctx.out.problem("could not read last week's cold email numbers: %s" % exc)
        return
    rows = [x.strip() for x in str(text or "").splitlines() if x.strip()]
    if rows:
        ctx.out.head = rows[0]
        for x in rows[1:]:
            ctx.out.line(x)
