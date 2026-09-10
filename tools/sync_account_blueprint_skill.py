#!/usr/bin/env python3
"""Copy the account_blueprint package and the newest blueprint into the skill.

The unit of truth is src/account_blueprint/ and output/blueprints/. The skill
carries a COPY so a community member gets one zip with everything, and a test
asserts the copy matches the source so the two cannot drift.

    python tools/sync_account_blueprint_skill.py
"""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "account_blueprint"
DST = ROOT / "skills" / "account-blueprint" / "scripts" / "account_blueprint"
BP_SRC = ROOT / "output" / "blueprints"
BP_DST = ROOT / "skills" / "account-blueprint" / "blueprints"
MODULES = ("__init__.py", "client.py", "blueprint.py", "translate.py", "export.py", "apply.py",
           "probe.py", "report.py", "cli.py", "staff_creds.py")


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    for m in MODULES:
        shutil.copyfile(SRC / m, DST / m)
    shutil.copyfile(ROOT / "src" / "clone_account.py", DST.parent / "clone_account.py")
    bps = sorted(glob.glob(str(BP_SRC / "ty2_*.json")))
    if bps:
        BP_DST.mkdir(parents=True, exist_ok=True)
        for old in BP_DST.glob("ty2_*.json"):
            old.unlink()
        shutil.copyfile(bps[-1], BP_DST / os.path.basename(bps[-1]))
        print("blueprint: %s" % os.path.basename(bps[-1]))
    print("synced %d modules -> %s" % (len(MODULES), DST.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
