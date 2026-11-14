"""SiftStack's command-line scripts as a catalog, and the rule for running one from Slack.

Why this exists: in Claude Code the owner runs any src/*.py script with any flags. In Slack the team
could run its wired workflows and nothing else, so "run dream100 --phase build" had no answer. This
module lets an employee ask for a script without being handed a shell. It reads every script's
command line WITHOUT importing it (an import runs module code: a .env read, a login, a network call)
and sorts each script into one class:

  wired     a live roster workflow already runs it: refused, naming the workflow, so the ask goes
            through the workflow and its caps instead of around them
  retired   a one-off or a superseded script: refused, naming what replaced it
  browser   drives a browser (Playwright, the DataSift web app, the notice scraper); the brain image
            has none, so it is refused with the command line to type on the workstation
  local     reads files on the owner's PC (the Deal Room checkout, a Desktop path): the same
  package   imports a package the brain image does not install: the same
  runnable  may run on the brain

A runnable script's own command line then decides what happens to one ask:

  writes    commit   a write flag (--commit, --finish, --fix, ...) turns its writes on; without one
                     the run is a dry run
            default  it writes unless its dry switch (--dry, --dry-run) is given
            always   it reaches a write with no switch at all
  paid      its imports reach a paid vendor client (Trestle, SmartSkip, Tracerfy, Enformion,
            Scrapfly, Zillow through OpenWeb Ninja, Claude, OpenRouter, ...)

A command line that writes, and any run of a paid script except a vetted free dry run, never runs
straight from a model: run_script stages it as a batch a named person releases (agents/release.py).
Everything else (reports, dry runs, audits, plans) runs when asked.

Rules, each from a way this goes wrong without it:
  * Exact option strings only. argparse accepts an unambiguous prefix, so --comm runs as --commit;
    a check that looked for "--commit" in the tokens would wave it through.
  * A value that looks like a file must sit under output/ (or a run folder on the box). A read-only
    report given --out /data/agents.db would overwrite the brain's own database.
  * No shell, and no shell characters in a token even so: the model must never believe it can
    chain a second command onto the first.
  * The class is worked out from the source tree each time a script is asked for (cached by file
    times), so a stale team/commands.json can never wave through a script that gained --commit.
    The committed file is the reviewed list a person reads; the selftest fails when it is stale.
  * A run that is not released keeps no paid vendor key, no Slack token and no outreach credential
    in its environment (child_env). If a paid path was missed here, the vendor call fails instead
    of spending; the record allowance and DataSift writes still rest on the class above.

    python src/agents/commands.py --write        # rebuild team/commands.json
    python src/agents/commands.py --check        # exit 1 when team/commands.json is stale
    python src/agents/commands.py --show dream100
"""
from __future__ import annotations

import ast
import difflib
import io
import json
import logging
import os
import re
import shlex
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):          # run as a file: python src/agents/commands.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "agents"             # noqa: A001

from . import config  # noqa: E402

log = logging.getLogger(__name__)

WORKFLOW = "run_script"            # the direct workflow that runs or stages one command
BATCH_KIND = "command"             # the release kind (agents/release.py RELEASE)
DEFAULT_OWNER = "ops"              # Quinn, when the roster names no owner for run_script
GENERATED = "GENERATED from src/*.py by python src/agents/commands.py --write. Do not edit by hand."

# ------------------------------------------------------------------------------------ settings

def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def enabled() -> bool:
    """AGENTS_SCRIPT_TOOLS=0 takes script_catalog and run_script off every employee."""
    return (os.environ.get("AGENTS_SCRIPT_TOOLS") or "1").strip().lower() not in ("0", "off", "no", "false")


def timeout_s() -> float:
    """One script's time limit (AGENTS_SCRIPT_TIMEOUT_MIN, 20). The brain has ONE worker, so a script
    holds every other task (Slack asks, the clock's stages, a webhook's deep prospect) until it ends."""
    return max(10.0, _num("AGENTS_SCRIPT_TIMEOUT_MIN", 20.0) * 60.0)


def script_root() -> Path:
    """Where a script runs (AGENTS_SCRIPT_ROOT): the repo root, /app on the box."""
    return Path(os.environ.get("AGENTS_SCRIPT_ROOT") or config.ROOT)


# ------------------------------------------------------------------------------------ tables
# Scripts retired on purpose, each with why and what to use instead ("" when nothing replaces it).
# Kept short and explicit: a pattern would one day retire a real script that happened to match it.
_ONE_OFF = ("a one-off for a single estate or house; the steps it ran by hand are the deep "
            "prospect chain now", "the deep_prospect workflow (Diggs)")
_PROBE = ("an investigation probe of the DataSift web app's screens, written while building the "
          "upload wizard", "")
RETIRED: dict[str, tuple[str, str]] = {
    "soi_county_pull": ("the Columbus sphere-of-influence beta, retired 2026-11-03", ""),
    "soi_enformion": ("the Columbus sphere-of-influence beta, retired 2026-11-03", ""),
    "soi_enrich": ("the Columbus sphere-of-influence beta, retired 2026-11-03", ""),
    "soi_intake": ("the Columbus sphere-of-influence beta, retired 2026-11-03", ""),
    "soi_owner_db": ("the Columbus sphere-of-influence beta, retired 2026-11-03", ""),
    "soi_owner_match": ("the Columbus sphere-of-influence beta, retired 2026-11-03", ""),
    "scrapfly_spike": ("a one-notice spike for the Scrapfly notice route, which the scraper no longer uses", ""),
    "enformion_heir_test": ("a test harness for the v4 Enformion heir search, which deep prospecting v5 replaced",
                            "the deep_prospect workflow (Diggs)"),
    "run_deep_prospect": ("the v4 Enformion heir waterfall, replaced by deep prospecting v5 (SmartSkip first)",
                          "the deep_prospect workflow (Diggs)"),
    "staging_build_39049": ("the one-shot build of the ty+1 staging account (2026-08-19)",
                            "the account_setup workflow (Atlas)"),
    "run_brice_full_workflow": _ONE_OFF,
    "run_brice_heirs_enformion": _ONE_OFF,
    "run_brice_st_skiptrace": _ONE_OFF,
    "run_jarboe_linda_rd_skiptrace": _ONE_OFF,
    "run_laurans_brown_skiptrace": _ONE_OFF,
    "scout_overton": _ONE_OFF,
    "scout_overton2": _ONE_OFF,
    "score_overton": _ONE_OFF,
    "build_linda_rd_report": ("a one-off report for a single estate", "the deep_prospect workflow (Diggs)"),
    "restore_priority1": ("a one-off repair after a tag was stripped account-wide", ""),
    "repair_pr_owners": ("a one-off repair of the owner on the August probate backfill", ""),
    "probate_audit_restore": ("a one-off audit and repair of the August probate backfill", ""),
    "ty2_reactivation_rebuild": ("a one-off rebuild of the Not Interested campaigns (2026-08-25)", ""),
    "upload_rei_skills_drive": ("the old Google Drive upload of the skill files",
                                "the plugin marketplace (python tools/build_skills.py --marketplace)"),
    "dropbox_get_refresh_token": ("a one-time setup that waits for a code typed at the keyboard", ""),
    "obituary_experiment": ("an experiment harness from April 2026", ""),
    "_debug_login": _PROBE,
    "_discover_remove_from_list": _PROBE,
    "_dropbox_proof": ("a one-off proof of the retired notice screenshot hosting", ""),
    "_probe_lists_ui": _PROBE,
    "_probe_nolist": _PROBE,
    "_wizard_discover": _PROBE,
}

# Paid scripts whose dry run (the same command without its write flag) was read line by line: it
# counts and prices, then returns before any paid call. Only these run a dry run from Slack, and
# only these get a quote before a person releases the real run. Every other paid script is staged
# whole, because a dry run that still pays (deep_prospect_batch's research does) would spend on a
# model's word.
FREE_DRY_RUN: dict[str, str] = {
    "obituary_owner_phones": "without --commit it reads the local cache, counts the numbers it would score and "
                             "returns before the first Trestle call (read 2026-11-13)",
}
# Scripts whose import graph reaches a vendor client they never call, each read by hand. Without this
# a read-only report that borrows one helper from a paid module would be staged on every run.
NOT_PAID: dict[str, str] = {
    "agency_call_list": "imports d4d_call_list for its CRM call wrapper, its gate and its file helpers only; it "
                        "never scores a number (read 2026-11-13)",
}

# Flags whose name says "write" but which only read, and flags that write although their name does not
# say so. Each is a script-and-flag pair checked by hand.
NOT_WRITE_FLAGS: dict[str, set[str]] = {}
WRITE_FLAGS_EXTRA: dict[str, set[str]] = {}

# How a script writes when the flags do not say (a script with no switch whose own code calls a write).
# "always": every run is staged; "": read by hand and it writes nothing to the account.
WRITE_MODE: dict[str, str] = {}

# Words that make a SWITCH a write flag (a flag that takes no value: --commit, --post, --fix). A value
# flag is never a write flag by its name alone (--run <folder> names a folder).
WRITE_WORDS = {"commit", "apply", "upload", "push", "send", "finish", "fix", "write", "post", "publish",
               "deploy", "delete", "remove", "release", "queue", "launch", "schedule", "notify", "lock",
               "mark", "unlist", "restore", "repair", "reconcile", "merge", "sync", "import", "create",
               "add", "attach", "tag", "undo", "reset", "clear", "enable", "disable", "execute", "live",
               "confirm", "yes", "once", "run", "submit", "trigger", "stamp", "strip"}
# Switches that make a default-writing script dry.
DRY_FLAGS = {"--dry", "--dry-run", "--dryrun", "--plan-only", "--read-only", "--no-write"}

# Modules whose import means a browser (the brain image has none).
BROWSER_MODULES = {"playwright", "datasift_core", "sift_upload_wizard", "datasift_uploader", "mms_sender", "scraper"}
# Modules that live in the Deal Room checkout on the owner's PC, reached through sys.path.
DEAL_ROOM_MODULES = {"crm_api", "clients", "reisift_auth", "dossier", "mms_ledger", "mms_schedule",
                     "smrtphone_login", "siftmap_client", "kpi_engine", "dial_kpis"}
# Code that names a path on the owner's PC. A file that also names a key-only client falls back to
# it, so the Deal Room is a preference there, not a need (the audit tool's rule).
LOCAL_RX = re.compile(r"C:\\{1,2}Users|C:/Users/|OneDrive[\\/]{1,2}Desktop|Deal Room Coaching Call|_api[\\/]{1,2}clients")
KEY_FALLBACK_RX = re.compile(r"crm_key_client|crm_standalone|siftmap_standalone")

# Paid vendors, by the host a client calls or the SDK it imports. A module named config.py declares
# hosts and calls none, so it is never scanned for them.
VENDOR_HOSTS = (
    ("Trestle", re.compile(r"trestleiq\.com")),
    ("Tracerfy", re.compile(r"tracerfy\.com")),
    ("Enformion", re.compile(r"enformion\.com")),
    ("OpenRouter", re.compile(r"openrouter\.ai")),
    ("Zillow (OpenWeb Ninja)", re.compile(r"openwebninja\.com")),
    ("Scrapfly", re.compile(r"scrapfly\.io")),
    ("2Captcha", re.compile(r"2captcha\.com")),
    ("SerpApi", re.compile(r"serpapi\.com")),
    ("Serper", re.compile(r"serper\.dev")),
    ("Firecrawl", re.compile(r"firecrawl\.dev")),
    ("Google Maps", re.compile(r"maps\.googleapis\.com")),
    ("Smarter Contact", re.compile(r"smartercontact\.com")),
    ("smrtPhone", re.compile(r"smrt\.studio")),
    ("Smarty", re.compile(r"smarty\.com|smartystreets\.com")),
)
VENDOR_IMPORTS = {"anthropic": "Claude", "claude_agent_sdk": "Claude", "twocaptcha": "2Captcha",
                  "scrapfly": "Scrapfly", "smartskip_trace": "SmartSkip", "parse_smartskip": "SmartSkip",
                  "openai": "OpenAI", "smartystreets_python_sdk": "Smarty"}
# SiftMap reads that DataSift's acceptable use limits: property detail pulled in volume without adding
# the records counts as scraping, and autocomplete is not for volume. A script whose own code makes
# them is staged for a person, whatever its flags.
SIFTMAP_READ_RX = re.compile(r"\.get_detail\(|\.autocomplete\(|/properties/detail/|search-autocomplete")

# Import names that differ from the package name in deploy/requirements-agents.txt, and the packages
# those pull in themselves (an import of pydantic works because fastapi brought it).
IMPORT_NAMES = {"python-dotenv": "dotenv", "pyyaml": "yaml", "pillow": "PIL", "claude-agent-sdk": "claude_agent_sdk",
                "gender-guesser": "gender_guesser", "scrapfly-sdk": "scrapfly", "python-docx": "docx",
                "beautifulsoup4": "bs4"}
CARRIED = {"pydantic", "pydantic_core", "starlette", "anyio", "sniffio", "h11", "httpcore", "idna", "certifi",
           "charset_normalizer", "urllib3", "typing_extensions", "annotated_types", "cffi", "pycparser",
           "distro", "jiter", "click", "soupsieve", "lxml", "dateutil", "loguru", "backoff", "decorator",
           "et_xmlfile", "mcp", "jsonschema", "sse_starlette", "pkg_resources", "setuptools"}

# Shell characters a token may not carry, though no shell ever sees it. Plain words, numbers,
# dates, ranges, emails, a street with an apostrophe and key=value all pass.
SHELL_CHARS = set(";&|$`<>\\\"*?{}[]()\n\r\x00")
PATH_EXT = re.compile(r"\.(csv|json|jsonl|xlsx|xls|txt|md|pdf|docx|kml|kmz|geojson|py|db|sqlite|env|yaml|yml|toml|"
                      r"html|png|jpe?g|zip|log|ini|cfg|pkl|parquet)$", re.I)
NEG_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")     # argparse's own test for a negative number
TOKEN_MAX = 400
TOKENS_MAX = 60

# ------------------------------------------------------------------------------------ source scan

def _text_without_prose(src: str) -> str:
    """The source with docstrings and comments blanked: a host or a Desktop path named in prose is
    documentation, not a dependency. Line structure is kept."""
    lines = src.splitlines(keepends=True)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    for node in [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                                         ast.ClassDef))]:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            for i in range(body[0].lineno - 1, min(body[0].end_lineno, len(lines))):
                lines[i] = "\n"
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                r, c = tok.start
                if lines[r - 1] != "\n":
                    lines[r - 1] = lines[r - 1][:c] + "\n"
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "".join(lines)


def _catches_import_error(h: ast.ExceptHandler) -> bool:
    names = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    if h.type is None:
        return True
    pool = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
    return any(isinstance(x, ast.Name) and x.id in names for x in pool)


def _is_type_checking(test) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
        (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


@dataclass
class Imp:
    name: str           # dotted, as resolved (relative imports made absolute against the package)
    alias_of: str       # "" or the module the name was imported from (from X import Y gives X.Y and X)
    lazy: bool          # inside a function
    optional: bool      # inside a try that catches ImportError


def _imports(tree, pkg: list[str]) -> list[Imp]:
    out: list[Imp] = []

    def record(n, lazy: bool, optional: bool) -> None:
        if isinstance(n, ast.Import):
            for a in n.names:
                out.append(Imp(a.name, "", lazy, optional))
        else:
            base = pkg[: len(pkg) - (n.level - 1)] if n.level else []
            mod = ".".join(base + ([n.module] if n.module else []))
            if mod:
                out.append(Imp(mod, "", lazy, optional))
            for a in n.names:
                if a.name != "*":
                    out.append(Imp("%s.%s" % (mod, a.name) if mod else a.name, mod, lazy, optional))

    def visit(stmts, lazy: bool, optional: bool) -> None:
        for n in stmts:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                record(n, lazy, optional)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(n.body, True, optional)
            elif isinstance(n, ast.ClassDef):
                visit(n.body, lazy, optional)
            elif isinstance(n, ast.Try) or type(n).__name__ == "TryStar":
                catches = any(_catches_import_error(h) for h in n.handlers)
                visit(n.body, lazy, optional or catches)
                for h in n.handlers:
                    visit(h.body, lazy, optional)
                visit(n.orelse, lazy, optional or catches)
                visit(n.finalbody, lazy, optional)
            elif isinstance(n, ast.If):
                if _is_type_checking(n.test):
                    continue
                visit(n.body, lazy, optional)
                visit(n.orelse, lazy, optional)
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
                visit(n.body, lazy, optional)
                visit(n.orelse, lazy, optional)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                visit(n.body, lazy, optional)
            elif type(n).__name__ == "Match":
                for case in n.cases:
                    visit(case.body, lazy, optional)
    visit(tree.body, False, False)
    return out


# ------------------------------------------------------------------------------------ argparse, read not run

_SWITCH_ACTIONS = {"store_true", "store_false", "store_const", "count", "append_const", "help", "version"}


class _Parser:
    def __init__(self):
        self.args: list[dict] = []
        self.subs: dict[str, "_Parser"] = {}
        self.sub_required = False


class _Subs:
    def __init__(self, parent: _Parser):
        self.parent = parent


def _ascii(text) -> str:
    """Plain ASCII for the catalog: the house writes no long dashes, and a docstring may hold them."""
    s = str(text or "")
    for a, b in ((chr(0x2014), ", "), (chr(0x2013), "-"), (chr(0x2192), "->"), (chr(0x2190), "<-"),
                 (chr(0x2265), ">="), (chr(0x2264), "<="), (chr(0x00d7), "x"), (chr(0x2018), "'"),
                 (chr(0x2019), "'"), (chr(0x201c), '"'), (chr(0x201d), '"'), (chr(0x2026), "..."),
                 (chr(0x00a0), " "), (chr(0x2248), "about ")):
        s = s.replace(a, b)
    s = s.encode("ascii", "ignore").decode("ascii")
    return " ".join(s.split())


class _Consts:
    """Module-level and local NAME = literal assignments, so choices=PHASES can be read."""

    def __init__(self, tree):
        self.nodes: dict[str, ast.AST] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                self.nodes.setdefault(n.targets[0].id, n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
                self.nodes.setdefault(n.target.id, n.value)

    def seq(self, node, depth: int = 0):
        """A literal list of choices, or None when the code builds it at run time."""
        if node is None or depth > 6:
            return None
        try:
            v = ast.literal_eval(node)
            if isinstance(v, dict):
                return list(v.keys())
            if isinstance(v, (list, tuple, set, frozenset)):
                return sorted(v, key=str) if isinstance(v, (set, frozenset)) else list(v)
            return None
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            pass
        if isinstance(node, ast.Name):
            return self.seq(self.nodes.get(node.id), depth + 1)
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in ("list", "tuple", "sorted", "set", "frozenset") and len(node.args) == 1:
                return self.seq(node.args[0], depth + 1)
            if isinstance(f, ast.Name) and f.id == "range" and node.args:
                try:
                    vals = [ast.literal_eval(a) for a in node.args]
                    r = list(range(*vals))
                    return r if len(r) <= 1000 else None
                except (ValueError, TypeError, SyntaxError):
                    return None
            if isinstance(f, ast.Attribute) and f.attr == "keys" and not node.args:
                return self.seq(f.value, depth + 1)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, b = self.seq(node.left, depth + 1), self.seq(node.right, depth + 1)
            return None if a is None or b is None else list(a) + list(b)
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for e in node.elts:
                if isinstance(e, ast.Starred):
                    v = self.seq(e.value, depth + 1)
                    if v is None:
                        return None
                    out.extend(v)
                else:
                    try:
                        out.append(ast.literal_eval(e))
                    except (ValueError, TypeError, SyntaxError):
                        return None
            return out
        return None


def _const(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _type_name(node) -> str:
    if node is None:
        return "str"
    if isinstance(node, ast.Name):
        return {"int": "int", "float": "float", "str": "str", "Path": "path"}.get(node.id, "custom")
    if isinstance(node, ast.Attribute):
        return "path" if node.attr == "Path" else "custom"
    return "custom"


def _help_text(node) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _ascii(node.value)[:140]
    if isinstance(node, ast.JoinedStr):
        return _ascii("".join(v.value for v in node.values if isinstance(v, ast.Constant)))[:140]
    return ""


def _arg(call: ast.Call, consts: _Consts) -> dict | None:
    flags = []
    for a in call.args:
        v = _const(a)
        if not isinstance(v, str):
            return {"dynamic": True}
        flags.append(v)
    if not flags:
        return {"dynamic": True}
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    if any(k.arg is None for k in call.keywords):
        return {"dynamic": True}
    action = kw.get("action")
    act = _const(action) if action is not None else "store"
    if isinstance(action, ast.Attribute) and action.attr == "BooleanOptionalAction":
        act = "bool"
    positional = not flags[0].startswith("-")
    if positional:
        kind = "positional"
    elif act == "bool":
        kind = "bool"
    elif act in _SWITCH_ACTIONS:
        kind = "switch"
    else:
        kind = "value"
    out: dict = {"flags": flags, "kind": kind}
    nargs = _const(kw["nargs"]) if "nargs" in kw else None
    if nargs is not None:
        out["nargs"] = nargs
    if "choices" in kw:
        ch = consts.seq(kw["choices"])
        out["choices"] = [c if isinstance(c, (int, float)) else _ascii(c) for c in ch] if ch is not None else "dynamic"
    t = _type_name(kw.get("type"))
    if t != "str" and kind in ("value", "positional"):
        out["type"] = t
    if _const(kw.get("required")) is True:
        out["required"] = True
    if "default" in kw and kind in ("value", "positional"):
        d = _const(kw["default"])
        if isinstance(d, (str, int, float, bool)) and d not in ("", None):
            out["default"] = _ascii(d) if isinstance(d, str) else d
    h = _help_text(kw.get("help"))
    if h:
        out["help"] = h
    return out


def _cli(tree, consts: _Consts) -> dict:
    """The script's argparse definition, read from the code: root options, subcommands, and options
    added to a parser this walk could not follow (a helper given the parser), kept as shared."""
    roots: list[_Parser] = []
    shared: list[dict] = []
    dynamic = [False]

    def ev(node, env):
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if not isinstance(node, ast.Call):
            return None
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == "ArgumentParser") or \
                (isinstance(f, ast.Name) and f.id == "ArgumentParser"):
            p = _Parser()
            roots.append(p)
            return p
        if not isinstance(f, ast.Attribute):
            return None
        recv = ev(f.value, env)
        m = f.attr
        if m == "add_subparsers" and isinstance(recv, _Parser):
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            recv.sub_required = bool(_const(kw.get("required")))
            return _Subs(recv)
        if m == "add_parser" and isinstance(recv, _Subs):
            child = _Parser()
            names = []
            if node.args:
                a0 = node.args[0]
                v = _const(a0)
                if isinstance(v, str):
                    names = [v]
                elif isinstance(a0, ast.Name) and isinstance(env.get(a0.id), list):
                    names = env[a0.id]
                    hit = env.get(("loop_parser", a0.id))
                    if hit is not None:
                        return hit
                    env[("loop_parser", a0.id)] = child
                else:
                    dynamic[0] = True
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            for al in consts.seq(kw.get("aliases")) or []:
                if isinstance(al, str):
                    names.append(al)
            for nm in names:
                recv.parent.subs[_ascii(nm)] = child
            return child
        if m in ("add_argument_group", "add_mutually_exclusive_group") and isinstance(recv, _Parser):
            return recv
        if m == "add_argument":
            a = _arg(node, consts)
            if a and a.get("dynamic"):
                dynamic[0] = True
            elif a:
                (recv.args if isinstance(recv, _Parser) else shared).append(a)
        return None

    def walk(stmts, env):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(s.body, dict(env))
            elif isinstance(s, ast.ClassDef):
                walk(s.body, dict(env))
            elif isinstance(s, ast.Assign):
                val = ev(s.value, env) if isinstance(s.value, (ast.Call, ast.Name)) else None
                for t in s.targets:
                    if isinstance(t, ast.Name):
                        env[t.id] = val
            elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.value is not None:
                env[s.target.id] = ev(s.value, env) if isinstance(s.value, (ast.Call, ast.Name)) else None
            elif isinstance(s, ast.Expr):
                ev(s.value, env)
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                vals = consts.seq(s.iter)
                inner = dict(env)
                if isinstance(s.target, ast.Name) and vals is not None and all(isinstance(v, str) for v in vals):
                    inner[s.target.id] = list(vals)
                walk(s.body, inner)
                for k, v in inner.items():
                    if isinstance(k, str) and k not in env and isinstance(v, (_Parser, _Subs)):
                        env[k] = v
                walk(s.orelse, env)
            elif isinstance(s, (ast.If, ast.While)):
                walk(s.body, env)
                walk(s.orelse, env)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                walk(s.body, env)
            elif isinstance(s, ast.Try) or type(s).__name__ == "TryStar":
                walk(s.body, env)
                for h in s.handlers:
                    walk(h.body, env)
                walk(s.orelse, env)
                walk(s.finalbody, env)

    walk(tree.body, {})
    if not roots:
        return {}
    root = roots[0]
    for extra in roots[1:]:                    # a second parser in one file: its options are allowed too
        root.args.extend(extra.args)
        root.subs.update(extra.subs)

    def dump(p: _Parser) -> dict:
        d: dict = {"args": p.args}
        if p.subs:
            d["subcommands"] = {n: {"args": c.args} for n, c in sorted(p.subs.items())}
            d["subcommand_required"] = p.sub_required
        return d
    out = dump(root)
    if shared:
        if root.subs:
            out["shared"] = shared
        else:
            out["args"] = root.args + shared
    out["complete"] = not dynamic[0]
    return out


def _argv_cli(tree) -> dict:
    """A script with no argparse that reads sys.argv itself: literal flags it compares against
    (--selftest in sys.argv) become switches, and a sys.argv[N] read becomes one free value."""
    flags, words, free = set(), set(), False

    def is_argv(n) -> bool:
        return any(isinstance(x, ast.Attribute) and x.attr == "argv" and isinstance(x.value, ast.Name)
                   and x.value.id == "sys" for x in ast.walk(n))
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and (is_argv(n.left) or any(is_argv(c) for c in n.comparators)):
            for side in [n.left] + list(n.comparators):
                v = _const(side)
                for s in (v if isinstance(v, (list, tuple, set)) else [v]):
                    if isinstance(s, str) and s:
                        (flags if s.startswith("-") else words).add(_ascii(s))
        elif isinstance(n, ast.Subscript) and is_argv(n.value):
            free = True
    if not (flags or words or free):
        return {}
    args = [{"flags": [f], "kind": "switch"} for f in sorted(flags)]
    if words:
        args.append({"flags": ["argv"], "kind": "positional", "nargs": "?", "choices": sorted(words)})
    elif free:
        args.append({"flags": ["argv"], "kind": "positional", "nargs": "?"})
    return {"args": args, "complete": False, "argv": True}


def _is_cli(tree) -> bool:
    for n in tree.body:
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare):
            parts = [n.test.left] + list(n.test.comparators)
            if any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts) and \
                    any(_const(p) == "__main__" for p in parts):
                return True
    return any(isinstance(n, ast.Call) and ((isinstance(n.func, ast.Attribute) and n.func.attr == "ArgumentParser")
                                            or (isinstance(n.func, ast.Name) and n.func.id == "ArgumentParser"))
               for n in ast.walk(tree))


# ------------------------------------------------------------------------------------ the module graph

class _Tree:
    """Every src module this build reads, parsed once, with the facts each class rests on."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.src = self.root / "src"
        self.mods: dict[Path, dict] = {}
        self.stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
        self.local_top = {p.stem for p in self.src.glob("*.py")} | \
            {p.name for p in self.src.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))}
        self.skill_mods = {p.stem for p in (self.root / "skills").rglob("*.py")} if (self.root / "skills").is_dir() else set()
        self.installed = self._installed()

    def _installed(self) -> set[str]:
        """Import names the brain image installs: deploy/requirements-agents.txt (the image copies it
        to requirements.txt, which is where the box itself finds it), plus what those bring along."""
        for f in (self.root / "deploy" / "requirements-agents.txt", self.root / "requirements.txt"):
            if f.is_file() and (f.name != "requirements.txt" or not (self.root / "deploy").is_dir()):
                names = set()
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.split("#", 1)[0].strip()
                    m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
                    if m:
                        dist = m.group(1).lower()
                        names.add(IMPORT_NAMES.get(dist, dist.replace("-", "_")))
                return names | CARRIED
        return set(CARRIED)

    def file_for(self, dotted: str) -> Path | None:
        base = self.src.joinpath(*dotted.split("."))
        for cand in (base.with_suffix(".py"), base / "__init__.py"):
            if cand.is_file():
                return cand
        return None

    def info(self, p: Path) -> dict:
        p = p.resolve()
        if p in self.mods:
            return self.mods[p]
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
            self.mods[p] = d = {"tree": None, "imports": [], "code": "", "local_files_top": set(),
                                "local_files_lazy": set(), "external": []}
            return d
        try:
            pkg = list(p.relative_to(self.src.resolve()).parent.parts)
        except ValueError:
            pkg = []
        imps = _imports(tree, pkg)
        top, lazy, external = set(), set(), []
        for im in imps:
            f = self.file_for(im.name)
            if f:
                (lazy if im.lazy else top).add(f.resolve())
                continue
            if im.alias_of:          # from X import name: X itself was recorded on its own
                continue
            head = im.name.split(".")[0]
            if head in self.stdlib or head in self.local_top:
                continue
            external.append((head, im.lazy, im.optional))
        code = _text_without_prose(src)
        d = {"tree": tree, "imports": imps, "code": code, "local_files_top": top, "local_files_lazy": lazy - top,
             "external": external, "name": p.stem}
        self.mods[p] = d
        return d

    def closure(self, entry: Path) -> tuple[set[Path], set[Path]]:
        """(modules the script loads or calls into, modules reached only through a deeper lazy import).
        The script's own function-scope imports count as loaded: it calls them on its main paths."""
        entry = entry.resolve()
        strict: set[Path] = set()
        todo = [entry] + sorted(self.info(entry)["local_files_lazy"])
        while todo:
            f = todo.pop()
            if f in strict:
                continue
            strict.add(f)
            todo.extend(self.info(f)["local_files_top"] - strict)
        deeper: set[Path] = set()
        todo = [g for f in strict for g in self.info(f)["local_files_lazy"] if g not in strict]
        while todo:
            f = todo.pop()
            if f in strict or f in deeper:
                continue
            deeper.add(f)
            todo.extend(self.info(f)["local_files_top"] | self.info(f)["local_files_lazy"])
        return strict, deeper


def _vendors(info: dict, own: bool) -> set[str]:
    """Paid vendors one module calls: a vendor host in its code, or a vendor SDK it imports. Outside
    the script itself only a module-scope SDK import counts, the same line the local graph draws."""
    if info.get("name") == "config":
        return set()
    found = {label for label, rx in VENDOR_HOSTS if rx.search(info["code"])}
    for im in info["imports"]:
        if im.lazy and not own:
            continue
        head = im.name.split(".")[-1] if im.alias_of else im.name.split(".")[0]
        for key in {im.name.split(".")[0], head}:
            if key in VENDOR_IMPORTS:
                found.add(VENDOR_IMPORTS[key])
    return found


def _module_names(info: dict) -> set[str]:
    """Every module the file imports, by its first and last name part. An import guarded by except
    ImportError is left out: the file runs without it."""
    out = set()
    for im in info["imports"]:
        if im.optional:
            continue
        out.add(im.name.split(".")[0])
        out.add(im.name.split(".")[-1])
    return out


# ------------------------------------------------------------------------------------ one script

def _summary(tree) -> str:
    doc = (ast.get_docstring(tree) or "").strip()
    first = []
    for line in doc.splitlines():
        if not line.strip():
            break
        first.append(line.strip())
    return _ascii(" ".join(first))[:200]


def _flag_names(cli: dict) -> list[dict]:
    """Every option the script declares, root, shared and subcommand, in one list."""
    out = list(cli.get("args") or []) + list(cli.get("shared") or [])
    for sub in (cli.get("subcommands") or {}).values():
        out += sub.get("args") or []
    return out


def _write_flags(name: str, cli: dict) -> tuple[list[str], list[str]]:
    writes, dry = set(), set()
    for a in _flag_names(cli):
        if a.get("kind") == "positional":
            continue
        for f in a.get("flags") or []:
            if f in DRY_FLAGS:
                dry.add(f)
                continue
            if a.get("kind") not in ("switch", "bool"):
                continue
            words = f.lstrip("-").lower().split("-")
            if words and words[0] in ("no", "skip", "dry", "include", "exclude", "only", "allow", "max", "min"):
                continue
            if set(words) & WRITE_WORDS:
                writes.add(f)
    writes |= WRITE_FLAGS_EXTRA.get(name, set())
    writes -= NOT_WRITE_FLAGS.get(name, set())
    return sorted(writes), sorted(dry)


_WRITE_CALL_RX = re.compile(
    r"\.(?:add_tags|remove_tags|add_lists|remove_lists|add_notes?|post_note|post_message|pin_message|pin_note|"
    r"upsert_phones|update_values|update_custom_fields|write_fields|create_property|attach_to_record|set_status|"
    r"set_assignee|assign_record|patch_record|upload_csv|add_properties_by_query|create_preset|update_preset|"
    r"delete_preset|skip_trace|send_sms|send_text|mark_uploaded)\(|"
    r"[\"'][^\"'\n]*/(?:add-tags|remove-tags|add-lists|remove-lists|add-notes|upsert-phones|update-values|"
    r"skip-trace|add-properties-by-query|remove-from-list)/?[\"']|"
    r"\b(?:method|verb)\s*=\s*[\"'](?:PATCH|PUT|DELETE)[\"']|\.(?:patch|put|delete)\(\s*[\"'f]")


def _own_writes(info: dict) -> list[str]:
    return sorted({m.group(0).strip(".(\"' ")[:40] for m in _WRITE_CALL_RX.finditer(info["code"])})


def build_entry(tree: _Tree, path: Path, wired: dict[str, list[str]], owners: dict[str, str]) -> dict:
    name = path.stem
    info = tree.info(path)
    t = info["tree"]
    if t is None:
        return {"class": "broken", "summary": "", "why": "its source does not parse", "args": []}
    consts = _Consts(t)
    cli = _cli(t, consts)
    via = ""
    if not cli:
        for im in info["imports"]:            # from account_blueprint.cli import main
            if im.alias_of and im.name.endswith(".main"):
                f = tree.file_for(im.alias_of)
                if f:
                    d = tree.info(f)
                    if d["tree"] is not None:
                        cli = _cli(d["tree"], _Consts(d["tree"]))
                        via = im.alias_of if cli else ""
                        if cli:
                            break
    if not cli:
        cli = _argv_cli(t)
    strict, deeper = tree.closure(path)
    mods = [tree.info(f) for f in sorted(strict)]

    # browser
    browser = set()
    for d in mods:
        names = _module_names(d)
        if d is info:
            browser |= names & BROWSER_MODULES
        else:
            top_ext = {h for h, lazy, opt in d["external"] if not lazy and not opt}
            browser |= top_ext & {"playwright"}
    browser |= {tree.info(f)["name"] for f in strict if tree.info(f).get("name") in BROWSER_MODULES and f != path.resolve()}

    # local and package: module-scope imports a fresh process makes before the script's first line runs
    loaded = {path.resolve()}
    todo = [path.resolve()]
    while todo:
        f = todo.pop()
        for g in tree.info(f)["local_files_top"]:
            if g not in loaded:
                loaded.add(g)
                todo.append(g)
    # An unguarded module-scope import of a Deal Room module fails on the box whatever else the file
    # says; a missing package counts only on the chain a fresh process loads before its first line.
    local, needs = set(), set()
    for f in loaded | strict:
        for head, lazy, opt in tree.info(f)["external"]:
            if lazy or opt:
                continue
            if head in DEAL_ROOM_MODULES:
                local.add("the Deal Room checkout (%s)" % head)
            elif f in loaded and not (head in tree.installed or head in tree.skill_mods or head in BROWSER_MODULES):
                needs.add(head)
    if LOCAL_RX.search(info["code"]) and not KEY_FALLBACK_RX.search(info["code"]):
        local.add("a path on the owner's PC")

    # paid
    paid = set()
    for d in mods:
        paid |= _vendors(d, own=d is info)
    caution = set()
    for f in deeper:
        caution |= _vendors(tree.info(f), own=False)
    for d in mods:                       # a helper's function-scope SDK import: a caution, not a class
        if d is not info:
            caution |= {VENDOR_IMPORTS[k] for im in d["imports"] if im.lazy
                        for k in {im.name.split(".")[0], im.name.split(".")[-1]} if k in VENDOR_IMPORTS}
    caution -= paid
    if name in NOT_PAID:
        caution |= paid
        paid = set()
    siftmap_reads = sorted({m.group(0).strip(".(/") for m in SIFTMAP_READ_RX.finditer(info["code"])})

    write_flags, dry_flags = _write_flags(name, cli)
    own = _own_writes(info)
    if name in WRITE_MODE:
        mode = WRITE_MODE[name]
    elif write_flags:
        mode = "commit"
    elif dry_flags:
        mode = "default"
    elif own:
        mode = "always"
    else:
        mode = ""

    e: dict = {"summary": _summary(t), "cli": "argparse" if cli and not cli.get("argv") else ("argv" if cli else "none")}
    if via:
        e["cli_via"] = via
    e.update({k: v for k, v in cli.items() if k not in ("argv",)} if cli else {"args": [], "complete": True})
    e["writes"] = mode
    if write_flags:
        e["write_flags"] = write_flags
    if dry_flags:
        e["dry_flags"] = dry_flags
    if own and mode == "always":
        e["write_calls"] = own[:6]
    if paid:
        e["paid"] = sorted(paid)
        if name in FREE_DRY_RUN:
            e["free_dry_run"] = _ascii(FREE_DRY_RUN[name])
    if name in NOT_PAID:
        e["not_paid"] = _ascii(NOT_PAID[name])
    if caution:
        e["paid_deeper"] = sorted(caution)
    if siftmap_reads:
        e["siftmap_reads"] = siftmap_reads

    if name in RETIRED:
        why, instead = RETIRED[name]
        e.update({"class": "retired", "why": _ascii(why), "replacement": _ascii(instead)})
    elif name in wired:
        e.update({"class": "wired", "workflows": wired[name],
                  "why": "runs inside %s" % ", ".join("%s (%s)" % (w, owners.get(w, "")) for w in wired[name])})
    elif browser:
        e.update({"class": "browser", "browser": sorted(browser),
                  "why": "drives a browser (%s), and the brain has none" % ", ".join(sorted(browser))})
    elif local:
        e.update({"class": "local", "local": sorted(local),
                  "why": "needs %s, which only the owner's PC has" % "; ".join(sorted(local))})
    elif needs:
        e.update({"class": "package", "needs": sorted(needs),
                  "why": "imports %s, which the brain image does not install" % ", ".join(sorted(needs))})
    else:
        e["class"] = "runnable"
    return e


def _wired(roster) -> tuple[dict[str, list[str]], dict[str, str]]:
    """script -> the live roster workflows that list it in sources, and workflow -> owner's name."""
    wired: dict[str, list[str]] = {}
    owners: dict[str, str] = {}
    if roster is None:
        return wired, owners
    for wf in roster.workflows.values():
        if not wf.live or wf.name == WORKFLOW:
            continue
        emp = roster.employees.get(wf.owner)
        owners[wf.name] = emp.display_name if emp else wf.owner
        for s in wf.sources:
            if "." not in s:
                wired.setdefault(s, []).append(wf.name)
    return {k: sorted(set(v)) for k, v in wired.items()}, owners


def _roster():
    try:
        from . import team
        return team.load()
    except Exception as exc:  # noqa: BLE001  (a broken roster: nothing counts as wired, which stages more)
        log.warning("roster did not load for the command catalog: %s", exc)
        return None


def script_files(root: Path | None = None) -> list[Path]:
    src = Path(root or config.ROOT) / "src"
    out = []
    for p in sorted(src.glob("*.py")):
        if p.name.startswith("__"):
            continue
        try:
            t = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
            continue
        if _is_cli(t):
            out.append(p)
    return out


def build(root: Path | None = None, roster=None) -> dict:
    """The whole catalog from the source tree: deterministic, so the committed copy can be diffed."""
    root = Path(root or config.ROOT)
    tree = _Tree(root)
    wired, owners = _wired(roster if roster is not None else _roster())
    scripts = {p.stem: build_entry(tree, p, wired, owners) for p in script_files(root)}
    counts: dict[str, int] = {}
    for e in scripts.values():
        counts[e["class"]] = counts.get(e["class"], 0) + 1
    counts["writes"] = sum(1 for e in scripts.values() if e["class"] == "runnable" and e.get("writes"))
    counts["paid"] = sum(1 for e in scripts.values() if e["class"] == "runnable" and e.get("paid"))
    return {"about": GENERATED, "counts": dict(sorted(counts.items())), "scripts": scripts}


_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def render(cat: dict) -> str:
    """The committed catalog. An email address in a script's defaults or choices is masked: this file is
    public, and an address belongs in the script that needs it, not in a second copy."""
    return _EMAIL_RX.sub("<email>", json.dumps(cat, indent=1, sort_keys=True, ensure_ascii=True)) + "\n"


def catalog_path() -> Path:
    return Path(config.TEAM_DIR) / "commands.json"


def committed() -> dict:
    p = catalog_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stale(root: Path | None = None) -> list[str]:
    """Script names whose committed entry differs from the source tree (added, removed, changed)."""
    fresh = json.loads(render(build(root))).get("scripts") or {}    # compared as written, emails masked
    old = committed().get("scripts") or {}
    return sorted(n for n in set(fresh) | set(old) if fresh.get(n) != old.get(n))


def write(root: Path | None = None) -> bool:
    """Rewrite team/commands.json; True when it changed."""
    text = render(build(root))
    p = catalog_path()
    if p.is_file() and p.read_text(encoding="utf-8") == text:
        return False
    p.write_text(text, encoding="utf-8")
    return True


# ------------------------------------------------------------------------------------ the live view
_LIVE: dict = {}


def _stamp(root: Path) -> tuple:
    src = root / "src"
    newest = 0.0
    for p in src.rglob("*.py"):
        if "__pycache__" not in p.parts:
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
    team = Path(config.TEAM_DIR)
    for f in (team / "roster.yaml", team / "autopilot.yaml"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    return (str(root), newest)


def live_catalog() -> dict:
    """The catalog as the source tree says right now (rebuilt when any source file or the roster
    changes). What decides an ask; the committed file is for people to read."""
    root = script_root()
    key = _stamp(root)
    if _LIVE.get("key") != key:
        _LIVE["cat"] = build(root)
        _LIVE["key"] = key
    return _LIVE["cat"]


def reset() -> None:
    _LIVE.clear()


def normalize_name(script: str) -> str:
    s = (script or "").strip().replace("\\", "/")
    s = s.split()[-1] if s.split() and s.split()[0].lower() in ("python", "python3", "py") else s
    s = s.rsplit("/", 1)[-1]
    return s[:-3] if s.endswith(".py") else s


def entry(script: str) -> dict | None:
    return (live_catalog().get("scripts") or {}).get(normalize_name(script))


def close_matches(script: str, n: int = 4) -> list[str]:
    names = list((live_catalog().get("scripts") or {}))
    return difflib.get_close_matches(normalize_name(script), names, n=n, cutoff=0.5)


# ------------------------------------------------------------------------------------ one command line

def command_line(script: str, argv: list[str]) -> str:
    return "python src/%s.py%s" % (script, (" " + shlex.join(argv)) if argv else "")


@dataclass
class Decision:
    kind: str                     # refuse | run | stage
    script: str
    argv: list[str] = field(default_factory=list)
    say: str = ""                 # the refusal, or why it is staged
    reasons: list[str] = field(default_factory=list)
    quote: list[str] | None = None   # for a staged paid run: the free dry run that prices it
    paid: list[str] = field(default_factory=list)

    @property
    def line(self) -> str:
        return command_line(self.script, self.argv)


def split_args(args) -> tuple[list[str] | None, str]:
    """A list of tokens, or a string split like a terminal would (no shell ever runs it)."""
    if args is None or args == "":
        return [], ""
    if isinstance(args, str):
        try:
            toks = shlex.split(args, posix=True)
        except ValueError as exc:
            return None, "Those arguments do not split into words (%s). Send them as a list." % exc
        return toks, ""
    if isinstance(args, (list, tuple)):
        if not all(isinstance(a, (str, int, float)) and not isinstance(a, bool) for a in args):
            return None, "Each argument must be a word or a number."
        return [str(a) for a in args], ""
    return None, "Send the arguments as a list of words, for example [\"--phase\", \"build\"]."


def _options(entry_: dict, sub: str | None) -> dict[str, dict]:
    pool = list(entry_.get("args") or []) + list(entry_.get("shared") or [])
    if sub:
        pool += ((entry_.get("subcommands") or {}).get(sub) or {}).get("args") or []
    out: dict[str, dict] = {}
    for a in pool:
        if a.get("kind") == "positional":
            continue
        for f in a.get("flags") or []:
            out[f] = a
            if a.get("kind") == "bool" and f.startswith("--"):
                out["--no-" + f[2:]] = a
    out.setdefault("-h", {"flags": ["-h", "--help"], "kind": "switch"})
    out.setdefault("--help", {"flags": ["-h", "--help"], "kind": "switch"})
    return out


def _positionals(entry_: dict, sub: str | None) -> list[dict]:
    pool = list(entry_.get("args") or [])
    if sub:
        pool += ((entry_.get("subcommands") or {}).get(sub) or {}).get("args") or []
    return [a for a in pool if a.get("kind") == "positional"]


def _looks_like_path(flag: str, value: str, typ: str) -> bool:
    if typ in ("int", "float") or re.match(r"^-?\d*\.?\d+$", value):
        return False                           # a number, however it is written (.5, -83.91)
    if typ == "path" or value.startswith(("/", "~", ".")) or PATH_EXT.search(value):
        return True
    if "/" in value and any(any(c.isalpha() for c in seg) for seg in value.split("/")):
        return True
    return False


def _path_problem(flag: str, value: str, typ: str) -> str:
    """'' when the value is fine; else one sentence. A value that looks like a file must be under
    output/ (relative to the repo) or inside the brain's run folders."""
    if not _looks_like_path(flag, value, typ):
        return ""
    if value.startswith("https://"):
        host = value[8:].split("/", 1)[0].split(":", 1)[0].lower()
        if not host or host in ("localhost",) or re.match(r"^[\d.]+$", host) or \
                host.endswith((".internal", ".local", ".flycast", ".localhost")) or "." not in host:
            return "%s may not point at %s: only a public https address is allowed." % (flag or "A value", host or "that")
        return ""
    if value.startswith("http://"):
        return "%s must be an https address." % (flag or "A value")
    if value.startswith("~"):
        return "%s may not name a home folder; give a path under output/." % (flag or "A value")
    p = Path(value)
    if p.is_absolute() or re.match(r"^[A-Za-z]:", value):
        try:
            runs = Path(config.RUNS_DIR).resolve()
            if p.resolve().is_relative_to(runs):
                return ""
        except (OSError, ValueError):
            pass
        extra = os.environ.get("SIFTSTACK_OUTPUT_DIR") or ""
        if extra:
            try:
                if p.resolve().is_relative_to(Path(extra).resolve()):
                    return ""
            except (OSError, ValueError):
                pass
        return ("%s names %s, outside the folders a script may touch: give a path under output/ or a run "
                "folder on the box." % (flag or "A value", value))
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        return "%s may not climb out of its folder with '..'." % (flag or "A value")
    if parts[0] != "output" or len(parts) < 2 or not parts[-1]:
        return "%s %s must be a path under output/ (for example output/%s)." % (
            flag or "A value", value, parts[-1] or "report.csv")
    return ""


def _value_problem(flag: str, a: dict, value: str) -> str:
    choices = a.get("choices")
    typ = a.get("type") or "str"
    if typ == "int":
        try:
            int(value)
        except ValueError:
            return "%s takes a whole number, not %r." % (flag, value)
    elif typ == "float":
        try:
            float(value)
        except ValueError:
            return "%s takes a number, not %r." % (flag, value)
    if isinstance(choices, list) and choices:
        ok = [str(c) for c in choices]
        if typ in ("int", "float"):
            try:
                hit = any(float(value) == float(c) for c in choices)
            except ValueError:
                hit = False
        else:
            hit = value in ok
        if not hit:
            return "%s takes one of %s, not %r." % (flag, ", ".join(ok[:20]), value)
    return _path_problem(flag, value, typ)


def validate_args(entry_: dict, tokens: list[str]) -> tuple[str, list[str]]:
    """('', tokens) when every token is one this script declares, else (one sentence, [])."""
    if len(tokens) > TOKENS_MAX:
        return "That is %d arguments; the most one run takes is %d." % (len(tokens), TOKENS_MAX), []
    for t in tokens:
        if len(t) > TOKEN_MAX:
            return "One argument is %d characters long; the most is %d." % (len(t), TOKEN_MAX), []
        bad = sorted({c for c in t if c in SHELL_CHARS}, key=ord)
        if bad:
            shown = ", ".join(repr(c) for c in bad)
            return ("The argument %r carries %s. No shell runs these, so nothing can be chained or piped: "
                    "send plain words only." % (t[:60], shown)), []
    subs = entry_.get("subcommands") or {}
    sub: str | None = None
    opts = _options(entry_, None)
    pos = _positionals(entry_, None)
    pos_i, pos_used = 0, 0
    seen: set[str] = set()
    i = 0
    if entry_.get("cli") == "none" and tokens and tokens not in (["--help"], ["-h"]):
        return "%s reads no arguments, so it runs only as it is." % entry_.get("name", "This script"), []
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            return "The bare -- marker is not accepted; give each flag by name.", []
        is_flag = tok.startswith("-") and len(tok) > 1 and not NEG_NUMBER.match(tok)
        if is_flag:
            flag, eq, inline = tok.partition("=")
            a = opts.get(flag)
            if a is None:
                near = difflib.get_close_matches(flag, list(opts), n=1, cutoff=0.6)
                return "%s does not take %s.%s" % (entry_.get("name", "This script"), flag,
                                                     (" Did you mean %s?" % near[0]) if near else ""), []
            if a.get("kind") in ("switch", "bool"):
                if eq:
                    return "%s is a switch and takes no value." % flag, []
                seen.add(flag)
                i += 1
                continue
            nargs = a.get("nargs")
            vals: list[str] = []
            if eq:
                vals = [inline]
                i += 1
            else:
                i += 1
                want_min, want_max = 1, 1
                if nargs == "?":
                    want_min, want_max = 0, 1
                elif nargs == "*":
                    want_min, want_max = 0, TOKENS_MAX
                elif nargs == "+":
                    want_min, want_max = 1, TOKENS_MAX
                elif isinstance(nargs, int):
                    want_min = want_max = nargs
                while len(vals) < want_max and i < len(tokens):
                    nxt = tokens[i]
                    if nxt.startswith("-") and len(nxt) > 1 and not NEG_NUMBER.match(nxt):
                        break
                    vals.append(nxt)
                    i += 1
                if len(vals) < want_min:
                    return "%s needs a value." % flag, []
            for v in vals:
                prob = _value_problem(flag, a, v)
                if prob:
                    return prob, []
            seen.add(flag)
            continue
        if subs and sub is None:
            if tok not in subs:
                return "%s starts with one of %s, not %r." % (entry_.get("name", "This script"),
                                                                ", ".join(sorted(subs)), tok), []
            sub = tok
            opts = _options(entry_, sub)
            pos = _positionals(entry_, sub)
            pos_i, pos_used = 0, 0
            i += 1
            continue
        if pos_i >= len(pos):
            return "%s does not take a loose value like %r; every value goes after its flag." % (
                entry_.get("name", "This script"), tok), []
        a = pos[pos_i]
        prob = _value_problem(a["flags"][0], a, tok)
        if prob:
            return prob, []
        pos_used += 1
        if a.get("nargs") not in ("*", "+") or pos_used >= TOKENS_MAX:
            pos_i += 1
            pos_used = 0
        i += 1
    if subs and sub is None and entry_.get("subcommand_required") and tokens not in (["--help"], ["-h"]):
        return "%s needs one of %s first." % (entry_.get("name", "This script"), ", ".join(sorted(subs))), []
    if tokens not in (["--help"], ["-h"]):
        for a in list(entry_.get("args") or []) + list((subs.get(sub) or {}).get("args") or [] if sub else []):
            if a.get("required") and a.get("kind") != "positional" and not set(a.get("flags") or []) & seen:
                return "%s needs %s." % (entry_.get("name", "This script"), a["flags"][0]), []
    return "", list(tokens)


def _present(tokens: list[str], flags: list[str]) -> list[str]:
    names = {t.partition("=")[0] for t in tokens if t.startswith("--") or (t.startswith("-") and len(t) == 2)}
    return [f for f in flags if f in names]


def _where_else(e: dict, script: str, argv: list[str]) -> str:
    return "On the workstation: %s" % command_line(script, argv)


def decide(script: str, args, cat_entry: dict | None = None) -> Decision:
    """What one ask becomes: refused with a sentence, run now, or staged for a person to release."""
    name = normalize_name(script)
    e = cat_entry if cat_entry is not None else entry(name)
    toks, why = split_args(args)
    if e is None:
        near = close_matches(name)
        return Decision("refuse", name, say="There is no script named %s in src/.%s" % (
            name or "(empty)", (" Close: %s." % ", ".join(near)) if near else ""))
    if toks is None:
        return Decision("refuse", name, say=why)
    shown = [t if not set(t) & SHELL_CHARS else "<unsafe>" for t in toks][:TOKENS_MAX]
    cls = e.get("class")
    if cls == "retired":
        instead = e.get("replacement") or ""
        return Decision("refuse", name, say="%s is retired: %s.%s" % (
            name, e.get("why"), (" Use %s instead." % instead) if instead else " Nothing replaces it."))
    if cls == "wired":
        return Decision("refuse", name, say="%s already %s. Ask for that workflow instead, so its caps and "
                        "checks apply." % (name, e.get("why") or "runs inside a workflow"))
    if cls in ("browser", "local", "package"):
        return Decision("refuse", name, say="%s %s, so it cannot run here. %s" % (
            name, e.get("why"), _where_else(e, name, shown)))
    if cls != "runnable":
        return Decision("refuse", name, say="%s cannot run here: %s." % (name, e.get("why") or cls))
    e = dict(e, name=name)
    problem, argv = validate_args(e, toks)
    if problem:
        return Decision("refuse", name, say=problem)
    if argv in (["--help"], ["-h"]):
        return Decision("run", name, argv=argv)
    reasons: list[str] = []
    mode = e.get("writes") or ""
    wf = e.get("write_flags") or []
    on = _present(argv, wf)
    if mode == "commit" and on:
        reasons.append("it writes (%s)" % ", ".join(on))
    elif mode == "default" and not _present(argv, e.get("dry_flags") or []):
        reasons.append("it writes unless %s is given" % " or ".join(e.get("dry_flags") or ["its dry switch"]))
    elif mode == "always":
        reasons.append("it changes things on every run (%s)" % ", ".join(e.get("write_calls") or ["by its own code"]))
    elif on:
        reasons.append("it writes (%s)" % ", ".join(on))
    if e.get("siftmap_reads"):
        reasons.append("it reads SiftMap property by property (%s), which DataSift's acceptable use limits: "
                       "detail pulled in volume without adding the records counts as scraping"
                       % ", ".join(e["siftmap_reads"]))
    paid = list(e.get("paid") or [])
    if paid and e.get("free_dry_run") and mode == "commit" and not on and not reasons:
        return Decision("run", name, argv=argv, paid=paid)     # the vetted free dry run of a paid script
    if paid:
        reasons.append("it pays %s" % ", ".join(paid))
    if reasons:
        quote = None
        if paid and e.get("free_dry_run") and mode == "commit" and on:
            quote = [t for t in argv if t.partition("=")[0] not in set(wf)]
        return Decision("stage", name, argv=argv, say="; ".join(reasons), reasons=reasons, quote=quote, paid=paid)
    return Decision("run", name, argv=argv)


# ------------------------------------------------------------------------------------ what a person reads

def describe(name: str, e: dict) -> dict:
    """One script as an employee needs it to choose: what it is, where it runs, and its flags."""
    cls = e.get("class")
    how = {"runnable": "runs here when asked; a write or a paid run is staged for a person to release",
           "wired": "use the workflow named in why", "retired": "retired",
           "browser": "workstation only", "local": "workstation only", "package": "workstation only"}.get(cls, cls)
    out: dict = {"script": name, "class": cls, "summary": e.get("summary", ""), "how": how}
    for k in ("why", "replacement", "workflows", "writes", "write_flags", "dry_flags", "paid", "free_dry_run"):
        if e.get(k):
            out[k] = e[k]

    def opts(args: list[dict]) -> list[dict]:
        res = []
        for a in args or []:
            o = {"flag": " ".join(a.get("flags") or [])}
            for k in ("kind", "nargs", "choices", "type", "required", "default", "help"):
                if a.get(k) not in (None, "", []):
                    o[k] = a[k]
            res.append(o)
        return res
    out["options"] = opts(list(e.get("args") or []) + list(e.get("shared") or []))
    if e.get("subcommands"):
        out["subcommands"] = {n: opts(s.get("args") or []) for n, s in e["subcommands"].items()}
    if not e.get("complete", True):
        out["note"] = "Some of its options are built at run time and are not listed; only the listed ones are accepted."
    return out


def search(text: str, limit: int = 15) -> list[dict]:
    """Scripts whose name or summary carries every word asked, best first."""
    scripts = live_catalog().get("scripts") or {}
    words = [w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if w]
    rows = []
    for n, e in scripts.items():
        hay = (n + " " + (e.get("summary") or "")).lower()
        if all(w in hay for w in words):
            score = sum(3 for w in words if w in n) + (5 if n == "_".join(words) else 0)
            rows.append((-score, n))
    rows.sort()
    return [{"script": n, "class": scripts[n]["class"], "summary": scripts[n].get("summary", "")[:120]}
            for _, n in rows[:limit]]


# ------------------------------------------------------------------------------------ the tools' halves
CATALOG_HELP = ("Look up SiftStack's command-line scripts (src/<script>.py): what one does, its options and "
                "choices, and whether it runs here, is staged for a person, or runs only on the workstation. Give a "
                "script name for its options, a few words to search, or nothing for the list of scripts that run "
                "here. Free; nothing runs.")
RUN_HELP = ("Run one of SiftStack's command-line scripts on the brain, the way the owner runs it in Claude Code: "
            "python src/<script>.py <args>. Look it up with script_catalog first. A report, a dry run, an audit or a "
            "plan is queued and posts its output in this thread. A run that writes (--commit and the like) or pays a "
            "vendor is staged as a batch a named person releases. A script a workflow already runs, a retired one, or "
            "one that needs a browser or the owner's PC is refused with what to do instead. One script at a time: the "
            "brain has one worker, and a script holds it until it ends (up to %d minutes). Arguments are plain words "
            "with no shell, so nothing can be piped or chained, and a file must be a path under output/.")
RUN_SCHEMA = {"script": {"type": "string", "description": "The script's name, as in src/<name>.py (dream100, sold_guard)"},
              "args": {"type": "array", "items": {"type": "string"},
                       "description": "The arguments as separate words, for example [\"--phase\", \"build\"]; empty for none"},
              "details": {"type": "string", "description": "Why it is being run, in a few words, for the thread"}}
CATALOG_SCHEMA = {"query": {"type": "string", "description": "A script name, a few words to search, or empty for the list"}}


def run_help() -> str:
    return RUN_HELP % round(timeout_s() / 60)


def catalog_answer(args: dict) -> dict:
    """script_catalog: one script's options and how it would run, a search, or the scripts that run here."""
    q = str((args or {}).get("query") or "").strip()
    cat = live_catalog()
    scripts = cat.get("scripts") or {}
    name = normalize_name(q) if q else ""
    if name and name in scripts:
        return describe(name, scripts[name])
    if q:
        hits = search(q)
        return {"query": q, "matches": hits,
                "note": "Ask again with one script name for its options." if hits else
                "No script matches every word; try fewer words, or part of the file name."}
    here = [{"script": n, "summary": e.get("summary", "")[:90]} for n, e in sorted(scripts.items())
            if e.get("class") == "runnable"]
    return {"counts": cat.get("counts"), "runs_here": here,
            "note": "Every other script is wired into a workflow, retired, or runs only on the workstation; ask by name "
                    "for which and why."}


def _owner(roster) -> str:
    wf = getattr(roster, "workflows", {}).get(WORKFLOW) if roster is not None else None
    return wf.owner if wf else DEFAULT_OWNER


def queue_ask(task: dict, employee, roster, args: dict) -> tuple[str, bool, int]:
    """run_script: (what to tell the employee, is it a refusal, the task queued or 0). Runs nothing:
    a direct task is queued on the same thread and the worker decides again before it acts."""
    from . import store
    args = dict(args or {})
    script, raw = str(args.get("script") or ""), args.get("args")
    if not raw and len(script.split()) > 1:          # the whole command line sent as the script
        toks, why = split_args(script)
        if toks is None:
            return why, True, 0
        if toks and toks[0].lower() in ("python", "python3", "py"):
            toks = toks[1:]
        script, raw = (toks[0] if toks else ""), toks[1:]
    d = decide(script, raw)
    if d.kind == "refuse":
        return d.say, True, 0
    from . import workflows
    if workflows.handler(WORKFLOW) is None:
        # Without its line in workflows.REGISTRY the worker would read the task as an employee turn:
        # a paid model turn under Quinn instead of the script.
        return ("run_script is not wired into the worker yet (its line in agents/workflows REGISTRY is "
                "missing), so nothing was queued."), True, 0
    busy = store.open_task(WORKFLOW)
    if busy:
        t = store.task(busy) or {}
        return ("Task %d is %s (%s). One script at a time: the brain has one worker, and a script holds it until "
                "it ends (up to %d minutes). Ask again when that one posts." % (
                    busy, "running a script" if t.get("status") == "running" else "a script waiting to run",
                    t.get("subject") or "", round(timeout_s() / 60))), True, 0
    tid, _ = store.enqueue(
        _owner(roster), WORKFLOW, "", subject=d.line[:120],
        payload={"requested_by": getattr(employee, "display_name", ""), "script": d.script, "args": d.argv,
                 "action": d.kind, "details": str(args.get("details") or "")[:500]},
        thread_id=task.get("thread_id") or "", parent_id=int(task.get("id") or 0), hop=int(task.get("hop") or 0),
        source="agent:%s" % getattr(employee, "id", ""), mode="direct",
        slack_channel=task.get("slack_channel") or "", slack_thread=task.get("slack_thread") or "")
    if d.kind == "stage":
        return ("Queued task %d to stage `%s` as a batch, because %s. Nothing runs until a named releaser types "
                "release batch N, then confirm N; the batch number posts in this thread." % (tid, d.line, d.say)), False, tid
    return ("Queued `%s` as task %d. It runs on the brain now and posts its output in this thread when it ends "
            "(limit %d minutes); the team's other work waits while it runs." % (d.line, tid, round(timeout_s() / 60))), False, tid


# ------------------------------------------------------------------------------------ command line

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="The command catalog (team/commands.json).")
    ap.add_argument("--write", action="store_true", help="rebuild team/commands.json")
    ap.add_argument("--check", action="store_true", help="exit 1 when team/commands.json is stale")
    ap.add_argument("--show", default="", help="print one script's entry")
    a = ap.parse_args(argv)
    if a.show:
        e = entry(a.show)
        print(json.dumps(describe(normalize_name(a.show), e) if e else {"missing": a.show}, indent=1))
        return 0 if e else 1
    if a.write:
        changed = write()
        cat = committed()
        print("team/commands.json %s: %s" % ("written" if changed else "unchanged",
                                              ", ".join("%s %d" % kv for kv in cat.get("counts", {}).items())))
        return 0
    bad = stale()
    print("team/commands.json is %s%s" % ("stale for " if bad else "current", ", ".join(bad[:12])))
    return 1 if (a.check and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
