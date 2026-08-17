#!/usr/bin/env python3
"""Skill packaging: source trees in, distributable ZIPs out.

The unit of truth is the SOURCE TREE at skills/<name>/ and plugins/<name>/.
The .skill and .plugin files under dist/ are build products, regenerated from
source. That direction matters: a ZIP is opaque to git, so a skill that only
ever existed as a ZIP could not be diffed, reviewed, or fixed by anyone but
the person holding the original folder. Fifteen months of skill edits are
invisible in this repo's history for exactly that reason.

    python tools/build_skills.py --unpack     # ZIPs -> source trees (migration)
    python tools/build_skills.py --build      # source trees -> dist/*.skill
    python tools/build_skills.py --manifest   # regenerate skills/manifest.json
    python tools/build_skills.py --verify     # dist matches source? (CI gate)

--unpack is a ONE-TIME migration and is destructive to the source trees it
writes. Everything else is safe to re-run.

Stdlib only, on purpose: this runs in CI, on a community member's laptop, and
inside a Co-Work session, and none of those are guaranteed to have pip.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
PLUGINS = ROOT / "plugins"
DIST = ROOT / "dist"
LEGACY = ROOT / "Skills for REI" / "improved"
MANIFEST = SKILLS / "manifest.json"

REPO = "DataSift-Ty-Personal/SiftStack"
BRANCH = "main"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"

# Skills that have been replaced by a newer generation. They stay in the repo
# so a run that references them still resolves, but the installer skips them
# unless asked by name, and the manifest labels them so nobody adopts a dead
# version by accident.
SUPERSEDED = {
    "deep-prospecting": "deep-prospecting-v5",
    "deep-prospecting-v4": "deep-prospecting-v5",
}

# Category drives grouping in the README, the manifest, and the install UI.
CATEGORY = {
    "sift-market-research": "Market Intelligence",
    "first-market-county-data": "Market Intelligence",
    "buyer-prospector": "Market Intelligence",
    "real-estate-comping": "Deal Analysis",
    "comp-package": "Deal Analysis",
    "rehab-estimator": "Deal Analysis",
    "deep-prospecting-v5": "Deal Analysis",
    "deep-prospecting-v4": "Deal Analysis",
    "deep-prospecting": "Deal Analysis",
    "probate-property-finder": "Deal Analysis",
    "phone-validator": "Operations",
    "sequential-presets": "Operations",
    "playbook-creator": "Operations",
    "text-touch-builder": "Operations",
    "candidate-intake": "Operations",
    "team-hiring": "Operations",
    "caller-reputation-monitor": "Operations",
    "kpi-engine": "Coaching & Performance",
    "cold-call-coach": "Coaching & Performance",
    "lead-manager-coach": "Coaching & Performance",
    "closer-coach": "Coaching & Performance",
    "sift-sequences": "CRM",
    "sift-operations": "CRM",
    "deal-analyzer": "Deal Analysis",
}


# --------------------------------------------------------------------------
# frontmatter
# --------------------------------------------------------------------------
def read_frontmatter(text: str) -> dict:
    """Parse the leading YAML block of a SKILL.md.

    Deliberately not a YAML parser. Skill frontmatter is a flat map of scalars
    where the only structure that shows up in practice is `description: >` with
    a folded block under it, and pulling in PyYAML to read four keys would cost
    this script its stdlib-only guarantee.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    key: str | None = None
    folded: list[str] = []
    for line in block.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            if key and folded:
                out[key] = " ".join(folded).strip()
                folded = []
            key, val = m.group(1), m.group(2).strip()
            if val in (">", "|", ">-", "|-"):
                out[key] = ""
            else:
                out[key] = val.strip("'\"")
                key = None
        elif key is not None:
            folded.append(line.strip())
    if key and folded:
        out[key] = " ".join(folded).strip()
    return out


def _normalize_name(text: str, slug: str) -> tuple[str, str | None]:
    """Force the frontmatter `name:` to equal the package slug.

    Returns (text, old_value_if_changed). Only the name line is touched;
    description and everything below the frontmatter are left alone.
    """
    if not text.startswith("---"):
        return text, None
    end = text.find("\n---", 3)
    if end == -1:
        return text, None
    head, rest = text[:end], text[end:]
    old: str | None = None

    def sub(m: re.Match) -> str:
        nonlocal old
        old = m.group(1).strip().strip("'\"").strip()
        return f"name: {slug}"

    new_head = re.sub(r"^name:[ \t]*(.*?)[ \t\r]*$", sub, head, count=1, flags=re.M)
    if old is None or old == slug:
        return text, None
    return new_head + rest, old


def find_skill_md(root: Path) -> Path | None:
    direct = root / "SKILL.md"
    if direct.is_file():
        return direct
    hits = sorted(root.glob("*/SKILL.md"))
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# unpack (one-time migration)
# --------------------------------------------------------------------------
def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract, refusing any member that escapes dest.

    A ZIP entry named ../../.ssh/authorized_keys is a real attack and
    ZipFile.extractall does not stop it on its own.
    """
    dest = dest.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        target = (dest / info.filename).resolve()
        if not str(target).startswith(str(dest) + os.sep):
            raise SystemExit(f"refusing path traversal in archive: {info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def unpack() -> None:
    if not LEGACY.is_dir():
        raise SystemExit(f"no legacy skill directory at {LEGACY}")
    archives = sorted(LEGACY.glob("*.skill")) + sorted(LEGACY.glob("*.plugin"))
    if not archives:
        raise SystemExit(f"no .skill/.plugin archives in {LEGACY}")

    for arc in archives:
        is_plugin = arc.suffix == ".plugin"
        stage = ROOT / ".tmp_unpack" / arc.stem
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        with zipfile.ZipFile(arc) as zf:
            _safe_extract(zf, stage)

        # Some archives wrap everything in a single top folder and some do not.
        # Normalize to "contents at the root of the source tree" so every skill
        # installs the same way and the installer needs no per-skill special
        # cases.
        entries = [p for p in stage.iterdir() if p.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir():
            probe = entries[0]
            if (probe / "SKILL.md").is_file() or (probe / ".claude-plugin").is_dir():
                stage = probe

        # The ARCHIVE STEM is the slug, not the frontmatter `name`. Unpacking
        # exposed two defects that made frontmatter unusable as an identity:
        # the three coach skills carry a display title ("Closer Coach") where
        # a slug belongs, and all three deep-prospecting generations declare
        # the same `name: deep-prospecting`, so installing v5 over v4 silently
        # replaced it. The stems are already unique and already correct.
        name = arc.stem
        smd = find_skill_md(stage)
        if smd:
            fixed, changed = _normalize_name(smd.read_text(encoding="utf-8", errors="replace"), name)
            if changed:
                smd.write_text(fixed, encoding="utf-8")
                print(f"  fixed frontmatter name: {changed!r} -> {name!r}")

        dest = (PLUGINS if is_plugin else SKILLS) / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, dest)
        n = sum(1 for _ in dest.rglob("*") if _.is_file())
        print(f"unpacked {arc.name:38s} -> {dest.relative_to(ROOT)}  ({n} files)")

    tmp = ROOT / ".tmp_unpack"
    if tmp.exists():
        shutil.rmtree(tmp)


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------
def _iter_files(root: Path):
    """Yield every real file under root, in a platform-independent order.

    Sorting Path objects directly is NOT portable: PurePath comparison is
    case-insensitive on Windows and case-sensitive on Linux, so "SKILL.md"
    lands after "references/..." on one and before it on the other. That put
    the archive entries and the manifest file lists in a different order
    depending on who ran the build, which reads as a real diff and fails CI
    on a tree where nothing changed. Sort the relative POSIX string instead.
    """
    files = [p for p in root.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"]
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def _zip_into(src: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    # Deterministic: fixed timestamp and sorted order, so rebuilding an
    # unchanged skill produces a byte-identical file and does not show up as a
    # spurious diff on every commit.
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in _iter_files(src):
            # Forward slashes always. Compress-Archive writes backslash entry
            # names on Windows and those archives do not unpack on macOS or
            # Linux, which is how two skills shipped broken.
            arc = p.relative_to(src).as_posix()
            info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, p.read_bytes())
    tmp.replace(out)


def build() -> list[dict]:
    DIST.mkdir(exist_ok=True)
    built = []
    for base, suffix in ((SKILLS, ".skill"), (PLUGINS, ".plugin")):
        if not base.is_dir():
            continue
        for src in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name):
            out = DIST / f"{src.name}{suffix}"
            _zip_into(src, out)
            built.append({"name": src.name, "path": out, "kind": suffix.lstrip(".")})
            print(f"built {out.relative_to(ROOT).as_posix():44s} {out.stat().st_size/1024:9.1f} KB")
    return built


def verify() -> int:
    """CI gate: fail if any dist archive drifts from its source tree."""
    problems = []
    for base, suffix in ((SKILLS, ".skill"), (PLUGINS, ".plugin")):
        if not base.is_dir():
            continue
        for src in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name):
            out = DIST / f"{src.name}{suffix}"
            if not out.exists():
                problems.append(f"missing dist archive: {out.name}")
                continue
            expect = {p.relative_to(src).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in _iter_files(src)}
            with zipfile.ZipFile(out) as zf:
                actual = {i.filename: hashlib.sha256(zf.read(i)).hexdigest()
                          for i in zf.infolist() if not i.is_dir()}
            for k in sorted(set(expect) | set(actual)):
                if expect.get(k) != actual.get(k):
                    problems.append(f"{out.name}: {k} differs between source and dist")
    for p in problems:
        print(f"DRIFT {p}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s). Run: python tools/build_skills.py --build --manifest",
              file=sys.stderr)
        return 1
    print("dist matches source")
    return 0


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def manifest() -> dict:
    entries = []
    for base, suffix, kind in ((SKILLS, ".skill", "skill"), (PLUGINS, ".plugin", "plugin")):
        if not base.is_dir():
            continue
        for src in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name):
            # A plugin's identity lives in .claude-plugin/plugin.json; a
            # skill's lives in SKILL.md frontmatter. Read whichever applies
            # rather than leaving every plugin with an empty description.
            fm: dict = {}
            pj = src / ".claude-plugin" / "plugin.json"
            if pj.is_file():
                try:
                    fm = json.loads(pj.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    fm = {}
            if not fm.get("description"):
                smd = find_skill_md(src)
                if smd:
                    fm = {**read_frontmatter(smd.read_text(encoding="utf-8", errors="replace")),
                          **{k: v for k, v in fm.items() if v}}
            files = list(_iter_files(src))
            rel = base.relative_to(ROOT).as_posix()
            dist_rel = f"dist/{src.name}{suffix}"
            dist_path = ROOT / dist_rel
            entry = {
                "name": src.name,
                "kind": kind,
                "category": CATEGORY.get(src.name, "Uncategorized"),
                "version": fm.get("version"),
                "description": (fm.get("description") or "").strip(),
                "status": "superseded" if src.name in SUPERSEDED else "current",
                "source_dir": f"{rel}/{src.name}",
                "files": [p.relative_to(src).as_posix() for p in files],
                "file_count": len(files),
                "bytes": sum(p.stat().st_size for p in files),
                "download": f"{RAW}/{dist_rel}",
                "sha256": hashlib.sha256(dist_path.read_bytes()).hexdigest() if dist_path.exists() else None,
            }
            if src.name in SUPERSEDED:
                entry["superseded_by"] = SUPERSEDED[src.name]
            entries.append(entry)

    entries.sort(key=lambda e: (e["category"], e["name"]))
    doc = {
        "$schema": "https://siftstack.dev/schema/skills-manifest-v1.json",
        "repo": REPO,
        "branch": BRANCH,
        "raw_base": RAW,
        "install_target": "~/.claude/skills",
        "counts": {
            "total": len(entries),
            "current": sum(1 for e in entries if e["status"] == "current"),
            "skills": sum(1 for e in entries if e["kind"] == "skill"),
            "plugins": sum(1 for e in entries if e["kind"] == "plugin"),
        },
        "categories": sorted({e["category"] for e in entries}),
        "skills": entries,
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" explicitly: write_text otherwise translates to CRLF on
    # Windows, and this file is compared byte for byte against a CI rebuild.
    MANIFEST.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"manifest: {doc['counts']['total']} packages "
          f"({doc['counts']['current']} current) -> {MANIFEST.relative_to(ROOT).as_posix()}")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unpack", action="store_true", help="ZIPs -> source trees (one-time)")
    ap.add_argument("--build", action="store_true", help="source trees -> dist/*.skill")
    ap.add_argument("--manifest", action="store_true", help="regenerate skills/manifest.json")
    ap.add_argument("--verify", action="store_true", help="fail if dist drifts from source")
    args = ap.parse_args()
    if not any((args.unpack, args.build, args.manifest, args.verify)):
        ap.error("pick at least one of --unpack / --build / --manifest / --verify")
    if args.unpack:
        unpack()
    if args.build:
        build()
    if args.manifest:
        manifest()
    if args.verify:
        return verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
