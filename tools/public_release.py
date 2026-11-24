#!/usr/bin/env python
"""Publish SiftStack's public main branch from this working branch, with nothing private in it.

    python tools/public_release.py build             # export HEAD, scrub, regenerate, scan
    python tools/public_release.py test              # build, then every check and selftest inside the release
    python tools/public_release.py publish           # build, test, commit onto main in its own worktree
    python tools/public_release.py publish --push    # ... and push main

Main is the evergreen, installable SiftStack. This branch is where the owner works, and it carries his own
business (his team, accounts, deals and notes). Main is never merged from here: each release is ONE new
commit on main whose tree is this branch's HEAD after the scrub, so none of this branch's history reaches it.

The scrub, in order:
  1. export the committed tree of --ref (default HEAD) with git archive;
  2. leave out the private files (the `exclude` globs);
  3. move the public overlays into place (`overlay`: public/CLAUDE.public.md becomes CLAUDE.md);
  4. blank the account-specific ids in the playbook (uuids, design numbers, the callers list);
  5. replace names, account labels, emails and paths by the ordered `substitutions`, and rename paths;
  6. regenerate everything generated (script catalog, team docs, agent map, skill archives, marketplace);
  7. scan every file, archives included (tools/public_scan.py with the private words): one finding stops it.

The private half (what to leave out, the phrase map, the private scan words) lives in
private/public_release.json. It is committed to the PRIVATE repo only (since 2026-11-24, so the issue
fixer's box releases with the current map), and every release leaves private/ out whatever the map says;
the scan would refuse it anyway, since it spells the private words. Without it the tool refuses to run.
PUBLIC_RELEASE_PRIVATE points at another copy.

Two repositories: the owner's checkout has origin (the PRIVATE working repo, every branch and its history)
and `public` (the published repo, main only). A pre-push hook in the owner's checkout refuses any push to
the public repo that is not a release on main; this tool sets PUBLIC_RELEASE=1, which the hook asks for.

Who runs it (Ty, 2026-11-24: "let's just do a release whenever a fix goes out"): the issue fixer, after
each fix it ships (tools/issue_fixer/fixer.py, release_due), and a person for anything else. A release's
commit message names the lessons it adds to docs/lessons-learned.md, and the last line printed is
`RESULT released|unchanged|stopped ...`, which the fixer reads.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path(os.environ.get("PUBLIC_RELEASE_PRIVATE") or (ROOT / "private" / "public_release.json"))
WORK = Path(os.environ.get("PUBLIC_RELEASE_WORK", Path(tempfile.gettempdir()) / "siftstack_public_release"))
MAIN_WT = Path(os.environ.get("PUBLIC_RELEASE_MAIN_WORKTREE", Path(tempfile.gettempdir()) / "siftstack_main_worktree"))
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Left out of every release whatever the map says: the map itself lives there.
ALWAYS_LEAVE_OUT = ["private/**"]
LESSONS = "docs/lessons-learned.md"
LESSON_RX = re.compile(r"^## (.+?)\s*$", re.M)
BUILT_FROM_RX = re.compile(r"Built by tools/public_release\.py from ([0-9a-f]{7,40})")
sys.path.insert(0, str(ROOT / "tools"))
import public_scan  # noqa: E402


def say(msg: str) -> None:
    print(msg, flush=True)


def load_private() -> dict:
    if not PRIVATE.exists():
        raise SystemExit("no %s: the private half of the release (exclusions, names) is required" % PRIVATE)
    return json.loads(PRIVATE.read_text(encoding="utf-8"))


def git(*args, cwd=ROOT, check=True, capture=True, env_extra: dict | None = None) -> str:
    env = dict(os.environ, **(env_extra or {})) if env_extra else None
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=capture, text=True, env=env)
    if check and r.returncode:
        raise SystemExit("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:400]))
    return (r.stdout or "").strip()


def public_remote() -> str:
    """The remote that is the PUBLIC repo. The owner's checkout has two: origin is the private working
    repo and `public` is the published one (a pre-push hook there refuses anything but a release on main).
    A plain clone of the public repo has only origin, which then is the public one."""
    named = os.environ.get("PUBLIC_RELEASE_REMOTE", "")
    if named:
        return named
    remotes = git("remote").split()
    return "public" if "public" in remotes else "origin"


def export(ref: str, dest: Path) -> None:
    """The committed tree of `ref`, or with ref "WORKTREE" the files git would track right now (tracked plus
    untracked, ignored left out): only for trying the tool out, a release is always built from a commit."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if ref == "WORKTREE":
        names = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT,
                               capture_output=True, check=True).stdout.decode("utf-8").split(chr(0))
        for n in filter(None, names):
            s = ROOT / n
            if s.is_file():
                (dest / n).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(s, dest / n)
        return
    # The files exactly as committed. git archive otherwise converts line endings on the way out by the
    # working-tree rules (core.autocrlf on Windows, eol=lf under skills/), so two byte-identical files
    # in the commit came out different and the test that pins a copy to its source failed.
    data = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=tar", ref],
                          cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        try:
            t.extractall(dest, filter="data")
        except TypeError:
            t.extractall(dest)


def files(dest: Path) -> list[Path]:
    return sorted(p for p in dest.rglob("*") if p.is_file())


def leave_out(dest: Path, patterns: list[str]) -> list[str]:
    match = public_scan.matcher(patterns)
    gone = []
    for p in files(dest):
        rel = p.relative_to(dest).as_posix()
        if match(rel):
            p.unlink()
            gone.append(rel)
    for d in sorted((d for d in dest.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        if not any(d.iterdir()):
            d.rmdir()
    return gone


def overlay(src_tree: Path, dest: Path, mapping: dict, strict: bool = True) -> None:
    """Overlay files come from the ORIGINAL export (they were just left out of dest by the public/** rule)."""
    for src, target in mapping.items():
        s = src_tree / src
        if not s.exists():
            if not strict:
                say("  overlay %s is missing (dry run from the working tree: carrying on)" % src)
                continue
            raise SystemExit("overlay %s is missing: the release needs it" % src)
        t = dest / target
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s, t)


def is_text(p: Path) -> bool:
    b = p.read_bytes()[:8192]
    if b"\0" in b:
        return False
    try:
        b.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def playbook(dest: Path, rules: dict) -> int:
    p = dest / rules["path"]
    if not p.exists():
        return 0
    t = p.read_text(encoding="utf-8")
    n = len(UUID.findall(t))
    t = UUID.sub(rules.get("uuid_placeholder", "00000000-0000-0000-0000-000000000000"), t)
    for rx, rep in rules.get("line_rules", []):
        t = re.sub(rx, rep, t, flags=re.M)
    p.write_text(t, encoding="utf-8", newline="\n")
    return n


def substitute(dest: Path, subs: list, skip_globs: list) -> Counter:
    skip = public_scan.matcher(skip_globs)
    compiled = [(re.compile(a) if kind == "regex" else a, b, kind) for a, b, kind in subs]
    counts: Counter = Counter()
    for p in files(dest):
        rel = p.relative_to(dest).as_posix()
        if skip(rel) or not is_text(p):
            continue
        raw = p.read_bytes()
        t = raw.decode("utf-8")
        before = t
        for pat, rep, kind in compiled:
            if kind == "regex":
                t, k = pat.subn(rep, t)
            else:
                k = t.count(pat)
                t = t.replace(pat, rep)
            if k:
                counts[rep] += k
        if t != before:
            p.write_bytes(t.encode("utf-8"))
    return counts


def rename_paths(dest: Path, subs: list) -> list[tuple]:
    moved = []
    for p in files(dest):
        rel = p.relative_to(dest).as_posix()
        new = rel
        for rx, rep in subs:
            new = re.sub(rx, rep, new)
        if new != rel:
            target = dest / new
            target.parent.mkdir(parents=True, exist_ok=True)
            p.rename(target)
            moved.append((rel, new))
    return moved


def run(cmd: list, cwd: Path, label: str, timeout: int = 1800, env_extra: dict | None = None) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("REISIFT", "DATASIFT", "ANTHROPIC", "OPENROUTER", "SLACK", "SMRTPHONE", "TRESTLE",
                                "SMARTSKIP", "TRACERFY", "ENFORMION", "SCRAPFLY", "SERPER", "SERPAPI", "FIRECRAWL",
                                "FLY_", "APIFY", "AGENTS_", "SMS_AGENT", "OPENWEBNINJA", "GOOGLE", "DROPBOX"))}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(env_extra or {})
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    say("  %-44s exit %d" % (label, r.returncode))
    return r.returncode, out


def regenerate(dest: Path, py: str) -> list[str]:
    problems = []
    steps = [
        ([py, "src/agents/commands.py", "--write"], "script catalog (team/commands.json)"),
        ([py, "src/agents/cli.py", "team"], "team docs (TEAM.md, COVERAGE.md)"),
        ([py, "tools/agent_map.py"], "agent map (docs/AGENT-MAP.md)"),
        ([py, "tools/build_agent_page.py"], "agent page (docs/agent-system.html)"),
        ([py, "tools/build_skills.py", "--marketplace", "--build", "--manifest"], "skills, archives, marketplace"),
    ]
    for cmd, label in steps:
        code, out = run(cmd, dest, label, timeout=900)
        if code:
            problems.append("%s: %s" % (label, out.strip()[-600:]))
    return problems


def build(ref: str, py: str) -> tuple[Path, dict]:
    cfg = load_private()
    src = WORK / "export"
    dest = WORK / "release"
    say("export %s -> %s" % (ref, dest))
    export(ref, src)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    gone = leave_out(dest, ALWAYS_LEAVE_OUT + list(cfg["exclude"]))
    overlay(src, dest, cfg.get("overlay", {}), strict=ref != "WORKTREE")
    blanked = playbook(dest, cfg["playbook"]) if cfg.get("playbook") else 0
    counts = substitute(dest, cfg["substitutions"], cfg.get("skip_substitution", []))
    moved = rename_paths(dest, cfg.get("path_substitutions", []))
    say("left out %d file(s); overlaid %d; blanked %d playbook id(s); %d replacement(s); renamed %d path(s)" % (
        len(gone), len(cfg.get("overlay", {})), blanked, sum(counts.values()), len(moved)))
    problems = regenerate(dest, py)
    sc = public_scan.Scanner(public_scan.load_rules(dest / "tools" / "public_scan_rules.json"), cfg)
    findings = sc.scan_tree(dest)
    report = {"ref": ref if ref == "WORKTREE" else git("rev-parse", ref), "left_out": gone, "renamed": moved, "replacements": dict(counts),
              "regen_problems": problems, "findings": [list(f) for f in findings]}
    (WORK / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    for f in findings[:80]:
        say("  FOUND %s:%d %s %s" % tuple(f))
    say("scan: %d finding(s); regeneration problems: %d; report %s" % (len(findings), len(problems), WORK / "report.json"))
    return dest, report


def checks(release: Path, py: str, pytest_files: list[str]) -> list[str]:
    """What CI runs on main, plus both selftests, inside a COPY of the release tree, so nothing a test
    writes (output/, caches) can reach the published tree."""
    dest = WORK / "check"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(release, dest)
    failed = []
    steps = [
        ([py, "tools/agent_map.py", "--audit"], "agent map: every file reference exists"),
        ([py, "tools/agent_map.py", "--check"], "agent map is current"),
        ([py, "tools/build_skills.py", "--verify"], "skill archives match their source"),
        ([py, "tools/check_docs.py"], "docs and packages"),
        ([py, "src/agents/cli.py", "selftest"], "agents selftest"),
        ([py, "src/sms_agent/cli.py", "selftest"], "texting selftest"),
    ]
    for cmd, label in steps:
        # an hour, not half: the issue fixer's box (two shared cores) runs the same suites for its own gate
        code, out = run(cmd, dest, label, timeout=3600)
        if code:
            failed.append("%s:\n%s" % (label, "\n".join(l for l in out.splitlines() if "FAIL" in l or "rror" in l)[-1500:]
                                       or out.strip()[-800:]))
    dashes = [p for p in list((dest / "docs").rglob("*.md")) + [dest / "README.md", dest / "CLAUDE.md"]
              if p.exists() and re.search("[\u2013\u2014]", p.read_text(encoding="utf-8", errors="replace"))]
    if dashes:
        failed.append("long dashes in: %s" % ", ".join(p.relative_to(dest).as_posix() for p in dashes))
    if pytest_files:
        present = [f for f in pytest_files if (dest / f).exists()]
        code, out = run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *present], dest, "pytest (%d files)" % len(present),
                        timeout=3600)
        if code:
            failed.append("pytest:\n" + out.strip()[-2500:])
    return failed


def lesson_titles(path: Path) -> list[str]:
    """The lessons in docs/lessons-learned.md, by their headings, newest first."""
    try:
        return LESSON_RX.findall(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def release_message(day: str, new_lessons: list[str], message: str | None = None) -> str:
    """The release commit's subject, then the lessons it adds, so main's history reads as what was fixed."""
    if message:
        head = message
    elif new_lessons:
        m = re.match(r"\d{4}-\d{2}-\d{2}: (.+)", new_lessons[0])
        head = "Release %s: %s" % (day, m.group(1) if m else new_lessons[0])
        if len(new_lessons) > 1:
            head += " (and %d more)" % (len(new_lessons) - 1)
    else:
        head = "Release %s: the evergreen SiftStack" % day
    if not new_lessons:
        return head
    return "\n".join([head, "", "New in %s:" % LESSONS] + ["- %s" % t for t in new_lessons])


def built_from(message: str) -> str:
    """The working-branch commit a release was built from, read off its commit message."""
    m = BUILT_FROM_RX.search(message or "")
    return m.group(1) if m else ""


def publish(dest: Path, report: dict, push: bool, message: str | None) -> str:
    remote = public_remote()
    base = "%s/main" % remote
    git("fetch", remote, "main")
    if not (MAIN_WT / ".git").exists():
        if MAIN_WT.exists():
            shutil.rmtree(MAIN_WT)
        git("worktree", "add", "--detach", str(MAIN_WT), base)
    else:
        git("checkout", "--detach", base, cwd=MAIN_WT)
        git("reset", "--hard", base, cwd=MAIN_WT)
        git("clean", "-fdx", "-q", cwd=MAIN_WT)
    published_lessons = set(lesson_titles(MAIN_WT / LESSONS))      # what main already says, before it is replaced
    for p in MAIN_WT.iterdir():
        if p.name == ".git":
            continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    for p in dest.iterdir():
        target = MAIN_WT / p.name
        shutil.copytree(p, target) if p.is_dir() else shutil.copyfile(p, target)
    git("add", "-A", cwd=MAIN_WT)
    if not git("status", "--porcelain", cwd=MAIN_WT):
        sha = git("rev-parse", "HEAD", cwd=MAIN_WT)
        say("main already matches this release: nothing to commit")
        say("RESULT unchanged %s" % sha[:12])
        return sha
    new_lessons = [t for t in lesson_titles(MAIN_WT / LESSONS) if t not in published_lessons]
    msg = release_message(date.today().isoformat(), new_lessons, message)
    body = ("%s\n\nBuilt by tools/public_release.py from %s: %d private file(s) left out, names, accounts and paths\n"
            "replaced, every file and archive scanned with no finding. Main gets one commit per release, never\n"
            "the working branch's history.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n") % (
        msg, report["ref"][:12], len(report["left_out"]))
    f = WORK / "commit_message.txt"
    f.write_text(body, encoding="utf-8")
    git("commit", "-q", "-F", str(f), cwd=MAIN_WT)
    sha = git("rev-parse", "HEAD", cwd=MAIN_WT)
    say("committed %s on top of %s" % (sha[:12], base))
    if push:
        # PUBLIC_RELEASE=1 is what the owner's pre-push hook asks for before anything reaches the public repo
        git("push", remote, "HEAD:main", cwd=MAIN_WT, capture=True, env_extra={"PUBLIC_RELEASE": "1"})
        say("pushed %s main -> %s" % (remote, sha[:12]))
        say("RESULT released %s from %s, %d new lesson(s)" % (sha[:12], report["ref"][:12], len(new_lessons)))
    else:
        say("RESULT committed %s from %s, not pushed" % (sha[:12], report["ref"][:12]))
    return sha


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["build", "test", "publish"])
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--python", default=sys.executable, help="the interpreter with the repo's requirements")
    ap.add_argument("--pytest", nargs="*", default=None, help="test files to run inside the release (default: tests/test_*.py)")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--message")
    a = ap.parse_args(argv)
    dest, report = build(a.ref, a.python)
    if report["findings"] or report["regen_problems"]:
        for p in report["regen_problems"]:
            say("REGEN " + p)
        say("stopped: fix the findings above (in the source, or in private/public_release.json) and run again")
        where = sorted({str(f[0]) for f in report["findings"]})
        say("RESULT stopped: the scan found %d private thing(s)%s and %d file(s) did not regenerate" % (
            len(report["findings"]), (" (in %s)" % ", ".join(where[:3])) if where else "", len(report["regen_problems"])))
        return 1
    if a.action == "build":
        say("RESULT built %s" % report["ref"][:12])
        return 0
    pytest_files = a.pytest if a.pytest is not None else sorted(
        p.relative_to(dest).as_posix() for p in (dest / "tests").glob("test_*.py"))
    failed = checks(dest, a.python, pytest_files)
    for f in failed:
        say("FAILED " + f)
    if failed:
        say("stopped: %d check(s) failed inside the release" % len(failed))
        say("RESULT stopped: %d check(s) failed inside the release (%s)" % (
            len(failed), ", ".join(f.split(":", 1)[0] for f in failed)))
        return 1
    say("every check passed inside the release")
    if a.action == "test":
        say("RESULT tested %s" % report["ref"][:12])
        return 0
    publish(dest, report, a.push, a.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
