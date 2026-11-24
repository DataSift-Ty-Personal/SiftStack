"""Every workflow SiftStack ships has to run for the person who installs it, on their own Open API key.

The class of fault this guards: a workflow that ran on the team's own workstation shipped as if anyone could run
it. The curative board shipped without the ranking that feeds it; the schema setup and the dispo phases loaded a
client from one desktop folder; the agency pools read the production cluster behind a staff MCP; the Dream 100
pack looked for its logo on one desktop. Each one read fine and failed on a user's machine.

Checks, over the shipped Python (src, tools, deploy, skills, plugins; selftests and tests aside):

  machine-path  a string in code naming one machine's folders (a Windows user folder, C:\\pp, /Users/<name>,
                a Desktop or OneDrive folder), or a sys.path entry built from the home folder
  import        an import nothing installs: not the standard library, not in a requirements file, not a module in
                the repo or beside the file, and not inside a try that catches ImportError
  staff         a staff-only source (the production OpenSearch cluster through the staff MCP, a pasted or minted
                DataSift login, impersonation, the Deal Room checkout) in a file the org chart (docs/agents.json)
                lists for a live or gated agent, when that agent is not itself labelled staff only

A line that is meant to be staff-only says so: `staff-only` on the line or in the two lines above it, or in the
module's docstring for a whole staff module. Docstrings and comments are never read as code.

  python tools/workflow_audit.py                 # the repo
  python tools/workflow_audit.py <tree> --json report.json
Exit 1 on any finding. tests/test_workflow_audit.py runs it in pytest, and the public release runs pytest.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOTS = ("src", "tools", "deploy", "skills", "plugins")
SKIP_PARTS = {"__pycache__", "node_modules", ".venv", "dist", "archive", "vendor", "output"}
MARK = re.compile(r"staff[- ]only|audit-pattern", re.I)   # audit-pattern: a regex that finds such paths, not a use
MACHINE = re.compile(r"(?i)(\b[a-z]:[\\/]{1,2}(?:users|pp)[\\/])|(/Users/[A-Za-z][^/\s]*/)|([\\/]Desktop[\\/])|"
                     r"(OneDrive[\\/])")
HOME_PATH = re.compile(r"sys\.path\.(?:insert|append)\([^)]*(?:Path\.home\(\)|expanduser\()")
STAFF = [
    (re.compile(r"\bopensearch_mcp\b|\bOpenSearchMCP\b"), "the production OpenSearch cluster (the staff MCP)"),
    (re.compile(r"/impersonate/"), "impersonation"),
    (re.compile(r"\breisift_session\b|\breisift_auth\b|\bminted_crm\b"), "a pasted or minted DataSift login"),
    (re.compile(r"Deal Room Coaching Call|\bcrm_api\b|\bsiftmap_api\b|\bfrom dossier\b|\bimport dossier\b"),
     "the Deal Room checkout"),
]
# A distribution whose import name differs from its name in requirements.
ALIAS = {"beautifulsoup4": "bs4", "opencv_python_headless": "cv2", "opencv_python": "cv2", "python_dotenv": "dotenv",
         "pyyaml": "yaml", "pillow": "PIL", "scrapfly_sdk": "scrapfly", "google_api_python_client": "googleapiclient",
         "google_auth": "google", "google_auth_oauthlib": "google_auth_oauthlib", "python_dateutil": "dateutil",
         "pdfminer_six": "pdfminer", "pdfminer.six": "pdfminer", "2captcha_python": "twocaptcha", "pyjwt": "jwt",
         "python_multipart": "multipart", "scikit_learn": "sklearn", "pymupdf": "fitz", "python_docx": "docx",
         "dropbox_sdk": "dropbox", "strands_agents_sops": "strands_agents_sops", "claude_agent_sdk": "claude_agent_sdk",
         "pillow_heif": "pillow_heif", "opensearch_py": "opensearchpy"}


def shipped_files(root: Path):
    for top in ROOTS:
        base = root / top
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root)
            if any(part in SKIP_PARTS for part in rel.parts) or p.name.startswith(("selftest", "test_")):
                continue
            yield p


def requirement_imports(root: Path) -> set[str]:
    out: set[str] = set()
    for req in list(root.glob("requirements*.txt")) + list(root.glob("deploy/requirements*.txt")) + \
            list(root.glob("skills/*/requirements*.txt")):
        for ln in req.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.split("#")[0].strip()
            if not ln or ln.startswith("-"):
                continue
            name = re.split(r"[<>=!~\[; ]", ln, maxsplit=1)[0].strip().lower().replace("-", "_")
            if name:
                out |= {name, ALIAS.get(name, name)}
    return out


def local_names(root: Path) -> set[str]:
    """Every module name the repo can supply: the top level of src, tools and deploy, their packages and folders
    (a folder of modules imports without an __init__), every module under src and tools (code puts its own
    subfolder on the path), and each skill's scripts (a skill puts its scripts folder on the path)."""
    names: set[str] = set()
    for top in ("src", "tools", "deploy", "."):
        base = root / top
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if p.suffix == ".py":
                names.add(p.stem)
            elif p.is_dir() and not p.name.startswith(".") and any(p.glob("*.py")):
                names.add(p.name)
    for top in ("src", "tools"):
        for p in (root / top).rglob("*.py") if (root / top).is_dir() else []:
            if "__pycache__" not in p.parts:
                names.add(p.stem)
    for p in root.glob("skills/*/scripts/*.py"):
        names.add(p.stem)
    return names


def docstring_ids(tree: ast.AST) -> set[int]:
    """The id of every string that is a statement on its own (a docstring, or a string used as a comment)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out.add(id(node.value))
    return out


def guarded(node, parents: dict) -> bool:
    for anc in parents.get(id(node), []):
        if isinstance(anc, ast.Try):
            for h in anc.handlers:
                n = ast.unparse(h.type) if h.type is not None else "BaseException"
                if any(x in n for x in ("ImportError", "ModuleNotFoundError", "Exception")):
                    return True
    return False


def parent_chains(tree: ast.AST) -> dict:
    out: dict = {}

    def walk(node, chain):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = chain + [node]          # every ancestor, the immediate parent included
            walk(child, chain + [node])
    walk(tree, [])
    return out


def marked(lines: list[str], lineno: int) -> bool:
    lo = max(0, lineno - 3)
    return any(MARK.search(x) for x in lines[lo:lineno])


def staff_agents(root: Path) -> dict[str, list[dict]]:
    """source file -> the live or gated agents the org chart says run it, with whether each is labelled staff only."""
    p = root / "docs" / "agents.json"
    if not p.exists():
        return {}
    out: dict[str, list[dict]] = {}

    def walk(o):
        if isinstance(o, dict):
            if "id" in o and "role" in o and o.get("status") in ("live", "gated"):
                label = MARK.search(" ".join(str(o.get(k) or "") for k in ("role", "gotcha"))) is not None
                for src in str(o.get("source") or "").split(","):
                    src = src.strip()
                    if src:
                        out.setdefault(src, []).append({"id": o["id"], "staff": label})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(json.loads(p.read_text(encoding="utf-8")))
    return out


def audit(root: Path) -> list[dict]:
    root = root.resolve()
    reqs, local = requirement_imports(root), local_names(root)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    agents = staff_agents(root)
    found: list[dict] = []
    for p in shipped_files(root):
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            found.append({"check": "syntax", "file": rel, "line": exc.lineno or 0, "what": str(exc)[:120]})
            continue
        module_doc = ast.get_docstring(tree) or ""
        whole_file_staff = bool(MARK.search(module_doc))
        docs = docstring_ids(tree)
        parents = parent_chains(tree)
        siblings = {q.stem for q in p.parent.glob("*.py")} | {q.name for q in p.parent.iterdir() if q.is_dir()}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if MACHINE.search(node.value) and not whole_file_staff and not marked(lines, node.lineno):
                    found.append({"check": "machine-path", "file": rel, "line": node.lineno,
                                  "what": node.value[:100]})
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m in stdlib or m in local or m in siblings or m.lower() in reqs or m in reqs:
                    continue
                if guarded(node, parents) or whole_file_staff or marked(lines, node.lineno):
                    continue
                found.append({"check": "import", "file": rel, "line": node.lineno,
                              "what": "imports %s, which nothing installs" % m})
        for i, ln in enumerate(lines, 1):
            if HOME_PATH.search(ln) and not marked(lines, i) and not whole_file_staff:
                found.append({"check": "machine-path", "file": rel, "line": i, "what": ln.strip()[:100]})
        owners = agents.get(rel) or []
        if owners and not whole_file_staff and not all(a["staff"] for a in owners):
            names = ", ".join(a["id"] for a in owners if not a["staff"])
            doc_lines: set[int] = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and id(n) in docs:
                    doc_lines.update(range(n.lineno, (n.end_lineno or n.lineno) + 1))
            for rx, why in STAFF:
                for i, ln in enumerate(lines, 1):
                    stripped = ln.split("#", 1)[0]
                    if rx.search(stripped) and i not in doc_lines and not marked(lines, i):
                        found.append({"check": "staff", "file": rel, "line": i,
                                      "what": "%s, in a file the org chart runs for %s" % (why, names)})
                        break
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--json", default="", help="write the findings here too")
    args = ap.parse_args(argv)
    found = audit(Path(args.tree))
    if args.json:
        Path(args.json).write_text(json.dumps(found, indent=1), encoding="utf-8")
    for f in found:
        print("%-12s %s:%d  %s" % (f["check"], f["file"], f["line"], f["what"]))
    print("workflow audit: %d finding%s" % (len(found), "" if len(found) == 1 else "s"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
