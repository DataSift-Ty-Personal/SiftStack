#!/usr/bin/env python3
"""Copy the commercial_classifier package into the skill.

The unit of truth is src/commercial_classifier/ (plus src/commercial_classify.py,
which ships as scripts/cc.py). The skill carries a COPY so a community member
gets one install with the whole engine, and a test asserts the copy matches
the source so the two cannot drift.

    python tools/sync_commercial_classifier_skill.py
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "commercial_classifier"
DST = ROOT / "skills" / "commercial-classifier" / "scripts" / "commercial_classifier"
ENTRY_SRC = ROOT / "src" / "commercial_classify.py"
ENTRY_DST = ROOT / "skills" / "commercial-classifier" / "scripts" / "cc.py"


def main() -> int:
    if DST.exists():
        # remove files the source no longer has, without deleting the directory
        # itself (a OneDrive checkout can fail an rmtree half way through)
        for dp, _, files in os.walk(DST):
            for fn in files:
                p = Path(dp) / fn
                if not (SRC / p.relative_to(DST)).exists():
                    p.unlink()
    n = 0
    for dp, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            src = Path(dp) / fn
            dst = DST / src.relative_to(SRC)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            n += 1
    shutil.copyfile(ENTRY_SRC, ENTRY_DST)
    print("synced %d files -> %s (+ scripts/cc.py)" % (n, DST.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
