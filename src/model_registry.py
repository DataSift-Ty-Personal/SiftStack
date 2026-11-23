"""Which model does which job. The one place in SiftStack where a model is named.

Ty, 2026-11-17: "make sure that all of the workflows use these proven models." Every model call reads
its model through model(job). A box can still move one job to another model through that job's own
setting (the `env` name beside it; the names are the ones the code read before this file existed, so
nothing already set on a box changes meaning), and `python src/model_registry.py` prints what every
job resolves to on the machine it runs on. tests/test_model_registry.py fails when a model id is
written anywhere else in src/ or in team/*.yaml.

Where the lineup came from: a bake-off on one real comps job (2026-11-17: 11 models, the skill's own
generator, objective checks) and the research bake-off on 11 known-answer houses (2026-11-12). Ty's
call the same night: switch now and let real examples be the tests. The file skills re-check their
own math on every run and fall back once to a second model before they give up (agents/skillrun.py);
the who-signs step keeps its signer guard in code (research/open_research.guard_signers).

`via` says how the model is reached: "anthropic" is the Claude API with ANTHROPIC_API_KEY; "openrouter"
is OpenRouter with OPENROUTER_API_KEY (data_collection: deny on every call). OpenRouter ids are
vendor/name; Claude API ids are bare.
"""
from __future__ import annotations

import os

JOBS: dict[str, dict] = {
    "employees": dict(
        model="claude-haiku-5-5", env="AGENTS_MODEL", via="anthropic",
        what="the four Slack employees (Atlas, Diggs, Roma, Quinn): every turn, the skills they work included",
        proof="comps skill, 2 of 2 runs right on a real house (2026-11-17); real examples from here"),
    "file_skills": dict(
        model="openai/gpt-6.1-terra", env="AGENTS_SKILL_MODEL", via="openrouter",
        what="the thinking step of the comping and rehab skills (the skill_report tool)",
        proof="comps skill, 2 of 2 runs right and identical, 93 seconds, $0.045 a run (2026-11-17)"),
    "file_skills_fallback": dict(
        model="anthropic/claude-sonnet-5.5", env="AGENTS_SKILL_FALLBACK_MODEL", via="openrouter",
        what="a file skill whose answer failed its own checks twice on the first model",
        proof="comps skill, 2 of 2 runs right (2026-11-17)"),
    "who_signs": dict(
        model="openai/gpt-6.1-terra", env="AGENTS_ANALYSIS_MODEL", via="openrouter",
        what="the deep prospect's analysis passes (who signs, the call order) on the open research engine",
        proof="real examples; the signer guard runs on every answer"),
    "research_primary": dict(
        model="deepseek/deepseek-v4.1-flash-1017", env="AGENTS_RESEARCH_MODEL", via="openrouter",
        what="the first web researcher on a deep prospect",
        proof="11 known-answer houses, 29 of 36 facts, zero dangerous statements (2026-11-12)"),
    "research_second": dict(
        model="meta/muse-ember", env="AGENTS_RESEARCH_SECOND", via="openrouter",
        what="the second web researcher, cross-checked against the first", proof="as research_primary"),
    "research_referee": dict(
        model="anthropic/claude-sonnet-5.5", env="AGENTS_RESEARCH_REFEREE", via="openrouter",
        what="settles a hard disagreement between the two researchers", proof="in use since 2026-11-12"),
    "research_claude": dict(
        model="claude-opus-5-5", env="AGENTS_RESEARCH_CLAUDE_MODEL", via="anthropic",
        what="the older Claude research engine, used only where AGENTS_RESEARCH_ENGINE is not 'open' "
             "(an install without OpenRouter); the production box runs the open engine",
        proof="the engine before 2026-11-12"),
    "text_replies": dict(
        model="claude-haiku-5-5", env="SMS_AGENT_MODEL", via="anthropic",
        what="the texting app: reading every seller and buyer reply and writing its own replies",
        proof="real examples; opt-outs are decided by rules and every draft passes the validator in code"),
    "call_grading": dict(
        model="claude-haiku-5-5", env="AGENTS_COACH_MODEL", via="anthropic",
        what="grading recorded calls against the coaching rubrics", proof="real examples"),
    "call_review": dict(
        model="claude-haiku-5-5", env="AGENTS_CALL_REVIEW_MODEL", via="anthropic",
        what="lead notes from a call and the evening call screen (every value needs a quote from the call)",
        proof="built and tested on this model (2026-11-15)"),
    "reading_images": dict(
        model="claude-haiku-5-5", env="AGENTS_VISION_MODEL", via="anthropic",
        what="listing photos before a first call, walkthrough video frames, returned mail pages",
        proof="built and tested on this model (2026-11-15)"),
    "slack_posts": dict(
        model="claude-haiku-5-5", env="AGENTS_SLACK_VOICE_MODEL", via="anthropic",
        what="rewriting the team's Slack posts in Ty's voice, with a second look that fails closed",
        proof="in use since 2026-11-15"),
    "audio_to_text": dict(
        model="google/gemini-2.5-flash", env="CALL_COACHING_TRANSCRIBE_MODEL", via="openrouter",
        what="turning call recordings and walkthrough video audio into text", proof="in use since 2026-07"),
    "extraction": dict(
        model="claude-haiku-5-5", env="LLM_MODEL", via="anthropic",
        what="the first-to-market box and the import tools: notice parsing, photo and PDF import, entity "
             "research, buyer list cleanup", proof="real examples"),
    "obituary_reading": dict(
        model="claude-sonnet-5-5", env="OBITUARY_LLM_MODEL", via="anthropic",
        what="reading an obituary for who died and who survives (the step a weaker model once invented "
             "whole heir maps on, so it stays on Sonnet)", proof="the Sonnet line since 2026-07"),
    "openrouter_backend": dict(
        model="qwen/qwen-2.5-72b-instruct", env="OPENROUTER_MODEL", via="openrouter",
        what="llm_client's OpenRouter backend, used only when LLM_BACKEND=openrouter", proof="legacy option"),
    "issue_fixer": dict(
        model="claude-opus-5-5", env="ISSUE_FIXER_MODEL", via="anthropic",
        what="the issue fixer's Claude Code session on the workstation (Claude Code's own login, not the API "
             "key): finds the cause of each new post in the issues channel, sorts it, and writes and tests the "
             "fix for a bug (tools/issue_fixer)",
        proof="the method that repaired the 2026-11-18 morning's issues by hand"),
}

# Dollars per million tokens: input, output, cache read, cache write (five minutes). Read 2026-11-17 off the
# Claude model table. OpenRouter bills the true cost back on each call, so only Claude API models need a row.
# A model not listed is priced at the dearest rate, so a cap can only be met early, never overrun.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20, 10.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10, 1.25),
}
DEAREST = (10.0, 50.0, 1.0, 12.5)


def job(name: str) -> dict:
    if name not in JOBS:
        raise KeyError("no model job named %r; the jobs are %s" % (name, ", ".join(sorted(JOBS))))
    return JOBS[name]


def model(name: str) -> str:
    """The model for a job: its own setting when the box sets one, else the lineup's."""
    j = job(name)
    return (os.environ.get(j["env"]) or "").strip() or j["model"]


def default(name: str) -> str:
    """The lineup's model for a job, ignoring any override on this machine."""
    return job(name)["model"]


def overridden(name: str) -> bool:
    j = job(name)
    v = (os.environ.get(j["env"]) or "").strip()
    return bool(v) and v != j["model"]


def price(model_id: str) -> tuple[float, float, float, float]:
    return PRICES.get((model_id or "").strip(), DEAREST)


def thinking_off(model_id: str) -> dict | None:
    """The parameter that turns thinking off for a short extraction call, or None when the model only thinks
    when asked (older models) or cannot turn it off (Opus 5.5). Haiku 5.5 takes "disabled" at effort high or
    below; Sonnet 5.5 refuses "disabled" with a 400 and takes "between_tools" instead."""
    m = (model_id or "").lower()
    if "haiku-5" in m:
        return {"type": "disabled"}
    if "sonnet-5" in m:
        return {"type": "between_tools"}
    return None


def server_fallback(model_id: str) -> bool:
    """Whether the Claude API's server-side refusal fallback exists for this model. Haiku 5.5 has none."""
    m = (model_id or "").lower()
    return m.startswith("claude-") and "haiku" not in m


def lineup() -> list[dict]:
    """Every job, what it resolves to here, and whether this machine overrides it."""
    return [dict(job=k, model=model(k), lineup=v["model"], env=v["env"], via=v["via"], what=v["what"],
                 proof=v["proof"], overridden=overridden(k)) for k, v in JOBS.items()]


if __name__ == "__main__":
    for row in lineup():
        flag = "  OVERRIDDEN by %s (lineup %s)" % (row["env"], row["lineup"]) if row["overridden"] else ""
        print("%-21s %-36s %s%s" % (row["job"], row["model"], row["via"], flag))
