---
name: account-blueprint
description: >
  Build a DataSift account in the same structure as the DataSift reference account, over the API, with a read-back on every object: preset folders and presets, lead statuses, lists, tags, custom fields, task presets, sequences, and SiftMap auto-add presets. Ships the current reference blueprint. Also exports any account you hold an Open API key for into the same portable format. Trigger for: set up my DataSift account like the challenge account, clone the preset system, install the sequential marketing presets, copy the sequences into a new account, or export my account structure.
---

# Account Blueprint

Set up a DataSift account the way the reference account is set up, without
clicking through 98 presets and 27 sequences by hand, and without trusting
that a create "worked" because the server said 200.

Use when someone says "set my account up like the challenge account", "install
the preset system", "clone the sequences", or "export my account structure".

## What you get

One command creates, in dependency order, everything a working account needs:

| family | what lands |
|---|---|
| statuses | the custom lead statuses, with their colors |
| lists | the classification lists (Absentee Owners, Probate, Foreclosure, ...) |
| tags | only the tags the system uses (Priority 1, Courthouse Data, recently sold, ...) |
| custom fields | groups, fields and select options |
| task presets | task groups and the presets sequences create tasks from |
| presets | every preset folder and preset, filters translated to your account |
| sequences | folders and sequences, board columns and task presets re-linked |
| SiftMap | the auto-add presets, created with auto-add OFF |

Every object is read back after it is created and compared to what was sent.
A preset whose saved filter differs from the one sent is a mismatch and the
run exits 2. Anything that could not be reproduced is a named gap in the
report, never a silent omission.

## Requirements

- Python 3.10+ (stdlib only, nothing to install)
- Your own DataSift login. Either a JWT copied from the browser session, or
  your email and password (the script mints its own token)
- A blueprint file. The reference one is in `blueprints/`

## Run it

```bash
cd scripts

# 1. See what would happen. Nothing is written.
python clone_account.py --phase plan --blueprint ../blueprints/ty2_2026-09-10.json \
    --target you@example.com --email you@example.com --password '...'

# 2. Create it. Still nothing is written without --commit.
python clone_account.py --phase apply --blueprint ../blueprints/ty2_2026-09-10.json \
    --target you@example.com --email you@example.com --password '...' --commit

# 3. Check parity later, read-only.
python clone_account.py --phase verify --blueprint ../blueprints/ty2_2026-09-10.json \
    --target you@example.com --jwt '<paste>'
```

A pasted token works instead of a password: `--jwt '<token>'` or the
`REISIFT_TARGET_JWT` environment variable. Copy it from any request to
`apiv2.reisift.io` in your browser's network tab (the `authorization: Bearer`
header).

The report lands next to the blueprint as `apply_<you>_<time>.md`. Read the
"Configure by hand" section: it lists exactly what the API could not do.

## What it refuses to do, on purpose

- **Write to the wrong account.** The token's email must match `--target`, the
  token's account must differ from the blueprint's source account, and a live
  read must come back from that same account. Any doubt is exit 3 before the
  first write.
- **Create a preset that means something different.** If a list, tag, board or
  column a preset references does not exist in your account, that preset is
  skipped and named in the report. A "Ready to Call" preset minus its
  Priority 1 gate would select every record with a phone number.
- **Create a sequence that fires into nothing.** A sequence whose trigger points
  at a board column you do not have is skipped. One that lost only a
  notification action (send SMS, send email carry the source account's own
  numbers and are never copied) is created inactive with a note.
- **Spend your record allowance.** SiftMap presets are created with auto-add
  off. Turn it on per preset once the addresses are your market
  (`--siftmap-auto-add` keeps the source setting).
- **Bring market data along.** Neighborhood exclusions from the source market
  are stripped (`--keep-neighborhoods` to keep them).

## Options worth knowing

| flag | what it does |
|---|---|
| `--only presets,sequences` | create only these families; the rest are indexed but never created |
| `--skip siftmap` | leave a family alone |
| `--user-map "Adriana=Jane,Tinaa=Sam"` | map the source callers to yours for the per-caller queue presets; unmapped ones point at you with a note |
| `--folders all` | include non-numbered preset folders (default: only `01.` style folders) |
| `--move-presets` | a preset that already exists in a differently named folder is moved to the blueprint's folder (a folder rename at the source is normal drift) |
| `--stub-inactive` | create a sequence inactive when one of its actions cannot be re-linked |
| `--probe-only --commit` | create one object on each never-before-used route, read it back, stop |
| `--strict-counts` | treat a preset that matches zero records as a failure |

## Export your own account

If you hold an Open API key for an account, you can turn it into a blueprint
the same way:

```bash
python clone_account.py --phase export --api-key '<key>' --label myaccount --no-counts
python clone_account.py --phase validate --blueprint ../../output/blueprints/myaccount_<date>.json
```

Validation refuses a file that carries any uuid other than the source account
id, any phone number, or any email address, so a blueprint is safe to hand to
someone else.

## How the reference blueprint was made

`references/blueprint-format.md` describes the file. The reference blueprint
was exported live from the DataSift challenge account on 2026-09-10: 20 preset
folders and 98 presets, 27 sequences, 13 task presets, 37 custom fields, 37
SiftMap presets, 56 lists, 21 tags and 4 custom statuses. Sequences that send
SMS or email in the source account are included without those actions and are
created inactive, with the message text kept in the file so you can re-add
them with your own integration.
