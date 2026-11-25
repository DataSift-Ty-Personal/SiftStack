"""Manage DataSift filter presets and preset folders on your Open API key.

    python manage_presets.py --discover                          # every folder and its presets (read only)
    python manage_presets.py --discover --folder "00 Niche Sequential Marketing"
    python manage_presets.py --add-sold-exclusion                # dry: what would change, nothing sent
    python manage_presets.py --add-sold-exclusion --commit       # adds it, reads each preset back
    python manage_presets.py --create plan.json                  # dry: the folders and presets it would make
    python manage_presets.py --create plan.json --commit         # makes them, reads each one back
    python manage_presets.py --undo output/preset_changes_<stamp>.jsonl --commit
    python manage_presets.py --selftest                          # the rules on invented data, no network

Your key: REISIFT_API_KEY, your DataSift Open API key (see datasift_core.py).

Every write is dry until --commit. With --commit each write is read back and must equal what was
sent, or the run stops there. Every write is logged with what it replaced under --output-dir, and
--undo puts a Sold exclusion back the way it was. A preset title is unique in an account, so a plan
whose title already exists with a different filter is refused rather than overwritten.

A plan (--create) is JSON. Tags and lists are written by TITLE, statuses the way DataSift shows them;
the script turns titles into the account's own ids and statuses into their stored spelling.
`must_not` sits INSIDE `must`. Every preset gets the Sold exclusion unless it reads Sold on purpose.

    {"folders": [{"title": "00 Niche Sequential Marketing",
                  "presets": [{"title": "00. Needs Skip Traced",
                               "must": {"any_tags": ["Courthouse Data"], "skiptraced": 0}}]}]}
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datasift_core import ApiError, DataSiftApi  # noqa: E402

FOLDERS = "/api/internal/filter-preset-folder/?type=properties&limit=999"
FOLDER_PRESETS = "/api/internal/filter-preset-folder/%s/filter-preset/?limit=999"
FOLDER_NEW = "/api/internal/filter-preset-folder/"
FOLDER_ONE = "/api/internal/filter-preset-folder/%s/"
PRESET_NEW = "/api/internal/filter-preset/"
PRESET_ONE = "/api/internal/filter-preset/%s/"
TAGS = "/api/internal/tag/?offset=0&limit=10000&ordering=title"
LISTS = "/api/internal/list/?offset=0&limit=10000"
STATUSES = "/api/internal/status/?limit=999"
PIPELINE_STATUSES = "/api/internal/global-status/?limit=999"
USER = "/api/internal/user/"
RECORDS = "/api/internal/property/"

# The two folders this skill builds. Folder titles vary in case and punctuation across accounts.
SEQUENTIAL_FOLDERS = ("00 Niche Sequential Marketing", "01. Bulk Sequential Marketing")
TITLE_KEYS = {"any_tags": "tag", "all_tags": "tag", "any_lists": "list", "all_lists": "list"}
STATUS_KEYS = ("any_property_status",)
SOLD = "sold"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class Refused(SystemExit):
    """The run stops here and says why; nothing after it is sent."""


def rows_of(r) -> list:
    if isinstance(r, list):
        return r
    return (r.get("results") or r.get("data") or []) if isinstance(r, dict) else []


def fold(s) -> str:
    """Compare titles the way a person reads them: case, spaces, dots and underscores do not matter."""
    return re.sub(r"[\s._]+", " ", str(s or "")).strip().casefold()


def same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class Account:
    """What the account holds, read once a run (GET only)."""

    def __init__(self, api):
        self.api = api
        self._folders = None
        self._presets = {}
        self._tags = self._lists = self._statuses = None
        self._account = None

    def folders(self) -> list:
        if self._folders is None:
            self._folders = rows_of(self.api.get(FOLDERS))
        return self._folders

    def presets(self, folder: dict) -> list:
        if folder["uuid"] not in self._presets:
            self._presets[folder["uuid"]] = rows_of(self.api.get(FOLDER_PRESETS % folder["uuid"]))
        return self._presets[folder["uuid"]]

    def folder(self, title: str):
        return next((f for f in self.folders() if fold(f.get("title")) == fold(title)), None)

    def titles(self, kind: str) -> dict:
        """fold(title) -> uuid for tags or lists."""
        attr = "_tags" if kind == "tag" else "_lists"
        if getattr(self, attr) is None:
            rows = rows_of(self.api.get(TAGS if kind == "tag" else LISTS))
            setattr(self, attr, {fold(r.get("title")): r.get("uuid") for r in rows if r.get("uuid")})
        return getattr(self, attr)

    def statuses(self) -> dict:
        """fold(status) -> the spelling the account stores ("Not Interested" -> "not_interested")."""
        if self._statuses is None:
            try:
                rows = rows_of(self.api.get(STATUSES))
            except ApiError:
                rows = rows_of(self.api.get(PIPELINE_STATUSES))
            self._statuses = {}
            for r in rows:
                stored = r.get("title") or r.get("value") or r.get("status") or ""
                if stored:
                    self._statuses[fold(stored)] = stored
        return self._statuses

    def account_id(self) -> str:
        """The account's own id, which every saved preset carries in filters.account."""
        if self._account is None:
            for f in self.folders():
                for p in self.presets(f):
                    acct = (p.get("filters") or {}).get("account")
                    if acct:
                        self._account = acct
                        return acct
            me = self.api.get(USER)
            me = me[0] if isinstance(me, list) and me else me
            acct = (me or {}).get("account")
            self._account = (acct.get("uuid") if isinstance(acct, dict) else acct) or ""
        return self._account


# ---- discover ----
def skips_sold(must: dict) -> bool:
    return SOLD in {str(s).lower() for s in ((must or {}).get("must_not") or {}).get("any_property_status") or []}


def reads_sold(must: dict) -> bool:
    return SOLD in {str(s).lower() for s in (must or {}).get("any_property_status") or []}


def chosen_folders(acc: Account, names: list[str], every: bool) -> list:
    if every:
        return list(acc.folders())
    wanted = names or list(SEQUENTIAL_FOLDERS)
    found = [f for f in acc.folders() if any(fold(f.get("title")) == fold(n) for n in wanted)]
    if names:
        missing = [n for n in names if not any(fold(f.get("title")) == fold(n) for f in acc.folders())]
        if missing:
            raise Refused("no preset folder titled %s. The folders are: %s" % (
                ", ".join(repr(m) for m in missing), ", ".join(str(f.get("title")) for f in acc.folders())))
    return found


def discover(acc: Account, names: list[str], out=print) -> dict:
    result = {}
    folders = chosen_folders(acc, names, every=not names)
    for f in folders:
        presets = acc.presets(f)
        result[f.get("title")] = [p.get("title") for p in presets]
        out(f"\n{f.get('title')}  ({len(presets)} presets)")
        for p in presets:
            must = (p.get("filters") or {}).get("must") or {}
            mark = "skips Sold" if skips_sold(must) else ("reads Sold" if reads_sold(must) else "does NOT skip Sold")
            out(f"  - {p.get('title')}  [{mark}]")
    return result


# ---- the Sold exclusion ----
def plan_sold(acc: Account, names: list[str], every: bool) -> dict:
    stored = acc.statuses().get(fold(SOLD), SOLD)
    folders = chosen_folders(acc, names, every)
    plan = {"stored": stored, "add": [], "already": [], "on_purpose": [], "folders": [f.get("title") for f in folders]}
    for f in folders:
        for p in acc.presets(f):
            must = (p.get("filters") or {}).get("must") or {}
            if reads_sold(must):
                plan["on_purpose"].append(p.get("title"))
            elif skips_sold(must):
                plan["already"].append(p.get("title"))
            else:
                after = copy.deepcopy(must)
                after.setdefault("must_not", {}).setdefault("any_property_status", []).append(stored)
                plan["add"].append({"uuid": p["uuid"], "title": p.get("title"), "folder": f.get("title"),
                                    "before_must": must, "after_must": after})
    return plan


def apply_sold(api, plan: dict, *, commit: bool, log_path: Path, out=print) -> dict:
    done = {"patched": [], "already": []}
    if not commit:
        out(f"DRY: nothing sent. A real run adds the Sold exclusion to {len(plan['add'])} preset(s). Add --commit.")
        return done
    for e in plan["add"]:
        path = PRESET_ONE % e["uuid"]
        live = api.get(path) or {}
        filters = copy.deepcopy(live.get("filters") or {})
        if same(filters.get("must") or {}, e["after_must"]):
            done["already"].append(e["title"])
            continue
        if not same(filters.get("must") or {}, e["before_must"]):
            raise Refused(f"stopped: {e['title']!r} changed since the plan was read; run again")
        filters["must"] = e["after_must"]              # a PATCH replaces the whole filters object
        api.patch(path, {"filters": filters})
        back = api.get(path) or {}
        if not same((back.get("filters") or {}).get("must") or {}, e["after_must"]):
            raise Refused(f"stopped: {e['title']!r} reads back differently from what was sent; "
                          "nothing after it was touched")
        log(log_path, {"step": "sold_exclusion", "uuid": e["uuid"], "title": e["title"],
                       "before_filters": live.get("filters") or {}, "after_must": e["after_must"]})
        done["patched"].append(e["title"])
        out(f"patched  {e['title']}")
    return done


# ---- create from a plan ----
def resolve_must(acc: Account, must: dict, where: str, problems: list) -> dict:
    """The plan's must with tag and list titles turned into ids and statuses into their stored spelling."""
    out = {}
    for key, value in (must or {}).items():
        if key == "must_not":
            out[key] = resolve_must(acc, value, where + " (must_not)", problems)
        elif key in TITLE_KEYS:
            kind, ids = TITLE_KEYS[key], []
            for title in value or []:
                if isinstance(title, dict):            # {"$ref": "tag", "title": ...}, the blueprint form
                    title = title.get("title")
                if isinstance(title, str) and UUID_RE.match(title):
                    ids.append(title)
                    continue
                uid = acc.titles(kind).get(fold(title))
                if uid:
                    ids.append(uid)
                else:
                    problems.append(f"{where}: there is no {kind} called {title!r} in this account; create it first")
            out[key] = ids
        elif key in STATUS_KEYS:
            vals = []
            for s in value or []:
                stored = acc.statuses().get(fold(s))
                if stored:
                    vals.append(stored)
                else:
                    problems.append(f"{where}: there is no status called {s!r} in this account")
            out[key] = vals
        else:
            out[key] = copy.deepcopy(value)
    return out


def plan_create(acc: Account, doc: dict) -> dict:
    problems, folders, presets, already, notes = [], [], [], [], []
    existing = {}
    for f in acc.folders():
        for p in acc.presets(f):
            existing[fold(p.get("title"))] = dict(p, folder_title=f.get("title"))
    seen = set()
    stored_sold = acc.statuses().get(fold(SOLD), SOLD)
    for fspec in doc.get("folders") or []:
        ftitle = (fspec.get("title") or "").strip()
        if not ftitle:
            problems.append("a folder in the plan has no title")
            continue
        live = acc.folder(ftitle)
        if live is None and fold(ftitle) not in {fold(x["title"]) for x in folders}:
            folders.append({"title": ftitle})
        for pspec in fspec.get("presets") or []:
            title = (pspec.get("title") or "").strip()
            if not title:
                problems.append(f"{ftitle}: a preset has no title")
                continue
            if fold(title) in seen:
                problems.append(f"{title!r} is in the plan twice; a preset title is unique in an account")
                continue
            seen.add(fold(title))
            must = resolve_must(acc, pspec.get("must") or {}, title, problems)
            if not reads_sold(must) and not skips_sold(must):
                must.setdefault("must_not", {}).setdefault("any_property_status", []).append(stored_sold)
                notes.append(f"{title}: added the Sold exclusion")
            there = existing.get(fold(title))
            if there is not None:
                if same((there.get("filters") or {}).get("must") or {}, must):
                    already.append({"title": title, "folder": there["folder_title"], "uuid": there.get("uuid")})
                else:
                    problems.append(f"a preset named {there.get('title')!r} already exists (in "
                                    f"{there['folder_title']}) with a different filter; it is left alone")
                continue
            presets.append({"title": title, "folder": ftitle, "must": must})
    return {"folders": folders, "presets": presets, "already": already, "problems": problems, "notes": notes}


def apply_create(api, acc: Account, plan: dict, *, commit: bool, log_path: Path, out=print) -> dict:
    done = {"folders": [], "presets": []}
    if plan["problems"]:
        raise Refused("nothing sent; fix these first:\n  " + "\n  ".join(plan["problems"]))
    if not commit:
        out(f"DRY: nothing sent. A real run makes {len(plan['folders'])} folder(s) and "
            f"{len(plan['presets'])} preset(s). Add --commit.")
        return done
    account = acc.account_id()
    if not account:
        raise Refused("could not read this account's id, so a preset cannot be made; nothing sent")
    ids = {fold(f.get("title")): f.get("uuid") for f in acc.folders()}
    for f in plan["folders"]:
        try:
            made = api.post(FOLDER_NEW, {"title": f["title"], "type": "properties", "permissions": []}) or {}
        except ApiError as exc:
            if exc.code not in (0, 502, 503, 504):
                raise
            made = {}                                  # it may have landed: look for it by title
        acc._folders = None
        uid = made.get("uuid") or (acc.folder(f["title"]) or {}).get("uuid")
        if not uid or fold((api.get(FOLDER_ONE % uid) or {}).get("title")) != fold(f["title"]):
            raise Refused(f"stopped: the folder {f['title']!r} was sent but does not read back")
        ids[fold(f["title"])] = uid
        log(log_path, {"step": "folder", "uuid": uid, "title": f["title"]})
        done["folders"].append(f["title"])
        out(f"made folder  {f['title']}")
    for p in plan["presets"]:
        folder_uuid = ids.get(fold(p["folder"]))
        body = {"title": p["title"], "folder": folder_uuid, "quick_filter": False, "type": "properties",
                "filters": {"must": p["must"], "account": account}}
        try:
            made = api.post(PRESET_NEW, body) or {}
        except ApiError as exc:
            if exc.code not in (0, 502, 503, 504):
                raise
            made = {}
        uid = made.get("uuid") if isinstance(made, dict) else None
        if not uid:
            uid = next((r.get("uuid") for r in rows_of(api.get(FOLDER_PRESETS % folder_uuid))
                        if fold(r.get("title")) == fold(p["title"])), None)
        back = api.get(PRESET_ONE % uid) if uid else {}
        if fold((back or {}).get("title")) != fold(p["title"]) or \
                not same(((back or {}).get("filters") or {}).get("must") or {}, p["must"]):
            raise Refused(f"stopped: {p['title']!r} was sent but reads back differently (id {uid}); "
                          "look at it in the app")
        log(log_path, {"step": "preset", "uuid": uid, "title": p["title"], "folder": p["folder"]})
        done["presets"].append(p["title"])
        out(f"made preset  {p['folder']} / {p['title']}")
    return done


def count_records(api, must: dict):
    """How many records a filter holds today, or None when the search cannot read it (a saved preset's
    relative date windows, for one, are resolved by the app, not by the search)."""
    try:
        r = api.get(RECORDS, {"query": json.dumps({"must": must}), "limit": 1})
        return int(r.get("count")) if isinstance(r, dict) and r.get("count") is not None else None
    except (ApiError, ValueError, TypeError):
        return None


# ---- the change log and undo ----
def log(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(dict(row, at=datetime.now().isoformat(timespec="seconds"))) + "\n")


def undo(api, log_file: Path, *, commit: bool, out=print) -> dict:
    """Put each Sold exclusion in the log back the way it was, while the preset still reads as this
    script left it. Folders and presets a --create made are listed: delete those in the app."""
    rows = [json.loads(l) for l in Path(log_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    done = {"restored": [], "left": [], "made": []}
    for row in rows:
        if row.get("step") in ("folder", "preset"):
            done["made"].append(f"{row['step']} {row['title']}")
            continue
        if row.get("step") != "sold_exclusion":
            continue
        path = PRESET_ONE % row["uuid"]
        live = api.get(path) or {}
        if not same((live.get("filters") or {}).get("must") or {}, row["after_must"]):
            done["left"].append(row["title"])
            out(f"left alone (changed since): {row['title']}")
            continue
        if commit:
            api.patch(path, {"filters": row["before_filters"]})
            back = api.get(path) or {}
            if not same((back.get("filters") or {}).get("must") or {},
                        (row["before_filters"] or {}).get("must") or {}):
                raise Refused(f"stopped: {row['title']!r} did not read back as it was")
        done["restored"].append(row["title"])
        out(f"{'restored' if commit else 'would restore'}  {row['title']}")
    if done["made"]:
        out("Made by this log (delete in the app if you want them gone): " + "; ".join(done["made"]))
    if not commit:
        out("DRY: nothing sent. Add --commit.")
    return done


# ---- selftest: the rules on an invented account, no network ----
class FakeApi:
    def __init__(self):
        self.calls, self.n = [], 0
        self.folders = [{"uuid": "f-niche", "title": "00 NICHE SEQUENTIAL MARKETING", "type": "properties"},
                        {"uuid": "f-other", "title": "Other", "type": "properties"}]
        acct = "acct-1"
        self.presets = {
            "p1": {"uuid": "p1", "folder": "f-niche", "title": "00. Needs Skip Traced",
                   "filters": {"account": acct, "must": {"any_tags": ["t-court"], "skiptraced": 0}}},
            "p2": {"uuid": "p2", "folder": "f-niche", "title": "01. Ready to Text",
                   "filters": {"account": acct, "must": {"phone": 1, "must_not": {"any_property_status": ["sold"]}}}},
            "p3": {"uuid": "p3", "folder": "f-niche", "title": "Sold Review",
                   "filters": {"account": acct, "must": {"any_property_status": ["sold"]}}},
            "p4": {"uuid": "p4", "folder": "f-other", "title": "Untouched", "filters": {"account": acct, "must": {}}},
        }
        self.tags = [{"uuid": "t-court", "title": "Courthouse Data"}, {"uuid": "t-dm", "title": "Do Not Market"}]
        self.lists = [{"uuid": "l-pro", "title": "Probate"}]
        self.statuses = [{"title": t} for t in ("sold", "not_interested", "Hot Lead", "new_lead")]

    def _rows(self, rows):
        return {"count": len(rows), "results": copy.deepcopy(rows)}

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path == FOLDERS:
            return self._rows(self.folders)
        m = re.match(r"^/api/internal/filter-preset-folder/([^/]+)/filter-preset/", path)
        if m:
            return self._rows([p for p in self.presets.values() if p["folder"] == m.group(1)])
        m = re.match(r"^/api/internal/filter-preset-folder/([^/?]+)/$", path)
        if m:
            return copy.deepcopy(next(f for f in self.folders if f["uuid"] == m.group(1)))
        m = re.match(r"^/api/internal/filter-preset/([^/?]+)/$", path)
        if m:
            return copy.deepcopy(self.presets[m.group(1)])
        if path == TAGS:
            return self._rows(self.tags)
        if path == LISTS:
            return self._rows(self.lists)
        if path == STATUSES:
            return self._rows(self.statuses)
        if path == USER:
            return {"account": "acct-1"}
        raise AssertionError("unexpected GET " + path)

    def post(self, path, body):
        self.calls.append(("POST", path))
        self.n += 1
        if path == FOLDER_NEW:
            f = {"uuid": f"f-new{self.n}", "title": body["title"], "type": body["type"]}
            self.folders.append(f)
            return copy.deepcopy(f)
        if path == PRESET_NEW:
            if any(fold(p["title"]) == fold(body["title"]) for p in self.presets.values()):
                raise ApiError(400, "POST", path, "The fields title must make a unique set.")
            uid = f"p-new{self.n}"
            self.presets[uid] = {"uuid": uid, "folder": body["folder"], "title": body["title"],
                                 "filters": copy.deepcopy(body["filters"])}
            return {"uuid": uid}
        raise AssertionError("unexpected POST " + path)

    def patch(self, path, body):
        self.calls.append(("PATCH", path))
        uid = re.match(r"^/api/internal/filter-preset/([^/]+)/$", path).group(1)
        self.presets[uid]["filters"] = copy.deepcopy(body["filters"])
        return copy.deepcopy(self.presets[uid])


def selftest() -> int:
    import tempfile
    results = []

    def check(label, got, want):
        results.append((label, got == want, got, want))

    quiet = lambda *_: None  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        logp = Path(tmp) / "changes.jsonl"
        api = FakeApi()
        acc = Account(api)
        found = discover(acc, [], out=quiet)
        check("discover lists every folder", list(found), ["00 NICHE SEQUENTIAL MARKETING", "Other"])
        one = discover(Account(api), ["00 Niche Sequential Marketing"], out=quiet)
        check("a folder named in any case is found", list(one), ["00 NICHE SEQUENTIAL MARKETING"])

        plan = plan_sold(Account(api), [], every=False)
        check("Sold exclusion planned only where missing", [e["title"] for e in plan["add"]], ["00. Needs Skip Traced"])
        check("a preset that already skips Sold is left alone", plan["already"], ["01. Ready to Text"])
        check("a preset that reads Sold on purpose is left alone", plan["on_purpose"], ["Sold Review"])
        dry = apply_sold(api, plan, commit=False, log_path=logp, out=quiet)
        check("a dry run sends nothing", [c for c in api.calls if c[0] != "GET"], [])
        apply_sold(api, plan, commit=True, log_path=logp, out=quiet)
        check("the exclusion lands inside must", api.presets["p1"]["filters"]["must"].get("must_not"),
              {"any_property_status": ["sold"]})
        check("the account id is kept on the PATCH", api.presets["p1"]["filters"].get("account"), "acct-1")
        api.presets["p1"]["filters"]["must"]["skiptraced"] = 1          # a person edits it after the run
        u1 = undo(api, logp, commit=True, out=quiet)
        check("undo leaves a preset a person changed since", u1["left"], ["00. Needs Skip Traced"])
        api.presets["p1"]["filters"]["must"]["skiptraced"] = 0
        u2 = undo(api, logp, commit=True, out=quiet)
        check("undo restores the filter it replaced", api.presets["p1"]["filters"]["must"],
              {"any_tags": ["t-court"], "skiptraced": 0})
        check("undo reports the restore", u2["restored"], ["00. Needs Skip Traced"])
        del dry

        doc = {"folders": [
            {"title": "01. Bulk Sequential Marketing", "presets": [
                {"title": "00. Bulk Needs Skipped", "must": {"any_tags": ["courthouse data"], "skiptraced": 0,
                                                             "must_not": {"any_property_status": ["Not Interested"],
                                                                          "any_lists": ["Probate"]}}},
                {"title": "01. Ready to Text", "must": {"phone": 0}}]},
            {"title": "00 Niche Sequential Marketing", "presets": [
                {"title": "02. Missing Tag", "must": {"any_tags": ["No Such Tag"]}}]}]}
        cp = plan_create(Account(api), doc)
        new = next((p for p in cp["presets"] if p["title"] == "00. Bulk Needs Skipped"), {})
        check("tag and list titles become the account's ids", (new.get("must", {}).get("any_tags"),
              new.get("must", {}).get("must_not", {}).get("any_lists")), (["t-court"], ["l-pro"]))
        check("a status is written in its stored spelling, plus the Sold exclusion",
              new.get("must", {}).get("must_not", {}).get("any_property_status"), ["not_interested", "sold"])
        check("a new folder is planned only when missing", [f["title"] for f in cp["folders"]],
              ["01. Bulk Sequential Marketing"])
        check("a same title with a different filter is refused, never overwritten",
              any("01. Ready to Text" in p and "different filter" in p for p in cp["problems"]), True)
        check("a missing tag is a problem, not a guess", any("No Such Tag" in p for p in cp["problems"]), True)
        writes = len([c for c in api.calls if c[0] != "GET"])
        try:
            apply_create(api, Account(api), cp, commit=True, log_path=logp, out=quiet)
            refused = False
        except Refused:
            refused = True
        check("a plan with problems sends nothing", (refused, len([c for c in api.calls if c[0] != "GET"])),
              (True, writes))

        good = {"folders": [{"title": "01. Bulk Sequential Marketing", "presets": [
            {"title": "00. Bulk Needs Skipped", "must": {"any_tags": ["Courthouse Data"], "skiptraced": 0}}]}]}
        gp = plan_create(Account(api), good)
        made = apply_create(api, Account(api), gp, commit=True, log_path=logp, out=quiet)
        newp = next(p for p in api.presets.values() if p["title"] == "00. Bulk Needs Skipped")
        check("create makes the folder and the preset", (made["folders"], made["presets"]),
              (["01. Bulk Sequential Marketing"], ["00. Bulk Needs Skipped"]))
        check("the new preset carries the account id and the read-back filter",
              (newp["filters"]["account"], newp["filters"]["must"]),
              ("acct-1", {"any_tags": ["t-court"], "skiptraced": 0, "must_not": {"any_property_status": ["sold"]}}))
        again = plan_create(Account(api), good)
        check("a second run of the same plan finds it already there", ([p["title"] for p in again["already"]],
              again["presets"], again["problems"]), (["00. Bulk Needs Skipped"], [], []))

    bad = 0
    for label, ok, got, want in results:
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {got!r} (want {want!r})"))
    print(f"{len(results) - bad} of {len(results)} passed")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage DataSift filter presets on your Open API key "
                                             "(dry until --commit)")
    ap.add_argument("--discover", action="store_true", help="list the folders and presets (read only)")
    ap.add_argument("--add-sold-exclusion", action="store_true", help="add Property Status: do not include Sold")
    ap.add_argument("--all", action="store_true", help="--discover, then --add-sold-exclusion")
    ap.add_argument("--create", metavar="PLAN_JSON", help="make the folders and presets in a plan file")
    ap.add_argument("--undo", metavar="LOG_JSONL", help="put the Sold exclusions in a change log back")
    ap.add_argument("--folder", action="append", default=[],
                    help="a folder by its title (repeatable). Default: the two sequential folders")
    ap.add_argument("--all-folders", action="store_true", help="every preset folder in the account")
    ap.add_argument("--count", action="store_true", help="with --create: how many records each new preset holds")
    ap.add_argument("--commit", action="store_true", help="send the writes (without it nothing is sent)")
    ap.add_argument("--output-dir", default="./output", help="where the change log goes (default ./output)")
    ap.add_argument("--selftest", action="store_true", help="check the rules on an invented account, no network")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not (args.discover or args.add_sold_exclusion or args.all or args.create or args.undo):
        ap.print_help()
        return 0
    api = DataSiftApi()
    acc = Account(api)
    log_path = Path(args.output_dir) / f"preset_changes_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    try:
        if args.undo:
            undo(api, Path(args.undo), commit=args.commit)
            return 0
        if args.discover or args.all:
            discover(acc, args.folder)
        if args.add_sold_exclusion or args.all:
            plan = plan_sold(acc, args.folder, args.all_folders)
            print(f"\nSold exclusion over {', '.join(plan['folders']) or 'no folder'}: "
                  f"{len(plan['add'])} to add, {len(plan['already'])} already skip Sold, "
                  f"{len(plan['on_purpose'])} read Sold on purpose (left alone)")
            for e in plan["add"]:
                print(f"  add  {e['folder']} / {e['title']}")
            if not plan["folders"]:
                print("  No sequential folder found. Name folders with --folder, or use --all-folders.")
            done = apply_sold(api, plan, commit=args.commit, log_path=log_path)
            if done["patched"]:
                print(f"{len(done['patched'])} patched and read back. Log: {log_path}")
        if args.create:
            doc = json.loads(Path(args.create).read_text(encoding="utf-8"))
            plan = plan_create(acc, doc)
            for f in plan["folders"]:
                print(f"  new folder   {f['title']}")
            for p in plan["presets"]:
                n = count_records(api, p["must"]) if args.count else None
                print(f"  new preset   {p['folder']} / {p['title']}" + (f"  ({n} records today)" if n is not None else ""))
            for a in plan["already"]:
                print(f"  already      {a['folder']} / {a['title']}")
            for note in plan["notes"]:
                print(f"  note         {note}")
            for prob in plan["problems"]:
                print(f"  PROBLEM      {prob}")
            done = apply_create(api, acc, plan, commit=args.commit, log_path=log_path)
            if done["presets"] or done["folders"]:
                print(f"Made {len(done['folders'])} folder(s) and {len(done['presets'])} preset(s), each read back. "
                      f"Log: {log_path}")
    except Refused as exc:
        print(str(exc))
        return 2
    except ApiError as exc:
        print(f"DataSift refused a request, so the run stopped: {exc}")
        return 1
    finally:
        print(f"({api.calls} requests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
