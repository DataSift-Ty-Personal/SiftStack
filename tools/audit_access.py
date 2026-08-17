#!/usr/bin/env python3
"""Who can change this repository, and what protects the default branch.

This exists because the answer was not knowable without digging through five
different settings pages, and not knowing it is how a public repo quietly ends
up with more write access than anyone intended.

    python tools/audit_access.py

Read-only. Needs the gh CLI, authenticated. Prints a verdict and exits
non-zero if something looks wrong, so it can run in CI or before a release.
"""
from __future__ import annotations

import json
import subprocess
import sys

REPO = "DataSift-Ty-Personal/SiftStack"
ORG = REPO.split("/")[0]

# The people who are supposed to have write access. Anyone else showing up is
# the finding. Update this list deliberately, never to silence the check.
EXPECTED_WRITERS = {"tyvhb"}


def gh(path: str, jq: str | None = None):
    cmd = ["gh", "api", path]
    if jq:
        cmd += ["--jq", jq]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    if not out:
        return None
    # gh --jq emits one value per line, and a value may be a bare string
    # rather than JSON (`.[].login` yields `tyvhb`, not `"tyvhb"`), so decode
    # per line and fall back to the raw text.
    def one(s: str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s

    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) == 1:
        return one(lines[0])
    return [one(ln) for ln in lines]


def main() -> int:
    problems: list[str] = []

    print(f"Access audit: {REPO}\n")

    # --- who can write -----------------------------------------------------
    collabs = gh(f"repos/{REPO}/collaborators",
                 '.[] | {login, admin: .permissions.admin, push: .permissions.push}') or []
    if isinstance(collabs, dict):
        collabs = [collabs]
    writers = {c["login"] for c in collabs if c.get("push")}

    print("Collaborators")
    for c in sorted(collabs, key=lambda x: x["login"]):
        role = "admin" if c["admin"] else ("write" if c["push"] else "read")
        print(f"  {c['login']:<24} {role}")
    if not collabs:
        print("  (none)")

    unexpected = writers - EXPECTED_WRITERS
    if unexpected:
        problems.append(f"unexpected write access: {', '.join(sorted(unexpected))}")

    # --- the setting that grants access without naming anyone --------------
    org = gh(f"orgs/{ORG}", "{default_repository_permission}") or {}
    base = org.get("default_repository_permission")
    print(f"\nOrg default repository permission: {base}")
    if base not in ("read", "none", None):
        problems.append(
            f"org default permission is '{base}', which silently grants every org "
            f"member that level on this repo")

    members = gh(f"orgs/{ORG}/members", ".[].login") or []
    if isinstance(members, str):
        members = [members]
    print(f"Org members: {', '.join(members) if members else '(none)'}")

    teams = gh(f"repos/{REPO}/teams", '.[] | {name, permission}') or []
    if isinstance(teams, dict):
        teams = [teams]
    if teams:
        print("Teams with access")
        for t in teams:
            print(f"  {t['name']:<24} {t['permission']}")
            if t["permission"] in ("push", "admin", "maintain"):
                problems.append(f"team '{t['name']}' has {t['permission']} access")
    else:
        print("Teams with access: none")

    # --- what protects the default branch ----------------------------------
    rules = gh(f"repos/{REPO}/rules/branches/main", ".[].type") or []
    if isinstance(rules, str):
        rules = [rules]
    rules = set(rules)
    print(f"\nRules enforced on main: {', '.join(sorted(rules)) if rules else 'NONE'}")
    for need, why in (("non_fast_forward", "main could be force-pushed"),
                      ("deletion", "main could be deleted"),
                      ("pull_request", "changes could land without a PR")):
        if need not in rules:
            problems.append(f"no '{need}' rule: {why}")

    # --- fork PR blast radius ----------------------------------------------
    secrets = gh(f"repos/{REPO}/actions/secrets", ".total_count")
    wf = gh(f"repos/{REPO}/actions/permissions/workflow",
            "{default_workflow_permissions, can_approve_pull_request_reviews}") or {}
    print(f"\nRepo Actions secrets: {secrets}")
    print(f"Default workflow token: {wf.get('default_workflow_permissions')}")
    if wf.get("default_workflow_permissions") == "write":
        problems.append("workflow token defaults to write; read is enough here")
    if wf.get("can_approve_pull_request_reviews"):
        problems.append("workflows can approve PRs, which defeats the PR requirement")

    repo = gh(f"repos/{REPO}", "{visibility, forks_count, open_issues_count}") or {}
    print(f"Visibility: {repo.get('visibility')}   forks: {repo.get('forks_count')}")

    # A fork is a copy. It grants nothing. Said out loud because the fork
    # count is the number people find alarming and it is the harmless one.
    print("\nForks and fork PRs grant no write access. A PR changes nothing until merged.")

    if repo.get("visibility") != "public":
        problems.append(
            "repo is not public: raw.githubusercontent URLs stop working, which breaks "
            "the install command, the manifest and all dist archives")

    print()
    for p in problems:
        print(f"PROBLEM  {p}")
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
