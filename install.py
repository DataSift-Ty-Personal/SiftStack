#!/usr/bin/env python3
"""Install the SiftStack REI skill library into Claude Code.

    # everything current
    python install.py

    # pick what you want
    python install.py --only rehab-estimator comp-package deep-prospecting-v5
    python install.py --category "Deal Analysis"

    # see what would happen, touch nothing
    python install.py --list
    python install.py --dry-run

By default this fetches over HTTPS from GitHub, so it works from a bare
directory with no clone. Run it inside a checkout and it installs from the
local source trees instead, which is what you want while editing a skill.

Skills land in ~/.claude/skills/<name>/ and plugins in ~/.claude/plugins/.
Use --dest to put them in a project instead (e.g. --dest .claude/skills) when
a skill should travel with a repo rather than follow the user.

Stdlib only. No pip install, no virtualenv, no clone.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "DataSift-Ty-Personal/SiftStack"
BRANCH = os.environ.get("SIFTSTACK_BRANCH", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
MANIFEST_URL = f"{RAW}/skills/manifest.json"

# `curl ... | python3 -` is the headline install command, and piping a script
# through stdin leaves __file__ undefined. Without this fallback the very
# first thing a new user runs dies on a NameError before printing anything.
# Piped means there is no checkout, so the local-source path is off by
# definition and we resolve everything over HTTPS.
try:
    HERE = Path(__file__).resolve().parent
    PIPED = False
except NameError:
    HERE = Path.cwd()
    PIPED = True

LOCAL_MANIFEST = HERE / "skills" / "manifest.json"

GREEN, YELLOW, RED, DIM, BOLD, OFF = (
    ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    else ("", "", "", "", "", "")
)


# Windows consoles still default to cp1252, and skill descriptions contain
# arrows and dashes that cp1252 cannot encode. Without this, listing the
# catalog dies on a UnicodeEncodeError before printing half of it, on the
# exact machines most of the people installing this are using.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: int = 60) -> bytes:
    """GET a URL, with one plain-text explanation per failure mode.

    Corporate TLS interception is common enough on the laptops this runs on
    that a bare SSLCertVerificationError traceback would strand people. Say
    what happened and what to do instead.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "siftstack-installer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(
                f"{RED}Not found:{OFF} {url}\n"
                f"The branch '{BRANCH}' may not carry this file yet. "
                f"Set SIFTSTACK_BRANCH to a branch that does."
            )
        raise SystemExit(f"{RED}HTTP {e.code}{OFF} fetching {url}")
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            raise SystemExit(
                f"{RED}TLS verification failed.{OFF}\n"
                f"This is usually a corporate proxy intercepting HTTPS. Either run this on an\n"
                f"unfiltered network, or clone the repo and run install.py from inside it:\n"
                f"  git clone https://github.com/{REPO}.git && cd SiftStack && python install.py"
            )
        raise SystemExit(f"{RED}Network error:{OFF} {e.reason}\nURL: {url}")


def load_manifest(prefer_local: bool) -> tuple[dict, bool]:
    if prefer_local and not PIPED and LOCAL_MANIFEST.is_file():
        return json.loads(LOCAL_MANIFEST.read_text(encoding="utf-8")), True
    return json.loads(fetch(MANIFEST_URL).decode("utf-8")), False


def default_dest(kind: str) -> Path:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return root / ("plugins" if kind == "plugin" else "skills")


def _safe_members(zf: zipfile.ZipFile, dest: Path):
    """Yield (info, target) pairs, refusing anything that escapes dest."""
    dest = dest.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        target = (dest / info.filename).resolve()
        if not str(target).startswith(str(dest) + os.sep):
            raise SystemExit(f"{RED}Refusing path traversal in archive:{OFF} {info.filename}")
        yield info, target


def install_one(entry: dict, dest_root: Path, local: bool, dry: bool, force: bool) -> str:
    name = entry["name"]
    dest = dest_root / name

    if dest.exists() and not force:
        marker = dest / ".siftstack-version"
        installed = marker.read_text(encoding="utf-8").strip() if marker.is_file() else None
        if installed and installed == entry.get("sha256"):
            return "current"

    if dry:
        return "would install"

    staging = dest.with_name(dest.name + ".siftstack-tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    if local:
        src = HERE / entry["source_dir"]
        if not src.is_dir():
            raise SystemExit(f"{RED}Missing local source:{OFF} {src}")
        shutil.copytree(src, staging, dirs_exist_ok=True)
    else:
        blob = fetch(entry["download"])
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info, target in _safe_members(zf, staging):
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as s, open(target, "wb") as o:
                    shutil.copyfileobj(s, o)

    if entry.get("sha256"):
        (staging / ".siftstack-version").write_text(entry["sha256"] + "\n", encoding="utf-8")

    # Swap last. A half-written skill directory is worse than an old one,
    # because Claude will load it and behave in a way nobody can reproduce.
    existed = dest.exists()
    if existed:
        backup = dest.with_name(dest.name + ".siftstack-old")
        if backup.exists():
            shutil.rmtree(backup)
        dest.rename(backup)
    staging.rename(dest)
    if existed:
        shutil.rmtree(dest.with_name(dest.name + ".siftstack-old"), ignore_errors=True)
    return "updated" if existed else "installed"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", metavar="NAME", help="install just these packages")
    ap.add_argument("--category", nargs="+", metavar="CAT", help="install a whole category")
    ap.add_argument("--all", action="store_true",
                    help="include superseded packages (default installs current only)")
    ap.add_argument("--dest", metavar="DIR", help="install skills here instead of ~/.claude/skills")
    ap.add_argument("--list", action="store_true", help="show the catalog and exit")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    ap.add_argument("--force", action="store_true", help="reinstall even if already current")
    ap.add_argument("--remote", action="store_true", help="ignore a local checkout, always fetch")
    args = ap.parse_args()

    doc, local = load_manifest(prefer_local=not args.remote)
    entries = doc["skills"]

    if args.list:
        say(f"\n{BOLD}SiftStack REI skill library{OFF}  "
            f"{doc['counts']['current']} current packages, {doc['counts']['total']} total\n")
        for cat in doc["categories"]:
            say(f"{BOLD}{cat}{OFF}")
            for e in (x for x in entries if x["category"] == cat):
                tag = "" if e["status"] == "current" else f"  {YELLOW}[superseded by {e['superseded_by']}]{OFF}"
                say(f"  {e['name']:<28} {DIM}{e['kind']:<7}{OFF}{tag}")
                desc = (e["description"] or "").strip()
                if desc:
                    say(f"    {DIM}{desc[:150]}{'...' if len(desc) > 150 else ''}{OFF}")
            say()
        say(f"{DIM}Install all:  python install.py{OFF}")
        say(f"{DIM}Install some: python install.py --only rehab-estimator comp-package{OFF}\n")
        return 0

    wanted = [e for e in entries if args.all or e["status"] == "current"]
    if args.category:
        cats = {c.lower() for c in args.category}
        wanted = [e for e in wanted if e["category"].lower() in cats]
        if not wanted:
            raise SystemExit(f"{RED}No packages in{OFF} {args.category}. "
                             f"Known: {', '.join(doc['categories'])}")
    if args.only:
        by_name = {e["name"]: e for e in entries}
        unknown = [n for n in args.only if n not in by_name]
        if unknown:
            raise SystemExit(f"{RED}Unknown package(s):{OFF} {', '.join(unknown)}\n"
                             f"Run `python install.py --list` to see the catalog.")
        wanted = [by_name[n] for n in args.only]

    src_label = "local checkout" if local else f"github.com/{REPO}@{BRANCH}"
    say(f"\n{BOLD}SiftStack skill install{OFF}  {DIM}source: {src_label}{OFF}")
    if args.dry_run:
        say(f"{YELLOW}Dry run. Nothing will be written.{OFF}")
    say()

    tally: dict[str, int] = {}
    for e in wanted:
        root = Path(args.dest).expanduser() if args.dest else default_dest(e["kind"])
        root.mkdir(parents=True, exist_ok=True)
        try:
            result = install_one(e, root, local, args.dry_run, args.force)
        except SystemExit:
            raise
        except Exception as exc:  # keep going; one bad package must not stop the rest
            say(f"  {RED}failed{OFF}     {e['name']}  ({exc})")
            tally["failed"] = tally.get("failed", 0) + 1
            continue
        colour = {"installed": GREEN, "updated": GREEN, "current": DIM}.get(result, YELLOW)
        say(f"  {colour}{result:<10}{OFF} {e['name']:<28} {DIM}-> {root / e['name']}{OFF}")
        tally[result] = tally.get(result, 0) + 1

    say()
    say("  ".join(f"{BOLD}{v}{OFF} {k}" for k, v in sorted(tally.items())) or "nothing to do")
    if not args.dry_run and (tally.get("installed") or tally.get("updated")):
        say(f"\n{GREEN}Done.{OFF} Restart Claude Code, then run {BOLD}/help{OFF} "
            f"or just describe the task and the right skill will trigger.")
    say()
    return 1 if tally.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
