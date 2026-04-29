"""Convert .env credentials → input.json for Apify Actor (local test + cloud schedule).

The input field names match .actor/input_schema.json. Output is gitignored
(input.json is in .gitignore from before).

Usage:
    python scripts/env_to_apify_input.py
    # writes ./input.json — ready for `apify run` (local test) or paste-into-Apify-Console

Cloud deploy flow:
    1. Run this script to produce input.json from your .env
    2. apify run --purge          # local test of the Actor
    3. apify push                  # upload code to Apify cloud
    4. In Apify Console → Actor → Settings → Default run input → paste contents of input.json
    5. Apify Console → Schedules → Create → Cron `0 11 * * *` (= 6:00 AM ET)
"""

import json
import os
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
OUT_PATH = ROOT / "input.json"

# Mapping from input_schema.json field name → .env variable name.
# Matches Apify Actor input fields exactly.
ENV_TO_INPUT = {
    # Mode + filters
    "mode": (None, "daily"),                    # default literal value
    # OH credentials
    "realauction_email": ("REALAUCTION_EMAIL", ""),
    "realauction_password": ("REALAUCTION_PASSWORD", ""),
    "captcha_api_key": ("CAPTCHA_API_KEY", ""),
    # Enrichment APIs
    "anthropic_api_key": ("ANTHROPIC_API_KEY", ""),
    "smarty_auth_id": ("SMARTY_AUTH_ID", ""),
    "smarty_auth_token": ("SMARTY_AUTH_TOKEN", ""),
    "openwebninja_api_key": ("OPENWEBNINJA_API_KEY", ""),
    "serper_api_key": ("SERPER_API_KEY", ""),
    "firecrawl_api_key": ("FIRECRAWL_API_KEY", ""),
    "tracerfy_api_key": ("TRACERFY_API_KEY", ""),
    "trestle_api_key": ("TRESTLE_API_KEY", ""),
    # CRM
    "datasift_email": ("DATASIFT_EMAIL", ""),
    "datasift_password": ("DATASIFT_PASSWORD", ""),
    # Notifications
    "slack_webhook_url": ("SLACK_WEBHOOK_URL", ""),
    "gsheet_webhook_url": ("GSHEET_WEBHOOK_URL", ""),
    # Optional Google Drive backup
    "google_drive_folder_id": ("GOOGLE_DRIVE_FOLDER_ID", ""),
    "google_service_account_key": ("GOOGLE_SERVICE_ACCOUNT_KEY", ""),
    # Pipeline toggles
    "upload_datasift": (None, True),
    "enrich_datasift": (None, True),
    "skip_trace_datasift": (None, True),
    "run_tracerfy": (None, True),
    "notify_slack": (None, True),
    # Buy box
    "include_vacant": (None, False),
    "include_commercial": (None, False),
    "include_entities": (None, False),
}


def main() -> None:
    if not ENV_PATH.exists():
        print(f"ERROR: {ENV_PATH} not found — run from project root")
        raise SystemExit(1)

    env = dotenv_values(ENV_PATH)

    actor_input: dict = {}
    missing: list[str] = []
    placeholder: list[str] = []

    for field, (env_var, default) in ENV_TO_INPUT.items():
        if env_var is None:
            actor_input[field] = default
            continue
        val = env.get(env_var, "") or ""
        actor_input[field] = val if val else default
        if env_var:
            if not val:
                missing.append(env_var)
            elif "your_" in val.lower() or "_here" in val.lower() or val.startswith("YOUR/"):
                placeholder.append(f"{env_var}={val}")

    OUT_PATH.write_text(json.dumps(actor_input, indent=2) + "\n", encoding="utf-8")

    secret_count = sum(1 for k, v in actor_input.items() if isinstance(v, str) and v and len(v) > 20)
    print(f"✓ Wrote {OUT_PATH}")
    print(f"  Fields populated: {sum(1 for v in actor_input.values() if v not in ('', None, False))}")
    print(f"  Real secrets:    {secret_count}")
    print(f"  Placeholders:    {len(placeholder)}")
    print(f"  Missing entirely: {len(missing)}")
    print()

    if missing:
        print("⚠  These env vars are MISSING from .env (Actor will skip them):")
        for v in missing:
            print(f"   - {v}")
        print()

    if placeholder:
        print("⚠  These env vars still have PLACEHOLDER values (replace before deploy):")
        for kv in placeholder:
            print(f"   - {kv}")
        print()

    print("Next steps:")
    print("  1. Verify input.json contents look correct (real secrets, not placeholders)")
    print("  2. Local test:  apify run --purge")
    print("  3. Cloud push:  apify push")
    print("  4. Apify Console → Actor → Settings → Default run input → paste input.json contents")
    print("  5. Apify Console → Schedules → Create → cron '0 11 * * *' (= 6 AM ET)")


if __name__ == "__main__":
    main()
