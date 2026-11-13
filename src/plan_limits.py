"""Check an account's plan before building a preset system in it (build plan 5.5).

The account blueprint creates up to 98 filter presets in one pass. A Professional plan allows 8.
Applied to an account on that plan, the run creates the first few presets, then starts failing,
and leaves a half-built sequential system whose remaining lanes read as "nothing waiting". The
person who ran it sees presets, not the 90 that are missing.

This asks the account what plan it is on (a documented read: GET /api/internal/user/ returns the
key's own user with `plan_name`) and compares what the blueprint would ADD against what that plan
is known to allow, BEFORE the first create.

WHAT IS KNOWN, AND WHAT IS NOT. The limits below come from the teaching (the Professional plan:
8 presets, 10,000 records a month, 3 boards; boards 3, 8 or unlimited by plan; Expert has
unlimited presets). The Business plan's preset count is not published and is left as unknown: an
unknown limit is reported as unknown and never blocks, because a made-up number that refuses a
valid apply is as wrong as no check at all. DATASIFT_PLAN_LIMITS overrides or fills in the table
when DataSift publishes the figures:

    DATASIFT_PLAN_LIMITS='{"business": {"presets": 150}}'

Read-only. It refuses nothing by itself: the caller decides what to do with `problems`.
"""
from __future__ import annotations

import json
import os

UNLIMITED = -1

# plan name (as /api/internal/user/ reports it, lowercased) -> resource -> limit.
# None = not known. UNLIMITED = no cap.
PLAN_LIMITS: dict[str, dict] = {
    "professional": {"presets": 8, "boards": 3, "monthly_records": 10000},
    "business": {"presets": None, "boards": 8, "monthly_records": None},
    "expert": {"presets": UNLIMITED, "boards": UNLIMITED, "monthly_records": None},
}
_NOUN = {"presets": "filter presets", "boards": "boards", "monthly_records": "records a month"}


def limits() -> dict:
    """The table with any DATASIFT_PLAN_LIMITS override folded in. A bad override is ignored."""
    table = {plan: dict(row) for plan, row in PLAN_LIMITS.items()}
    raw = (os.environ.get("DATASIFT_PLAN_LIMITS") or "").strip()
    if raw:
        try:
            for plan, row in (json.loads(raw) or {}).items():
                if isinstance(row, dict):
                    table.setdefault(str(plan).lower(), {}).update(row)
        except (ValueError, AttributeError):
            pass
    return table


def read_plan(client) -> str:
    """The account's plan name, lowercased, or "" when the read fails or carries none."""
    try:
        user = client.call("/api/internal/user/")
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(user, dict) and isinstance(user.get("results"), list) and user["results"]:
        user = user["results"][0]
    return str((user or {}).get("plan_name") or "").strip().lower() if isinstance(user, dict) else ""


def check(plan: str, needs: dict, have: dict | None = None) -> dict:
    """Would adding `needs` to what the account already `have`s cross a known limit of `plan`?

    needs / have: {"presets": n, "boards": n}. Returns
        {"plan", "ok", "problems": [...], "unknown": [...], "checked": {resource: {...}}}
    `ok` is False only when a KNOWN limit would be crossed. An unknown plan, or a resource whose
    limit is not known, lands in `unknown` and leaves `ok` True."""
    have = have or {}
    plan = (plan or "").strip().lower()
    out = {"plan": plan, "ok": True, "problems": [], "unknown": [], "checked": {}}
    row = limits().get(plan)
    if row is None:
        out["unknown"].append("the account's plan (%s) is not one this check knows, so no limit was applied"
                              % (plan or "not reported"))
        return out
    for resource, need in needs.items():
        need = int(need or 0)
        if need <= 0:
            continue
        limit = row.get(resource)
        has = int(have.get(resource) or 0)
        noun = _NOUN.get(resource, resource)
        out["checked"][resource] = {"limit": limit, "have": has, "would_add": need}
        if limit is None:
            out["unknown"].append("the %s plan's limit on %s is not known, so %d more were not checked"
                                  % (plan, noun, need))
        elif limit != UNLIMITED and has + need > limit:
            out["ok"] = False
            out["problems"].append("the %s plan allows %d %s. This account has %d and the blueprint would add %d."
                                   % (plan, limit, noun, has, need))
    return out


def blueprint_needs(bp: dict, existing_presets: set | None = None, existing_boards: set | None = None,
                    folders: str = "numbered") -> dict:
    """How many presets and boards a blueprint apply would CREATE in this account.

    Counts only what is not already there by title: an apply onto an account that already holds
    the system adds nothing and must not be refused."""
    existing_presets = existing_presets or set()
    existing_boards = existing_boards or set()
    chosen = [f for f in bp.get("preset_folders") or [] if f.get("numbered") or folders == "all"]
    presets = {p.get("title") for f in chosen for p in f.get("presets") or [] if p.get("title")}
    boards = {b.get("title") for b in bp.get("boards") or [] if b.get("title")}
    return {"presets": len(presets - set(existing_presets)), "boards": len(boards - set(existing_boards))}


def check_blueprint(client, bp: dict, existing_presets: set | None = None, existing_boards: set | None = None,
                    folders: str = "numbered") -> dict:
    """The whole check for a blueprint apply: read the plan, count what would be added, compare."""
    needs = blueprint_needs(bp, existing_presets, existing_boards, folders)
    have = {"presets": len(existing_presets or ()), "boards": len(existing_boards or ())}
    return check(read_plan(client), needs, have)
