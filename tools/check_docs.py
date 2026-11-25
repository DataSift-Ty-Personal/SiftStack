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


def check_marketplace(current: list[dict]) -> list[str]:
    """The plugin marketplace must offer exactly what the manifest says exists.

    Freshness (is the file regenerated?) is a git-diff gate in CI. This checks
    the things a stale-but-committed file cannot tell you: that every package
    is reachable by ONE install, and that nothing will be refused downstream.
    """
    out: list[str] = []
    mp = ROOT / ".claude-plugin" / "marketplace.json"
    if not mp.is_file():
        return ["no .claude-plugin/marketplace.json. Run: python tools/build_skills.py --marketplace"]
    market = json.loads(mp.read_text(encoding="utf-8"))
    entries = {p["name"]: p for p in market["plugins"]}
    names = {e["name"] for e in current}

    for n in sorted(names - set(entries)):
        out.append(f"marketplace: {n} is a current package but is not installable as a plugin")

    deps: dict[str, list[str]] = {}
    for name, p in entries.items():
        src = ROOT / p["source"]
        if not src.is_dir():
            out.append(f"marketplace: {name} source {p['source']} does not exist")
            continue
        # Claude Code treats a declared version as the ONLY update signal.
        pj = src / ".claude-plugin" / "plugin.json"
        manifest = json.loads(pj.read_text(encoding="utf-8")) if pj.is_file() else {}
        if "version" in p or "version" in manifest:
            out.append(f"marketplace: {name} declares a version, which pins it and blocks updates")
        # Cowork's organization sync rejects the whole plugin for this.
        if (src / "bin").is_dir():
            out.append(f"marketplace: {name} has a top-level bin/, which Cowork refuses. Use scripts/")
        if "dependencies" in manifest:
            deps[name] = manifest["dependencies"]
            for d in manifest["dependencies"]:
                if d not in entries:
                    out.append(f"marketplace: bundle {name} depends on unknown plugin {d}")

    if "siftstack-all" not in deps:
        out.append("marketplace: no siftstack-all bundle, so there is no one-install route")
    else:
        for n in sorted(names - set(deps["siftstack-all"])):
            out.append(f"marketplace: siftstack-all does not install {n}")
    division = [d for b, ds in deps.items() if b != "siftstack-all" for d in ds
                if d != "siftstack-core"]
    for n in sorted(names - {"siftstack-core"}):
        if division.count(n) != 1:
            out.append(f"marketplace: {n} is in {division.count(n)} division bundles, expected 1")
    return out


# Nine and up only. "Two superseded packages" and "three DataSift skills" are
# small named subsets, not library totals, and flagging them is noise. Every
# count that actually went stale was library-scale ("Nine skills", "Eleven of").
_WORD_NUMBERS = ("nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
                 "eighteen nineteen twenty thirty").split()


def check_counts(doc: dict) -> list[str]:
    """Hand-written package counts must match the manifest.

    Four documents carried four different wrong totals at once (24, 22, nine,
    21) because nothing checked them. Counts are written in a few fixed
    phrasings so they can be checked, and spelled-out numbers are refused
    because a regex cannot verify "Eleven".
    """
    out: list[str] = []
    cur, tiers = doc["counts"]["current"], doc["tier_counts"]
    rules = [
        (r"\b(\d+) current packages\b", cur, "current packages"),
        (r"\b(\d+) work on install\b", tiers["none"], "tier none"),
        (r"\| No credentials \| (\d+) \|", tiers["none"], "tier none"),
        (r"\| An account you already have \| (\d+) \|", tiers["account"], "tier account"),
        (r"\| A metered API key \| (\d+) \|", tiers["api"], "tier api"),
    ]
    words = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b[\w -]{0,20}?\b(skills|packages)\b", re.I)
    for path in (ROOT / "README.md", ROOT / "docs" / "setup" / "GETTING-STARTED.md", ENV_EXAMPLE):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        for pattern, expect, label in rules:
            for m in re.finditer(pattern, text):
                if int(m.group(1)) != expect:
                    out.append(f"{rel}: says {m.group(0)!r} but the manifest has {expect} ({label})")
        for m in words.finditer(text):
            out.append(f"{rel}: {m.group(0)!r} is a spelled-out count nothing can verify. "
                       "Write it as digits, e.g. '27 current packages'")
    return out


# Users reach DataSift with their Open API key only (REISIFT_API_KEY, "Authorization:
# Api-Key <key>"). Nothing a user reads may tell them to use a DataSift login: no
# password, no token copied out of the browser, no JWT (Ty, 2026-11-24). A file whose
# module docstring (or, for any other file, its first lines) says `staff-only` is staff
# tooling shipped beside the community path and is exempt. Generated copies are not
# scanned; their sources are (the REQUIRES table for the manifest and core's data).
LOGIN_ROOTS = ("skills", "plugins", "docs/setup", "docs/api")
LOGIN_FILES = (".env.skills.example",)
LOGIN_GENERATED = {"skills/manifest.json", "plugins/siftstack-core/data/requires.json",
                   "plugins/siftstack-core/data/env.skills.example"}
LOGIN_EXT = {".md", ".py", ".json", ".txt", ".yaml", ".yml", ".js", ".sh", ".example", ".toml", ".cfg", ".ini"}
STAFF_ONLY = re.compile(r"staff[- ]only", re.I)
LOGIN_ALWAYS = [
    (re.compile(r"\b(DATASIFT_EMAIL|DATASIFT_PASSWORD|REISIFT_TOKEN|REISIFT_TARGET_JWT)\b"),
     "a DataSift login variable"),
    (re.compile(r"\bjwt\b", re.I), "a JWT"),
    (re.compile(r"(?<![\w-])--(jwt|jwt-file|email|password)\b"), "a login flag"),
    (re.compile(r"/api/token/"), "the route that trades a password for a token"),
    (re.compile(r"\blogin token\b", re.I), "a login token"),
    (re.compile(r"\b(datasift|reisift)\s+(logins?|passwords?)\b", re.I), "a DataSift login"),
]
# Words that are fine about other services (SmartSkip has a password, a county portal is read
# with DevTools) and wrong about DataSift: flagged only with DataSift in the line or the two
# lines either side of it.
LOGIN_NEAR_DATASIFT = [
    (re.compile(r"\bdev\s?tools\b", re.I), "DevTools"),
    (re.compile(r"\bcopy as curl\b", re.I), "Copy as cURL"),
    (re.compile(r"\bbearer\b", re.I), "a Bearer token"),
    (re.compile(r"\bpassword\b", re.I), "a password"),
]
DATASIFT_WORD = re.compile(r"datasift|reisift|apiv2\.", re.I)


def _staff_only(path: Path, text: str) -> bool:
    if path.suffix == ".py":
        import ast
        try:
            doc = ast.get_docstring(ast.parse(text)) or ""
        except SyntaxError:
            doc = ""
        return bool(STAFF_ONLY.search(doc))
    return bool(STAFF_ONLY.search("\n".join(text.splitlines()[:15])))


def login_hits(text: str) -> list[tuple[int, str, str]]:
    """(line number, what it names, the line) for every DataSift login mention in `text`."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        for pat, what in LOGIN_ALWAYS:
            if pat.search(line):
                out.append((i + 1, what, line.strip()))
                break
        else:
            near = "\n".join(lines[max(0, i - 2): i + 3])
            for pat, what in LOGIN_NEAR_DATASIFT:
                if pat.search(line) and DATASIFT_WORD.search(near):
                    out.append((i + 1, what, line.strip()))
                    break
    return out


def check_login_wording(root: Path = ROOT, requires: dict | None = None) -> list[str]:
    """Every DataSift login mention in what a user installs or reads, as problems."""
    paths = []
    for base in LOGIN_ROOTS:
        top = root / base
        if top.is_dir():
            paths += [p for p in sorted(top.rglob("*")) if p.is_file() and p.suffix in LOGIN_EXT
                      and "__pycache__" not in p.parts]
    paths += [root / f for f in LOGIN_FILES if (root / f).is_file()]
    out = []
    for p in paths:
        rel = p.relative_to(root).as_posix()
        if rel in LOGIN_GENERATED or p.stat().st_size > 4_000_000:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if _staff_only(p, text):
            continue
        for n, what, line in login_hits(text):
            out.append("%s:%d names %s, but users reach DataSift with their Open API key only: %s"
                       % (rel, n, what, line[:140]))
    if requires is None:
        sys.path.insert(0, str(root / "tools"))
        try:
            from build_skills import REQUIRES as requires  # noqa: N811
        except ImportError:
            requires = {}
    for name, req in sorted((requires or {}).items()):
        said = " ".join(str(v) for k, v in req.items() if k in ("note", "cost", "env", "accounts"))
        for _, what, _line in login_hits(said):
            out.append("tools/build_skills.py REQUIRES[%r] names %s; it reaches the manifest and the "
                       "doctor, so it must speak of the Open API key only" % (name, what))
    return out


def main() -> int:
    problems: list[str] = []
    problems.extend(check_login_wording())
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

    problems.extend(check_marketplace(current))
    problems.extend(check_counts(doc))

    for p in problems:
        print(f"::error::{p}" if "--ci" in sys.argv else f"PROBLEM {p}")
    print(f"checked {len(current)} current packages, "
          f"{len(play_anchors)} playbook anchors; {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
