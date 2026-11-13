"""DataSift API audit: every route SiftStack calls, checked against the published Open API, rolled
up per team workflow, so we know which workflows can be handed to a user who connects with their
Open API key alone.

    python tools/datasift_api_audit.py                  # per-workflow readiness + the open route list
    python tools/datasift_api_audit.py --refresh        # re-download the published specs first
    python tools/datasift_api_audit.py --check          # exit 1 on a route neither the spec nor KNOWN covers
    python tools/datasift_api_audit.py --json out.json  # the full map (default: output/datasift_api/)

Sources of truth:
  * the OpenAPI bundles behind developers.datasift.ai (DataSift API 2.0 + SiftMap API 1.0), cached
    in output/datasift_api/specs/ (gitignored) and parsed as data only (yaml.safe_load);
  * team/roster.yaml + team/autopilot.yaml (the workflows) and docs/agents.json (the org chart),
    read through src/agents/team.py so this sees exactly what the team sees.

Static and read-only: no DataSift call is made, nothing is written outside output/. A workflow's
modules are its roster `sources`, the org-chart `source` files of the agents it covers, and every
src/ module those import (lazy imports included), so a finding is "this code path can reach it",
with the module that does named beside it.

The rules each grade comes from are on developers.datasift.ai: Authentication (Api-Key only, a
browser bearer token is not supported), Acceptable use (partner namespaces, image lookup,
autocomplete volume, detail pulls without adding = scraping, app-only operations), the 2026-11-01
changelog (webhook management and duplicate mirror routes taken out of the reference).
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CACHE = ROOT / "output" / "datasift_api"
SPECS = {"datasift": "https://developers.datasift.ai/_bundle/apis/datasift.yaml",
         "siftmap": "https://developers.datasift.ai/_bundle/apis/siftmap.yaml"}

# Undocumented routes SiftStack calls today, each reviewed: what the published API offers instead,
# or why there is no substitute. `fix` is the documented route; None means none exists.
# kind: replace (a documented route does the same job) | app (an app-only operation per Acceptable
# use) | absent (not in the API at all; ask DataSift or drop) | staff (our accounts only, never a user's)
KNOWN = {
    "/api/internal/property/{x}/message/": ("replace", "POST /api/internal/owner/{owner_uuid}/message/ (the board is per owner; the property mirror was unlisted 2026-11-01)"),
    "/api/internal/property/{x}/message/{x}/": ("replace", "/api/internal/owner/{owner_uuid}/message/{uuid}/"),
    "/api/internal/property/{x}/message/{x}/pin/": ("replace", "POST /api/internal/owner/{owner_uuid}/message/{uuid}/pin/"),
    # Measured 2026-11-13 on the key: global-status answers 7 rows, one per lead pipeline heading, naming
    # 8 of the account's 30 statuses. It is a spelling check, not a list of every status.
    "/api/internal/status/": ("absent", "no published route lists every status: GET /api/internal/global-status/ names only the lead pipeline's (8 of 30 on ty+2); keep a table of the stored spellings and check it against global-status"),
    "/api/internal/account/user/": ("absent", "listing the account's users is not in the API (GET /api/internal/user/ is the key's own user only); name people by uuid"),
    "/api/internal/account/users/": ("absent", "listing the account's users is not in the API; name people by uuid"),
    "/api/internal/users/": ("absent", "listing the account's users is not in the API; name people by uuid"),
    "/api/internal/property/{x}/tags/": ("replace", "the record's tags are on GET /api/internal/property/{uuid}/"),
    "/api/internal/property/{x}/lists/": ("replace", "the record's lists are on GET /api/internal/property/{uuid}/"),
    "/api/internal/board/": ("replace", "GET /api/internal/siftline/board/"),
    "/api/internal/boards/": ("replace", "GET /api/internal/siftline/board/"),
    "/api/internal/property/add-tags/": ("replace", "POST /api/internal/property/{uuid}/add-tags/ per record (tagging by query is not in the API)"),
    "/api/internal/property/remove-tags/": ("replace", "POST /api/internal/property/{uuid}/remove-tags/ per record"),
    "/api/internal/siftline/property/{x}/card/": ("replace", "POST /api/internal/siftline/board/column/{column_uuid}/card/"),
    "/api/internal/siftline/board/{x}/column/": ("absent", "columns are not listed in the API; the board detail is GET /api/internal/siftline/board/{uuid}/"),
    "/properties/search-autocomplete/": ("replace", "POST /properties/search/ (Acceptable use: no volume through autocomplete)"),
    "/api/internal/sequence/": ("absent", "sequences are not in the API: build them in Sequence Builder"),
    "/api/internal/sequence/{x}/": ("absent", "sequences are not in the API: build them in Sequence Builder"),
    "/api/internal/sequence-folder/": ("absent", "sequences are not in the API: build them in Sequence Builder"),
    "/api/internal/integration/webhook/": ("app", "creating or changing a webhook is app-only (Settings > Integrations > Webhooks)"),
    "/api/internal/integration/webhook/{x}/": ("app", "creating or changing a webhook is app-only"),
    "/api/internal/integration/webhook/{x}/test/": ("app", "the webhook's Test button; a person selects Test in the app"),
    "/api/internal/property/direct-mail-campaign/": ("absent", "SiftMail campaigns are not in the API"),
    "/api/internal/accounting/direct-mail-pricing/": ("absent", "SiftMail pricing is not in the API"),
    "/api/internal/property/{x}/directmail-attempts/": ("absent", "no direct-mail attempt counter in the API (sms, rvm, predictivecall and skiptrace have one)"),
    "/api/internal/upload/usage/": ("absent", "record allowance usage is not in the API"),
    "/api/internal/activity/skiptrace/stats/": ("absent", "skip trace stats are not in the API (GET /api/internal/activity/ lists the jobs)"),
    "/api/internal/impersonate/{x}/": ("staff", "staff impersonation: a user connects with their own key instead"),
    "/api/token/": ("staff", "password token mint: the documented auth is the Open API key only"),
    "mail.reisift.io": ("absent", "SiftMail (designs, campaigns) is not in the API"),
}

# Per-module markers of HOW a module reaches DataSift. Declarations (config.py) are not usage, and
# Playwright alone is not the DataSift app (it also drives the notice site, Ancestry, Market Finder
# is the exception and names app.reisift.io), so "browser" needs Playwright AND the app in one file.
AUTH_RX = {
    "api_key": re.compile(r"Api-Key|REISIFT_API_KEY"),
    "jwt": re.compile(r"/api/token/|datasift-admin|REISIFT_TOKEN"),      # a minted or pasted token
    "password": re.compile(r"DATASIFT_PASSWORD"),                          # a password path still exists
    "impersonation": re.compile(r"/impersonate/"),
    # reisift_auth is the Deal Room auth store (it can hold the key account, but it lives there)
    "deal_room": re.compile(r"Deal Room Coaching Call|_api[\\/]clients|from crm_api|import crm_api|CRMClient\(|reisift_auth"),
    "desktop_path": re.compile(r"C:\\{1,2}Users\\{1,2}|C:/Users/|OneDrive\\{1,2}Desktop|OneDrive/Desktop"),
}
PLAYWRIGHT = re.compile(r"sync_playwright|async_playwright|from playwright|import playwright")
DATASIFT_APP = re.compile(r"app\.reisift\.io|datasift_core")
KEY_FALLBACK = re.compile(r"crm_key_client|crm_standalone|siftmap_standalone")

# Routes a module calls only behind an explicit opt-in that is off by default: listed, not graded.
GATED = {
    "src/agents/autopilot/crm.py": {
        "/api/internal/property/direct-mail-campaign/": "SiftMail, behind the playbook's internal_routes: true",
        "/api/internal/accounting/direct-mail-pricing/": "SiftMail, behind internal_routes: true",
        "mail.reisift.io": "SiftMail designs, behind internal_routes: true",
        "/api/internal/property/{x}/directmail-attempts/": "behind undocumented_counter: true",
        "/api/internal/activity/skiptrace/stats/": "a cost estimate only; the stage works without it",
        "/api/internal/account/user/": "only when a caller is named without a uuid",
    },
    "src/datasift_api_upload.py": {
        "/api/token/": "password mint, used only when DATASIFT_EMAIL and DATASIFT_PASSWORD are set; the key otherwise",
    },
}
RISK_RX = {
    "partner_namespace": re.compile(r"/api/internal/(?:calltools|launch-control|reirail|smarter-contact|smrtphone|smrtdialer|xencall|kixie|aircall|twilio|plivo|dataflik)/"),
    "image_lookup": re.compile(r"/properties/images/"),
    "siftmap_autocomplete": re.compile(r"search-autocomplete|\.autocomplete\("),
    "siftmap_detail": re.compile(r"/properties/detail/|\.get_detail\("),
}
CORE_DQ = re.compile(r'"(/api/(?:internal|v1|token)[^"\n]*)"')
CORE_SQ = re.compile(r"'(/api/(?:internal|v1|token)[^'\n]*)'")
MAP_DQ = re.compile(r'"(/(?:properties|filters)/[^"\n]*)"')
MAP_SQ = re.compile(r"'(/(?:properties|filters)/[^'\n]*)'")


# Verdicts a person checked against the code, where the static scan under-reads a dependency
# (usually the main write path living behind a function-scope import). Each says what to change.
REVIEWED = {
    "list_pull": ("needs work", "the pull itself (workflows/list_pull, pull_by_ids) runs on published routes and the key; "
                  "agency_call_list, knox_blount_top15k and franklin_top30k load the uploader, siftmap_pull's allowance "
                  "read and SiftMap autocomplete at module scope"),
    "account_setup": ("needs work", "the install path (workflows/account_setup) runs on published routes and the key; "
                      "the staff export and pasted-token apply (account_blueprint), datasift_schema_setup and the browser "
                      "preset manager keep the covered agents off the key"),
    "courthouse_import": ("needs work", "uploads through the browser wizard with the password "
                          "(dropbox_watcher -> datasift_uploader); switch to datasift_api_upload.upload_csv as ftm_runner does"),
}

# ------------------------------------------------------------------------------------- specs

def fetch_specs(refresh: bool) -> dict[str, Path]:
    d = CACHE / "specs"
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, url in SPECS.items():
        p = d / ("%s.yaml" % name)
        if refresh or not p.is_file():
            req = urllib.request.Request(url, headers={"User-Agent": "siftstack-api-audit", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=60) as r:
                p.write_bytes(r.read())
        out[name] = p
    return out


def spec_ops(paths: dict[str, Path]) -> list[dict]:
    import yaml
    ops = []
    for api, p in paths.items():
        spec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for path, item in (spec.get("paths") or {}).items():
            for m, o in (item or {}).items():
                if m in ("get", "post", "put", "patch", "delete"):
                    o = o or {}
                    ops.append({"api": api, "method": m.upper(), "path": path, "tag": (o.get("tags") or ["-"])[0],
                                "deprecated": bool(o.get("deprecated"))})
    return ops


def norm(route: str) -> str:
    r = route.split("?")[0]
    r = re.sub(r"%\([a-z_]+\)[sd]|%[sd]", "{x}", r)
    r = re.sub(r"\{[^{}]*\}", "{x}", r)
    r = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{x}", r)
    r = re.sub(r"/+", "/", r)
    if "{" in r and "{x}" not in r:          # an f-string field cut by a quote inside it
        r = r[:r.index("{")] + "{x}/"
    if not r.endswith("/"):
        r += "/"
    return r


def match(route: str, ops: list[dict]) -> list[dict]:
    """Segment by segment; a spec parameter matches only an id in our route, never a literal word
    (otherwise /property/{uuid}/ would swallow /property/direct-mail-campaign/)."""
    ours = [s for s in route.strip("/").split("/") if s]
    hits = []
    for o in ops:
        theirs = [s for s in o["path"].strip("/").split("/") if s]
        if len(theirs) == len(ours) and all((b.startswith("{") and a == "{x}") or a == b
                                            for a, b in zip(ours, theirs)):
            hits.append(o)
    return hits


# ------------------------------------------------------------------------------------- scan

def scan_files() -> list[Path]:
    me = Path(__file__).resolve()
    files = []
    for pat in ("src/**/*.py", "skills/**/*.py", "deploy/*.py", "tools/*.py"):
        for p in ROOT.glob(pat):
            if "__pycache__" in p.parts or "archive" in p.parts or p.resolve() == me:
                continue
            # A selftest module's routes are fakes it answers itself; both selftests refuse the
            # network outright, so none of them is a dependency on DataSift.
            if p.name.startswith("selftest"):
                continue
            files.append(p)
    return sorted(set(files))


def code_text(t: str) -> str:
    """The source with docstrings and comments blanked: a route or a Deal Room path named in
    prose is documentation, not a dependency. Line structure is kept."""
    import io
    import tokenize
    lines = t.splitlines(keepends=True)
    try:
        tree = ast.parse(t)
    except SyntaxError:
        return t
    spans = []
    for node in [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            spans.append((body[0].lineno, body[0].end_lineno))
    for a, b in spans:
        for i in range(a - 1, min(b, len(lines))):
            lines[i] = "\n"
    try:
        for tok in tokenize.generate_tokens(io.StringIO(t).readline):
            if tok.type == tokenize.COMMENT:
                r, c = tok.start
                if lines[r - 1] != "\n":
                    lines[r - 1] = lines[r - 1][:c] + "\n"
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "".join(lines)


def scan_module(p: Path) -> dict:
    try:
        t = code_text(p.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, OSError):
        return {"routes": [], "gated": {}, "auth": [], "risks": []}
    routes = set()
    for rx in (CORE_DQ, CORE_SQ):
        routes.update(norm(m.group(1)) for m in rx.finditer(t))
    if "map.reisift.io" in t or "siftmap" in p.name.lower() or "SiftMap" in t:
        for rx in (MAP_DQ, MAP_SQ):
            routes.update(norm(m.group(1)) for m in rx.finditer(t))
    if "mail.reisift.io" in t:
        routes.add("mail.reisift.io")
    routes = {r for r in routes if r.startswith("/") and " " not in r and len(r) > 4
              and r not in ("/api/internal/", "/api/v1/")} | ({"mail.reisift.io"} & routes)
    auth = set() if p.name == "config.py" else {k for k, rx in AUTH_RX.items() if rx.search(t)}
    if PLAYWRIGHT.search(t) and DATASIFT_APP.search(t):
        auth.add("browser")
    if "deal_room" in auth and KEY_FALLBACK.search(t):
        auth.discard("deal_room")
        auth.add("deal_room_optional")          # prefers the Deal Room client, falls back to the key
    if "jwt" in auth and "api_key" in auth:
        auth.discard("jwt")
        auth.add("jwt_fallback")                # the key is the default, a password path remains
    rel = p.relative_to(ROOT).as_posix()
    gated = {r: why for r, why in GATED.get(rel, {}).items() if r in routes}
    if rel in GATED and "/api/token/" in gated:
        auth.discard("jwt_fallback")
    return {"routes": sorted(set(routes) - set(gated)), "gated": gated, "auth": sorted(auth),
            "risks": [k for k, rx in RISK_RX.items() if rx.search(t)]}


def module_file(name: str) -> Path | None:
    """src/<a>/<b>.py or src/<a>/<b>/__init__.py for a dotted module name, else None."""
    base = SRC.joinpath(*name.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def imports_of(p: Path) -> tuple[set[Path], set[Path]]:
    """(module-scope imports, imports inside a function) of src/ modules. A function-scope import
    is how this codebase pulls an optional helper, so the two are graded differently."""
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set(), set()
    try:
        pkg = list(p.resolve().relative_to(SRC.resolve()).parent.parts)
    except ValueError:
        pkg = []
    top, lazy = set(), set()

    def visit(node, in_func: bool) -> None:
        for n in ast.iter_child_nodes(node):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = pkg[: len(pkg) - (n.level - 1)] if n.level else []
                mod = ".".join(base + ([n.module] if n.module else []))
                names = [mod] + ["%s.%s" % (mod, a.name) if mod else a.name for a in n.names]
            for name in names:
                f = module_file(name) if name else None
                if f:
                    (lazy if in_func else top).add(f.resolve())
            visit(n, in_func or isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

    visit(tree, False)
    return top, lazy - top


def closure(entries: set[Path]) -> dict[Path, bool]:
    """module -> True when it loads with the entry modules (module-scope import chain), False when
    it is reached only through a function-scope import somewhere on the way."""
    state: dict[Path, bool] = {}
    todo = [(e.resolve(), True) for e in entries]
    while todo:
        f, strict = todo.pop()
        if state.get(f) is True or (f in state and not strict):
            continue
        state[f] = strict or state.get(f, False)
        if SRC.resolve() in f.parents:
            top, lazy = imports_of(f)
            todo.extend((g, strict) for g in top)
            todo.extend((g, False) for g in lazy)
    return state


def entries_for(wf, agents: dict) -> set[Path]:
    out = set()
    for s in wf.sources:
        f = module_file(s)
        if f:
            out.add(f)
        elif (SRC / s).is_dir():
            out.update((SRC / s).glob("*.py"))
        for d in (ROOT / "skills" / s, ROOT / "skills" / ("%s-monitor" % s)):
            if d.is_dir():
                out.update(d.rglob("*.py"))
    if "agents.autopilot" in wf.sources:
        out.update((SRC / "agents" / "autopilot").glob("*.py"))
    # An autopilot stage runs its own engine (src/agents/autopilot), not the covered agent's
    # module: ftm_retrace calls Tracerfy through autopilot/sources.py, never tracerfy_ftm.py.
    for aid in ([] if getattr(wf, "autopilot", False) else wf.covers):
        for part in (agents.get(aid, {}).get("source") or "").split(","):
            part = part.strip()
            if not part or part.endswith(".md"):
                continue
            q = ROOT / part
            if q.is_file() and q.suffix == ".py":
                out.add(q)
            elif q.is_dir():
                out.update(q.rglob("*.py"))
    return {p for p in out if "__pycache__" not in p.parts}


# ------------------------------------------------------------------------------------- grade

def grade(found: dict) -> tuple[str, list[str]]:
    """found: {"documented", "replace", "app", "absent", "staff", "unreviewed", "auth", "risks"}."""
    auth, risks = set(found["auth"]), set(found["risks"])
    hard = []
    if found["app"]:
        hard.append("app-only operations by API (%s)" % ", ".join(found["app"]))
    if found["absent"]:
        hard.append("routes the API does not have (%s)" % ", ".join(found["absent"]))
    if found["staff"] or "impersonation" in auth or "jwt" in auth:
        hard.append("a pasted browser token, password mint or impersonation instead of the user's key")
    if "browser" in auth:
        hard.append("drives the DataSift web app in a browser")
    if "deal_room" in auth:
        hard.append("needs the Deal Room checkout on Ty's machine")
    if "desktop_path" in auth:
        hard.append("hardcodes a path on Ty's machine")
    if "partner_namespace" in risks or "image_lookup" in risks:
        hard.append("calls an endpoint Acceptable use reserves for the app or partners")
    if found["unreviewed"]:
        hard.append("unreviewed undocumented routes (%s)" % ", ".join(found["unreviewed"]))
    soft = []
    if found["replace"]:
        soft.append("switch %d route(s) to the documented equivalent" % len(found["replace"]))
    if "siftmap_autocomplete" in risks:
        soft.append("replace SiftMap autocomplete with POST /properties/search/")
    if "jwt_fallback" in auth or "password" in auth:
        soft.append("remove the password path (the key already works)")
    if "deal_room_optional" in auth:
        soft.append("drop the Deal Room client preference (the key fallback already works)")
    touches = any(found[k] for k in ("documented", "replace", "app", "absent", "staff", "unreviewed", "gated")) \
        or auth & {"api_key", "jwt", "jwt_fallback", "password", "browser", "deal_room", "deal_room_optional"}
    if not touches:
        return "no DataSift API", []
    if hard:
        return "needs work", hard + soft
    if soft:
        return "switch routes", soft
    return "ready on the key", []


def run(refresh: bool) -> dict:
    sys.path.insert(0, str(SRC))
    from agents import team                     # noqa: E402  (the roster exactly as the team loads it)
    ops = spec_ops(fetch_specs(refresh))
    agents = team.org_chart_agents()
    roster = team.load()

    files = scan_files()
    mods = {}
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        mods[rel] = scan_module(f)

    routes = {}
    for rel, m in mods.items():
        for r in m["routes"]:
            routes.setdefault(r, {"modules": []})["modules"].append(rel)
    for r, e in routes.items():
        hits = [] if r == "mail.reisift.io" else match(r, ops)
        e["spec"] = sorted({"%s %s" % (o["method"], o["path"]) for o in hits})
        e["deprecated"] = any(o["deprecated"] for o in hits)
        if hits:
            e["kind"], e["note"] = "documented", ""
        elif r in KNOWN:
            e["kind"], e["note"] = KNOWN[r]
        elif r.rstrip("/").endswith("{x}") and r.count("/") <= 4:
            e["kind"], e["note"] = "dynamic", "a route built at run time; read the module"
        else:
            e["kind"], e["note"] = "unreviewed", "not in the spec and not reviewed in KNOWN"

    workflows = {}
    for wf in roster.workflows.values():
        reach = closure(entries_for(wf, agents))
        found = {k: set() for k in ("documented", "replace", "app", "absent", "staff", "unreviewed", "dynamic",
                                    "gated")}
        found["auth"], found["risks"] = set(), set()
        via, lazy = {}, {}
        for f, strict in reach.items():
            try:
                rel = f.relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                continue
            m = mods.get(rel)
            if not m:
                continue
            if not strict:      # reached only through a function-scope import: a caution, not a grade
                for r in m["routes"]:
                    if routes[r]["kind"] != "documented":
                        lazy.setdefault(r, set()).add(rel)
                for a in m["auth"]:
                    if a != "api_key":
                        lazy.setdefault("auth:" + a, set()).add(rel)
                for k in m["risks"]:
                    lazy.setdefault("risk:" + k, set()).add(rel)
                continue
            for r in m["routes"]:
                found[routes[r]["kind"]].add(r)
                via.setdefault(r, set()).add(rel)
            for r in m["gated"]:
                found["gated"].add(r)
                via.setdefault(r, set()).add(rel)
            for a in m["auth"]:
                found["auth"].add(a)
                via.setdefault("auth:" + a, set()).add(rel)
            for k in m["risks"]:
                found["risks"].add(k)
                via.setdefault("risk:" + k, set()).add(rel)
        found_l = {k: sorted(v) for k, v in found.items()}
        verdict, why = grade(found_l)
        if wf.name in REVIEWED:
            verdict, note = REVIEWED[wf.name]
            why = ["reviewed: " + note] + why
        if lazy:
            why = why + ["caution: %d more finding(s) behind function-scope imports (%s)" % (
                len(lazy), ", ".join(sorted({Path(m).stem for v in lazy.values() for m in v}))[:120])]
        workflows[wf.name] = {"owner": wf.owner, "status": "autopilot" if wf.autopilot else wf.status,
                              "covers": wf.covers, "modules": len(reach), "verdict": verdict, "why": why,
                              "found": found_l, "lazy": {k: sorted(v)[:6] for k, v in lazy.items()},
                              "caution_detail": "siftmap_detail" in found["risks"] or "risk:siftmap_detail" in lazy,
                              "via": {k: sorted(v)[:6] for k, v in via.items()}}
    return {"generated": date.today().isoformat(), "spec_ops": len(ops), "files": len(files),
            "modules_touching": sum(1 for m in mods.values() if m["routes"] or m["auth"] or m["risks"]),
            "routes": routes, "workflows": workflows,
            "employees": {e.id: e.display_name for e in roster.employees.values()}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="re-download the published specs")
    ap.add_argument("--check", action="store_true", help="exit 1 when an undocumented route is not reviewed")
    ap.add_argument("--json", default="", help="write the full map here")
    a = ap.parse_args(argv)
    res = run(a.refresh)
    out = Path(a.json) if a.json else CACHE / ("audit_%s.json" % res["generated"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=sorted), encoding="utf-8")

    kinds = {}
    for e in res["routes"].values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("%d spec operations | %d files, %d touch DataSift | %d distinct routes: %s" % (
        res["spec_ops"], res["files"], res["modules_touching"], len(res["routes"]),
        ", ".join("%s %d" % kv for kv in sorted(kinds.items()))))
    tally = {}
    for name, w in sorted(res["workflows"].items(), key=lambda kv: (kv[1]["owner"], kv[0])):
        tally[w["verdict"]] = tally.get(w["verdict"], 0) + 1
        print("  %-8s %-20s %-17s %s%s" % (res["employees"].get(w["owner"], w["owner"]), name, w["verdict"],
                                          "; ".join(w["why"])[:150], " [detail pulls]" if w["caution_detail"] else ""))
    print("workflows: " + ", ".join("%s %d" % kv for kv in sorted(tally.items())))
    bad = sorted(r for r, e in res["routes"].items() if e["kind"] == "unreviewed")
    for r in bad:
        print("UNREVIEWED %s  (%s)" % (r, ", ".join(Path(m).stem for m in res["routes"][r]["modules"][:5])))
    dep = sorted(r for r, e in res["routes"].items() if e.get("deprecated"))
    for r in dep:
        print("DEPRECATED %s  (%s)" % (r, ", ".join(Path(m).stem for m in res["routes"][r]["modules"][:5])))
    print("full map: %s" % out)
    return 1 if (a.check and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
