"""Apply a blueprint to a target account, one family at a time, in
dependency order, with a read-back on every object.

The gate, the order and the read-backs are the product; the creates are the
easy part. verify_target() runs before the first write and again before the
SiftMap family because that is the one step that can start spending the
target's record allowance. Every family indexes the target by title first,
so a re-run after a killed process adopts what already landed and creates
only what is missing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

from . import FAMILIES
from .blueprint import UUID_RE, is_ref
from .client import MAP, ApiError
from .export import count_shape
from .probe import create
from .report import Log, atomic_json, render_markdown, stamp
from .translate import Registry, refs_to_uuids, sequence_to_uuids, subset_equal


@dataclass
class Options:
    commit: bool = False
    only: set = field(default_factory=set)
    skip: set = field(default_factory=set)
    folders: str = "numbered"
    move_presets: bool = False
    user_map: dict = field(default_factory=dict)
    assign_fallback: str = "self"
    strip_neighborhoods: bool = True
    stub_inactive: bool = False
    siftmap_auto_add: bool = False
    probe_only: bool = False
    allow_staff_target: bool = False
    allow_same_account: str = ""
    strict_counts: bool = False
    count_probe: bool = True
    verify_only: bool = False
    state_path: str = ""
    report_path: str = ""
    blueprint_path: str = ""
    min_token_left_s: int = 1800


class Ctx:
    def __init__(self, client, bp, opts: Options, target: str):
        self.client, self.bp, self.opts, self.target = client, bp, opts, target
        self.log = Log()
        self.ident: dict = {}
        self.reg: Registry | None = None
        self.state: dict = {"routes": {}, "families": {}}
        self.routes: dict = self.state["routes"]
        self.probed: set = set()
        self.failed = False
        self.preset_bodies: dict = {}      # title -> (folder, must, source_count)
        self.summary: dict = {}
        sel = set(FAMILIES)
        if opts.only:
            sel = set(opts.only)
        sel -= set(opts.skip)
        self.selected = sel

    def selected_family(self, fam: str) -> bool:
        return fam in self.selected

    def save_state(self) -> None:
        if self.opts.state_path and self.opts.commit:
            self.state["target"] = {"email": self.target, "account": self.ident.get("account"),
                                    "blueprint_sha256": _sha(self.opts.blueprint_path)}
            self.state["at"] = time.time()
            atomic_json(self.state, self.opts.state_path)

    def remember(self, family: str, title: str, action: str, uuid=None) -> None:
        fam = self.state["families"].setdefault(family, {})
        fam[title] = {"action": action, "uuid": uuid, "at": time.time()}


def _sha(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------- the gate

class Refused(SystemExit):
    """Exit code 3, message on stderr. A SystemExit no per-item except can
    swallow, and a distinct code so a wrapper can tell a refusal from a
    read-back mismatch (2) or a bad blueprint (1)."""

    def __init__(self, msg: str):
        self.msg = msg
        print("REFUSED: " + msg, file=sys.stderr)
        super().__init__(3)


def verify_target(client, bp: dict, target_email: str, opts: Options) -> dict:
    """Hard gate: Refused (exit 3) on any doubt. No caller can swallow it."""
    if client.kind == "api_key":
        raise Refused("an Api-Key carries no claims to gate a write on; "
                         "apply needs the target's JWT - REFUSING")
    claims = client.claims()
    email = (claims.get("email") or "").strip().lower()
    if email != target_email.strip().lower():
        raise Refused("token email is %r, expected %r - REFUSING to write"
                         % (email, target_email))
    account = str(claims.get("account") or "").lower()
    if not account:
        raise Refused("token has no account claim - REFUSING")
    src = str((bp.get("source") or {}).get("account_uuid") or "").lower()
    if account == src and not opts.allow_same_account:
        raise Refused("target account == blueprint source account %s. "
                         "A different email is not a different account; cloning an account onto "
                         "itself re-creates everything deleted since the export - REFUSING"
                         % src[:8])
    left = int(claims.get("exp", 0)) - time.time()
    if left < opts.min_token_left_s:
        raise Refused("token has %.0f min left (< %d); paste a fresh one - REFUSING"
                         % (left / 60, opts.min_token_left_s // 60))
    flags = claims.get("feature_flags") or []
    if "staff" in flags and not opts.allow_staff_target:
        raise Refused("target token carries the staff flag (an internal account); "
                         "pass --allow-staff-target if that is intended - REFUSING")
    if client.kind == "impersonated":
        if not claims.get("impersonated"):
            raise Refused("impersonated token lacks the impersonated flag - REFUSING")
        staff_acct = str(client._staff.claims().get("account") or "").lower()
        if account == staff_acct:
            raise Refused("impersonated token still carries the staff account - REFUSING")
    elif claims.get("impersonated"):
        raise Refused("a pasted token carries the impersonated flag (stale state) - REFUSING")
    # Live read proves the server accepts the token for THIS account.
    rows = client.call("/api/internal/sequence/?limit=5")
    rows = rows.get("results", rows) if isinstance(rows, dict) else rows
    for r in rows or []:
        acct = str(r.get("account_uuid") or "").lower()
        if acct and acct != account:
            raise Refused("live sequence read returned account %s, token says %s - REFUSING"
                             % (acct[:8], account[:8]))
        if acct and acct == src and not opts.allow_same_account:
            raise Refused("live data belongs to the blueprint's source account - REFUSING")
    return {"email": claims.get("email"), "account": account, "user_id": claims.get("user_id"),
            "impersonated": bool(claims.get("impersonated")), "kind": client.kind,
            "exp_iso": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(claims["exp"]))),
            "feature_flags": flags, "addons": claims.get("addons") or []}


# ------------------------------------------------------------ families

def _want(ctx: Ctx, family: str, n: int) -> None:
    ctx.summary[family] = {"family": family, "wanted": n}


def apply_statuses(ctx: Ctx) -> None:
    reg, log = ctx.reg, ctx.log
    custom = [s for s in ctx.bp["statuses"] if not s.get("system")]
    _want(ctx, "statuses", len(custom))
    for s in ctx.bp["statuses"]:
        t = s["title"]
        if t in reg.statuses:
            if not s.get("system"):
                got = reg.status_rows.get(t) or {}
                if (got.get("color") or "").lower() != (s.get("color") or "").lower():
                    log.add("statuses", "exists_differs", t, "color %s vs blueprint %s (left alone)"
                            % (got.get("color"), s.get("color")))
                else:
                    log.add("statuses", "exists", t)
            continue
        alt = reg.status_title(t)
        if alt:
            log.add("statuses", "exists", t, "matched by case to %r" % alt)
            log.add("statuses", "translated", t, "status %r -> %r" % (t, alt))
            continue
        if s.get("system"):
            log.add("statuses", "gap", t, "system status missing in target (cannot be created)")
            continue
        if not ctx.selected_family("statuses"):
            log.add("statuses", "gap", t, "missing and family not selected")
            continue
        body = {"title": t, "color": s.get("color"), "is_active": bool(s.get("is_active", True))}

        def readback(t=t):
            reg.load_statuses()
            return reg.status_rows.get(t)
        got = create(ctx, "statuses", "status", "/api/internal/status/", body, t, readback)
        if got and not got.get("_dry"):
            if (got.get("color") or "").lower() != (s.get("color") or "").lower():
                log.add("statuses", "mismatch", t, "color stored %s, sent %s" % (got.get("color"), s.get("color")))
                ctx.failed = True
            else:
                log.add("statuses", "verified", t)
            ctx.remember("statuses", t, "created", got.get("uuid"))
        elif got:
            reg.statuses[t] = got["uuid"]
            reg.status_titles[t.casefold()] = t


def apply_lists(ctx: Ctx) -> None:
    _want(ctx, "lists", len(ctx.bp["lists"]))
    for x in ctx.bp["lists"]:
        t = x["title"]
        same = ctx.reg._same("list", t)
        if same is not None:
            ctx.log.add("lists", "exists", t, "" if same == t else "as %r" % same)
            if same != t:
                ctx.log.add("lists", "translated", t, "list %r -> target's %r" % (t, same))
            continue
        if ctx.opts.verify_only:
            ctx.log.add("lists", "gap", t, "missing in target")
            continue
        if not ctx.selected_family("lists"):
            ctx.log.add("lists", "gap", t, "missing and family not selected")
            continue
        u = ctx.reg.ensure("list", t, t)
        if u and ctx.opts.commit:
            ctx.log.add("lists", "verified", t)
            ctx.remember("lists", t, "created", u)
        elif not u:
            ctx.failed = True


def apply_tags(ctx: Ctx) -> None:
    _want(ctx, "tags", len(ctx.bp["tags"]))
    for x in ctx.bp["tags"]:
        t = x["title"]
        if t in ctx.reg.tags:
            ctx.log.add("tags", "exists", t)
            continue
        low = {k.casefold(): k for k in ctx.reg.tags}
        if t.casefold() in low:
            ctx.log.add("tags", "warn", t, "target has %r (different case); creating the exact title"
                        % low[t.casefold()])
        if ctx.opts.verify_only:
            ctx.log.add("tags", "gap", t, "missing in target")
            continue
        if not ctx.selected_family("tags"):
            ctx.log.add("tags", "gap", t, "missing and family not selected")
            continue
        u = ctx.reg.ensure("tag", t, t)
        if u and ctx.opts.commit:
            ctx.log.add("tags", "verified", t)
            ctx.remember("tags", t, "created", u)
        elif not u:
            ctx.failed = True


def apply_custom_fields(ctx: Ctx) -> None:
    reg, log, bp = ctx.reg, ctx.log, ctx.bp
    _want(ctx, "custom_fields", len(bp["custom_fields"]))
    if not ctx.selected_family("custom_fields") and not ctx.opts.verify_only:
        for f in bp["custom_fields"]:
            if f["label"] not in reg.custom_fields:
                log.add("custom_fields", "gap", f["label"], "family not selected")
        return
    for g in bp["custom_field_groups"]:
        t = g["title"]
        if t in reg.custom_groups:
            log.add("custom_fields", "exists", "group " + t)
            continue

        def readback(t=t):
            reg.load_custom_fields()
            gid = reg.custom_groups.get(t)
            return {"id": gid} if gid else None
        got = create(ctx, "custom_fields", "custom_field_group", "/api/internal/custom-fields/group/",
                     {"label": t, "entity_type": g.get("entity_type") or "property"}, "group " + t, readback)
        if got and got.get("_dry"):
            # Register the dry create so the plan for the fields below is the
            # plan the real run will execute, not a cascade of false gaps.
            reg.custom_groups[t] = "DRY-RUN-group"
    for f in bp["custom_fields"]:
        lab = f["label"]
        have = reg.custom_fields.get(lab)
        if have:
            if have.get("field_type") != f["field_type"]:
                log.add("custom_fields", "exists_differs", lab, "target type %s, blueprint %s (left alone)"
                        % (have.get("field_type"), f["field_type"]))
                continue
            log.add("custom_fields", "exists", lab)
            want = [o["label"] for o in f.get("options") or []]
            got = {o.get("label") for o in (have.get("options") or [])}
            missing = [o for o in want if o not in got]
            if missing and f["field_type"] in ("select", "multiselect"):
                fid = have.get("id") or have.get("uuid")
                for o in missing:
                    if ctx.opts.verify_only:
                        log.add("custom_fields", "gap", lab, "option %r missing" % o)
                    elif not ctx.opts.commit:
                        log.add("custom_fields", "created", lab, "(dry run) add option %r" % o)
                    else:
                        ctx.client.call("/api/internal/custom-fields/%s/option/" % fid, "POST",
                                        {"label": o, "value": o})
                        log.add("custom_fields", "created", lab, "option %r" % o)
                if ctx.opts.commit and not ctx.opts.verify_only:
                    reg.load_custom_fields()
                    now = {o.get("label") for o in (reg.custom_fields.get(lab) or {}).get("options") or []}
                    if not set(want) <= now:
                        log.add("custom_fields", "mismatch", lab, "options missing after add: %s"
                                % sorted(set(want) - now))
                        ctx.failed = True
                    else:
                        log.add("custom_fields", "verified", lab, "options")
            continue
        gid = reg.custom_groups.get(f.get("group") or "")
        if not gid:
            gid = reg.custom_groups.get("Misc.")
            if f.get("group"):
                log.add("custom_fields", "translated", lab, "group %r missing -> %s"
                        % (f.get("group"), "'Misc.'" if gid else "NO GROUP"))
        if not gid:
            log.add("custom_fields", "gap", lab, "no custom-field group available in target")
            continue
        body = {"label": lab, "field_type": f["field_type"],
                "entity_type": f.get("entity_type") or "property", "group_id": gid}
        if f.get("required"):
            body["required"] = True
        if f.get("placeholder"):
            body["placeholder"] = f["placeholder"]
        if f["field_type"] in ("select", "multiselect"):
            body["options"] = [{"label": o["label"], "value": o.get("value") or o["label"]}
                               for o in f.get("options") or []]
            if not body["options"]:
                log.add("custom_fields", "gap", lab, "select field with no options cannot be created")
                continue

        def readback(lab=lab):
            reg.load_custom_fields()
            return reg.custom_fields.get(lab)
        got = create(ctx, "custom_fields", "custom_field", "/api/internal/custom-fields/", body, lab, readback)
        if got and not got.get("_dry"):
            ok = got.get("field_type") == f["field_type"]
            want = {o["label"] for o in f.get("options") or []}
            have_o = {o.get("label") for o in (got.get("options") or [])}
            if ok and want <= have_o:
                log.add("custom_fields", "verified", lab)
                ctx.remember("custom_fields", lab, "created", got.get("uuid"))
            else:
                log.add("custom_fields", "mismatch", lab, "type %s / options %s after create"
                        % (got.get("field_type"), sorted(want - have_o)))
                ctx.failed = True


def _user_uuid(ctx: Ctx, r, where: str, family: str):
    if not is_ref(r):
        return None
    u = ctx.reg.resolve(r, where, family, quiet=(ctx.opts.assign_fallback == "self"))
    if u:
        return u
    if ctx.opts.assign_fallback == "self" and ctx.reg.self_user:
        ctx.log.add(family, "placeholder", where, "assignee %r -> the applying user" % r.get("title"))
        ctx.log.add(family, "manual_todo", where, "set the assignee (source had %r)" % r.get("title"))
        return ctx.reg.self_user
    return None


def apply_task_presets(ctx: Ctx) -> None:
    reg, log, bp = ctx.reg, ctx.log, ctx.bp
    _want(ctx, "task_presets", len(bp["task_presets"]))
    if not ctx.selected_family("task_presets") and not ctx.opts.verify_only:
        for p in bp["task_presets"]:
            if (p["group"], p["title"]) not in reg.task_presets:
                log.add("task_presets", "gap", p["title"], "family not selected")
        return
    for g in bp["task_groups"]:
        t = g["title"]
        if t in reg.task_groups:
            log.add("task_presets", "exists", "group " + t)
            continue

        def readback(t=t):
            reg.load_task_presets()
            u = reg.task_groups.get(t)
            return {"uuid": u} if u else None
        got = create(ctx, "task_presets", "task_group", "/api/internal/task-group/", {"title": t},
                     "group " + t, readback)
        if got and got.get("_dry"):
            reg.task_groups[t] = "DRY-RUN-group"
    for p in bp["task_presets"]:
        key = (p["group"], p["title"])
        if key in reg.task_presets:
            log.add("task_presets", "exists", p["title"])
            continue
        gid = reg.task_groups.get(p["group"])
        if not gid:
            log.add("task_presets", "gap", p["title"], "task group %r not in target" % p["group"])
            continue
        body = {"title": p["title"], "notes": p.get("notes"), "round_robin": bool(p.get("round_robin")),
                "expires_in": p.get("expires_in"), "all_day": bool(p.get("all_day")),
                "due_time": p.get("due_time"), "skip_weekends": bool(p.get("skip_weekends"))}
        if p.get("order") is not None:
            body["order"] = p["order"]
        # Verified live 2026-09-10, two rejections in a row: the create route
        # wants EXACTLY ONE of assigned_to_role / assigned_to_users /
        # assigned_to_user, and the other two must be ABSENT. An empty list
        # 400s ("This list may not be empty.") and two non-null keys 400 with
        # "can't be not null together". The GET shape shows all three, which
        # is what misled the first payload.
        users = [u for u in (_user_uuid(ctx, r, p["title"], "task_presets")
                             for r in p.get("assigned_to_users") or []) if u]
        if p.get("assigned_to_role"):
            body["assigned_to_role"] = p["assigned_to_role"]
        elif users:
            body["assigned_to_users"] = users
        else:
            atu = _user_uuid(ctx, p.get("assigned_to_user"), p["title"], "task_presets")
            if not atu:
                atu = reg.self_user
                log.add("task_presets", "placeholder", p["title"],
                        "no assignee in the source; assigned to the applying user")
            body["assigned_to_user"] = atu
        path = "/api/internal/task-group/%s/task-preset/" % gid

        def readback(key=key):
            reg.load_task_presets()
            u = reg.task_presets.get(key)
            if not u:
                return None
            for row in ctx.client.get_all("/api/internal/task-group/%s/task-preset/?offset=0&limit=999" % gid):
                if row.get("uuid") == u:
                    return row
            return {"uuid": u}
        got = create(ctx, "task_presets", "task_preset", path, body, p["title"], readback)
        if got and got.get("_dry"):
            reg.task_presets[key] = "DRY-RUN-task-preset"
        if got and not got.get("_dry"):
            checks = {k: body[k] for k in ("expires_in", "all_day", "skip_weekends", "round_robin")}
            if subset_equal(checks, got):
                log.add("task_presets", "verified", p["title"])
                ctx.remember("task_presets", p["title"], "created", got.get("uuid"))
            else:
                log.add("task_presets", "mismatch", p["title"], "stored %s, sent %s"
                        % ({k: got.get(k) for k in checks}, checks))
                ctx.failed = True


def apply_boards(ctx: Ctx) -> None:
    """Boards and columns are the skeleton every card.moved sequence hangs
    on. Both create routes are unverified, so they go through the probe:
    the first board (or column) is POSTed and read back before the rest."""
    reg, log, bp = ctx.reg, ctx.log, ctx.bp
    _want(ctx, "boards", len(bp["boards"]))
    if not ctx.selected_family("boards") and not ctx.opts.verify_only:
        for b in bp["boards"]:
            if b["title"] not in reg.boards:
                log.add("boards", "gap", b["title"], "family not selected")
        return
    for b in bp["boards"]:
        bt = b["title"]
        if bt in reg.boards:
            log.add("boards", "exists", bt)
        else:
            def readback(bt=bt):
                reg.load_boards()
                u = reg.boards.get(bt)
                return {"uuid": u} if u else None
            got = create(ctx, "boards", "siftline_board", "/api/internal/siftline/board/",
                         {"title": bt}, bt, readback)
            if not got:
                for col in b.get("columns", []):
                    log.add("boards", "gap", "%s/%s" % (bt, col), "board could not be created")
                continue
            if got.get("_dry"):
                reg.boards[bt] = "DRY-RUN-board"
            else:
                log.add("boards", "verified", bt)
                ctx.remember("boards", bt, "created", got.get("uuid"))
        buid = reg.boards[bt]
        for order, col in enumerate(b.get("columns", [])):
            have = reg.column_uuid(bt, col)
            if have:
                if (bt, col) not in reg.columns:
                    log.add("boards", "translated", "%s/%s" % (bt, col),
                            "column exists under a case/whitespace variant; using it")
                continue
            path = "/api/internal/siftline/board/%s/column/" % buid

            def readback(bt=bt, col=col):
                reg.load_boards()
                u = reg.column_uuid(bt, col)
                return {"uuid": u} if u else None
            got = create(ctx, "boards", "siftline_column", path, {"title": col, "order": order},
                         "%s/%s" % (bt, col), readback, unique_title=True)
            if got and got.get("_dry"):
                reg.columns[(bt, col)] = "DRY-RUN-column"
            elif got:
                log.add("boards", "verified", "%s/%s" % (bt, col))
                ctx.remember("boards", "%s/%s" % (bt, col), "created", got.get("uuid"))


def _move_preset(ctx: Ctx, title: str, uuid: str, old_folder: str, dst: dict, new_folder: str) -> None:
    """Re-folder an existing preset (a folder renamed at the source is normal
    drift). PATCH the folder only, read back, and the preset's own filters
    are never touched."""
    log, c = ctx.log, ctx.client
    if not ctx.opts.commit or dst.get("_dry"):
        log.add("presets", "translated", title, "(dry run) would move from %r to %r" % (old_folder, new_folder))
        return
    c.call("/api/internal/filter-preset/%s/" % uuid, "PATCH", {"folder": dst["uuid"]})
    got = c.call("/api/internal/filter-preset/%s/" % uuid)
    gf = got.get("folder")
    gf = gf.get("uuid") if isinstance(gf, dict) else gf
    if gf == dst["uuid"]:
        log.add("presets", "translated", title, "moved from %r to %r" % (old_folder, new_folder))
        log.add("presets", "verified", title, "moved")
        ctx.remember("presets", title, "moved", uuid)
    else:
        log.add("presets", "mismatch", title, "PATCH folder accepted but read-back shows %r" % gf)
        ctx.failed = True


def apply_presets(ctx: Ctx) -> None:
    reg, log, bp, c = ctx.reg, ctx.log, ctx.bp, ctx.client
    folders = [f for f in bp["preset_folders"] if f.get("numbered") or ctx.opts.folders == "all"]
    _want(ctx, "presets", sum(len(f["presets"]) for f in folders))
    for f in bp["preset_folders"]:
        if f not in folders:
            log.add("presets", "skipped", f["title"], "non-numbered folder (%d presets); --folders all to include"
                    % len(f["presets"]))
    if not ctx.selected_family("presets") and not ctx.opts.verify_only:
        log.add("presets", "gap", "presets", "family not selected")
        return
    dst = {x["title"]: x for x in c.get_all("/api/internal/filter-preset-folder/?type=properties&limit=999")}
    # Global title index: preset titles are unique per ACCOUNT, not per folder.
    where_is: dict[str, str] = {}
    preset_uuid: dict[str, str] = {}
    for ft, fol in dst.items():
        for p in c.get_all("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % fol["uuid"]):
            where_is[p["title"]] = ft
            preset_uuid[p["title"]] = p["uuid"]
    for fol in folders:
        ft = fol["title"]
        d = dst.get(ft)
        if not d:
            if not fol.get("numbered"):
                log.add("presets", "gap", ft, "non-numbered folder not present in target; never created")
                for p in fol["presets"]:
                    log.add("presets", "gap", p["title"], "folder %r missing" % ft)
                continue

            def readback(ft=ft):
                rows = c.get_all("/api/internal/filter-preset-folder/?type=properties&limit=999")
                return next((x for x in rows if x["title"] == ft), None)
            d = create(ctx, "presets", "preset_folder", "/api/internal/filter-preset-folder/",
                       {"title": ft, "type": "properties", "permissions": []}, "folder " + ft, readback)
            if not d:
                for p in fol["presets"]:
                    log.add("presets", "gap", p["title"], "folder %r could not be created" % ft)
                continue
            dst[ft] = d
        for p in fol["presets"]:
            t = p["title"]
            if t in where_is:
                if where_is[t] == ft:
                    log.add("presets", "exists", t)
                    if not d.get("_dry"):
                        must = None
                        if ctx.opts.verify_only and ctx.opts.count_probe:
                            # verify counts EXISTING presets too, off the
                            # filter the target actually stores
                            got = c.call("/api/internal/filter-preset/%s/" % preset_uuid[t])
                            must = (got.get("filters") or {}).get("must")
                        ctx.preset_bodies[t] = (ft, must, p.get("source_count"))
                elif ctx.opts.move_presets and not ctx.opts.verify_only:
                    _move_preset(ctx, t, preset_uuid[t], where_is[t], d, ft)
                    if not d.get("_dry"):
                        ctx.preset_bodies[t] = (ft, None, p.get("source_count"))
                    where_is[t] = ft
                else:
                    log.add("presets", "gap", t, "exists in folder %r, wanted %r (titles are account-unique;"
                            " --move-presets re-folders it)" % (where_is[t], ft))
                continue
            filt, dropped = refs_to_uuids(copy.deepcopy(p["filters"]), reg, log, t, "presets",
                                          assign_fallback=ctx.opts.assign_fallback,
                                          strip_neighborhoods=ctx.opts.strip_neighborhoods)
            must = (filt or {}).get("must") or {}
            if dropped:
                # A preset minus one of its references selects a DIFFERENT
                # population (Ready to Call without its Priority 1 gate is
                # every record with a phone). Never create a narrower or
                # wider preset than the source; report it instead.
                log.add("presets", "gap", t, "%d reference(s) did not resolve; preset NOT created" % dropped)
                continue
            if not must or (list(must.keys()) == ["must_not"]):
                log.add("presets", "gap", t, "must is empty after translation; not created")
                continue
            filt["account"] = ctx.ident["account"]
            body = {"title": t, "folder": d["uuid"], "quick_filter": bool(p.get("quick_filter")),
                    "filters": filt, "type": "properties"}
            if d.get("_dry"):
                log.add("presets", "created", t, "(dry run) POST /api/internal/filter-preset/")
                ctx.preset_bodies[t] = (ft, must, p.get("source_count"))
                continue

            def readback(t=t, d=d):
                rows = c.get_all("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % d["uuid"])
                row = next((x for x in rows if x["title"] == t), None)
                return c.call("/api/internal/filter-preset/%s/" % row["uuid"]) if row else None
            got = create(ctx, "presets", "preset", "/api/internal/filter-preset/", body, t, readback,
                         unique_title=True)
            if got and not got.get("_dry"):
                gm = (got.get("filters") or {}).get("must")
                acct = str((got.get("filters") or {}).get("account") or "").lower()
                if gm == must and bool(got.get("quick_filter")) == body["quick_filter"] \
                        and acct in ("", ctx.ident["account"]):
                    log.add("presets", "verified", t)
                    ctx.remember("presets", t, "created", got.get("uuid"))
                else:
                    log.add("presets", "mismatch", t, "stored must differs from sent: stored=%s sent=%s"
                            % (json.dumps(gm, sort_keys=True)[:400], json.dumps(must, sort_keys=True)[:400]))
                    ctx.failed = True
                where_is[t] = ft
                ctx.preset_bodies[t] = (ft, must, p.get("source_count"))
            elif got:
                ctx.preset_bodies[t] = (ft, must, p.get("source_count"))


def apply_sequences(ctx: Ctx) -> None:
    reg, log, bp, c = ctx.reg, ctx.log, ctx.bp, ctx.client
    _want(ctx, "sequences", len(bp["sequences"]))
    if not ctx.selected_family("sequences") and not ctx.opts.verify_only:
        log.add("sequences", "gap", "sequences", "family not selected")
        return
    for f in bp["sequence_folders"]:
        t = f["title"]
        if t in reg.sequence_folders:
            log.add("sequences", "exists", "folder " + t)
            continue

        def readback(t=t):
            reg.sequence_folders = {x["title"]: x["uuid"] for x in
                                    c.get_all("/api/internal/sequence-folder/?limit=999")}
            u = reg.sequence_folders.get(t)
            return {"uuid": u} if u else None
        got = create(ctx, "sequences", "sequence_folder", "/api/internal/sequence-folder/", {"title": t},
                     "folder " + t, readback)
        if got and got.get("_dry"):
            reg.sequence_folders[t] = "DRY-RUN-folder"
    have = {q["title"]: q for q in c.get_all("/api/internal/sequence/?limit=999")}
    for q in bp["sequences"]:
        t = q["title"]
        if t in have:
            log.add("sequences", "exists", t)
            continue
        body = sequence_to_uuids(q, reg, log, assign_fallback=ctx.opts.assign_fallback,
                                 stub_inactive=ctx.opts.stub_inactive)
        if body is None:
            continue
        if "folder" in body and str(body["folder"]).startswith("DRY-RUN"):
            body["folder"] = "DRY-RUN"

        def readback(t=t):
            rows = c.get_all("/api/internal/sequence/?limit=999")
            row = next((x for x in rows if x["title"] == t), None)
            return c.call("/api/internal/sequence/%s/" % row["uuid"]) if row else None
        got = create(ctx, "sequences", "sequence", "/api/internal/sequence/", body, t, readback)
        if got and not got.get("_dry"):
            check = {k: body[k] for k in ("trigger", "conditions", "actions", "is_active")}
            if subset_equal(check, got):
                log.add("sequences", "verified", t, "" if body["is_active"] else "inactive by design")
                ctx.remember("sequences", t, "created", got.get("uuid"))
            else:
                log.add("sequences", "mismatch", t, "stored definition differs from sent (see report json)",
                        sent=check, got={k: got.get(k) for k in check})
                ctx.failed = True


def apply_siftmap(ctx: Ctx) -> None:
    reg, log, bp, c = ctx.reg, ctx.log, ctx.bp, ctx.client
    _want(ctx, "siftmap", len(bp["siftmap_presets"]))
    if not ctx.selected_family("siftmap") and not ctx.opts.verify_only:
        log.add("siftmap", "gap", "siftmap", "family not selected")
        return
    have = {}
    for f in c.get_all("/filters/?scope=account&page_size=100&page=1", base=MAP):
        if f.get("is_active") is not False:
            have.setdefault(f["name"], f)
    for m in bp["siftmap_presets"]:
        n = m["name"]
        if n in have:
            log.add("siftmap", "exists", n)
            continue
        for kind, key in (("list", "lists"), ("tag", "tags")):
            for t in m.get(key) or []:
                reg.ensure(kind, t, n)
        auto = bool(m.get("auto_add_enabled")) and ctx.opts.siftmap_auto_add
        if m.get("auto_add_enabled") and not auto:
            log.add("siftmap", "translated", n, "auto-add forced OFF (--siftmap-auto-add to keep it)")
            log.add("siftmap", "manual_todo", n, "enable auto-add once the market addresses are yours")
        if m.get("counties"):
            log.add("siftmap", "warn", n, "carries the source market's addresses: %s" % m["counties"])
        body = {"name": n, "description": m.get("description") or "", "auto_add_enabled": auto,
                "replace_owners_enabled": bool(m.get("replace_owners_enabled")),
                "email_enabled": bool(m.get("email_enabled")),
                "lists": list(m.get("lists") or []), "tags": list(m.get("tags") or []),
                "filter_data": m.get("filter_data") or {}}
        for k in ("limit", "limit_type"):
            if m.get(k) is not None:
                body[k] = m[k]

        def readback(n=n):
            rows = c.get_all("/filters/?scope=account&page_size=100&page=1", base=MAP)
            row = next((x for x in rows if x["name"] == n and x.get("is_active") is not False), None)
            return c.call("/filters/%s/" % row["id"], base=MAP) if row else None
        got = create(ctx, "siftmap", "siftmap", "/filters/", body, n, readback)
        if got and not got.get("_dry"):
            check = {k: body[k] for k in ("auto_add_enabled", "replace_owners_enabled", "lists", "tags", "filter_data")}
            if subset_equal(check, got):
                log.add("siftmap", "verified", n)
                ctx.remember("siftmap", n, "created", got.get("id"))
            else:
                log.add("siftmap", "mismatch", n, "stored filter differs from sent", sent=check,
                        got={k: got.get(k) for k in check})
                ctx.failed = True


# ---------------------------------------------------------------- the sweep

def count_probes(ctx: Ctx) -> list[dict]:
    if not ctx.opts.count_probe or not ctx.preset_bodies:
        return []
    c, reg = ctx.client, ctx.reg
    try:
        u = c.call("/api/internal/upload/usage/", "POST", {})
        used = int((u or {}).get("upload_usage") or 0)
    except ApiError:
        used = -1
    tag_empty: dict[str, bool] = {}
    out = []
    for title, (folder, must, src) in ctx.preset_bodies.items():
        if must is None:
            continue
        shaped, approx = count_shape(must)
        try:
            n = c.search_count(shaped)
        except ApiError as e:
            out.append({"folder": folder, "title": title, "target": "err %s" % e.code,
                        "source": src, "verdict": "count failed"})
            continue
        verdict = "OK"
        if n == 0:
            if used == 0:
                verdict = "expected-on-empty-account"
            else:
                verdict = "suspicious: 0 matches, refs non-empty"
                for tu in (must.get("any_tags") or []) + (must.get("all_tags") or []):
                    if tu not in tag_empty:
                        try:
                            tag_empty[tu] = c.search_count({"any_tags": [tu]}) == 0
                        except ApiError:
                            tag_empty[tu] = False
                    if tag_empty[tu]:
                        name = next((k for k, v in reg.tags.items() if v == tu), tu[:8])
                        verdict = "suspicious: gated on empty tag %r" % name
                        break
        if approx and verdict == "OK":
            verdict = "OK (date window ignored)"
        out.append({"folder": folder, "title": title, "target": n, "source": src, "verdict": verdict})
    return out


def summarize(ctx: Ctx) -> list[dict]:
    rows = []
    for fam in FAMILIES:
        s = ctx.summary.get(fam) or {"family": fam, "wanted": 0}
        cnt = ctx.log.counts(fam)
        # folders and groups are containers, not the objects the row counts
        cnt["exists"] = sum(1 for e in ctx.log.entries if e["family"] == fam and e["action"] == "exists"
                            and not e["object"].startswith(("group ", "folder ")))
        cnt["created"] = sum(1 for e in ctx.log.entries if e["family"] == fam and e["action"] == "created"
                             and not e["object"].startswith(("group ", "folder ")))
        s.update({"existed": cnt.get("exists", 0), "created": cnt.get("created", 0),
                  "verified": cnt.get("verified", 0), "gaps": cnt.get("gap", 0),
                  "manual": cnt.get("manual_todo", 0)})
        rows.append(s)
    return rows


def run_apply(client, bp: dict, target: str, opts: Options) -> int:
    ctx = Ctx(client, bp, opts, target)
    mode = "VERIFY" if opts.verify_only else ("COMMIT" if opts.commit else "DRY RUN")
    print("=== %s: blueprint %s -> %s ===" % (mode, os.path.basename(opts.blueprint_path or "?"), target))
    ctx.ident = verify_target(client, bp, target, opts)
    print("target verified: %s account %s (%s, token to %s)" % (
        ctx.ident["email"], ctx.ident["account"][:8], ctx.ident["kind"], ctx.ident["exp_iso"]))
    if opts.state_path and os.path.exists(opts.state_path):
        try:
            with open(opts.state_path, encoding="utf-8") as f:
                prev = json.load(f)
            if (prev.get("target") or {}).get("account") not in (None, ctx.ident["account"]):
                raise Refused("state file %s belongs to another target account" % opts.state_path)
            # "failed" is a payload rejection, which a code fix resolves; only
            # a verified route or a route the server does not serve persists.
            ctx.state["routes"].update({k: v for k, v in (prev.get("routes") or {}).items()
                                        if v in ("verified", "manual")})
            ctx.routes = ctx.state["routes"]
        except (OSError, ValueError):
            pass
    allowed = set(ctx.selected) if not opts.verify_only else set()
    ctx.reg = Registry(client, ctx.log, commit=opts.commit and not opts.verify_only, allowed_create=allowed,
                       self_user=ctx.ident.get("user_id") or "", account=ctx.ident["account"],
                       user_map=opts.user_map)
    ctx.reg.load()
    if not ctx.reg.users:
        raise Refused("target user index is EMPTY; every assignee would be wrong")
    print("target index: %d lists, %d tags, %d statuses, %d boards, %d users, %d task presets, "
          "%d sequence folders, %d custom fields"
          % (len(ctx.reg.lists), len(ctx.reg.tags), len(ctx.reg.statuses), len(ctx.reg.boards),
             len(ctx.reg.users), len(ctx.reg.task_presets), len(ctx.reg.sequence_folders),
             len(ctx.reg.custom_fields)))

    steps = [("statuses", apply_statuses), ("lists", apply_lists), ("tags", apply_tags),
             ("custom_fields", apply_custom_fields), ("task_presets", apply_task_presets),
             ("boards", apply_boards), ("presets", apply_presets), ("sequences", apply_sequences),
             ("siftmap", apply_siftmap)]
    for fam, fn in steps:
        if fam == "siftmap" and opts.commit and not opts.verify_only:
            verify_target(client, bp, target, opts)
        print("\n--- %s ---" % fam)
        fn(ctx)
        cnt = ctx.log.counts(fam)
        print("  " + ", ".join("%s %d" % (k, v) for k, v in sorted(cnt.items())))
        ctx.log.print_family(fam)
        ctx.save_state()
        if opts.probe_only and fam == "sequences":
            print("\n--probe-only: stopping after the probed families")
            break

    counts = count_probes(ctx) if (opts.commit or opts.verify_only) else []
    summary = summarize(ctx)
    mismatches = ctx.log.by("mismatch")
    suspicious = [c for c in counts if str(c["verdict"]).startswith("suspicious")]
    code = 0
    if mismatches or ctx.failed:
        code = 2
    if opts.strict_counts and suspicious:
        code = 2
    verdict = ("exit %d: %d mismatches, %d gaps, %d manual items, %d suspicious counts"
               % (code, len(mismatches), len(ctx.log.by("gap")), len(ctx.log.by("manual_todo")),
                  len(suspicious)))
    header = {"mode": mode, "blueprint": opts.blueprint_path, "blueprint_sha256": _sha(opts.blueprint_path),
              "source": "%s (%s)" % ((bp.get("source") or {}).get("email"),
                                     str((bp.get("source") or {}).get("account_uuid"))[:8]),
              "target": "%s (%s)" % (ctx.ident["email"], ctx.ident["account"][:8]),
              "token": ctx.ident["kind"], "folders": opts.folders,
              "strip_neighborhoods": opts.strip_neighborhoods, "siftmap_auto_add": opts.siftmap_auto_add,
              "allow_same_account": opts.allow_same_account or "no", "at": stamp()}
    print("\n=== summary ===")
    for s in summary:
        print("  %-14s wanted %-4s existed %-4s created %-4s verified %-4s gaps %-4s manual %s"
              % (s["family"], s["wanted"], s["existed"], s["created"], s["verified"], s["gaps"], s["manual"]))
    if counts:
        print("  count probes: %d OK, %d expected-on-empty, %d suspicious, %d failed"
              % (sum(1 for c in counts if c["verdict"] == "OK"),
                 sum(1 for c in counts if c["verdict"] == "expected-on-empty-account"),
                 len(suspicious), sum(1 for c in counts if c["verdict"] == "count failed")))
    todos = ctx.log.by("manual_todo")
    if todos:
        print("  configure by hand (%d):" % len(todos))
        for e in todos[:40]:
            print("    - [%s] %s: %s" % (e["family"], e["object"], e["detail"][:160]))
        if len(todos) > 40:
            print("    ... %d more in the report" % (len(todos) - 40))
    print(verdict)
    if opts.report_path:
        atomic_json({"header": header, "summary": summary, "counts": counts, "log": ctx.log.entries,
                     "verdict": verdict, "exit": code}, opts.report_path + ".json")
        os.makedirs(os.path.dirname(opts.report_path) or ".", exist_ok=True)
        with open(opts.report_path + ".md", "w", encoding="utf-8") as f:
            f.write(render_markdown(header, summary, ctx.log, counts, verdict))
        print("report: %s.md" % opts.report_path)
    ctx.save_state()
    return code
