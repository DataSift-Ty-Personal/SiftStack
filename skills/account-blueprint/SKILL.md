---
name: account-blueprint
description: >
  Build a DataSift account in the same structure as the DataSift reference account, on your own Open API key, with a read-back on every object it creates: preset folders and presets, lists, tags, custom fields, task presets and SiftMap auto-add presets. What DataSift's published API cannot create (custom lead statuses, boards and their columns, sequences) comes out as a click-through checklist in the report. Ships the current reference blueprint. Also exports your own account into the same portable format. Trigger for: set up my DataSift account like the challenge account, clone the preset system, install the sequential marketing presets, copy the sequences into a new account, or export my account structure.
---

# Account Blueprint

Set up a DataSift account the way the reference account is set up, without
clicking through 98 presets and 27 sequences by hand, and without trusting
that a create "worked" because the server said 200.

Use when someone says "set my account up like the challenge account", "install
the preset system", "clone the sequences", or "export my account structure".

## What you get

It runs on your DataSift Open API key. Everything DataSift publishes a create
route for is created in dependency order and read back; the rest comes out as
a click-through checklist, written click by click, in the report:

| family | on your key |
|---|---|
| lists | created: the classification lists (Absentee Owners, Probate, Foreclosure, ...) |
| tags | created: only the tags the system uses (Priority 1, Courthouse Data, recently sold, ...) |
| custom fields | created: groups, fields and select options |
| task presets | created: task groups and the presets sequences create tasks from |
| presets | created: every preset folder and preset, filters translated to your account |
| SiftMap | created: the auto-add presets, with auto-add OFF |
| statuses | checklist: the custom lead statuses and their colors |
| boards | checklist: any missing board and every missing column, in order |
| sequences | checklist: every missing folder and sequence, card by card (sequences are not in DataSift's published API) |

Every object the script creates is read back and compared to what was sent.
A preset whose saved filter differs from the one sent is a mismatch and the
run exits 2. Anything that could not be reproduced is a named gap in the
report, never a silent omission.

## Requirements

- Python 3.10+ (stdlib only, nothing to install)
- Your DataSift Open API key for the account you are setting up, as
  `REISIFT_API_KEY` (or name another variable with `--key-env`). An account
  admin creates it in DataSift (developers.datasift.ai explains it); it never
  expires
- A blueprint file. The reference one is in `blueprints/`

## Run it

```bash
cd scripts
export REISIFT_API_KEY=...        # your account's Open API key (PowerShell: $env:REISIFT_API_KEY = "...")

# 1. See what would happen. Nothing is written.
python clone_account.py --phase plan --blueprint ../blueprints/account_2026-11-23.json --target you@example.com

# 2. Create it. Still nothing is written without --commit.
python clone_account.py --phase apply --blueprint ../blueprints/account_2026-11-23.json --target you@example.com --commit

# 3. Work through the report's checklist in the app, then run step 2 again:
#    it creates whatever was waiting on the boards, columns and statuses you added.

# 4. Check parity later, read-only.
python clone_account.py --phase verify --blueprint ../blueprints/account_2026-11-23.json --target you@example.com
```

`--target` is the email of the user the key belongs to. The report lands as
`apply_<you>_<time>.md` (under `output/blueprints/`). Read the "Configure by
hand" section: it lists exactly what the API could not do, in build order
(statuses, then boards and columns, then sequence folders and sequences), with
every click spelled out. DataSift allows 120 requests a minute per account,
shared by every tool on it; the script stays near 80.

## What it refuses to do, on purpose

- **Write to the wrong account.** The script asks DataSift who the key belongs
  to: that user's email must match `--target`, its account must differ from the
  blueprint's source account, and a live read must come back from that same
  account. Any doubt is exit 3 before the first write.
- **Create a preset that means something different.** If a list, tag, board or
  column a preset references does not exist in your account, that preset is
  skipped and named in the report. A "Ready to Call" preset minus its
  Priority 1 gate would select every record with a phone number.
- **Hand you a sequence that fires into nothing.** Each checklist sequence names
  the boards, columns, tags and task presets it uses, and the checklist puts it
  after the boards and columns it needs. A sequence that sent SMS or email in
  the source account (those carry the source's own numbers and are never
  copied) is listed to start inactive, with a step to add your own message.
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
| `--user-map "Emerson=Jane,Sloane=Sam"` | map the source callers to yours for the per-caller queue presets; unmapped ones point at you with a note |
| `--folders all` | include non-numbered preset folders (default: only `01.` style folders) |
| `--move-presets` | a preset that already exists in a differently named folder is moved to the blueprint's folder (a folder rename at the source is normal drift) |
| `--key-env NAME` | read the key from another environment variable (one per account when you work with two) |
| `--probe-only --commit` | create one object on each never-before-used route, read it back, stop |
| `--strict-counts` | treat a preset that matches zero records as a failure |

## Export your own account

Your own account's Open API key turns it into a blueprint the same way:

```bash
python clone_account.py --phase export --label myaccount --no-counts
python clone_account.py --phase validate --blueprint ../../output/blueprints/myaccount_<date>.json
```

Validation refuses a file that carries any uuid other than the source account
id, any phone number, or any email address, so a blueprint is safe to hand to
someone else.

## How the reference blueprint was made

`references/blueprint-format.md` describes the file. The reference blueprint
was exported live from the DataSift challenge account on 2026-11-23: 22 preset
folders and 103 presets, 28 sequences, 13 task presets, 54 custom fields, 40
SiftMap presets, 69 lists, 32 tags, 9 SiftLine boards and 30 statuses. It is the
three marketing flows (foreclosures, estates, the Top 1,000) plus the Curative Title
board and the agency queue; `docs/account-structure.md` in the SiftStack repo walks
through every folder and preset step by step. Sequences that send
SMS or email in the source account are included without those actions, with
the message text kept in the file so you can re-add it with your own
integration.
