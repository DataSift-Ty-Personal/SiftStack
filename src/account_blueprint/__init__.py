"""account_blueprint: export a DataSift account's operating structure to a
portable, uuid-free JSON blueprint, and apply that blueprint to any other
DataSift account over the API.

A community member applies the reference blueprint to their own account with
their own Open API key. On the key, every family DataSift publishes a create
route for is created and read back (lists, tags, custom fields, task presets,
preset folders and presets, SiftMap presets); the rest (custom statuses,
boards and their columns, sequence folders and sequences) become a
click-through checklist in the report. The apply side (client, blueprint,
translate, apply, probe, report, cli) is stdlib-only and imports nothing from
the Deal Room checkout. staff_creds.py holds the staff-only credentials and is
never needed to apply with a key.

Entry point: python src/clone_account.py --phase export|validate|plan|apply|verify
"""

BLUEPRINT_VERSION = 1

# Dependency order. Every family is fully read-back-verified before the next
# one translates against it, because later families resolve titles to the
# uuids the earlier ones produced.
FAMILIES = (
    "statuses",      # on the key: a checklist item (no published create route)
    "lists",
    "tags",
    "custom_fields",
    "task_presets",
    "boards",        # on the key: boards and columns are checklist items
    "presets",
    "sequences",     # on the key: folders and sequences are checklist items
    "siftmap",
)
