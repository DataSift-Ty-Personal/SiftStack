"""account_blueprint: export a DataSift account's operating structure to a
portable, uuid-free JSON blueprint, and apply that blueprint to any other
DataSift account over the API.

Two audiences share one code path: a community member applying the ty+2
blueprint to their own account with their own JWT, and DataSift staff setting
an account up for a client. The apply side (client, blueprint, translate,
apply, probe, report, cli) is stdlib-only and imports nothing from the Deal
Room checkout; only export.py + staff_creds.py know where ty+2's credentials
live, and they are never needed to apply.

Entry point: python src/clone_account.py --phase export|validate|plan|apply|verify
"""

BLUEPRINT_VERSION = 1

# Dependency order. Every family is fully read-back-verified before the next
# one translates against it, because later families resolve titles to the
# uuids the earlier ones produced.
FAMILIES = (
    "statuses",
    "lists",
    "tags",
    "custom_fields",
    "task_presets",
    "boards",        # resolve-only: boards and columns are never created
    "presets",
    "sequences",
    "siftmap",
)
