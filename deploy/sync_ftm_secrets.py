"""Push the FTM runner's secrets from .env into a Fly app.

Hand-typing a dozen credentials into `fly secrets set` is how one of them ends
up truncated, and a truncated key fails at 6:30am in a way that reads like a
site change. This reads the local .env, reports exactly what it would send with
every value MASKED, and only transmits when you pass --commit.

    python deploy/sync_ftm_secrets.py                 # masked plan, sends nothing
    python deploy/sync_ftm_secrets.py --commit        # push to Fly
    python deploy/sync_ftm_secrets.py --app other-app --commit

Values are passed to flyctl through argv, so they never land in a file. Nothing
here prints a secret, including on failure.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP = "siftstack-ftm"

# Everything the scheduled run actually touches.
REQUIRED = [
    ("TNPN_EMAIL", "tnpublicnotice login"),
    ("TNPN_PASSWORD", "tnpublicnotice login"),
    ("CAPTCHA_API_KEY", "2Captcha, clears the Turnstile notice gate"),
    ("DATASIFT_EMAIL", "uploader mints its own JWT from this"),
    ("DATASIFT_PASSWORD", "uploader mints its own JWT from this"),
]

OPTIONAL = [
    ("ANTHROPIC_API_KEY", "LLM notice parsing; probate loses the PR and decedent without it"),
    ("APIFY_TOKEN", "resolves the egress proxy password (the site blocks datacenter IPs)"),
    ("SIFTSTACK_PROXY_URL", "explicit proxy instead of Apify"),
    ("SMARTY_AUTH_ID", "address standardization"),
    ("SMARTY_AUTH_TOKEN", "address standardization"),
    ("OPENWEBNINJA_API_KEY", "Zillow property enrichment"),
    ("SLACK_WEBHOOK_URL", "run summaries and dead-run alerts"),
    ("TRACERFY_API_KEY", "skip trace (only with --deep-heirs)"),
    ("TRESTLE_API_KEY", "phone scoring"),
    ("SCRAPFLY_KEY", "alternate scraping backend"),
]


def load_env(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def mask(value: str) -> str:
    """Enough to tell two values apart, not enough to use one.

    Short values get no prefix at all: on an 11-character password, three
    leading characters is a real disclosure, not a hint.
    """
    if not value:
        return "(empty)"
    if len(value) < 20:
        return f"set (len {len(value)})"
    return f"{value[:3]}...{value[-2:]} (len {len(value)})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync FTM secrets from .env into Fly")
    ap.add_argument("--app", default=DEFAULT_APP)
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--commit", action="store_true", help="Actually send to Fly")
    args = ap.parse_args()

    env = load_env(Path(args.env_file))
    # Process environment wins, so this works on a machine without a .env.
    for key, _ in REQUIRED + OPTIONAL:
        if os.environ.get(key):
            env[key] = os.environ[key]

    send: dict[str, str] = {}
    missing_required: list[str] = []

    print(f"\nFTM secrets -> Fly app '{args.app}'")
    print(f"source: {args.env_file}\n")

    print("required:")
    for key, why in REQUIRED:
        val = env.get(key, "")
        if val:
            send[key] = val
            print(f"  [OK  ] {key:24} {mask(val):28} {why}")
        else:
            missing_required.append(key)
            print(f"  [MISS] {key:24} {'':28} {why}")

    print("\noptional:")
    for key, why in OPTIONAL:
        val = env.get(key, "")
        if val:
            send[key] = val
            print(f"  [OK  ] {key:24} {mask(val):28} {why}")
        else:
            print(f"  [ -  ] {key:24} {'':28} {why}")

    if missing_required:
        print(f"\nBLOCKED: missing required {', '.join(missing_required)}")
        print("Nothing sent.\n")
        return 2

    if not send:
        print("\nNothing to send.\n")
        return 1

    if not args.commit:
        print(f"\nDRY RUN. {len(send)} secrets would be set. Re-run with --commit.\n")
        return 0

    # One call: each `fly secrets set` triggers its own release, so setting them
    # individually would restart the machine a dozen times.
    cmd = ["fly", "secrets", "set", "-a", args.app, "--stage"]
    cmd += [f"{k}={v}" for k, v in send.items()]

    print(f"\nSending {len(send)} secrets (staged; applied on next deploy)...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        print("flyctl not found on PATH. Install it, or run `fly secrets set` by hand.")
        return 1
    except subprocess.TimeoutExpired:
        print("flyctl timed out.")
        return 1

    # flyctl echoes only names, never values, but scrub anyway before printing.
    def scrub(text: str) -> str:
        for v in send.values():
            if v:
                text = text.replace(v, "***")
        return text

    if proc.returncode != 0:
        print(scrub(proc.stderr or proc.stdout))
        return proc.returncode

    print(scrub(proc.stdout))
    print("Staged. Apply with:  fly deploy --config fly.ftm.toml\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
