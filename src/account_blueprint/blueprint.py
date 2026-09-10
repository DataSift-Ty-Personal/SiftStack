"""Blueprint file: schema constants, $ref helpers, validation, load/dump.

A blueprint is title-keyed on purpose. The only uuid allowed in the file is
source.account_uuid, which exists so apply can refuse to clone an account
onto itself. Everything else that was a uuid in the source account is a
{"$ref": kind, "title": ...} the apply side resolves against the TARGET.

The leak scan is part of validation, not a warning: ty+2's sequences carry a
real phone number, a real email and an integration uuid, and the file is
meant to be handed to strangers.
"""
from __future__ import annotations

import json
import os
import re
import tempfile

from . import BLUEPRINT_VERSION

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
UUID_ANY = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
PHONE_RE = re.compile(r"(?<![\w-])\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w-])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

REF_KINDS = ("list", "tag", "status", "board", "column", "user", "task_preset",
             "task_group", "sequence_folder", "custom_field", "self")

FAMILY_KEYS = ("statuses", "lists", "tags", "custom_field_groups", "custom_fields",
               "task_groups", "task_presets", "boards", "users", "sequence_folders",
               "sequences", "preset_folders", "siftmap_presets")

# Cohort, import-batch and diagnostic tags. ty+2 carries ~134 of these and not
# one of them belongs in another account. A tag matching any of these can only
# be exported by being explicitly anchored, which none of them are.
JUNK_TAG_RE = re.compile(
    r"(^\d{4}-(W\d{2}|\d{2})\b)|(^(dataflik|datafik)[ _])|(^pulled_)|(^filed_)|(^sold \d{4})"
    r"|(^skip_?traced?[ _])|(^diag)|(^mailtest)|(^zzz)|(^test\b)|(, )|(^\d{4}-\d{2}-\d{2})",
    re.I)


def ref(kind: str, title: str, **extra) -> dict:
    d = {"$ref": kind, "title": title}
    d.update(extra)
    return d


def is_ref(x) -> bool:
    return isinstance(x, dict) and "$ref" in x


def is_unresolved(x) -> bool:
    return isinstance(x, dict) and "$unresolved" in x


def empty() -> dict:
    bp = {"blueprint_version": BLUEPRINT_VERSION, "exported_at": "",
          "source": {"label": "", "email": "", "account_uuid": "", "auth_by_family": {}}}
    for k in FAMILY_KEYS:
        bp[k] = []
    bp["export_log"] = []
    bp["warnings"] = []
    return bp


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump(bp: dict, path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(bp, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- validation

def _walk_refs(node, path, out):
    if is_ref(node):
        out.append((path, node))
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_refs(v, "%s.%s" % (path, k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_refs(v, "%s[%d]" % (path, i), out)


def scan_leaks(bp: dict) -> list[str]:
    """uuids outside source.account_uuid / $unresolved, phones, emails."""
    probe = json.loads(json.dumps(bp))
    src = probe.get("source") or {}
    allowed = {str(src.get("account_uuid") or "").lower()}
    src["account_uuid"] = ""
    src["email"] = ""
    probe["export_log"] = []
    probe["warnings"] = []
    for u in (probe.get("users") or []):
        u["email"] = ""

    def strip_unresolved(n):
        if is_unresolved(n):
            return {"$unresolved": "X"}
        if isinstance(n, dict):
            return {k: strip_unresolved(v) for k, v in n.items()}
        if isinstance(n, list):
            return [strip_unresolved(v) for v in n]
        return n
    text = json.dumps(strip_unresolved(probe))
    problems = []
    for m in UUID_ANY.findall(text):
        if m.lower() not in allowed:
            problems.append("uuid leaked: %s" % m)
    for m in EMAIL_RE.findall(text):
        problems.append("email leaked: %s" % m)
    for m in PHONE_RE.findall(text):
        problems.append("phone-shaped string leaked: %s" % m.strip())
    return sorted(set(problems))


def validate(bp: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if bp.get("blueprint_version") != BLUEPRINT_VERSION:
        errors.append("blueprint_version %r, expected %r"
                      % (bp.get("blueprint_version"), BLUEPRINT_VERSION))
    for k in FAMILY_KEYS:
        if not isinstance(bp.get(k), list):
            errors.append("missing family %r" % k)
    if errors:
        return errors, warnings
    src = bp.get("source") or {}
    if not UUID_RE.match(str(src.get("account_uuid") or "")):
        errors.append("source.account_uuid missing (apply cannot refuse self-clone)")

    titles = {
        "list": {x["title"] for x in bp["lists"]},
        "tag": {x["title"] for x in bp["tags"]},
        "status": {x["title"] for x in bp["statuses"]},
        "board": {x["title"] for x in bp["boards"]},
        "column": {(b["title"], c) for b in bp["boards"] for c in b.get("columns", [])},
        "user": {x.get("first_name") for x in bp["users"]},
        "task_group": {x["title"] for x in bp["task_groups"]},
        "task_preset": {(x["group"], x["title"]) for x in bp["task_presets"]},
        "sequence_folder": {x["title"] for x in bp["sequence_folders"]},
        "custom_field": {x["label"] for x in bp["custom_fields"]},
    }
    for s in bp["statuses"]:
        if not s.get("color"):
            errors.append("status %r has no color (POST /status/ requires it)" % s.get("title"))
    for t in bp["tags"]:
        if JUNK_TAG_RE.search(t["title"]) or UUID_RE.match(t["title"]):
            errors.append("junk tag exported: %r" % t["title"])
    for x in bp["lists"]:
        if UUID_RE.match(x["title"]):
            errors.append("uuid-titled list exported: %r" % x["title"])

    seen = set()
    for fol in bp["preset_folders"]:
        for p in fol.get("presets", []):
            if p["title"] in seen:
                errors.append("duplicate preset title %r (titles are account-unique)" % p["title"])
            seen.add(p["title"])
            filt = p.get("filters") or {}
            if not (filt.get("must") or {}):
                errors.append("preset %r has an empty must" % p["title"])
            if "account" in filt:
                errors.append("preset %r still carries filters.account" % p["title"])
    seen = set()
    for q in bp["sequences"]:
        if q["title"] in seen:
            errors.append("duplicate sequence title %r" % q["title"])
        seen.add(q["title"])
        if q.get("folder") and q["folder"] not in titles["sequence_folder"]:
            errors.append("sequence %r: folder %r not in sequence_folders" % (q["title"], q["folder"]))

    refs = []
    for k in ("task_presets", "sequences", "preset_folders", "siftmap_presets"):
        _walk_refs(bp[k], k, refs)
    for path, r in refs:
        kind = r.get("$ref")
        if kind == "self":
            continue
        if kind not in REF_KINDS:
            errors.append("%s: unknown $ref kind %r" % (path, kind))
            continue
        if kind == "column":
            key = (r.get("board"), r.get("title"))
        elif kind == "task_preset":
            key = (r.get("group"), r.get("title"))
        else:
            key = r.get("title")
        if key not in titles[kind]:
            errors.append("%s: $ref %s %r not present in the blueprint" % (path, kind, key))

    errors.extend(scan_leaks(bp))
    n_unres = json.dumps(bp).count('"$unresolved"')
    if n_unres:
        warnings.append("%d $unresolved references (apply treats each as a gap)" % n_unres)
    return errors, warnings
