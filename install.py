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
import time
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


def fetch(url: str, timeout: int = 60, attempts: int = 5) -> bytes:
    """GET a URL, with one plain-text explanation per failure mode.

    Retries on 429 and 5xx. Installing the whole library is 23 requests in a
    row, which is enough for raw.githubusercontent.com to start rate-limiting
    partway through: the first full-library run died on a 429 at package 15.
    A transient throttle must not end the run, so back off and keep going.

    Corporate TLS interception is common enough on the laptops this runs on
    that a bare SSLCertVerificationError traceback would strand people. Say
    what happened and what to do instead.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "siftstack-installer"})
    delay = 2.0
    for attempt in range(1, attempts + 1):
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
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts:
                # Honour Retry-After when the server sends one; it knows
                # better than our backoff curve does.
                wait = delay
                hdr = e.headers.get("Retry-After") if e.headers else None
                if hdr:
                    try:
                        wait = max(wait, float(hdr))
                    except ValueError:
                        pass
                say(f"  {DIM}rate limited ({e.code}), waiting {wait:.0f}s "
                    f"[{attempt}/{attempts - 1}]{OFF}")
                time.sleep(wait)
                delay = min(delay * 2, 30)
                continue
            if e.code == 429:
                raise SystemExit(
                    f"{RED}GitHub is still rate limiting after {attempts} tries.{OFF}\n"
                    f"Wait a few minutes and re-run: already-installed packages are skipped,\n"
                    f"so it picks up where it left off."
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
            if attempt < attempts:
                say(f"  {DIM}network error ({e.reason}), retrying in {delay:.0f}s{OFF}")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise SystemExit(f"{RED}Network error:{OFF} {e.reason}\nURL: {url}")
    raise SystemExit(f"{RED}Gave up fetching{OFF} {url}")


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

    # Anything that raises past here must not leave a half-written staging
    # directory next to the real one. The first full-library run died on a
    # rate limit and left a "<name>.siftstack-tmp" folder behind, which then
    # looked like an installed package to anyone reading the directory.
    try:
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
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

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


def _load_dotenv() -> dict:
    """Read a .env in the current directory, if there is one.

    Not a dotenv library, and deliberately not exported into os.environ: the
    doctor only needs to know which names are SET, never their values, so it
    never has to hold a credential in memory or risk printing one.
    """
    found = {}
    for path in (Path.cwd() / ".env", HERE / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if v.strip().strip("'\""):
                found[k.strip()] = True
    return found


def doctor(doc: dict) -> int:
    """Report what runs right now, what needs a credential, what to do instead.

    The point is that nobody discovers a missing key by watching a skill fail
    halfway through a real deal. Every blocked skill prints its fallback on
    the same line, so "I cannot run this" always comes with "do this instead".
    """
    dotenv = _load_dotenv()
    have = lambda k: bool(os.environ.get(k)) or k in dotenv  # noqa: E731

    entries = [e for e in doc["skills"] if e["status"] == "current"]
    ready, login, blocked = [], [], []
    for e in entries:
        req = e["requires"]
        missing = [k for k in req.get("env", []) if not have(k)]
        if missing:
            blocked.append((e, missing))
        elif req.get("accounts"):
            # Env vars are satisfied but the skill still drives a service you
            # have to be signed in to. Calling that "ready" would be a lie the
            # person only discovers when the browser lands on a login page.
            login.append((e, []))
        else:
            ready.append((e, []))

    say(f"\n{BOLD}SiftStack skill library, readiness check{OFF}")
    say(f"{DIM}{len(entries)} current packages. Nothing here sends a request or spends money.{OFF}\n")

    if dotenv:
        say(f"{DIM}Read a .env with {len(dotenv)} value(s) set. Values are never displayed.{OFF}\n")

    say(f"{GREEN}Works right now, nothing to configure{OFF}  {DIM}({len(ready)} of {len(entries)}){OFF}")
    for e, _ in ready:
        say(f"  {GREEN}+{OFF} {e['name']}")

    if login:
        say(f"\n{BOLD}Keys are set, but you need to be signed in{OFF}  {DIM}({len(login)}){OFF}")
        for e, _ in login:
            accts = ", ".join(e["requires"]["accounts"])
            say(f"  {GREEN}+{OFF} {e['name']:<28} {DIM}sign in to {accts}{OFF}")

    if blocked:
        say(f"\n{YELLOW}Needs a credential{OFF}  {DIM}({len(blocked)}){OFF}")
        for e, missing in blocked:
            say(f"  {YELLOW}-{OFF} {e['name']:<28} {DIM}set {', '.join(missing)}{OFF}")
            cost = e["requires"].get("cost")
            if cost and cost != "Free":
                say(f"      {DIM}cost: {cost}{OFF}")
            fb = e["requires"].get("fallback")
            if fb:
                if "#" in fb:
                    say(f"      {DIM}without it: docs/setup/{fb.replace('#', '.md, section ')}{OFF}")
                else:
                    known = [x["name"] for x, _ in ready] + [x["name"] for x, _ in login]
                    ok = "already installed" if fb in known else "install it"
                    say(f"      {DIM}without it: use the {fb} skill ({ok}){OFF}")
            else:
                say(f"      {DIM}without it: no substitute, this one needs the key{OFF}")

    # Accounts are logins rather than env vars, so they cannot be detected.
    # Listing them is the only honest thing to do.
    accounts = {}
    for e in entries:
        for a in e["requires"].get("accounts", []):
            accounts.setdefault(a, []).append(e["name"])
    if accounts:
        say(f"\n{BOLD}Logins these assume you already have{OFF}")
        say(f"{DIM}Cannot be checked from here. Confirm you can sign in.{OFF}")
        for a in sorted(accounts):
            names = accounts[a]
            shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
            say(f"  {a:<26} {DIM}{shown}{OFF}")

    say(f"\n{BOLD}Next{OFF}")
    if blocked:
        say("  Copy .env.skills.example to .env and fill in only the keys you want.")
        say("  Every skill degrades rather than failing: a missing key skips that step.")
    say("  Full setup guide:   docs/setup/GETTING-STARTED.md")
    say("  No-API routes:      docs/setup/no-api-playbook.md")
    say("  API contracts:      docs/api/\n")
    return 0


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
    ap.add_argument("--doctor", action="store_true",
                    help="what works right now, what needs a key, and what to do instead")
    args = ap.parse_args()

    doc, local = load_manifest(prefer_local=not args.remote)
    entries = doc["skills"]

    if args.doctor:
        return doctor(doc)

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
        if not local and not args.dry_run and result in ("installed", "updated"):
            time.sleep(0.3)
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
