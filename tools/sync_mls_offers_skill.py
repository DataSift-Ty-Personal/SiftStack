#!/usr/bin/env python3
"""Copy the MLS offer engine into the mls-offer-engine skill.

The unit of truth is src/ (the engine) and data/ (the county table). The skill
carries a COPY so a community member gets one archive with everything, and
tests/test_mls_offers_skill_sync.py asserts each copy matches its source so the
two cannot drift.

Copies are written with LF line endings. The skill archive is zipped from the
working tree and skills/** is LF in git, so a CRLF copy made on Windows would
put different bytes in the archive than a CI checkout builds, and
`build_skills.py --verify` would report drift on a tree where nothing changed.

A source that does not exist yet is reported and skipped, and a copy left over
from a source that is gone is removed, so the skill never ships a file the repo
no longer has.

The county table goes in the skill's own data/ folder, not under scripts/. The
engine finds it as ../data/us_counties.csv from scripts/, the same place it sits
relative to src/ in the repo. Inside the archive its path is data/..., which the
public scanner treats as reference data, as it does skills/buyer-prospector/data.
Census county names collide with words the scanner keeps out of the public repo,
so a copy anywhere else stops the public release.

    python tools/sync_mls_offers_skill.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "mls-offer-engine"

# (source, destination inside the skill)
FILES = (
    ("src/mls_offers.py", "scripts/mls_offers.py"),
    ("src/mls_offers_api.py", "scripts/mls_offers_api.py"),
    ("src/mortgage_model.py", "scripts/mortgage_model.py"),
    ("src/siftmap_standalone.py", "scripts/siftmap_standalone.py"),
    ("data/us_counties.csv", "data/us_counties.csv"),
)


def lf(data: bytes) -> bytes:
    """The same text with LF line endings."""
    return data.replace(b"\r\n", b"\n")


def main() -> int:
    copied, missing = 0, []
    for src_rel, dst_rel in FILES:
        src, dst = ROOT / src_rel, SKILL / dst_rel
        if not src.is_file():
            missing.append(src_rel)
            if dst.is_file():
                dst.unlink()
                print("removed %s (its source %s is gone)" % (dst.relative_to(ROOT).as_posix(), src_rel))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = lf(src.read_bytes())
        if not dst.is_file() or dst.read_bytes() != data:
            dst.write_bytes(data)
        copied += 1
    print("synced %d of %d files -> %s" % (copied, len(FILES), SKILL.relative_to(ROOT).as_posix()))
    for m in missing:
        print("missing source, skipped: %s (re-run this once it exists)" % m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
