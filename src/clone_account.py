"""Clone the reference DataSift account structure into any DataSift account.

    python src/clone_account.py --phase export --label myaccount                       # your account -> output/blueprints/
    python src/clone_account.py --phase validate --blueprint output/blueprints/account_2026-11-23.json
    python src/clone_account.py --phase plan  --blueprint <bp> --target you@x.com            # dry diff + checklist
    python src/clone_account.py --phase apply --blueprint <bp> --target you@x.com --commit
    python src/clone_account.py --phase apply ... --probe-only --commit               # one create + read-back per unverified route, stop
    python src/clone_account.py --phase apply ... --only lists,tags --commit
    python src/clone_account.py --phase verify --blueprint <bp> --target you@x.com           # read-only parity + count probes

Every phase runs on a DataSift Open API key, from REISIFT_API_KEY (or the
variable --key-env names): for export the key of the account being read, for
plan, apply and verify the key of the TARGET account.

Two steps, one portable file. `export` reads an account live into a
title-keyed, uuid-free blueprint. `apply` takes that file plus the target's
key, creates every family DataSift publishes a create route for, in
dependency order (lists, tags, custom fields, task presets, presets, SiftMap
presets), reads each object back and compares, and writes a report naming
every translation, drop and gap, plus a click-through checklist for what the
API cannot create (custom statuses, boards and their columns, sequences). DRY
by default; --commit writes. The apply side is stdlib-only and imports nothing
from the Deal Room checkout, so a community member can run it on their own
account.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from account_blueprint.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
