"""Push the agents brain's secrets from .env into its Fly app. Masked plan by default.

    python deploy/sync_agents_secrets.py              # masked plan, sends nothing
    python deploy/sync_agents_secrets.py --commit     # stage on Fly (applied on next deploy)

DataSift access is the Open API key ONLY. DATASIFT_EMAIL / DATASIFT_PASSWORD are deliberately not
on either list: without them the uploader uses the key, which reaches every route the agents
write (verified 2026-11-03), and no install of this ever needs a person's password.

AGENTS_WEBHOOK_SECRET is generated on first run and appended to .env, because the DataSift
webhook URL embeds it and must not change between runs.
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_ftm_secrets import REPO_ROOT, load_env, mask  # noqa: E402

DEFAULT_APP = "siftstack-agents"

REQUIRED = [
    ("REISIFT_API_KEY", "DataSift Open API key: every CRM read and write"),
    ("ANTHROPIC_API_KEY", "the Research agent (Claude with web search)"),
    ("AGENTS_WEBHOOK_SECRET", "the secret path DataSift's webhook posts to"),
]
OPTIONAL = [
    ("SMARTSKIP_EMAIL", "relatives + phones (live mode)"),
    ("SMARTSKIP_PASSWORD", "relatives + phones (live mode)"),
    ("TRESTLE_API_KEY", "phone scoring and Real Contact"),
    ("TRESTLE_PAID_API_KEY", "phone scoring, paid key"),
    ("TRESTLE_FREE_API_KEY", "phone scoring, free monthly allowance"),
    ("TRESTLE_FREE_LIMIT", "free key monthly cap"),
    ("TRESTLE_FREE_RESET", "free key reset day"),
    ("TRACERFY_API_KEY", "gap-fill for decision makers SmartSkip left phoneless; the autopilot's first retrace source"),
    ("ENFORMION_AP_NAME", "the autopilot's second retrace source (street-anchored person search)"),
    ("ENFORMION_AP_PASSWORD", "the autopilot's second retrace source"),
    ("AUTOPILOT_MAIL_FROM_NAME", "return name on autopilot mail pieces"),
    ("AUTOPILOT_MAIL_FROM_PHONE", "call-back number printed on autopilot mail pieces"),
    ("AUTOPILOT_MAIL_FROM_STREET", "return address on autopilot mail pieces"),
    ("AUTOPILOT_MAIL_FROM_CITY", "return address on autopilot mail pieces"),
    ("AUTOPILOT_MAIL_FROM_STATE", "return address on autopilot mail pieces"),
    ("AUTOPILOT_MAIL_FROM_ZIP", "return address on autopilot mail pieces"),
    ("GOOGLE_STREETVIEW_API_KEY", "the house photo on the property file"),
    ("OPENROUTER_API_KEY", "call recording transcription, and the low-cost research engine"),
    ("SCRAPFLY_KEY", "opens bot-protected obituary, county and court pages for the researcher"),
    # The low-cost research engine (src/agents/research/open_research.py). Off unless
    # AGENTS_RESEARCH_ENGINE=open; without these the Claude researcher runs exactly as before.
    ("AGENTS_RESEARCH_ENGINE", "open = two low-cost research models, cross-checked (default: claude)"),
    ("AGENTS_RESEARCH_MODEL", "low-cost engine: the first researcher"),
    ("AGENTS_RESEARCH_SECOND", "low-cost engine: the second opinion (off = none)"),
    ("AGENTS_RESEARCH_REFEREE", "low-cost engine: settles a disagreement (off = none)"),
    ("AGENTS_ANALYSIS_MODEL", "low-cost engine: the analysis passes (unset = Claude)"),
    ("SERPER_API_KEY", "low-cost engine: web search, about $1 per 1,000"),
    ("SERPAPI_KEY", "low-cost engine: web search fallback (the monthly plan)"),
    ("FIRECRAWL_API_KEY", "low-cost engine: opens pages a plain request cannot"),
    ("SLACK_BOT_TOKEN", "the team's Slack app: post as each employee, in threads"),
    ("SLACK_SIGNING_SECRET", "the team's Slack app: verify what Slack sends in (no secret, no listener)"),
    ("AGENTS_SLACK_CHANNEL", "one Slack channel (its ID, C0...) for the whole team instead of one per employee"),
    ("AGENTS_SLACK_PIPELINE_CHANNEL", "the pipeline channel id: morning digest, live stage results, leads to call now"),
    ("AGENTS_SLACK_ISSUES_CHANNEL", "the issues channel id: anything broken, once a day, in plain words"),
    ("AGENTS_SLACK_VOICE", "ty (default): posts rewritten in Ty's voice; plain: free cleanup only"),
    ("AGENTS_RELAY_SECRET", "the secret path the first-to-market jobs post to instead of #first-to-market"),
    ("SLACK_APP_TOKEN", "the team's Slack app, Socket Mode (xapp-...): hear replies with no signing secret or URL check"),
    ("AGENTS_RELEASERS", "Slack user ids allowed to release a staged batch from the channel (empty: command line only)"),
    ("AGENTS_SLACK_WEBHOOK", "the agents' Slack channel (post-only fallback without the app)"),
    # The code sandbox (src/agents/sandbox.py)
    ("AGENTS_SANDBOX", "0 takes run_python and export_data off every employee (default on)"),
    ("AGENTS_SANDBOX_CPU_S", "code sandbox: CPU seconds one run may use (default 120)"),
    ("AGENTS_SANDBOX_WALL_S", "code sandbox: wall clock for one run, seconds (default 180)"),
    ("AGENTS_SANDBOX_MEM_MB", "code sandbox: memory for one run, MB (default 2048 = the whole machine)"),
    ("AGENTS_SANDBOX_FILE_MB", "code sandbox: largest file one run may write, MB (default 200)"),
    ("AGENTS_SANDBOX_PROCS", "code sandbox: processes one run may hold (default 64)"),
    ("AGENTS_SANDBOX_WORK_MB", "code sandbox: what one run may write in all, MB (default 1024)"),
    ("AGENTS_SANDBOX_OUT_FILES", "code sandbox: files kept from one run (default 20)"),
    ("AGENTS_SANDBOX_OUT_MB", "code sandbox: MB kept from one run (default 50)"),
    ("AGENTS_EXPORT_MAX_ROWS", "export_data: records written by one export (default 2000)"),
    ("AGENTS_EXPORT_DETAIL_MAX", "export_data: records whose list and tag titles one export reads (default 100)"),
    # Scripts from Slack (src/agents/commands.py, workflows/run_script.py)
    ("AGENTS_SCRIPT_TOOLS", "0 takes script_catalog and run_script off every employee (default on)"),
    ("AGENTS_SCRIPT_TIMEOUT_MIN", "run_script: one script's time limit; the brain's one worker waits on it (default 20)"),
    ("AGENTS_SCRIPT_SLACK_CHARS", "run_script: characters of output posted in the thread (default 2500)"),
    ("AGENTS_SCRIPT_LOG_MB", "run_script: full log kept per run, head and tail (default 5)"),
    ("AGENTS_SCRIPT_MAX_FILES", "run_script: files copied from output/ into the run folder (default 20)"),
    ("AGENTS_SCRIPT_FILE_MB", "run_script: largest file copied (default 25)"),
    # The query tools every employee carries (src/agents/query.py)
    ("AGENTS_QUERY_TOOLS", "0 takes the four read-only query tools off every employee (default on)"),
    ("AGENTS_QUERY_USERS", "teammates by first name for the assignee filter: Name=<user uuid>, comma separated"),
    ("AGENTS_QUERY_INDEX_TTL_S", "how long list, tag and preset titles are kept between questions (default 600)"),
    # Roma's comps (src/agents/workflows/quick_screen.py, underwrite.py)
    ("OPENWEBNINJA_API_KEY", "the Zillow sold pull for comp asks in Slack (paid, capped by AGENTS_COMPS_DAILY_USD)"),
    # Quinn's call coaching and batch QA (src/agents/workflows/call_coaching.py, qa_batch.py)
    ("AGENTS_COACH_RUN_CAP_USD", "call coaching: spend cap for one review pack (default 4.00)"),
    ("AGENTS_COACH_MAX_CALLS", "call coaching: calls graded in one pack (default 16)"),
    ("AGENTS_COACH_ROLES", "call coaching: Name=pipeline pins a caller to one rubric"),
    ("AGENTS_COACH_MODEL", "call coaching: the grading model (default the team model)"),
    ("AGENTS_COACH_EFFORT", "call coaching: grading effort (default high)"),
    ("AGENTS_COACH_GRADE_EST_USD", "call coaching: estimated cost of one grade, for the cap"),
    ("AGENTS_QA_MAX_READS", "batch QA: record reads in one check (default 60)"),
    ("AGENTS_VIDEO_DRY_PAID", "walkthrough video: 1 reads a video (paid) on a dry box too (default 0)"),
    ("AGENTS_MAX_VIDEO_MB", "walkthrough video: largest video taken from Slack (default 500)"),
    ("AGENTS_MAX_LINK_VIDEO_MB", "walkthrough video: largest video taken from a Drive or Dropbox link (default 2000)"),
    ("AGENTS_VIDEO_RUN_CAP_USD", "walkthrough video: dollars one ask may spend (default 1.00)"),
    ("AGENTS_VIDEO_DAILY_USD", "walkthrough video: dollars a day (default 3.00)"),
    ("AGENTS_VIDEO_PRIVATE_ROUTING", "walkthrough video: 1 asks OpenRouter for hosts that keep no data (default 1)"),
    ("AGENTS_KPI_HOOK_SECRET", "KPI sources: the secret in the webhook link a tool like Kixie or a Zap posts to"),
    ("AGENTS_KPI_TITLE", "KPI tracker: the page title and the optional Google Sheet's name (default KPI tracker)"),
    ("AGENTS_KPI_SLACK", "KPI tracker: edit (one Slack message a day, edited in place), posts, or off"),
    ("AGENTS_KPI_SHEET_ID", "KPI tracker, optional Google Sheet: write this sheet instead of making one"),
    ("AGENTS_KPI_SHEET_SHARE", "KPI tracker, optional Google Sheet: emails to share it with (email=writer for an editor)"),
    ("AGENTS_GOOGLE_SA_JSON", "KPI tracker, optional Google Sheet: a service account key, base64 of the JSON file"),
    ("AGENTS_GOOGLE_CLIENT_ID", "KPI tracker, optional Google Sheet: OAuth client id"),
    ("AGENTS_GOOGLE_CLIENT_SECRET", "KPI tracker, optional Google Sheet: OAuth client secret"),
    ("AGENTS_GOOGLE_REFRESH_TOKEN", "KPI tracker, optional Google Sheet: OAuth refresh token (kpisinks oauth prints it)"),
    ("SLACK_WEBHOOK_URL", "fallback Slack channel"),
]


def ensure_webhook_secret(env_file: Path, env: dict) -> None:
    if env.get("AGENTS_WEBHOOK_SECRET"):
        return
    value = secrets.token_urlsafe(24)
    with env_file.open("a", encoding="utf-8") as f:
        f.write("\n# agents brain: secret path for DataSift webhooks (generated)\nAGENTS_WEBHOOK_SECRET=%s\n" % value)
    env["AGENTS_WEBHOOK_SECRET"] = value
    print("generated AGENTS_WEBHOOK_SECRET and appended it to %s" % env_file)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync agents secrets from .env into Fly")
    ap.add_argument("--app", default=DEFAULT_APP)
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--exclude", action="append", default=[],
                    help="an optional name NOT to send even though .env holds it (repeatable). The first deploy "
                         "leaves the Slack names out so a brain in plan mode posts nowhere until its own channel exists.")
    args = ap.parse_args()
    env_file = Path(args.env_file)
    env = load_env(env_file)
    for key, _ in REQUIRED + OPTIONAL:
        if os.environ.get(key):
            env[key] = os.environ[key]
    required_names = {k for k, _ in REQUIRED}
    for key in args.exclude:
        if key in required_names:
            print("%s is required and cannot be excluded." % key)
            return 2
        if env.pop(key, None) is not None:
            print("excluded (not sent): %s" % key)
    ensure_webhook_secret(env_file, env)

    send, missing = {}, []
    print("\nagents secrets -> Fly app '%s'\n" % args.app)
    for title, rows, required in (("required", REQUIRED, True), ("optional", OPTIONAL, False)):
        print(title + ":")
        for key, why in rows:
            val = env.get(key, "")
            if val:
                send[key] = val
                print("  [OK  ] %-26s %-28s %s" % (key, mask(val), why))
            else:
                (missing.append(key) if required else None)
                print("  [%s] %-26s %-28s %s" % ("MISS" if required else " -  ", key, "", why))
    if missing:
        print("\nBLOCKED: missing %s. Nothing sent.\n" % ", ".join(missing))
        return 2
    if not args.commit:
        print("\nDRY RUN. %d secrets would be staged. Re-run with --commit.\n" % len(send))
        return 0
    cmd = ["fly", "secrets", "set", "-a", args.app, "--stage"] + ["%s=%s" % kv for kv in send.items()]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        print("flyctl not found on PATH.")
        return 1

    def scrub(text: str) -> str:
        for v in send.values():
            text = text.replace(v, "***") if v else text
        return text

    print(scrub(proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)))
    if proc.returncode == 0:
        print("Staged. Apply with:  fly deploy -c fly.agents.toml\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
