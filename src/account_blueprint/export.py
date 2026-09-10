"""Read a live DataSift account into a blueprint.

Reads every family through one client (the source account's Open API key,
which reaches all of them: verified 2026-09-10 against ty+2) and falls back
per family to a staff JWT if a route ever refuses the key. Never reads from
a cache: the old mirror's cache file is what hid an empty user index for a
month.
"""
from __future__ import annotations

import copy
import fnmatch
import time

from . import BLUEPRINT_VERSION
from .blueprint import JUNK_TAG_RE, UUID_RE, empty
from .client import MAP, ApiError
from .report import Log
from .translate import SourceIndex, preset_filters_to_refs, sequence_to_refs, ref

# Tags that define the system and are worth carrying even when no preset in
# the export happens to reference them on the day.
ANCHOR_TAGS = ("Courthouse Data", "Priority 1", "Priority 2", "FTM", "Tier 1", "Bulk Stacked",
               "Buy Box - *", "Auction Soon", "recently sold", "Mail Only", "Not Single Family",
               "Dispo Buyer", "Dispo Traced")


def _numbered(title: str) -> bool:
    return title[:3].rstrip(".").strip().isdigit()


# Keys the SAVED preset stores as relative windows (["36-months","month"])
# that the records search rejects ("Use YYYY-MM-DD"). The UI resolves them
# at query time; a count without them is an upper bound and is labelled so.
RELATIVE_DATE_KEYS = ("last_direct_mailed", "last_updated_date")


def count_shape(must: dict) -> tuple[dict, bool]:
    """(search-shaped must, approximate?) for a saved-preset `must`.

    Two differences between the saved and the searched shape: save nests
    ownerPropertiesOwned.show_properties, search wants it flat; and the
    relative date windows above are dropped. Best effort; never fatal."""
    m = copy.deepcopy(must)
    opo = m.pop("ownerPropertiesOwned", None)
    if isinstance(opo, dict):
        m.update(opo)
    m.pop("_ui", None)
    approx = False
    for k in RELATIVE_DATE_KEYS:
        if k in m:
            m.pop(k)
            approx = True
    return m, approx


class Exporter:
    def __init__(self, client, *, staff=None, log: Log | None = None, with_counts=True,
                 anchor_tags=ANCHOR_TAGS, label="ty+2", email="", account_hint=""):
        self.c, self.staff, self.log = client, staff, log or Log()
        self.with_counts, self.anchors = with_counts, anchor_tags
        self.label, self.email, self.account_hint = label, email, account_hint
        self.idx = SourceIndex()
        self.bp = empty()
        self.bp["source"]["label"] = label
        self.raw_tags: dict[str, str] = {}
        self.raw_lists: dict[str, str] = {}

    # ---- one family at a time, with the staff fallback
    def _family(self, name, fn):
        try:
            fn(self.c)
            self.bp["source"]["auth_by_family"][name] = self.c.kind
        except ApiError as e:
            if e.code in (401, 403) and self.staff is not None:
                print("  %s: %s under %s, retrying with staff JWT" % (name, e.code, self.c.kind))
                fn(self.staff)
                self.bp["source"]["auth_by_family"][name] = "staff_jwt"
            else:
                raise
        print("  exported %-16s %s" % (name, self._count(name)))

    def _count(self, name):
        fam = {"presets": lambda: sum(len(f["presets"]) for f in self.bp["preset_folders"]),
               "tags": lambda: len(self.raw_tags),
               "sequences": lambda: len(getattr(self, "_raw_sequences", [])),
               "siftmap": lambda: len(self.bp["siftmap_presets"]),
               "custom_fields": lambda: len(self.bp["custom_fields"]),
               "task_presets": lambda: len(self.bp["task_presets"])}.get(name)
        return fam() if fam else len(self.bp.get(name, []) or [])

    # ---- readers
    def read_statuses(self, c):
        rows = c.get_all("/api/internal/status/?limit=1000")
        self.bp["statuses"] = [{"title": x["title"], "color": x.get("color"),
                                "is_active": bool(x.get("is_active", True)),
                                "order": x.get("order"), "system": x.get("created_by") is None}
                               for x in rows]
        self.idx.statuses = {x["uuid"].lower(): x["title"] for x in rows}

    def read_lists(self, c):
        # Titles are stripped: ty+2 carries "Arrests " and the server treats it
        # as the same list as "Arrests" (400 on create), so the whitespace is a
        # data defect that must not enter the file.
        rows = c.get_all("/api/internal/list/?limit=999")
        for x in rows:
            x["title"] = (x.get("title") or "").strip()
        self.raw_lists = {x["uuid"].lower(): x["title"] for x in rows}
        self.idx.lists = dict(self.raw_lists)
        self.bp["lists"] = [{"title": x["title"]} for x in rows if not UUID_RE.match(x["title"])]
        for x in rows:
            if UUID_RE.match(x["title"]):
                self.log.add("lists", "dropped", x["title"], "uuid-titled junk list not exported")

    def read_tags(self, c):
        rows = c.get_all("/api/internal/tag/?offset=0&limit=10000&ordering=title")
        for x in rows:
            x["title"] = (x.get("title") or "").strip()
        self.raw_tags = {x["uuid"].lower(): x["title"] for x in rows}
        self.idx.tags = dict(self.raw_tags)

    def read_users(self, c):
        rows = c.get_all("/api/internal/account/user/?offset=0&limit=999")
        # First name only: it is the join key for assignee refs, and the file
        # is meant to be public. Last names and emails stay in the account.
        self.bp["users"] = [{"first_name": u.get("first_name"), "role": u.get("role"),
                             "is_active": bool(u.get("is_active"))} for u in rows]
        self.idx.users = {u["uuid"].lower(): (u.get("first_name") or "").strip() for u in rows}

    def read_custom_fields(self, c):
        groups = c.get_all("/api/internal/custom-fields/group/")
        self.bp["custom_field_groups"] = [{"title": g.get("label") or g.get("title"),
                                           "entity_type": g.get("entity_type", "property"),
                                           "position": g.get("position")} for g in groups]
        out = []
        for f in c.get_all("/api/internal/custom-fields/?limit=999"):
            if f.get("is_active") is False:
                continue
            g = f.get("group")
            out.append({"label": f["label"], "field_type": f["field_type"],
                        "entity_type": f.get("entity_type", "property"),
                        "group": (g.get("label") if isinstance(g, dict) else None),
                        "required": bool(f.get("required")), "placeholder": f.get("placeholder") or "",
                        "position": f.get("position"),
                        "options": [{"label": o["label"], "value": o.get("value") or o["label"]}
                                    for o in (f.get("options") or []) if o.get("is_active", True)]})
            self.idx.custom_fields[str(f.get("uuid") or "").lower()] = f["label"]
        self.bp["custom_fields"] = out

    def read_task_presets(self, c):
        groups = c.get_all("/api/internal/task-group/?offset=0&limit=999")
        self.bp["task_groups"] = [{"title": g["title"]} for g in groups]
        self.idx.task_groups = {g["uuid"].lower(): g["title"] for g in groups}
        out = []
        for g in groups:
            for p in c.get_all("/api/internal/task-group/%s/task-preset/?offset=0&limit=999" % g["uuid"]):
                self.idx.task_presets[p["uuid"].lower()] = (g["title"], p["title"])
                atu = p.get("assigned_to_user")
                out.append({
                    "group": g["title"], "title": p["title"], "notes": p.get("notes"),
                    "round_robin": bool(p.get("round_robin")), "expires_in": p.get("expires_in"),
                    "all_day": bool(p.get("all_day")), "due_time": p.get("due_time"),
                    "assigned_to_user": (ref("user", atu.get("first_name")) if isinstance(atu, dict) else None),
                    "assigned_to_users": [ref("user", self.idx.users.get(str(u).lower(), "?"))
                                          for u in (p.get("assigned_to_users") or [])
                                          if isinstance(u, str) and str(u).lower() in self.idx.users],
                    "assigned_to_role": p.get("assigned_to_role"),
                    "order": p.get("order"), "skip_weekends": bool(p.get("skip_weekends"))})
        self.bp["task_presets"] = out

    def read_boards(self, c):
        out = []
        for b in c.get_all("/api/internal/siftline/board/?offset=0&limit=999"):
            bt = b.get("title") or b.get("name")
            self.idx.boards[b["uuid"].lower()] = bt
            cols = c.get_all("/api/internal/siftline/board/%s/column/?offset=0&limit=999" % b["uuid"])
            cols.sort(key=lambda x: x.get("order") or 0)
            for col in cols:
                self.idx.columns[col["uuid"].lower()] = (bt, col.get("title") or col.get("name"))
            out.append({"title": bt, "columns": [col.get("title") or col.get("name") for col in cols]})
        self.bp["boards"] = out

    def read_sequences(self, c):
        folders = c.get_all("/api/internal/sequence-folder/?limit=999")
        self.bp["sequence_folders"] = [{"title": f["title"]} for f in folders]
        self.idx.sequence_folders = {f["uuid"].lower(): f["title"] for f in folders}
        self._raw_sequences = []
        for row in c.get_all("/api/internal/sequence/?limit=999"):
            if "conditions" not in row or "actions" not in row:
                row = c.call("/api/internal/sequence/%s/" % row["uuid"])
            self._raw_sequences.append(row)
        accts = {str(r.get("account_uuid") or "").lower() for r in self._raw_sequences}
        self._seq_accounts = {a for a in accts if a}
        emails = {((r.get("created_by") or {}).get("email") or "") for r in self._raw_sequences}
        self._seq_emails = {e for e in emails if e}

    def read_presets(self, c):
        folders = c.get_all("/api/internal/filter-preset-folder/?type=properties&limit=999")
        folders.sort(key=lambda f: (not _numbered(f["title"]), f["title"]))
        out, accounts = [], {}
        for f in folders:
            rows = c.get_all("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999" % f["uuid"])
            presets = []
            for p in rows:
                d = c.call("/api/internal/filter-preset/%s/" % p["uuid"])
                filt = d.get("filters") or {}
                acct = str(filt.get("account") or "").lower()
                if acct:
                    accounts[acct] = accounts.get(acct, 0) + 1
                entry = {"title": d.get("title"), "quick_filter": bool(d.get("quick_filter")),
                         "filters": preset_filters_to_refs(filt, self.idx, self.log, d.get("title")),
                         "source_count": None}
                if self.with_counts:
                    try:
                        shaped, approx = count_shape(filt.get("must") or {})
                        entry["source_count"] = c.search_count(shaped)
                        entry["source_count_approx"] = approx
                    except ApiError as e:
                        self.log.add("presets", "warn", d.get("title"),
                                     "source count failed: HTTP %s" % e.code)
                presets.append(entry)
            out.append({"title": f["title"], "type": f.get("type") or "properties",
                        "numbered": _numbered(f["title"]), "presets": presets})
            print("    %-40s %d presets" % (f["title"], len(presets)))
        self.bp["preset_folders"] = out
        self._preset_accounts = accounts

    def read_siftmap(self, c):
        out = []
        for f in c.get_all("/filters/?scope=account&page_size=100&page=1", base=MAP):
            if f.get("is_active") is False:
                continue
            fd = f.get("filter_data") or {}
            counties = sorted({a.get("county") or a.get("title") for a in (fd.get("addresses") or [])
                               if isinstance(a, dict)} - {None})
            out.append({"name": f["name"], "description": f.get("description") or "",
                        "auto_add_enabled": bool(f.get("auto_add_enabled")),
                        "replace_owners_enabled": bool(f.get("replace_owners_enabled")),
                        "email_enabled": bool(f.get("email_enabled")),
                        "lists": list(f.get("lists") or []), "tags": list(f.get("tags") or []),
                        "filter_data": fd, "is_favorite": bool(f.get("is_favorite")),
                        "limit": f.get("limit"), "limit_type": f.get("limit_type"),
                        "counties": counties})
        self.bp["siftmap_presets"] = out

    # ---- assembly
    def finish_sequences(self):
        keep = {"lists": {x["title"] for x in self.bp["lists"]},
                "tags": set()}   # filled after pruning below; remove-actions re-filtered then
        self.bp["sequences"] = [sequence_to_refs(q, self.idx, self.log, keep_titles=None)
                                for q in self._raw_sequences]

    def prune_tags(self):
        wanted: dict[str, list[str]] = {}

        def want(title, why):
            wanted.setdefault(title, [])
            if why not in wanted[title]:
                wanted[title].append(why)

        def walk(node, why):
            if isinstance(node, dict):
                if node.get("$ref") == "tag":
                    want(node["title"], why)
                    return
                for v in node.values():
                    walk(v, why)
            elif isinstance(node, list):
                for v in node:
                    walk(v, why)
        for fol in self.bp["preset_folders"]:
            for p in fol["presets"]:
                walk(p["filters"], "preset:" + p["title"])
        for q in self.bp["sequences"]:
            walk(q.get("conditions"), "sequence:" + q["title"])
            walk(q.get("actions"), "sequence:" + q["title"])
        for m in self.bp["siftmap_presets"]:
            for t in m.get("tags") or []:
                want(t, "siftmap:" + m["name"])
        titles = set(self.raw_tags.values())
        for pat in self.anchors:
            for t in titles:
                if fnmatch.fnmatchcase(t, pat):
                    want(t, "anchor")
        out = []
        for t, why in sorted(wanted.items()):
            if (JUNK_TAG_RE.search(t) or UUID_RE.match(t)) and why != ["anchor"]:
                self.log.add("tags", "dropped", t, "junk-shaped tag referenced by %s; not exported" % why)
                continue
            out.append({"title": t, "why": why})
        self.bp["tags"] = out
        # `remove` actions name lists/tags by TITLE; keep only what ships.
        keep = {"lists": {x["title"] for x in self.bp["lists"]}, "tags": {x["title"] for x in out}}
        for q in self.bp["sequences"]:
            for a in q.get("actions") or []:
                pl = a.get("payload") or {}
                if a.get("action") == "remove":
                    for key in ("lists", "tags"):
                        if isinstance(pl.get(key), list):
                            ok = [x for x in pl[key] if x in keep[key]]
                            if len(ok) != len(pl[key]):
                                self.log.add("sequences", "translated", q["title"],
                                             "remove action: dropped %d %s not carried by the blueprint"
                                             % (len(pl[key]) - len(ok), key))
                            pl[key] = ok
        self.log.add("tags", "warn", "pruning", "%d of %d source tags exported" % (len(out), len(titles)))

    def derive_source(self):
        claims = self.c.claims()
        cands = set()
        if claims.get("account"):
            cands.add(claims["account"].lower())
        if self._preset_accounts:
            cands.add(max(self._preset_accounts.items(), key=lambda kv: kv[1])[0])
        cands |= getattr(self, "_seq_accounts", set())
        if self.account_hint:
            cands.add(self.account_hint.lower())
        if len(cands) != 1:
            raise SystemExit("source account uuid is ambiguous: %s (presets say %s, sequences say %s)"
                             % (sorted(cands), self._preset_accounts, getattr(self, "_seq_accounts", None)))
        self.bp["source"]["account_uuid"] = cands.pop()
        self.bp["source"]["email"] = (self.email or claims.get("email")
                                      or (sorted(self._seq_emails)[0] if getattr(self, "_seq_emails", None) else ""))

    def run(self) -> dict:
        t0 = time.time()
        self._family("statuses", self.read_statuses)
        self._family("lists", self.read_lists)
        self._family("tags", self.read_tags)
        self._family("users", self.read_users)
        self._family("custom_fields", self.read_custom_fields)
        self._family("task_presets", self.read_task_presets)
        self._family("boards", self.read_boards)
        self._family("sequences", self.read_sequences)
        self._family("presets", self.read_presets)
        self._family("siftmap", self.read_siftmap)
        self.finish_sequences()
        self.prune_tags()
        self.derive_source()
        self.bp["blueprint_version"] = BLUEPRINT_VERSION
        self.bp["exported_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        n_pre = sum(len(f["presets"]) for f in self.bp["preset_folders"])
        neigh = sum(1 for f in self.bp["preset_folders"] for p in f["presets"]
                    if "any_neighborhood" in ((p["filters"].get("must") or {}).get("must_not") or {}))
        self.bp["warnings"] = [
            "%d presets carry market-specific must_not.any_neighborhood strings (apply strips them by default)" % neigh,
            "%d SiftMap presets carry the source market's county addresses (apply creates them with auto-add OFF)"
            % len(self.bp["siftmap_presets"]),
        ]
        self.bp["export_log"] = [e for e in self.log.entries
                                 if e["action"] in ("dropped", "gap", "manual_todo", "warn")]
        print("export: %d folders / %d presets, %d sequences, %d task presets, %d custom fields, "
              "%d siftmap presets, %d tags kept, %d lists, %d statuses in %.0fs (%d calls)"
              % (len(self.bp["preset_folders"]), n_pre, len(self.bp["sequences"]),
                 len(self.bp["task_presets"]), len(self.bp["custom_fields"]),
                 len(self.bp["siftmap_presets"]), len(self.bp["tags"]), len(self.bp["lists"]),
                 len(self.bp["statuses"]), time.time() - t0, getattr(self.c, "n_calls", 0)))
        return self.bp
