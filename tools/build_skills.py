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
    python tools/build_skills.py --marketplace  # regenerate the plugin marketplace

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
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
BUNDLES = ROOT / "marketplace" / "bundles"
CORE = PLUGINS / "siftstack-core"

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

# What each package needs before it can actually run, and what to do instead
# when you do not have it. This is the difference between a library someone
# installs and a library someone uses: a skill that silently needs a paid key
# fails on first contact and the person concludes the whole thing is broken.
#
# tier:      none    nothing to configure, works on install
#            account your own login for a service you already pay for
#            api     a third-party API key, usually metered
# env:       environment variables the scripts read
# accounts:  logins rather than keys
# cost:      what it actually costs, so nobody is surprised by a bill
# fallback:  the no-API route. Either a sibling skill that does the same job
#            by browser or by hand, or a named section of the no-API playbook.
REQUIRES = {
    # --- tier none: knowledge and generation, no credentials at all ---------
    "rehab-estimator": dict(tier="none", fallback=None,
                            note="Ships the locked Knox material list. Other markets use the cheat sheet."),
    "team-hiring": dict(tier="none", fallback=None),
    "first-market-county-data": dict(tier="none", fallback=None,
                                     note="Tells you where to pull county data. The pulling is manual by design."),
    "playbook-creator": dict(tier="none", fallback=None,
                             env=["OPENROUTER_API_KEY"],
                             cost="Free. Video transcription about $0.002 per audio minute (optional)",
                             note="The key is OPTIONAL and only powers scripts/transcribe_video.py "
                                  "(narrated screen recording in, transcript + action frames out). "
                                  "Without it, paste your recorder's own free transcript instead."),
    "probate-property-finder": dict(tier="none", fallback=None,
                                    note="Uses free county tax portals and people-search sites."),
    "text-touch-builder": dict(tier="none", fallback=None),
    "real-estate-comping": dict(tier="none", fallback=None,
                                note="This IS the no-key comping route: manual Zillow, Redfin and Realtor pulls."),
    "buyer-prospector": dict(tier="none", fallback=None,
                             note="Ships its own nationwide buyer data. Entity research is browser-driven."),
    "candidate-intake": dict(tier="none", accounts=["Google", "Claude in Chrome"], fallback=None,
                             note="Runs entirely through the Chrome extension. No API keys."),
    "deep-prospecting": dict(tier="none", fallback=None, note="Superseded. Manual research method."),
    "sift-operations": dict(tier="none", fallback=None),
    "vendor-directory-builder": dict(tier="none", fallback=None,
                                     note="Community mining needs your logged-in browser for private groups. The Excel engine is pure openpyxl."),
    "contractor-call-sheet": dict(tier="none", fallback=None,
                                  note="Drafts outreach for a human to send. Never sends anything itself."),
    "account-blueprint": dict(tier="account", accounts=["DataSift"],
                              env=["REISIFT_TARGET_JWT"],
                              cost="Free. Creates structure only; SiftMap auto-add is left OFF so no records are pulled",
                              fallback="no-api-playbook#presets-by-hand",
                              note="Apply needs your own DataSift JWT (or email + password). "
                                   "Export of your own account needs your Open API key."),
    "siftstack-core": dict(tier="none", fallback=None,
                           note="The doctor and the setup walkthrough. Every bundle depends on it, so whatever "
                                "someone installs, they can ask what works right now and what needs a key."),
    "dispo-deal-blast": dict(tier="none", fallback=None,
                             note="The method, the guards and the copy rules. The bundled cohort calculator is stdlib only. Wiring it to your own CRM and SMS provider is your call, and every send stays behind a human release."),

    # --- tier account: your own login for something you already pay for ----
    "sift-market-research": dict(tier="account", env=["DATASIFT_EMAIL", "DATASIFT_PASSWORD"],
                                 accounts=["DataSift"], cost="Included with DataSift",
                                 fallback="no-api-playbook#market-research",
                                 note="Browser automation of Market Finder. Your login, not an API key."),
    "sequential-presets": dict(tier="account", env=["DATASIFT_EMAIL", "DATASIFT_PASSWORD"],
                               accounts=["DataSift"], cost="Included with DataSift",
                               fallback="no-api-playbook#presets-by-hand"),
    "sift-sequences": dict(tier="account", env=["DATASIFT_EMAIL", "DATASIFT_PASSWORD"],
                           accounts=["DataSift"], cost="Included with DataSift",
                           fallback="no-api-playbook#sequences-by-hand"),
    "kpi-engine": dict(tier="account", env=["REISIFT_TOKEN"], accounts=["DataSift"],
                       cost="Included with DataSift",
                       fallback="no-api-playbook#kpis-by-hand",
                       note="Mints its own token from your DataSift login. No internal API access needed."),
    "cold-call-coach": dict(tier="account", accounts=["SmrtPhone", "OpenRouter or Anthropic"],
                            cost="About $0.002 per audio minute to transcribe",
                            fallback="no-api-playbook#coaching-without-a-dialer-api"),
    "lead-manager-coach": dict(tier="account", accounts=["SmrtPhone", "OpenRouter or Anthropic"],
                               cost="About $0.002 per audio minute to transcribe",
                               fallback="no-api-playbook#coaching-without-a-dialer-api"),
    "closer-coach": dict(tier="account", accounts=["SmrtPhone", "OpenRouter or Anthropic"],
                         cost="About $0.002 per audio minute to transcribe",
                         fallback="no-api-playbook#coaching-without-a-dialer-api"),

    # --- tier api: a metered third-party key --------------------------------
    "comp-package": dict(tier="api", env=["OPENWEBNINJA_API_KEY"],
                         cost="100 free lookups per month, then metered",
                         fallback="real-estate-comping",
                         note="Without the key, real-estate-comping does the same job by browser."),
    "phone-validator": dict(tier="api", env=["TRESTLE_API_KEY"],
                            cost="About $0.015 per number",
                            fallback="no-api-playbook#phone-scoring-without-trestle"),
    "deep-prospecting-v5": dict(tier="api",
                                env=["SMARTSKIP_EMAIL", "SMARTSKIP_PASSWORD", "TRESTLE_API_KEY"],
                                cost="About $0.24 per record end to end",
                                fallback="no-api-playbook#heir-research-by-hand",
                                note="Enformion is optional and only needed for LLC and trust owners."),
    "deep-prospecting-v4": dict(tier="api",
                                env=["ENFORMION_AP_NAME", "ENFORMION_AP_PASSWORD", "TRESTLE_API_KEY"],
                                cost="About $1.18 per record", fallback="deep-prospecting-v5",
                                note="Superseded. v5 is roughly 5x cheaper and finds more."),
    "caller-reputation-monitor": dict(tier="api", env=["TELNYX_API_KEY", "TELNYX_ENTERPRISE_ID"],
                                      accounts=["Telnyx"], cost="Included with a Telnyx account",
                                      fallback="no-api-playbook#spam-flag-checks-by-hand"),
    "deal-analyzer": dict(tier="api", env=["OPENWEBNINJA_API_KEY"],
                          cost="100 free lookups per month, then metered",
                          fallback="real-estate-comping",
                          note="Comping stage only. Rehab and offer math need no key."),
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
    "vendor-directory-builder": "Operations",
    "contractor-call-sheet": "Operations",
    "kpi-engine": "Coaching & Performance",
    "cold-call-coach": "Coaching & Performance",
    "lead-manager-coach": "Coaching & Performance",
    "closer-coach": "Coaching & Performance",
    "sift-sequences": "CRM",
    "account-blueprint": "CRM",
    "sift-operations": "CRM",
    "deal-analyzer": "Deal Analysis",
    "dispo-deal-blast": "Operations",
    "siftstack-core": "Setup",
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
def manifest_entries() -> list[dict]:
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

            req = dict(REQUIRES.get(src.name, {"tier": "unknown"}))
            req.setdefault("env", [])
            req.setdefault("accounts", [])
            req.setdefault("cost", "Free")
            req.setdefault("fallback", None)
            entry["requires"] = req
            entries.append(entry)

    unknown = [e["name"] for e in entries if e["requires"]["tier"] == "unknown"]
    if unknown:
        raise SystemExit(
            "These packages have no REQUIRES entry, so the doctor cannot tell "
            "anyone whether they can run them or what to do instead: "
            + ", ".join(unknown))

    entries.sort(key=lambda e: (e["category"], e["name"]))
    return entries


def manifest() -> dict:
    entries = manifest_entries()
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
        "tiers": {
            "none": "Works the moment it is installed. No key, no login.",
            "account": "Needs a login for a service you already pay for.",
            "api": "Needs a third-party API key, usually metered.",
        },
        "tier_counts": {
            t: sum(1 for e in entries if e["requires"]["tier"] == t and e["status"] == "current")
            for t in ("none", "account", "api")
        },
        "skills": entries,
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" explicitly: write_text otherwise translates to CRLF on
    # Windows, and this file is compared byte for byte against a CI rebuild.
    MANIFEST.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"manifest: {doc['counts']['total']} packages "
          f"({doc['counts']['current']} current) -> {MANIFEST.relative_to(ROOT).as_posix()}")
    return doc


# --------------------------------------------------------------------------
# plugin marketplace
# --------------------------------------------------------------------------
# One bundle per category, plus siftstack-all. A bundle is a plugin whose
# manifest is nothing but a dependencies array, so one install pulls the set.
BUNDLE_SLUG = {
    "Market Intelligence": "siftstack-market-intel",
    "Deal Analysis": "siftstack-deal-analysis",
    "CRM": "siftstack-crm",
    "Coaching & Performance": "siftstack-coaching",
    "Operations": "siftstack-operations",
}
ALL_BUNDLE = "siftstack-all"
OWNER = {"name": "DataSift", "url": "https://datasift.ai"}


def _short(desc: str, limit: int = 220) -> str:
    """First sentence of a skill description, for the /plugin browse list.

    SKILL.md descriptions are trigger text written for the model and run to
    900+ characters. The marketplace listing is read by a person choosing
    what to install.
    """
    desc = " ".join((desc or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", desc)
    first = m.group(1) if m else desc
    return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")


def marketplace() -> dict:
    """Generate .claude-plugin/marketplace.json, the bundles, and core's data.

    skills/<name>/ is published AS a plugin with no restructuring: strict=false
    makes the marketplace entry the whole definition, and a SKILL.md at the
    plugin root is a valid single-skill plugin. Verified by installing one
    from a scratch marketplace, on both an old and the current CLI.

    NO `version` anywhere, deliberately. A declared version pins the plugin
    and becomes the only update signal, so one forgotten bump strands every
    user on the old copy. Without it the commit SHA is the signal and every
    push to main is an update.

    Run BEFORE --build: this writes plugins/siftstack-core/data/, and the
    dist archive for siftstack-core is zipped from that tree.
    """
    doc = manifest_entries()
    current = [e for e in doc if e["status"] == "current"]

    uncategorized = [e["name"] for e in current
                     if e["category"] not in BUNDLE_SLUG and e["name"] != CORE.name]
    if uncategorized:
        raise SystemExit("These packages are in no bundle, so siftstack-all would "
                         "silently skip them. Give each a CATEGORY: " + ", ".join(uncategorized))

    for e in current:
        pj = ROOT / e["source_dir"] / ".claude-plugin" / "plugin.json"
        if pj.is_file() and "version" in json.loads(pj.read_text(encoding="utf-8")):
            raise SystemExit(f"{pj.relative_to(ROOT).as_posix()} declares a version. That pins the "
                             "plugin and blocks updates until someone remembers to bump it. Remove it.")

    plugins = []
    for e in current:
        entry = {
            "name": e["name"],
            "source": "./" + e["source_dir"],
            "description": _short(e["description"]),
            "category": e["category"],
            "keywords": sorted({"real-estate", "datasift", f"tier-{e['requires']['tier']}"}),
            "author": {"name": OWNER["name"]},
        }
        if e["kind"] == "skill":
            entry["strict"] = False
            entry["skills"] = ["./"]
        plugins.append(entry)

    bundles = {}
    for cat, slug in BUNDLE_SLUG.items():
        members = sorted(e["name"] for e in current if e["category"] == cat)
        bundles[slug] = (f"Every SiftStack {cat} skill in one install.",
                         sorted(members + [CORE.name]))
    bundles[ALL_BUNDLE] = ("The whole SiftStack REI skill library in one install.",
                           sorted(e["name"] for e in current))

    # Overwrite in place and prune only what is stale. Deleting the whole tree
    # first dies with WinError 5 inside a OneDrive-synced checkout (the sync
    # client holds the directory), and leaves the repo with no bundles at all.
    if BUNDLES.is_dir():
        for stale in (p for p in BUNDLES.iterdir() if p.is_dir() and p.name not in bundles):
            shutil.rmtree(stale, ignore_errors=True)
            if stale.exists():
                raise SystemExit(f"could not remove stale bundle {stale.name}; delete it by hand")
    for slug, (desc, deps) in bundles.items():
        _write_json(BUNDLES / slug / ".claude-plugin" / "plugin.json", {
            "name": slug, "description": desc,
            "author": {"name": OWNER["name"]}, "dependencies": deps,
        })
        plugins.append({
            "name": slug,
            "source": f"./{BUNDLES.relative_to(ROOT).as_posix()}/{slug}",
            "description": f"{desc} Installs {len(deps)} plugins.",
            "category": "Bundles",
            "keywords": ["bundle", "datasift", "real-estate"],
            "author": {"name": OWNER["name"]},
        })

    plugins.sort(key=lambda p: (p["category"] != "Bundles", p["category"], p["name"]))
    market = {
        "name": "siftstack",
        "owner": OWNER,
        "metadata": {
            "description": "The SiftStack real estate investing skill library for DataSift: "
                           "market research, comps, rehab, deep prospecting, CRM setup, "
                           "call coaching and dispo. Install siftstack-all for everything.",
        },
        "plugins": plugins,
    }
    _write_json(MARKETPLACE, market)

    # siftstack-core's doctor cannot reach ../skills/manifest.json once the
    # plugin is copied into the cache, and the manifest carries the sha of the
    # very archive that would contain it. So core gets its own slim table.
    _write_json(CORE / "data" / "requires.json", {
        "generated_by": "tools/build_skills.py --marketplace",
        "repo": REPO,
        "bundles": {slug: deps for slug, (_, deps) in bundles.items()},
        "packages": [{"name": e["name"], "category": e["category"], "requires": e["requires"]}
                     for e in current],
    })
    env_src = (ROOT / ".env.skills.example").read_text(encoding="utf-8")
    (CORE / "data" / "env.skills.example").write_text(env_src, encoding="utf-8", newline="\n")

    print(f"marketplace: {len(current)} plugins + {len(bundles)} bundles "
          f"-> {MARKETPLACE.relative_to(ROOT).as_posix()}")
    return market


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unpack", action="store_true", help="ZIPs -> source trees (one-time)")
    ap.add_argument("--build", action="store_true", help="source trees -> dist/*.skill")
    ap.add_argument("--manifest", action="store_true", help="regenerate skills/manifest.json")
    ap.add_argument("--verify", action="store_true", help="fail if dist drifts from source")
    ap.add_argument("--marketplace", action="store_true",
                    help="regenerate .claude-plugin/marketplace.json, bundles and core data")
    args = ap.parse_args()
    if not any((args.unpack, args.build, args.manifest, args.verify, args.marketplace)):
        ap.error("pick at least one of --unpack / --build / --manifest / --verify / --marketplace")
    if args.unpack:
        unpack()
    if args.marketplace:  # before --build: it writes into a tree that gets zipped
        marketplace()
    if args.build:
        build()
    if args.manifest:
        manifest()
    if args.verify:
        return verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
