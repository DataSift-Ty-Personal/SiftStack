"""Run log and report rendering.

Every substitution, drop, gap and TODO is a Log entry, because a clone that
prints "created 132 presets" and hides that 6 of them now point at the wrong
human is exactly the silent success this repo keeps re-learning to distrust.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

ACTIONS = ("translated", "created", "exists", "exists_differs", "verified", "dropped",
           "gap", "placeholder", "manual_todo", "skipped", "probe", "warn", "mismatch")


class Log:
    def __init__(self):
        self.entries: list[dict] = []

    def add(self, family: str, action: str, obj: str = "", detail: str = "", **extra) -> dict:
        assert action in ACTIONS, action
        e = {"family": family, "action": action, "object": obj, "detail": detail}
        e.update(extra)
        self.entries.append(e)
        return e

    def by(self, action: str, family: str | None = None) -> list[dict]:
        return [e for e in self.entries if e["action"] == action
                and (family is None or e["family"] == family)]

    def counts(self, family: str) -> dict:
        out = {}
        for e in self.entries:
            if e["family"] == family:
                out[e["action"]] = out.get(e["action"], 0) + 1
        return out

    def print_family(self, family: str, actions=("dropped", "gap", "placeholder",
                                                 "manual_todo", "mismatch", "warn")) -> None:
        for e in self.entries:
            if e["family"] == family and e["action"] in actions:
                print("    [%s] %s%s" % (e["action"], e["object"],
                                         (": " + e["detail"]) if e["detail"] else ""))


def atomic_json(obj, path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def render_markdown(header: dict, summary: list[dict], log: Log,
                    counts: list[dict] | None, verdict: str) -> str:
    L = []
    L.append("# Account blueprint apply report")
    L.append("")
    for k, v in header.items():
        L.append("- %s: %s" % (k, v))
    L.append("")
    L.append("## Families")
    L.append("")
    L.append("| family | wanted | existed | created | verified | gaps | manual |")
    L.append("|---|---|---|---|---|---|---|")
    for s in summary:
        L.append("| %(family)s | %(wanted)s | %(existed)s | %(created)s | %(verified)s | %(gaps)s | %(manual)s |" % s)
    L.append("")
    if counts:
        L.append("## Preset record counts (target vs source)")
        L.append("")
        L.append("| folder | preset | target | source | verdict |")
        L.append("|---|---|---|---|---|")
        for c in counts:
            L.append("| %(folder)s | %(title)s | %(target)s | %(source)s | %(verdict)s |" % c)
        L.append("")
    todos = log.by("manual_todo")
    L.append("## Configure by hand (%d)" % len(todos))
    L.append("")
    for i, e in enumerate(todos, 1):
        L.append("%d. [%s] %s: %s" % (i, e["family"], e["object"], e["detail"]))
    L.append("")
    for title, action in (("Gaps", "gap"), ("Placeholders", "placeholder"),
                          ("Dropped references", "dropped"), ("Mismatches", "mismatch"),
                          ("Warnings", "warn")):
        rows = log.by(action)
        L.append("## %s (%d)" % (title, len(rows)))
        L.append("")
        for e in rows:
            L.append("- [%s] %s: %s" % (e["family"], e["object"], e["detail"]))
        L.append("")
    L.append("## Translation log (%d)" % len(log.by("translated")))
    L.append("")
    for e in log.by("translated"):
        L.append("- [%s] %s: %s" % (e["family"], e["object"], e["detail"]))
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append(verdict)
    L.append("")
    return "\n".join(L)


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
