#!/usr/bin/env python3
"""Validate an agent SOP (.sop.md) against the Agent SOPs format.

Usage:
    python validate_sop.py path/to/my-process.sop.md [more.sop.md ...]

Exit 0 = no errors (warnings allowed). Exit 1 = at least one error.

Checks mirror the open Agent SOPs standard (strands-agents/agent-sop):
structural invariants are errors, quality issues are warnings. Stdlib only
so it runs anywhere, including inside a Co-Work session with no pip.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RFC_RX = re.compile(r"\bYou (MUST NOT|MUST|SHOULD NOT|SHOULD|MAY)\b")
NEGATIVE_RX = re.compile(r"\bYou (?:MUST NOT|SHOULD NOT|SHOULD NEVER)\b")
REASON_RX = re.compile(r"\b(because|since|as |to avoid|to prevent|so that)\b", re.I)
STEP_RX = re.compile(r"^### \d+\. \S")
PARAM_RX = re.compile(r"^- \*\*([a-z0-9_]+)\*\* \((required|optional)(, default: .+)?\): \S")
PARAM_LINE_RX = re.compile(r"^- \*\*(\S+)\*\*")


def validate(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not path.name.endswith(".sop.md"):
        errors.append("filename must end in .sop.md")
    if not path.exists():
        return [f"file not found: {path}"], []

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    if not re.search(r"^# \S", text, re.M):
        errors.append("missing H1 title (a line starting with '# ')")
    for section in ("## Overview", "## Parameters", "## Steps"):
        if not re.search(rf"^{re.escape(section)}\s*$", text, re.M):
            errors.append(f"missing required section: {section}")

    # Overview must carry extractable prose (it becomes the description).
    m = re.search(r"^## Overview\s*\n(.*?)(?=\n## |\Z)", text, re.S | re.M)
    if m and len(m.group(1).strip()) < 20:
        errors.append("## Overview has no real content (it doubles as the SOP description)")

    if "Constraints for parameter acquisition" not in text:
        errors.append("Parameters section missing the '**Constraints for parameter acquisition:**' block")

    if not any(STEP_RX.match(ln) for ln in lines):
        errors.append("no numbered step headings found (expected '### 1. Step Name')")
    if not re.search(r"^\*\*Constraints:\*\*\s*$", text, re.M):
        errors.append("no '**Constraints:**' block found in any step")

    # Every numbered step should have its own Constraints block.
    step_bodies = re.split(r"^### \d+\. ", text, flags=re.M)[1:]
    for body in step_bodies:
        name = body.splitlines()[0].strip()
        chunk = body.split("\n### ")[0].split("\n## ")[0]
        if "**Constraints:**" not in chunk:
            warnings.append(f"step '{name}' has no **Constraints:** block")

    if not RFC_RX.search(text):
        warnings.append("no RFC 2119 keywords found (You MUST / You SHOULD / You MAY)")

    for i, ln in enumerate(lines, 1):
        if NEGATIVE_RX.search(ln) and not REASON_RX.search(ln):
            warnings.append(f"line {i}: negative constraint without a reason "
                            "(write 'You MUST NOT ... because ...')")
        pm = PARAM_LINE_RX.match(ln)
        if pm and not PARAM_RX.match(ln) and "Constraints" not in ln:
            name = pm.group(1)
            if not re.fullmatch(r"[a-z0-9_]+", name):
                warnings.append(f"line {i}: parameter '{name}' is not snake_case lowercase")
            else:
                warnings.append(f"line {i}: parameter '{name}' does not match "
                                "'- **name** (required|optional[, default: ...]): description'")

    # Required params listed before optional ones.
    kinds = [m2.group(2) for ln in lines if (m2 := PARAM_RX.match(ln))]
    if "required" in kinds and kinds.index("required") > 0 and kinds[0] == "optional":
        warnings.append("required parameters should be listed before optional ones")

    for section in ("## Examples", "## Troubleshooting"):
        if section not in text:
            warnings.append(f"recommended section missing: {section}")

    return errors, warnings


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    failed = False
    for arg in argv:
        path = Path(arg)
        errors, warnings = validate(path)
        status = "FAIL" if errors else "PASS"
        print(f"[{status}] {path}")
        for e in errors:
            print(f"  ERROR: {e}")
        for w in warnings:
            print(f"  warn:  {w}")
        if errors:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
