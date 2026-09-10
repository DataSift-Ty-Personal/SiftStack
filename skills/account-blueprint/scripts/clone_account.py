"""Clone the ty+2 DataSift account structure into any DataSift account.

    python src/clone_account.py --phase export                                        # ty+2 -> output/blueprints/ty2_<date>.json
    python src/clone_account.py --phase validate --blueprint output/blueprints/ty2_2026-09-10.json
    python src/clone_account.py --phase plan  --blueprint <bp> --target you@x.com --jwt <paste>          # dry diff + TODO list
    python src/clone_account.py --phase apply --blueprint <bp> --target you@x.com --email you@x.com --password ... --commit
    python src/clone_account.py --phase apply --blueprint <bp> --target client@x.com --impersonate --commit   # staff, non-staff client
    python src/clone_account.py --phase apply ... --probe-only --commit               # one create + read-back per unverified route, stop
    python src/clone_account.py --phase apply ... --only statuses,lists,tags --commit
    python src/clone_account.py --phase verify --blueprint <bp> --target you@x.com --jwt <paste>          # read-only parity + count probes

Two steps, one portable file. `export` reads the source account live (Open
API key, staff-JWT fallback per family) into a title-keyed, uuid-free
blueprint. `apply` takes that file plus the TARGET's own credential, creates
every family in dependency order (statuses, lists, tags, custom fields, task
presets, presets, sequences, SiftMap presets), reads each object back and
compares, and writes a report naming every translation, drop, gap and
configure-by-hand item. DRY by default; --commit writes. The apply side is
stdlib-only and imports nothing from the Deal Room checkout, so a community
member can run it on their own account.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from account_blueprint.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
