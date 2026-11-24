"""The SiftStack dialer in the repo's test run (zero network).

The full suite is src/dialer/selftest.py; it runs in its own process here because it refuses the network
for the whole interpreter, which must not leak into the other tests. The real-browser checks
(python -m dialer.ui_check, python -m dialer.ext_check) need Playwright's Chromium and stay manual.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extensions" / "sift-dialer"


def test_dialer_selftest_passes():
    p = subprocess.run([sys.executable, "-m", "dialer.selftest"], cwd=ROOT / "src", capture_output=True, text=True,
                       timeout=600)
    tail = (p.stdout or "").strip().splitlines()[-12:]
    assert p.returncode == 0, "\n".join(tail) + "\n" + (p.stderr or "")[-2000:]
    assert any(" passed" in line for line in tail)


def test_extension_manifest():
    m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3
    assert "tabs" not in m["permissions"], "host permissions cover the two sites; tabs would add a history warning"
    assert set(m["permissions"]) <= {"storage", "scripting"}
    hosts = set(m["host_permissions"])
    assert "https://app.reisift.io/*" in hosts and "https://siftstack-dialer.fly.dev/*" in hosts
    for cs in m["content_scripts"]:
        for f in cs["js"]:
            assert (EXT / f).exists(), f
    for size, path in m["icons"].items():
        assert (EXT / path).exists(), path


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scripts_parse():
    files = sorted(EXT.glob("*.js")) + sorted((ROOT / "src" / "dialer" / "static").glob("*.js"))
    assert files
    for f in files:
        p = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, "%s: %s" % (f.name, p.stderr)


def test_no_dashes_in_shipped_files():
    bad = []
    for root in (EXT, ROOT / "src" / "dialer"):
        for f in root.rglob("*"):
            if f.is_dir() or "vendor" in f.parts or f.suffix not in (".py", ".js", ".html", ".css", ".json", ".md"):
                continue
            t = f.read_text(encoding="utf-8")
            if chr(0x2014) in t or chr(0x2013) in t:
                bad.append(str(f.relative_to(ROOT)))
    assert not bad, bad
