"""account_setup: the account blueprint planned, checked or applied on an install's own Open API key.

Atlas's workflow (team/roster.yaml), build plan 7.5 (the handoff to a user's install). Two inputs: the
phase (plan, verify or apply) and the email of the user whose key the box runs on.

What an install can do on its own key, and why the rest stays a staff step:
  * the identity check runs on GET /api/internal/user/ (the key's own user): the email must be the
    one named, and the account must not be the blueprint's source, so ty+2's own box can check
    ty+2 against its blueprint but can never clone ty+2 onto itself;
  * lists, tags, custom fields (groups and options), task presets (and their groups) and saved presets
    (and their folders) are read and created on published routes, by the blueprint package's OWN
    family functions with their read-backs, so a key install and the staff command line create
    exactly the same objects;
  * statuses, sequences, board columns and the account's user list have no published route (statuses
    are listed only in part, by global-status). Those come back as named items to set up by hand in
    the DataSift app, never as silent gaps;
  * SiftMap feeders are left out by default: the blueprint's carry the source market's counties, and
    in another account they would be clutter at best. ACCOUNT_SETUP_FAMILIES adds them back (auto-add
    is always forced off, because it spends the target's record allowance the moment it exists).
Staff-only, on the command line (src/clone_account.py): exporting a blueprint (it reads statuses,
sequences and columns through unpublished routes), and any apply on a pasted token, a password or
impersonation.

apply writes only when the brain is live AND ACCOUNT_SETUP_ALLOW_APPLY=1 is set on the box: creating a
hundred presets in an account is not something a chat message alone should trigger. Without both it
runs as plan and says why. The record-count probes the command line runs are skipped here (they need
the allowance route, which is unpublished).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CRM = None      # the selftest injects a fake DataSift; otherwise the budgeted Open API key client
MAP = None      # the selftest injects a fake SiftMap transport: post(path, method, body)
NAME = "account_setup"
PHASES = ("plan", "verify", "apply")
KEY_FAMILIES = ("lists", "tags", "custom_fields", "task_presets", "presets", "siftmap")
DEFAULT_FAMILIES = "lists,tags,custom_fields,task_presets,presets"


def _crm():
    if CRM is not None:
        return CRM
    from ..autopilot.crm import CRM as Client  # noqa: PLC0415
    return Client()


def _map_transport():
    if MAP is not None:
        return MAP

    class _Budgeted:
        def __init__(self):
            from siftmap_standalone import SiftMapClient  # noqa: PLC0415
            self.c = SiftMapClient((os.environ.get("REISIFT_API_KEY") or "").strip(), min_interval=0.5)

        def post(self, path, method="GET", body=None):
            from ..autopilot import budget  # noqa: PLC0415
            budget.acquire()
            return self.c._request(path, method=method, body=body)
    return _Budgeted()


class KeyClient:
    """The blueprint package's Client interface over the brain's budgeted Open API key client, so its
    family functions run unchanged. Errors come back as the package's own ApiError / NetworkError,
    because its create-and-read-back logic decides on those (a 400 "unique" adopts the existing title,
    a network error re-lists before it would ever retry a create)."""

    kind = "api_key"

    def __init__(self, crm, siftmap=None):
        self.crm, self.siftmap, self.n_calls = crm, siftmap, 0

    def claims(self) -> dict:
        return {}

    def call(self, path: str, method: str = "GET", body=None, *, base: str | None = None,
             method_override: str | None = None, timeout: int = 90, **_):
        from account_blueprint.client import ApiError, NetworkError  # noqa: PLC0415
        if path.startswith("http"):
            parts = urlsplit(path)
            base, path = "%s://%s" % (parts.scheme, parts.netloc), parts.path + ("?" + parts.query if parts.query else "")
        self.n_calls += 1
        try:
            if base and "map.reisift.io" in base:
                if self.siftmap is None:
                    self.siftmap = _map_transport()
                return self.siftmap.post(path, method, body)
            headers = {"x-http-method-override": method_override} if method_override else None
            return self.crm.call(path, method, body, headers=headers)
        except (ApiError, NetworkError):
            raise
        except OSError as exc:
            raise NetworkError("%s %s: %s" % (method, path, exc)) from exc
        except Exception as exc:  # noqa: BLE001  (the transports raise their own error types)
            code = getattr(exc, "code", None)
            if code is None:
                text = str(exc)
                digits = text.split("HTTP ", 1)[1][:3] if "HTTP " in text else ""
                code = int(digits) if digits.isdigit() else None
            if code is None:
                raise
            raise ApiError(code, method, path, getattr(exc, "body", "") or str(exc)) from exc

    def get_all(self, path: str, *, base: str | None = None) -> list:
        """Every row of a listing, following `next` (SiftMap pages at 10 and ignores ?limit=)."""
        from account_blueprint.client import ApiError  # noqa: PLC0415
        out, url, guard = [], path, 0
        while url and guard < 500:
            guard += 1
            r = self.call(url, base=base)
            if isinstance(r, list):
                return out + r
            if not isinstance(r, dict):
                raise ApiError(0, "GET", path, "unexpected listing shape: %r" % (r,)[:120])
            rows = r.get("results") if r.get("results") is not None else r.get("data")
            if rows is None:
                raise ApiError(0, "GET", path, "listing has neither results nor data: %s" % sorted(r)[:10])
            out.extend(rows)
            nxt = r.get("next")
            url = nxt if isinstance(nxt, str) and nxt else None
        return out

    def search_count(self, must: dict) -> int:
        r = self.call("/api/internal/property/", "POST", {"limit": 1, "offset": 0, "query": {"must": must}},
                      method_override="GET")
        return int(r.get("count") or 0) if isinstance(r, dict) else 0


def key_registry(client, log, *, commit: bool, allowed: set, ident: dict, user_map: dict):
    """The blueprint Registry, loaded from published routes only. What it cannot see it leaves empty
    and says so: statuses come from global-status (the lead pipeline's, not every status), boards by
    title only (columns have no route), no sequence folders (sequences are not in the API), and the
    only user it knows is the key's own (there is no route that lists an account's users), plus any
    the setting maps by uuid."""
    from account_blueprint.translate import Registry  # noqa: PLC0415

    class KeyRegistry(Registry):
        def load(self) -> None:
            c = self.c
            self.lists = {x["title"]: x["uuid"] for x in c.get_all("/api/internal/list/?limit=999")}
            self.tags = {x["title"]: x["uuid"] for x in c.get_all("/api/internal/tag/?offset=0&limit=10000&ordering=title")}
            self.load_statuses()
            self.load_boards()
            first = str(ident.get("first_name") or "").strip()
            if first and ident.get("user_id"):
                self.users[first.lower()] = ident["user_id"]
                self.user_names[ident["user_id"]] = ("%s %s" % (first, ident.get("last_name") or "")).strip()
            self.load_task_presets()
            self.sequence_folders = {}
            self.load_custom_fields()

        def load_statuses(self) -> None:
            rows = []
            for g in c_rows(self.c.get_all("/api/internal/global-status/")):
                rows.append(g)
                rows += [{"title": s} if isinstance(s, str) else s for s in g.get("statuses") or []
                         if isinstance(s, (str, dict))]
            rows = [x for x in rows if x.get("title")]
            self.statuses = {x["title"]: x.get("uuid") or x["title"] for x in rows}
            self.status_titles = {x["title"].casefold(): x["title"] for x in rows}
            self.status_rows = {x["title"]: x for x in rows}

        def load_boards(self) -> None:
            self.boards = {(b.get("title") or b.get("name")): b["uuid"]
                           for b in self.c.get_all("/api/internal/siftline/board/?offset=0&limit=999")}
            self.columns = {}

    def c_rows(r):
        return r if isinstance(r, list) else []

    reg = KeyRegistry(client, log, commit=commit, allowed_create=allowed, self_user=ident.get("user_id") or "",
                      account=ident.get("account") or "", user_map=user_map)
    reg.load()
    return reg


class NotThisAccount(RuntimeError):
    """The key is not the account the person named, or it is the blueprint's own source."""


def key_identity(client, bp: dict, email: str, *, writing: bool) -> dict:
    me = client.call("/api/internal/user/") or {}
    own = str(me.get("email") or "").strip().lower()
    account = str(me.get("account") or "").strip().lower()
    if not own or not account:
        raise NotThisAccount("the key's own user came back without an email or an account")
    if own != str(email or "").strip().lower():
        raise NotThisAccount("this box's key belongs to %s, not %s" % (own, str(email or "").strip() or "the email given"))
    src = str((bp.get("source") or {}).get("account_uuid") or "").strip().lower()
    if writing and account == src:
        raise NotThisAccount("this key is the blueprint's own source account; applying a blueprint onto the account "
                             "it was exported from re-creates everything deleted since the export")
    flags = me.get("feature_flags") or []
    if writing and "staff" in flags:
        raise NotThisAccount("this key's user carries the staff flag (an internal account); staff accounts are set "
                             "up from the command line")
    return {"email": me.get("email"), "account": account, "user_id": me.get("uuid") or "", "kind": "api_key",
            "first_name": me.get("first_name") or "", "last_name": me.get("last_name") or "",
            "impersonated": False, "exp_iso": "no expiry (Open API key)", "feature_flags": flags,
            "addons": me.get("addons") or [], "same_as_source": account == src}


# ------------------------------------------------------------- what the key cannot do, said out loud

def key_statuses(ctx) -> None:
    from account_blueprint.apply import _want  # noqa: PLC0415
    custom = [s for s in ctx.bp["statuses"] if not s.get("system")]
    _want(ctx, "statuses", len(custom))
    for s in custom:
        t = s["title"]
        if ctx.reg.status_title(t):
            ctx.log.add("statuses", "exists", t)
        else:
            ctx.log.add("statuses", "manual_todo", t, "create this lead status in the DataSift app (color %s); the "
                        "published API cannot list or create every status" % (s.get("color") or "any"))


def key_boards(ctx) -> None:
    from account_blueprint.apply import _want  # noqa: PLC0415
    _want(ctx, "boards", len(ctx.bp["boards"]))
    for b in ctx.bp["boards"]:
        cols = ", ".join(b.get("columns") or []) or "no columns"
        if b["title"] in ctx.reg.boards:
            ctx.log.add("boards", "exists", b["title"])
            ctx.log.add("boards", "manual_todo", b["title"], "check its columns in the app: %s (columns have no "
                        "published route)" % cols)
        else:
            ctx.log.add("boards", "manual_todo", b["title"], "create this board in the app with the columns %s; "
                        "a board's columns have no published route, so the key does not create half of one" % cols)


def key_sequences(ctx) -> None:
    from account_blueprint.apply import _want  # noqa: PLC0415
    _want(ctx, "sequences", len(ctx.bp["sequences"]))
    for q in ctx.bp["sequences"]:
        trig = (q.get("trigger") or {}).get("event") or (q.get("trigger") or {}).get("type") or "its trigger"
        ctx.log.add("sequences", "manual_todo", q["title"], "build it in Sequence Builder (on %s); sequences are not "
                    "in the published API" % trig)


def key_siftmap_note(ctx) -> None:
    from account_blueprint.apply import _want  # noqa: PLC0415
    _want(ctx, "siftmap", len(ctx.bp["siftmap_presets"]))
    if ctx.bp["siftmap_presets"]:
        ctx.log.add("siftmap", "skipped", "siftmap", "%d SiftMap feeders left out: they carry the source market's "
                    "counties. Build feeders for this account's own county (ACCOUNT_SETUP_FAMILIES adds them)"
                    % len(ctx.bp["siftmap_presets"]))


# ------------------------------------------------------------------------------------- the workflow

def blueprint_path() -> Path | None:
    p = (os.environ.get("ACCOUNT_SETUP_BLUEPRINT") or "").strip()
    if p:
        return Path(p) if Path(p).is_file() else None
    found = sorted((SRC.parent / "skills" / "account-blueprint" / "blueprints").glob("*.json"))
    return found[-1] if found else None


def families() -> set:
    raw = os.environ.get("ACCOUNT_SETUP_FAMILIES") or DEFAULT_FAMILIES
    return {x.strip() for x in raw.split(",") if x.strip() in KEY_FAMILIES}


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def run(task: dict) -> dict:
    from account_blueprint import apply as A  # noqa: PLC0415
    from account_blueprint.blueprint import load, validate  # noqa: PLC0415
    from account_blueprint.report import render_markdown, stamp  # noqa: PLC0415

    inputs = store.payload_of(task).get("inputs") or {}
    asked = str(inputs.get("phase") or "plan").strip().lower()
    email = str(inputs.get("account_email") or "").strip()
    folder = _folder(task)
    base = {"run_dir": str(folder), "asked": asked, "email": email}
    if asked not in PHASES:
        return dict(base, ok=False, problem="the phase is plan, verify or apply, not %r" % asked)
    bpp = blueprint_path()
    if not bpp:
        return dict(base, ok=False, problem="no blueprint on this box (ACCOUNT_SETUP_BLUEPRINT, or the account-blueprint "
                                             "skill's blueprints folder)")
    bp = load(str(bpp))
    errors, _warnings = validate(bp)
    if errors:
        return dict(base, ok=False, problem="the blueprint %s is invalid: %s" % (bpp.name, "; ".join(errors[:3])))
    allow = (os.environ.get("ACCOUNT_SETUP_ALLOW_APPLY") or "").strip().lower() in ("1", "true", "yes", "on")
    writing = asked == "apply" and config.live() and allow
    phase = asked if (asked != "apply" or writing) else "plan"
    why_plan = "" if phase == asked else (
        "the brain is in dry mode" if not config.live() else "ACCOUNT_SETUP_ALLOW_APPLY is not set on this box")
    client = KeyClient(_crm())
    try:
        ident = key_identity(client, bp, email, writing=writing)
    except NotThisAccount as exc:
        return dict(base, ok=False, problem=str(exc))
    fams = families()
    user_map = dict(kv.split("=", 1) for kv in (os.environ.get("ACCOUNT_SETUP_USER_MAP") or "").split(",") if "=" in kv)
    acct = "".join(ch for ch in ident["account"] if ch.isalnum())[:12]
    opts = A.Options(commit=writing, only=set(fams), folders="numbered", user_map=user_map, assign_fallback="self",
                     strip_neighborhoods=True, siftmap_auto_add=False, verify_only=(phase == "verify"),
                     count_probe=False, state_path=str(Path(config.DATA_DIR) / NAME / ("state_%s.json" % acct)),
                     report_path=str(folder / "files" / ("account_setup_%s" % phase)), blueprint_path=str(bpp))
    ctx = A.Ctx(client, bp, opts, email)
    ctx.ident = ident
    allowed = set(ctx.selected) if phase != "verify" else set()
    ctx.reg = key_registry(client, ctx.log, commit=writing, allowed=allowed, ident=ident, user_map=user_map)
    steps = [("statuses", key_statuses), ("lists", A.apply_lists), ("tags", A.apply_tags),
             ("custom_fields", A.apply_custom_fields), ("task_presets", A.apply_task_presets), ("boards", key_boards),
             ("presets", A.apply_presets), ("sequences", key_sequences),
             ("siftmap", A.apply_siftmap if "siftmap" in fams else key_siftmap_note)]
    for _fam, fn in steps:
        fn(ctx)
        ctx.save_state()
    summary = A.summarize(ctx)
    mismatches = ctx.log.by("mismatch")
    verdict = ("%d mismatches, %d gaps, %d items to set up by hand" % (
        len(mismatches), len(ctx.log.by("gap")), len(ctx.log.by("manual_todo"))))
    header = {"mode": {"plan": "DRY RUN", "verify": "VERIFY", "apply": "COMMIT"}[phase], "blueprint": bpp.name,
              "source": "%s (%s)" % ((bp.get("source") or {}).get("email"),
                                     str((bp.get("source") or {}).get("account_uuid"))[:8]),
              "target": "%s (%s)" % (ident["email"], ident["account"][:8]), "token": "api_key",
              "folders": "numbered", "strip_neighborhoods": True, "siftmap_auto_add": False,
              "allow_same_account": "read-only" if ident["same_as_source"] else "no", "at": stamp()}
    md = folder / "files" / ("account_setup_%s.md" % phase)
    md.write_text(render_markdown(header, summary, ctx.log, [], verdict), encoding="utf-8")
    (folder / "files" / ("account_setup_%s.json" % phase)).write_text(json.dumps(
        {"header": header, "summary": summary, "log": ctx.log.entries, "verdict": verdict}, indent=1, default=str),
        encoding="utf-8")
    manual = ctx.log.by("manual_todo")
    return dict(base, ok=not mismatches and not ctx.failed, phase=phase, why_plan=why_plan, blueprint=bpp.name,
                account=ident["account"][:8], same_as_source=ident["same_as_source"], families=sorted(fams),
                summary=summary, mismatches=len(mismatches), gaps=len(ctx.log.by("gap")), manual=len(manual),
                manual_by_family={f: sum(1 for e in manual if e["family"] == f) for f in ("statuses", "boards", "sequences")},
                calls=client.n_calls, files=[md.name, "account_setup_%s.json" % phase])


def render(task: dict, result: dict) -> str:
    if result.get("problem"):
        return "Account setup did not run: %s." % str(result["problem"]).rstrip(".")
    head = {"plan": "Account setup plan", "verify": "Account parity check", "apply": "Account setup applied"}[result["phase"]]
    lines = ["%s for %s against the blueprint %s." % (head, result.get("email"), result.get("blueprint"))]
    if result.get("why_plan"):
        lines.append("Asked to apply, ran as a plan instead: %s." % result["why_plan"])
    if result.get("same_as_source"):
        lines.append("This key is the blueprint's own source account, so this is a drift check; it can never apply here.")
    for s in result.get("summary") or []:
        if s["family"] in result.get("families", []) and (s.get("created") or s.get("gaps")):
            lines.append("%s: %d wanted, %d there, %d %s, %d gaps." % (
                s["family"].replace("_", " "), s["wanted"], s["existed"], s["created"],
                "created" if result["phase"] == "apply" else "to create", s["gaps"]))
    mb = result.get("manual_by_family") or {}
    lines.append("By hand in the app: %d statuses, %d boards, %d sequences (no published route for those)." % (
        mb.get("statuses", 0), mb.get("boards", 0), mb.get("sequences", 0)))
    if result.get("mismatches"):
        lines.append("%d objects did not read back as sent; see the report." % result["mismatches"])
    for n in result.get("files") or []:
        u = slack.file_url(task["id"], n)
        lines.append("%s: %s" % (n, u) if u else n)
    return "\n".join(lines)
