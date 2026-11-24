"""Shipped code never depends on files that only an unshipped folder produces.

The curative board and its loader shipped while the ranking and the heir research that feed them lived in a
gitignored research folder (output/research/...), so a user who installed SiftStack had the loader and nothing
to load. A research folder holds work in progress and private data; whatever shipped code reads from must be
made by shipped code. This test fails on any module in src/ (selftests aside) that reads a path under
output/research, or puts an output folder on the import path, unless the line says why with the marker
`research-fallback-ok` (a fallback that is read only when the shipped producer's own results are missing).
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
RESEARCH = re.compile(r"""output["']?\s*[/\\,]\s*["']?research|["']output["']\s*\)?\s*/\s*["']research["']|output/research""")
IMPORT_FROM_OUTPUT = re.compile(r"sys\.path\.(insert|append)\([^)]*output")
MARK = "research-fallback-ok"


def offenders():
    out = []
    for p in sorted(SRC.rglob("*.py")):
        if p.name.startswith("selftest") or "__pycache__" in p.parts:
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines):
            if not (RESEARCH.search(line) or IMPORT_FROM_OUTPUT.search(line)):
                continue
            context = " ".join(lines[max(0, i - 3):i + 1])
            if MARK in context:
                continue
            out.append("%s:%d: %s" % (p.relative_to(SRC.parent), i + 1, line.strip()[:120]))
    return out


def test_shipped_code_reads_nothing_only_a_research_folder_makes():
    bad = offenders()
    assert not bad, "shipped code reads an unshipped research folder:\n" + "\n".join(bad)


def test_the_patterns_catch_the_shapes_that_shipped_before():
    for line in ('PILOT = ROOT / "output" / "research" / "curative_title" / "pilot"',
                 'D = r"C:\\x\\output\\research\\curative_title\\work"',
                 "WORK = os.path.join(ROOT, 'output', 'research', 'curative_title')",
                 'p = "output/research/curative_title/pilot/stories_results.json"'):
        assert RESEARCH.search(line), line
    assert IMPORT_FROM_OUTPUT.search('sys.path.insert(0, "output/research/method")')
    assert not RESEARCH.search('out / "research" / ("%s.json" % slug)')


def test_the_marker_is_rare():
    marked = [p for p in SRC.rglob("*.py") if MARK in p.read_text(encoding="utf-8", errors="replace")]
    assert len(marked) <= 2, "research fallbacks are meant to be rare: %s" % marked
