"""Research has its own daily cap, apart from the team's (2026-11-19).

On 2026-11-19 the deep prospects spent $19.89 and the scoring stage $5.00 of the brain's shared $25,
and every Slack ask after that was refused although the team's own work had used $5. Research is now
checked against AGENTS_RESEARCH_DAILY_CAP_USD and booked to it (a deep prospect task, whoever queued
it: store.RESEARCH_WORKFLOWS); the employees' turns, their skills and the stages are checked against
AGENTS_DAILY_CAP_USD less what everything but research spent. Zero network: the selftest's own
database, invented tasks, and the brain's health page through the test client.
"""
from __future__ import annotations

from pathlib import Path

from . import config


def run(check, tmp) -> None:
    from . import runner, skillrun, store, team
    from .research import deep_prospect
    store.init()
    keep = (config.DAILY_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.RUN_CAP_USD, config.AGENT_BUDGET_USD,
            config.FILE_TURN_USD)
    made: list[int] = []
    try:
        _caps(check, store, runner, skillrun, deep_prospect, team, made)
        _health(check, store)
        _wiring(check)
    finally:
        (config.DAILY_CAP_USD, config.RESEARCH_DAILY_CAP_USD, config.RUN_CAP_USD, config.AGENT_BUDGET_USD,
         config.FILE_TURN_USD) = keep
        for tid in made:                   # nothing this section made is left queued for a later one
            store.finish(tid, "done", {})


def _caps(check, store, runner, skillrun, deep_prospect, team, made: list) -> None:
    print("  the two day caps")
    day = store.today()
    base_team, base_research = store.spent(day=day, research=False), store.spent(day=day, research=True)
    dp, _ = store.enqueue("research", "deep_prospect", "cap-test-record", source="autopilot:research_queue", force=True)
    stage, _ = store.enqueue("research", "top1000_score", source="schedule", force=True)
    made += [dp, stage]
    store.add_spend(dp, 19.89, "research (selftest)")
    store.add_spend(stage, 5.00, "autopilot top1000_score: phone scores (selftest)")
    check(abs(store.spent(day=day, research=True) - base_research - 19.89) < 1e-6
          and abs(store.spent(day=day, research=False) - base_team - 5.00) < 1e-6,
          "a deep prospect's spend is booked to research, a stage's to the team, and neither is counted twice")

    # 11-19 in miniature: the team has $20 of its day left, research has $0.11 of its own.
    config.RUN_CAP_USD, config.AGENT_BUDGET_USD, config.FILE_TURN_USD = 5.0, 5.0, 8.0
    config.DAILY_CAP_USD = base_team + 5.00 + 20.0
    config.RESEARCH_DAILY_CAP_USD = base_research + 19.89 + 0.11
    check(abs(runner.budget_now() - 5.0) < 1e-9 and abs(runner.budget_now(files=True) - 8.0) < 1e-9,
          "an employee turn gets its full cap while research is all but used up (the shared cap left it 11 cents)")
    check(abs(skillrun._room_today() - 20.0) < 1e-6, "a file skill sees the team's room, research not counted")
    fresh, _ = store.enqueue("research", "deep_prospect", "cap-test-record-2", force=True)
    made.append(fresh)
    b = deep_prospect.Budget(fresh)
    check(b.allows(0.10, "a small step") and not b.allows(0.20, "one step too many"),
          "the deep prospect is checked against research's own cap")
    config.DAILY_CAP_USD = base_team + 5.00
    check(b.allows(0.10, "a step on a day the team used its cap") and runner.budget_now() < 0.05,
          "a team at its own cap stops its turns and leaves research running")
    roster = team.load()
    ask, _ = store.enqueue("sales", "ask", subject="what did we spend", source="slack", mode="agent",
                           payload={"text": "what did we spend today?"})
    made.append(ask)
    text = runner.prompt_for(store.task(ask), roster, roster.employees.get("sales"))
    check("for the team's own work" in text and "Research (the deep prospects) has its own" in text,
          "an employee is told the team's day and research's day apart")


def _health(check, store) -> None:
    print("  the health page")
    try:
        from fastapi.testclient import TestClient
        from . import brain
    except ImportError as exc:
        check(False, "the health check needs fastapi: %s" % exc)
        return
    h = TestClient(brain.app).get("/health").json()
    day = store.today()
    check(h.get("research_spent_today_usd") == round(store.spent(day=day, research=True), 2)
          and h.get("research_cap_usd") == config.RESEARCH_DAILY_CAP_USD
          and h.get("spent_today_usd") == round(store.spent(day=day, research=False), 2)
          and h.get("daily_cap_usd") == config.DAILY_CAP_USD
          and h.get("total_spent_today_usd") == round(store.spent(day=day), 2),
          "/health shows the team's spend against its cap and research's against its own (%s)" % {
              k: h.get(k) for k in ("spent_today_usd", "daily_cap_usd", "research_spent_today_usd", "research_cap_usd")})


def _wiring(check) -> None:
    print("  the setting reaches the box")
    root = Path(__file__).resolve().parents[2]
    sync = (root / "deploy" / "sync_agents_secrets.py").read_text(encoding="utf-8")
    toml = (root / "fly.agents.toml").read_text(encoding="utf-8")
    check('("AGENTS_RESEARCH_DAILY_CAP_USD"' in sync, "AGENTS_RESEARCH_DAILY_CAP_USD is a name the secrets sync sends")
    check('AGENTS_RESEARCH_DAILY_CAP_USD = "25"' in toml, "the Fly config says research's default day cap out loud")
