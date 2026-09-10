"""uuid -> $ref (export) and $ref -> uuid (apply) for preset filters and
sequence conditions/actions.

Two passes on export: a KEY-DRIVEN pass that knows what each filter or
payload key means (any_lists holds list uuids, updated_to under a column
condition holds a column uuid), then a SNIFFER over whatever is left that
replaces any remaining uuid-shaped string with the $ref its source index
knows, or marks it $unresolved. The sniffer is why a field name the hint
table has never seen cannot smuggle a ty+2 uuid into the blueprint.

The one thing the sniffer must not do is treat a uuid-LOOKING non-uuid as a
uuid: create-task payloads carry a "uuid" key whose value is a client
timestamp string, which is why every match goes through UUID_RE, never a key
name.
"""
from __future__ import annotations

import copy
import re

from .blueprint import UUID_RE, is_ref, is_unresolved, ref
from .client import ApiError, NetworkError

LIST_KEYS = ("any_lists", "all_lists")
TAG_KEYS = ("any_tags", "all_tags")
STATUS_KEYS = ("any_property_status",)
MARKET_KEYS = ("any_neighborhood",)   # plain strings, market-specific

SEQ_META = ("uuid", "created", "runs", "created_by", "account_uuid", "updated")
MANUAL_ACTIONS = ("send-sms", "send-email")


# ------------------------------------------------------------- export side

class SourceIndex:
    """uuid -> what it is, built from the source account's own listings."""

    def __init__(self):
        self.lists: dict[str, str] = {}
        self.tags: dict[str, str] = {}
        self.statuses: dict[str, str] = {}
        self.boards: dict[str, str] = {}
        self.columns: dict[str, tuple[str, str]] = {}      # col uuid -> (board, col)
        self.users: dict[str, str] = {}                     # uuid -> first_name
        self.task_presets: dict[str, tuple[str, str]] = {}  # uuid -> (group, title)
        self.task_groups: dict[str, str] = {}
        self.sequence_folders: dict[str, str] = {}
        self.custom_fields: dict[str, str] = {}

    def name(self, uuid: str) -> dict | None:
        u = (uuid or "").lower()
        if u in self.lists:
            return ref("list", self.lists[u])
        if u in self.tags:
            return ref("tag", self.tags[u])
        if u in self.columns:
            b, c = self.columns[u]
            return ref("column", c, board=b)
        if u in self.boards:
            return ref("board", self.boards[u])
        if u in self.task_presets:
            g, t = self.task_presets[u]
            return ref("task_preset", t, group=g)
        if u in self.task_groups:
            return ref("task_group", self.task_groups[u])
        if u in self.users:
            return ref("user", self.users[u])
        if u in self.statuses:
            return ref("status", self.statuses[u])
        if u in self.sequence_folders:
            return ref("sequence_folder", self.sequence_folders[u])
        if u in self.custom_fields:
            return ref("custom_field", self.custom_fields[u])
        return None


def _dedupe(vals):
    out, seen = [], set()
    for v in vals:
        k = repr(v)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _refs_for(kind, uuids, table, log, where, path):
    """uuid list -> ref list for a flat title table; unknowns dropped."""
    out = []
    for u in _as_list(uuids):
        if not isinstance(u, str):
            continue
        title = table.get(u.lower())
        if title is None:
            log.add("presets", "dropped", where, "%s: unknown %s uuid %s" % (path, kind, u))
            continue
        if UUID_RE.match(title):
            log.add("presets", "dropped", where, "%s: uuid-titled junk %s %s" % (path, kind, u))
            continue
        out.append(ref(kind, title))
    return _dedupe(out)


def sniff(node, idx: SourceIndex, log, family, where, path=""):
    """Replace every remaining uuid-shaped string with a $ref or $unresolved."""
    if is_ref(node) or is_unresolved(node):
        return node
    if isinstance(node, dict):
        return {k: sniff(v, idx, log, family, where, "%s.%s" % (path, k)) for k, v in node.items()}
    if isinstance(node, list):
        return [sniff(v, idx, log, family, where, "%s[%d]" % (path, i)) for i, v in enumerate(node)]
    if isinstance(node, str) and UUID_RE.match(node):
        r = idx.name(node)
        if r:
            log.add(family, "translated", where, "%s: %s -> %s %r" % (path, node[:8], r["$ref"], r["title"]))
            return r
        log.add(family, "gap", where, "%s: uuid %s not in the source index" % (path, node))
        return {"$unresolved": node, "path": path}
    return node


def preset_filters_to_refs(filters: dict, idx: SourceIndex, log, title: str) -> dict:
    src = copy.deepcopy(filters or {})
    src.pop("account", None)

    def walk(node, path):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                p = "%s.%s" % (path, k)
                if k in LIST_KEYS:
                    vals = _refs_for("list", v, idx.lists, log, title, p)
                    if vals:
                        out[k] = vals
                elif k in TAG_KEYS:
                    vals = _refs_for("tag", v, idx.tags, log, title, p)
                    if vals:
                        out[k] = vals
                elif k == "any_boards":
                    vals = []
                    for u in _as_list(v):
                        t = idx.boards.get(str(u).lower())
                        if t:
                            vals.append(ref("board", t))
                        else:
                            log.add("presets", "dropped", title, "%s: unknown board %s" % (p, u))
                    if vals:
                        out[k] = _dedupe(vals)
                elif k == "assigned_to":
                    for u in _as_list(v):
                        name = idx.users.get(str(u).lower())
                        if name:
                            out[k] = ref("user", name)
                        else:
                            log.add("presets", "gap", title, "%s: unknown user %s" % (p, u))
                            out[k] = {"$unresolved": str(u), "path": p}
                elif k in MARKET_KEYS:
                    out[k] = v
                else:
                    out[k] = walk(v, p)
            return out
        if isinstance(node, list):
            return [walk(v, "%s[%d]" % (path, i)) for i, v in enumerate(node)]
        return node

    out = walk(src, "filters")
    return sniff(out, idx, log, "presets", title, "filters")


def sequence_to_refs(seq: dict, idx: SourceIndex, log, keep_titles: dict | None = None) -> dict:
    """Portable sequence: metadata stripped, folder as title, uuids as refs,
    send-sms/send-email replaced by a $manual marker, absolute end_of_day
    dropped, `remove` action values filtered to titles the blueprint carries."""
    title = seq.get("title") or "?"
    q = {k: copy.deepcopy(v) for k, v in seq.items() if k not in SEQ_META}
    fol = q.get("folder")
    if isinstance(fol, dict):
        q["folder"] = fol.get("title")
    elif isinstance(fol, str) and UUID_RE.match(fol):
        q["folder"] = idx.sequence_folders.get(fol.lower())
    manual = []

    conds = []
    for c in q.get("conditions") or []:
        pl = c.get("payload") or {}
        if c.get("condition") == "has_all" and pl.get("field") == "tags_uuid":
            pl["values"] = _refs_for("tag", pl.get("values") or [], idx.tags, log, title,
                                     "conditions.has_all.values")
        if c.get("condition") == "from_to" and pl.get("field") == "column":
            meta = pl.get("meta") or {}
            if "boards" in meta:
                meta["boards"] = [idx.name(b) or {"$unresolved": b, "path": "meta.boards"}
                                  for b in _as_list(meta["boards"]) if isinstance(b, str)]
        conds.append(c)
    q["conditions"] = conds

    acts = []
    for a in q.get("actions") or []:
        kind = a.get("action")
        pl = a.get("payload") or {}
        if kind in MANUAL_ACTIONS:
            note = ("%s action removed: it carried the source account's own %s. "
                    "Re-add it in the UI with your integration, then activate."
                    % (kind, "sending number and recipients" if kind == "send-sms"
                       else "email integration and recipients"))
            manual.append(note)
            log.add("sequences", "manual_todo", title, note)
            acts.append({"action": kind, "$manual": True,
                         "kept": {k: pl[k] for k in ("subject", "message") if k in pl}})
            continue
        if kind == "create-task-by-preset":
            if "end_of_day" in pl:
                pl.pop("end_of_day")
                log.add("sequences", "translated", title,
                        "create-task-by-preset: dropped absolute end_of_day")
        if kind == "remove" and keep_titles:
            for key in ("lists", "tags"):
                if key in pl and isinstance(pl[key], list):
                    ok = [t for t in pl[key] if t in keep_titles.get(key, set())]
                    dropped = len(pl[key]) - len(ok)
                    if dropped:
                        log.add("sequences", "translated", title,
                                "remove action: dropped %d %s not carried by the blueprint"
                                % (dropped, key))
                    pl[key] = ok
        acts.append(a)
    q["actions"] = acts
    if manual:
        q["manual"] = manual
    return sniff(q, idx, log, "sequences", title, "sequence")


# -------------------------------------------------------------- apply side

DEFAULT_TZ = "America/New_York"


def end_of_day_iso(tz_name: str = DEFAULT_TZ, now=None) -> str:
    """The UI's value for create-task-by-preset.end_of_day: 23:59:59.999 of
    today in the action's timezone, expressed in UTC. The create route
    requires the field (verified live 2026-09-10, 400 "This field is
    required"), while the source GET carries it only on sequences saved
    recently, so apply always computes a fresh one."""
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:
        tz = _dt.timezone.utc
    now = now or _dt.datetime.now(tz)
    local = now.astimezone(tz)
    eod = local.replace(hour=23, minute=59, second=59, microsecond=999000)
    return eod.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "999Z"


class Registry:
    """title -> target uuid for every family, loaded from the TARGET account.

    resolve() creates lists and tags on demand when their family is selected
    for creation; every other kind is resolve-only and a miss is a gap.
    """

    def __init__(self, client, log, *, commit: bool, allowed_create: set[str],
                 self_user: str, account: str, user_map: dict | None = None):
        self.c, self.log, self.commit = client, log, commit
        self.allowed = allowed_create
        self.self_user, self.account = self_user, account
        self.user_map = {k.lower(): v for k, v in (user_map or {}).items()}
        self.lists: dict[str, str] = {}
        self.tags: dict[str, str] = {}
        self.statuses: dict[str, str] = {}          # exact title -> uuid
        self.status_titles: dict[str, str] = {}     # casefold -> exact title
        self.boards: dict[str, str] = {}
        self.columns: dict[tuple[str, str], str] = {}
        self.users: dict[str, str] = {}             # first_name casefold -> uuid
        self.user_names: dict[str, str] = {}        # uuid -> "First Last"
        self.task_groups: dict[str, str] = {}
        self.task_presets: dict[tuple[str, str], str] = {}
        self.sequence_folders: dict[str, str] = {}
        self.custom_fields: dict[str, dict] = {}
        self.custom_groups: dict[str, int] = {}
        self.dry_counter = 0

    # ---- loading
    def load(self) -> None:
        c = self.c
        self.lists = {x["title"]: x["uuid"] for x in c.get_all("/api/internal/list/?limit=999")}
        self.tags = {x["title"]: x["uuid"] for x in
                     c.get_all("/api/internal/tag/?offset=0&limit=10000&ordering=title")}
        self.load_statuses()
        self.load_boards()
        for u in c.get_all("/api/internal/account/user/?offset=0&limit=999"):
            fn = (u.get("first_name") or "").strip()
            if fn and (fn.lower() not in self.users or u.get("is_active")):
                self.users[fn.lower()] = u["uuid"]
            self.user_names[u["uuid"]] = ("%s %s" % (fn, u.get("last_name") or "")).strip()
        self.load_task_presets()
        self.sequence_folders = {x["title"]: x["uuid"] for x in
                                 c.get_all("/api/internal/sequence-folder/?limit=999")}
        self.load_custom_fields()

    def load_boards(self) -> None:
        self.boards, self.columns = {}, {}
        for b in self.c.get_all("/api/internal/siftline/board/?offset=0&limit=999"):
            bt = b.get("title") or b.get("name")
            self.boards[bt] = b["uuid"]
            for col in self.c.get_all("/api/internal/siftline/board/%s/column/?offset=0&limit=999" % b["uuid"]):
                self.columns[(bt, col.get("title") or col.get("name"))] = col["uuid"]

    def column_uuid(self, board: str, title: str) -> str | None:
        """Exact first, then case/whitespace-insensitive: the column create
        route enforces uniqueness that way (verified live: "Send Back to Lead
        Management" collided with ty+1's own spelling of it)."""
        u = self.columns.get((board, title))
        if u:
            return u
        key = (board, (title or "").strip().casefold())
        for (b, t), cu in self.columns.items():
            if (b, (t or "").strip().casefold()) == key:
                return cu
        return None

    def load_statuses(self) -> None:
        rows = self.c.get_all("/api/internal/status/?limit=1000")
        self.statuses = {x["title"]: x["uuid"] for x in rows}
        self.status_titles = {x["title"].casefold(): x["title"] for x in rows}
        self.status_rows = {x["title"]: x for x in rows}

    def load_task_presets(self) -> None:
        self.task_groups, self.task_presets = {}, {}
        for g in self.c.get_all("/api/internal/task-group/?offset=0&limit=999"):
            self.task_groups[g["title"]] = g["uuid"]
            for p in self.c.get_all("/api/internal/task-group/%s/task-preset/?offset=0&limit=999" % g["uuid"]):
                self.task_presets[(g["title"], p["title"])] = p["uuid"]

    def load_custom_fields(self) -> None:
        self.custom_fields = {f["label"]: f for f in
                              self.c.get_all("/api/internal/custom-fields/?limit=999")}
        self.custom_groups = {g.get("label") or g.get("title"): g["id"] for g in
                              self.c.get_all("/api/internal/custom-fields/group/")}

    # ---- creation on demand (lists, tags only)
    def _dry_uuid(self, kind, title):
        self.dry_counter += 1
        return "DRY-RUN-%s-%d" % (kind, self.dry_counter)

    def _same(self, kind: str, title: str) -> str | None:
        """The target's own form of `title`, if it already holds one.

        Lists: the server rejects a create whose title differs only by case
        or surrounding whitespace ("Arrests " vs "Arrests"), so those match.
        Tags: ty+2 legitimately holds both "Foreclosure" and "foreclosure",
        so only whitespace is forgiven and case stays significant.
        """
        pool = self.lists if kind == "list" else self.tags
        if title in pool:
            return title
        key = title.strip().casefold() if kind == "list" else title.strip()
        for t in pool:
            k = t.strip().casefold() if kind == "list" else t.strip()
            if k == key:
                return t
        return None

    def ensure(self, kind: str, title: str, where: str = "") -> str | None:
        pool = self.lists if kind == "list" else self.tags
        same = self._same(kind, title)
        if same is not None:
            if same != title:
                self.log.add(kind + "s", "translated", where or title, "%s %r -> target's %r" % (kind, title, same))
            return pool[same]
        family = kind + "s"
        if family not in self.allowed:
            self.log.add(family, "gap", where, "%s %r missing in target and family not selected"
                         % (kind, title))
            return None
        if not self.commit:
            pool[title] = self._dry_uuid(kind, title)
            self.log.add(family, "created", title, "(dry run)")
            return pool[title]
        listing = ("/api/internal/list/?limit=999" if kind == "list"
                   else "/api/internal/tag/?offset=0&limit=10000&ordering=title")
        try:
            self.c.call("/api/internal/%s/" % kind, "POST", {"title": title})
        except ApiError as e:
            if e.code == 400 and "unique" in e.body.lower():
                # The server knows a form of this title the listing did not
                # show us the same way. Re-list and adopt it rather than fail.
                rows = self.c.get_all(listing)
                pool.clear()
                pool.update({x["title"]: x["uuid"] for x in rows})
                same = self._same(kind, title)
                if same is not None:
                    self.log.add(family, "translated", where or title,
                                 "%s %r exists as %r (server unique rule)" % (kind, title, same))
                    return pool[same]
            raise
        except NetworkError as e:
            # Lists and tags have no unique-title constraint, so a blind retry
            # after a dropped connection creates two. Look before retrying.
            if not any(x["title"] == title for x in self.c.get_all(listing)):
                raise
            self.log.add(family, "warn", title, "network error after POST but object present: %s" % e)
        # Read back by re-listing: the response uuid alone does not prove the
        # server stored the title we sent.
        rows = self.c.get_all(listing)
        match = [x for x in rows if x["title"] == title]
        if not match:
            self.log.add(family, "mismatch", title, "created but not present on re-list")
            return None
        pool[title] = match[0]["uuid"]
        self.log.add(family, "created", title, "uuid %s" % pool[title][:8])
        return pool[title]

    # ---- resolution
    def status_title(self, s: str) -> str | None:
        if s in self.statuses:
            return s
        return self.status_titles.get((s or "").casefold())

    def resolve(self, r: dict, where: str, family: str, *, quiet: bool = False) -> str | None:
        """quiet: the caller has its own fallback (an assignee placeholder), so
        a miss is not a gap and must not be logged as one."""
        kind, title = r.get("$ref"), r.get("title")
        if kind == "self":
            return self.self_user
        if kind in ("list", "tag"):
            return self.ensure(kind, title, where)
        if kind == "board":
            u = self.boards.get(title)
        elif kind == "column":
            u = self.column_uuid(r.get("board"), title)
        elif kind == "user":
            u = self.user_map.get((title or "").lower()) or self.users.get((title or "").lower())
            if u and u not in self.user_names and not UUID_RE.match(u):
                # --user-map gave a NAME, not a uuid: resolve it
                u = self.users.get(u.lower())
        elif kind == "task_preset":
            u = self.task_presets.get((r.get("group"), title))
        elif kind == "task_group":
            u = self.task_groups.get(title)
        elif kind == "sequence_folder":
            u = self.sequence_folders.get(title)
        elif kind == "status":
            t = self.status_title(title)
            u = self.statuses.get(t) if t else None
        elif kind == "custom_field":
            f = self.custom_fields.get(title)
            u = (f.get("uuid") or f.get("id")) if f else None
        else:
            u = None
        if not u and not quiet:
            self.log.add(family, "gap", where, "%s %r not found in target" % (kind, r.get("board", "") + "/" + title if kind == "column" else title))
        return u


def refs_to_uuids(node, reg: Registry, log, where: str, family: str, *,
                  assign_fallback: str = "self", strip_neighborhoods: bool = True,
                  path: str = "") -> tuple:
    """Resolve every $ref under node. Returns (node, dropped_count).

    A ref inside a list that fails to resolve is removed from the list; a list
    that becomes empty removes its key; a ref in a scalar slot that fails
    removes the key. assigned_to falls back to the applying user (logged as a
    placeholder) so the preset still exists, since a missing preset breaks
    the folder structure while a placeholder assignee is a visible TODO.
    """
    dropped = 0
    if is_unresolved(node):
        log.add(family, "dropped", where, "%s: unresolved source uuid dropped" % (path or node.get("path")))
        return None, 1
    if is_ref(node):
        u = reg.resolve(node, where, family)
        return (u, 0) if u else (None, 1)
    if isinstance(node, dict):
        out = {}
        # A sequence's property-assign action carries the user under `value`
        # with `field: "assigned_to"` beside it; same fallback as a preset's
        # assigned_to key.
        assignee_keys = {"assigned_to"}
        if node.get("field") == "assigned_to" and is_ref(node.get("value")):
            assignee_keys.add("value")
        for k, v in node.items():
            p = "%s.%s" % (path, k) if path else k
            if k in MARKET_KEYS and strip_neighborhoods:
                log.add(family, "translated", where, "%s: stripped %d market-specific values"
                        % (p, len(v) if isinstance(v, list) else 1))
                continue
            if k in assignee_keys and (is_ref(v) or is_unresolved(v)):
                u = reg.resolve(v, where, family, quiet=(assign_fallback == "self")) if is_ref(v) else None
                if not u:
                    if assign_fallback == "self" and reg.self_user:
                        u = reg.self_user
                        log.add(family, "placeholder", where,
                                "assigned_to %r -> the applying user (placeholder)"
                                % (v.get("title") if is_ref(v) else "?"))
                        log.add(family, "manual_todo", where,
                                "set the assignee (source had %r)" % (v.get("title") if is_ref(v) else "?"))
                    else:
                        dropped += 1
                        continue
                out[k] = u
                continue
            if k in STATUS_KEYS and isinstance(v, list):
                vals = []
                for s in v:
                    t = reg.status_title(s) if isinstance(s, str) else None
                    if t is None:
                        log.add(family, "warn", where, "%s: status %r not in target, kept verbatim" % (p, s))
                        vals.append(s)
                    else:
                        if t != s:
                            log.add(family, "translated", where, "%s: status %r -> %r" % (p, s, t))
                        vals.append(t)
                out[k] = vals
                continue
            nv, d = refs_to_uuids(v, reg, log, where, family, assign_fallback=assign_fallback,
                                  strip_neighborhoods=strip_neighborhoods, path=p)
            dropped += d
            if nv is None and (is_ref(v) or is_unresolved(v)):
                continue
            if isinstance(v, list) and isinstance(nv, list) and not nv and v:
                continue
            out[k] = nv
        return out, dropped
    if isinstance(node, list):
        out = []
        for i, v in enumerate(node):
            nv, d = refs_to_uuids(v, reg, log, where, family, assign_fallback=assign_fallback,
                                  strip_neighborhoods=strip_neighborhoods, path="%s[%d]" % (path, i))
            dropped += d
            if nv is None and (is_ref(v) or is_unresolved(v)):
                continue
            out.append(nv)
        return out, dropped
    return node, 0


def sequence_to_uuids(seq: dict, reg: Registry, log, *, assign_fallback="self",
                      stub_inactive=False) -> dict | None:
    """Target-side sequence body, or None when it must not be created.

    A reference that fails inside the TRIGGER CONDITIONS (a board or column
    the card must move to) means the sequence can never fire correctly, so
    it is skipped with a gap naming the missing title. A reference that fails
    inside an ACTION also skips it by default; with stub_inactive the sequence
    is created INACTIVE minus that action, with a TODO, so the operator can
    finish it in the UI. $manual actions (send-sms/send-email) are always
    dropped and always force INACTIVE, because a sequence missing its
    notification step is safe to have but is not what the source intended.
    """
    title = seq.get("title") or "?"
    body = {k: copy.deepcopy(v) for k, v in seq.items() if k not in ("manual",)}
    folder_uuid = reg.sequence_folders.get(body.get("folder") or "")
    if body.get("folder") and not folder_uuid:
        fallback = reg.sequence_folders.get("default")
        log.add("sequences", "gap" if not fallback else "translated", title,
                "sequence folder %r missing in target%s"
                % (body["folder"], "; using 'default'" if fallback else ""))
        folder_uuid = fallback
    downgraded = False
    conds, d_conds = refs_to_uuids(body.get("conditions") or [], reg, log, title, "sequences",
                                   assign_fallback=assign_fallback, strip_neighborhoods=False,
                                   path="conditions")
    if d_conds:
        log.add("sequences", "gap", title,
                "%d trigger reference(s) did not resolve; sequence NOT created" % d_conds)
        return None
    acts = []
    for i, a in enumerate(body.get("actions") or []):
        if a.get("$manual"):
            downgraded = True
            log.add("sequences", "manual_todo", title,
                    "re-add the %s action with your own integration, then activate" % a.get("action"))
            continue
        na, d = refs_to_uuids(a, reg, log, title, "sequences", assign_fallback=assign_fallback,
                              strip_neighborhoods=False, path="actions[%d]" % i)
        if d:
            if not stub_inactive:
                log.add("sequences", "gap", title,
                        "action %r reference(s) did not resolve; sequence NOT created" % a.get("action"))
                return None
            downgraded = True
            log.add("sequences", "manual_todo", title,
                    "action %r dropped (reference missing in target); finish it in the UI, then activate"
                    % a.get("action"))
            continue
        acts.append(na)
    # set-field-value status values are stored lowercase in sequences
    for a in acts:
        pl = a.get("payload") or {}
        if a.get("action") == "create-task-by-preset":
            pl.setdefault("timezone", DEFAULT_TZ)
            if not pl.get("end_of_day"):
                pl["end_of_day"] = end_of_day_iso(pl["timezone"])
            a["payload"] = pl
        if a.get("action") == "set-field-value" and str(pl.get("field", "")).endswith("status"):
            t = reg.status_title(str(pl.get("value")))
            if t is None:
                log.add("sequences", "warn", title, "status %r not in target, kept verbatim" % pl.get("value"))
    out = {"title": title, "is_active": bool(body.get("is_active")) and not downgraded,
           "trigger": body.get("trigger"), "conditions": conds, "actions": acts}
    if folder_uuid:
        out["folder"] = folder_uuid
    if downgraded and body.get("is_active"):
        log.add("sequences", "translated", title, "created INACTIVE (an action was removed)")
    return out


def subset_equal(sent, got) -> bool:
    """True when everything in `sent` is present and equal in `got`.
    Servers add uuid/created/expanded fields; they must never DROP a key."""
    if isinstance(sent, dict):
        if not isinstance(got, dict):
            return False
        return all(k in got and subset_equal(v, got[k]) for k, v in sent.items())
    if isinstance(sent, list):
        if not isinstance(got, list) or len(sent) != len(got):
            return False
        return all(subset_equal(a, b) for a, b in zip(sent, got))
    if isinstance(sent, str) and isinstance(got, str):
        return sent == got or (UUID_RE.match(sent) and sent.lower() == got.lower())
    return sent == got
