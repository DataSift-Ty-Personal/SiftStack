"""Command line for the account blueprint.

    --phase export     read the source account live -> output/blueprints/<label>_<date>.json
    --phase validate   schema, refs, leak scan on a blueprint file
    --phase plan       dry apply: what would be created, every gap and TODO
    --phase apply      the real thing (still DRY unless --commit)
    --phase verify     read-only parity check + record-count probes

Target credential, first match wins:
    --jwt <token> | REISIFT_TARGET_JWT
    --email + --password            (mints via POST /api/token/)
    --impersonate                   (staff JWT from REISIFT_STAFF_JWT or the Deal Room store)
    a pasted token for --target in the Deal Room store (internal convenience)
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import FAMILIES
from .blueprint import dump, load, validate
from .client import Client, decode_claims


def _out_dir() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(os.environ.get("SIFTSTACK_OUTPUT_DIR", os.path.join(root, "output")), "blueprints")


def _slug(email: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in email.lower())


def target_client(a) -> Client:
    jwt = (a.jwt or os.environ.get("REISIFT_TARGET_JWT", "")).strip()
    if a.jwt_file:
        with open(a.jwt_file, encoding="utf-8") as f:
            jwt = f.read().strip()
    if jwt:
        return Client.from_jwt(jwt)
    if a.email and a.password:
        return Client.from_password(a.email, a.password)
    if a.impersonate:
        from .staff_creds import staff_client
        staff = staff_client()
        if staff is None:
            raise SystemExit("--impersonate needs a valid staff JWT (REISIFT_STAFF_JWT or the store)")
        return Client.impersonate(staff, a.target)
    try:
        from .staff_creds import stored_target_jwt
        tok = stored_target_jwt(a.target)
    except Exception:
        tok = ""
    if tok:
        print("[cli] using the pasted token for %s from the Deal Room store" % a.target)
        return Client.from_jwt(tok)
    raise SystemExit("no target credential: pass --jwt, --email/--password, or --impersonate")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clone_account", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["export", "validate", "plan", "apply", "verify"])
    ap.add_argument("--blueprint", help="blueprint JSON (validate/plan/apply/verify)")
    ap.add_argument("--label", default="ty2", help="export: label for the file name and source")
    ap.add_argument("--source-account", default=None, help="export: Deal Room store account name")
    ap.add_argument("--api-key", default=None, help="export: Open API key (else REISIFT_API_KEY / store)")
    ap.add_argument("--no-counts", action="store_true", help="export: skip per-preset source counts")
    ap.add_argument("--target", help="target account email (apply/plan/verify)")
    ap.add_argument("--jwt", default=None)
    ap.add_argument("--jwt-file", default=None)
    ap.add_argument("--email", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--impersonate", action="store_true")
    ap.add_argument("--commit", action="store_true", help="apply: write (DRY by default)")
    ap.add_argument("--only", default="", help="comma list of families to create")
    ap.add_argument("--skip", default="", help="comma list of families to leave alone")
    ap.add_argument("--folders", choices=["numbered", "all"], default="numbered")
    ap.add_argument("--move-presets", action="store_true",
                    help="a preset that exists in another folder is moved to the blueprint's folder")
    ap.add_argument("--user-map", default="", help='"Adriana=Jane,Tinaa=Sam" source first name -> target first name')
    ap.add_argument("--assign-fallback", choices=["self", "drop"], default="self")
    ap.add_argument("--keep-neighborhoods", action="store_true")
    ap.add_argument("--stub-inactive", action="store_true",
                    help="create a sequence inactive when a non-structural ref is missing")
    ap.add_argument("--siftmap-auto-add", action="store_true",
                    help="keep auto-add ON for SiftMap presets (spends the target's allowance)")
    ap.add_argument("--probe-only", action="store_true",
                    help="create one object per unverified route (task-group, task-preset, "
                         "sequence-folder, custom-field group, board, column), read it back, stop")
    ap.add_argument("--allow-staff-target", action="store_true")
    ap.add_argument("--allow-same-account", default="", metavar="REASON")
    ap.add_argument("--strict-counts", action="store_true")
    ap.add_argument("--no-count-probe", action="store_true")
    ap.add_argument("--state", default=None)
    ap.add_argument("--report", default=None)
    a = ap.parse_args(argv)

    if a.phase == "export":
        from .export import Exporter
        from .staff_creds import api_key_client, staff_client
        if a.api_key:
            client, acct = Client.from_api_key(a.api_key), {}
        else:
            client, acct = api_key_client(a.source_account or "datasift-apikey")
        staff = None
        try:
            staff = staff_client()
        except Exception:
            staff = None
        print("export: source %s via %s%s" % (acct.get("email") or "?", client.kind,
                                             " (staff fallback available)" if staff else ""))
        ex = Exporter(client, staff=staff, with_counts=not a.no_counts, label=a.label,
                      email=acct.get("email") or "", account_hint=acct.get("account_id") or "")
        bp = ex.run()
        errors, warnings = validate(bp)
        path = a.blueprint or os.path.join(_out_dir(), "%s_%s.json" % (a.label, time.strftime("%Y-%m-%d")))
        dump(bp, path)
        print("wrote %s" % path)
        for w in warnings:
            print("  warn: " + w)
        for e in errors:
            print("  ERROR: " + e)
        return 1 if errors else 0

    if not a.blueprint:
        raise SystemExit("--blueprint is required for %s" % a.phase)
    bp = load(a.blueprint)
    errors, warnings = validate(bp)
    for w in warnings:
        print("  warn: " + w)
    if a.phase == "validate":
        for e in errors:
            print("  ERROR: " + e)
        n = sum(len(f["presets"]) for f in bp["preset_folders"])
        print("blueprint %s: %d folders / %d presets, %d sequences, %d task presets, %d custom fields, "
              "%d siftmap presets, %d tags, %d lists, %d statuses -> %s"
              % (a.blueprint, len(bp["preset_folders"]), n, len(bp["sequences"]),
                 len(bp["task_presets"]), len(bp["custom_fields"]), len(bp["siftmap_presets"]),
                 len(bp["tags"]), len(bp["lists"]), len(bp["statuses"]),
                 "INVALID (%d errors)" % len(errors) if errors else "valid"))
        return 1 if errors else 0
    if errors:
        for e in errors:
            print("  ERROR: " + e)
        raise SystemExit("blueprint is invalid; refusing to %s" % a.phase)
    if not a.target:
        raise SystemExit("--target <email> is required for %s" % a.phase)

    from .apply import Options, run_apply
    client = target_client(a)
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}
    bad = (only | skip) - set(FAMILIES)
    if bad:
        raise SystemExit("unknown families %s; choose from %s" % (sorted(bad), FAMILIES))
    user_map = dict(kv.split("=", 1) for kv in a.user_map.split(",") if "=" in kv)
    slug = _slug(a.target)
    opts = Options(
        commit=(a.commit and a.phase == "apply"), only=only, skip=skip, folders=a.folders,
        move_presets=a.move_presets,
        user_map=user_map, assign_fallback=a.assign_fallback,
        strip_neighborhoods=not a.keep_neighborhoods, stub_inactive=a.stub_inactive,
        siftmap_auto_add=a.siftmap_auto_add, probe_only=a.probe_only,
        allow_staff_target=a.allow_staff_target, allow_same_account=a.allow_same_account,
        strict_counts=a.strict_counts, count_probe=not a.no_count_probe,
        verify_only=(a.phase == "verify"),
        state_path=a.state or os.path.join(_out_dir(), "apply_%s_state.json" % slug),
        report_path=a.report or os.path.join(_out_dir(), "apply_%s_%s" % (slug, time.strftime("%Y%m%dT%H%M%S"))),
        blueprint_path=a.blueprint)
    return run_apply(client, bp, a.target, opts)


if __name__ == "__main__":
    sys.exit(main())
