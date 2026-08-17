#!/usr/bin/env python3
"""Keep the setup docs honest against the manifest.

The doctor tells someone "you cannot run this, do X instead". If X points at a
section that does not exist, or at a key nobody documented, the person is
stranded at exactly the moment they asked for help. These checks make that a
build failure rather than a support message.

    python tools/check_docs.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "skills" / "manifest.json"
PLAYBOOK = ROOT / "docs" / "setup" / "no-api-playbook.md"
GETTING = ROOT / "docs" / "setup" / "GETTING-STARTED.md"
ENV_EXAMPLE = ROOT / ".env.skills.example"
API_DIR = ROOT / "docs" / "api"


def anchors(md: str) -> set[str]:
    """Every anchor a link can target: explicit <a id> plus GitHub heading slugs."""
    found = set(re.findall(r'<a\s+id="([^"]+)"', md))
    for line in md.splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
        if m:
            slug = re.sub(r"[^a-z0-9\s-]", "", m.group(1).lower())
            found.add(re.sub(r"\s+", "-", slug.strip()))
    return found


def main() -> int:
    problems: list[str] = []
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    current = [e for e in doc["skills"] if e["status"] == "current"]

    play = PLAYBOOK.read_text(encoding="utf-8")
    play_anchors = anchors(play)
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    names = {e["name"] for e in doc["skills"]}

    for e in current:
        req = e["requires"]

        # 1. Every documented env var must appear in the example file, or
        #    nobody knows it exists.
        for var in req.get("env", []):
            if not re.search(rf"^{re.escape(var)}=", env_text, re.M):
                problems.append(f"{e['name']}: {var} is not in .env.skills.example")

        # 2. Every fallback must resolve: either a real sibling package or a
        #    real section of the playbook.
        fb = req.get("fallback")
        if not fb:
            continue
        if "#" in fb:
            page, _, anchor = fb.partition("#")
            if page != "no-api-playbook":
                problems.append(f"{e['name']}: fallback points at unknown page {page!r}")
            elif anchor not in play_anchors:
                problems.append(
                    f"{e['name']}: fallback anchor #{anchor} does not exist in the playbook")
        elif fb not in names:
            problems.append(f"{e['name']}: fallback names unknown package {fb!r}")

    # 3. Any skill needing a credential should have somewhere to send people.
    for e in current:
        req = e["requires"]
        if req["tier"] == "api" and not req.get("fallback"):
            problems.append(f"{e['name']}: tier 'api' with no fallback and no note")

    # 4. Files linked from the API index must exist.
    api_readme = (API_DIR / "README.md").read_text(encoding="utf-8")
    for link in re.findall(r"\]\((([a-z0-9-]+)\.md)\)", api_readme):
        if not (API_DIR / link[0]).is_file():
            problems.append(f"docs/api/README.md links to missing {link[0]}")

    # 5. No credential-shaped strings in anything we publish as documentation.
    secret = re.compile(r"(sk-ant-[A-Za-z0-9_-]{20,}|gh[pous]_[A-Za-z0-9]{20,}"
                        r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})")
    for p in list((ROOT / "docs").rglob("*.md")) + [ENV_EXAMPLE]:
        for m in secret.finditer(p.read_text(encoding="utf-8", errors="replace")):
            problems.append(f"{p.relative_to(ROOT).as_posix()}: possible credential "
                            f"{m.group(0)[:20]}...")

    # 6. The example env must never carry a filled-in value.
    for line in env_text.splitlines():
        if re.match(r"^[A-Z0-9_]+=.+", line.strip()):
            problems.append(f".env.skills.example has a value set: {line.split('=')[0]}")

    for p in problems:
        print(f"::error::{p}" if "--ci" in sys.argv else f"PROBLEM {p}")
    print(f"checked {len(current)} current packages, "
          f"{len(play_anchors)} playbook anchors; {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
