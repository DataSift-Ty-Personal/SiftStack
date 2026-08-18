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

# Platform limits enforced by Claude Code at INSTALL time, not by us. Breaking
# one does not fail here, it fails on a community member's machine with
# "Plugin validation failed", after they have already run the install command
# and told other people it is broken. That happened once, with a description
# of 1105 characters in the sift-operations plugin.
#
# Anything the platform validates belongs in this list.
DESCRIPTION_MAX = 1024
NAME_MAX = 64
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Warn before it becomes an error, so a small edit does not tip a skill over
# the line without anyone noticing.
DESCRIPTION_WARN = int(DESCRIPTION_MAX * 0.9)
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


def check_platform_limits() -> tuple[list[str], list[str]]:
    """Validate every SKILL.md the way Claude Code will at install time.

    Covers nested skills inside plugins too. The one that broke shipped inside
    a plugin, where the top-level frontmatter check never looked.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    from build_skills import read_frontmatter  # noqa: E402

    problems: list[str] = []
    warnings: list[str] = []
    for base in ("skills", "plugins"):
        root = ROOT / base
        if not root.is_dir():
            continue
        for smd in sorted(root.rglob("SKILL.md")):
            rel = smd.relative_to(ROOT).as_posix()
            fm = read_frontmatter(smd.read_text(encoding="utf-8", errors="replace"))

            name = fm.get("name", "")
            desc = fm.get("description", "") or ""

            if not name:
                problems.append(f"{rel}: no 'name' in frontmatter")
            else:
                if len(name) > NAME_MAX:
                    problems.append(f"{rel}: name is {len(name)} chars, limit {NAME_MAX}")
                if not NAME_RE.match(name):
                    problems.append(
                        f"{rel}: name {name!r} must be lowercase letters, digits "
                        f"and single hyphens")

            if not desc:
                problems.append(f"{rel}: no 'description' in frontmatter, so it can never trigger")
            elif len(desc) > DESCRIPTION_MAX:
                problems.append(
                    f"{rel}: description is {len(desc)} chars, limit {DESCRIPTION_MAX}. "
                    f"Claude Code rejects the whole package at install time.")
            elif len(desc) > DESCRIPTION_WARN:
                warnings.append(
                    f"{rel}: description is {len(desc)} chars, "
                    f"{DESCRIPTION_MAX - len(desc)} from the {DESCRIPTION_MAX} limit")
    return problems, warnings


# Paths a skill tells Claude to open. A skill is mostly a routing table, so a
# route that does not resolve is the whole feature failing: Claude reads the
# table, opens the file, finds nothing, and the person who installed it reports
# a broken command. That is how /sift-workflow shipped pointing at two
# reference files nobody had written.
ROUTE_RE = re.compile(
    r"(?:\$\{CLAUDE_PLUGIN_ROOT\}/)?"
    r"((?:[\w.-]+/)*(?:references|scripts|assets|data|commands)/[\w.-]+\.[\w]+)")


def route_roots(md: Path) -> list[Path]:
    """Every directory a route in this file could legitimately be written from.

    Packages address each other three ways and all three are in use: inside a
    plugin ${CLAUDE_PLUGIN_ROOT} is the plugin folder, deal-analyzer names its
    bundled siblings as `rehab-estimator/references/...` relative to the
    plugin's skills folder, and sift-operations names a companion skill's
    script the same way relative to skills/. Checking only one of those turns
    a working route into a false alarm, and a checker people learn to ignore
    is worse than no checker.
    """
    roots = [md.parent]
    for parent in md.parents:
        if parent == ROOT:
            break
        if (parent / "SKILL.md").is_file() or (parent / ".claude-plugin").is_dir():
            roots.append(parent)
        if (parent / ".claude-plugin").is_dir() and (parent / "skills").is_dir():
            roots.append(parent / "skills")
    roots.append(ROOT / "skills")
    roots.append(ROOT)
    return roots


def check_routes() -> list[str]:
    """Every references/ or scripts/ path a package points at must exist."""
    problems: list[str] = []
    for base in ("skills", "plugins"):
        top = ROOT / base
        if not top.is_dir():
            continue
        for md in sorted(top.rglob("*.md")):
            text = md.read_text(encoding="utf-8", errors="replace")
            roots = route_roots(md)
            for route in sorted(set(ROUTE_RE.findall(text))):
                # SKILL_DIR is the placeholder a skill uses when it has to
                # print a runnable command, not a folder on disk.
                probe = route[len("SKILL_DIR/"):] if route.startswith("SKILL_DIR/") else route
                if any((r / probe).exists() for r in roots):
                    continue
                problems.append(
                    f"{md.relative_to(ROOT).as_posix()}: points at {route}, "
                    f"which does not exist")
    return problems


def main() -> int:
    problems: list[str] = []
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))

    plat_problems, plat_warnings = check_platform_limits()
    problems.extend(plat_problems)
    problems.extend(check_routes())
    for w in plat_warnings:
        print(f"::warning::{w}" if "--ci" in sys.argv else f"WARN    {w}")
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
