---
name: account-setup-chain
description: Sets up a DataSift account the way every other SiftStack chain expects, once, on the Open API key alone. Runs the SiftStack doctor, applies the account blueprint (lists, tags, custom fields, task presets, preset folders and presets, each read back after it is written), gives every list you market its text, call and mail lanes, walks you through the statuses, boards, columns and sequences the API cannot build, and ends with a readiness check on one test record. Dry run first. Costs nothing and contacts no one. Use when someone says "set up my DataSift account", "set my account up like the bootcamp account", "install the presets and sequences", "get my account ready for SiftStack", "clone the account structure", "build my lanes", "is my account ready", or right after they create their Open API key.
---

# Account setup chain

The first chain, run once before any other: it builds the folders, presets, lists, tags and custom fields the other chains read, on your DataSift Open API key, so every record that lands in your account has a lane to move through. It hands you a short checklist for the few things only the app can build. It is for an operator setting up their own account, or anyone setting one up for a client with that client's own key.

## Who does what

**In Claude Code:** one session runs every step in order. It stops for your yes before anything is written, and it gives you the app steps as a checklist.

**On the Slack team:** Atlas owns this chain.

- Atlas runs `account_setup` with a phase (plan, verify or apply) on the box's own Open API key. Plan and verify are read only. Apply writes only when the box runs live and `ACCOUNT_SETUP_ALLOW_APPLY=1` is set on it.
- Atlas posts the items the API cannot build (custom statuses, boards and their columns, sequences). A person builds them in the app and replies in the thread. Atlas then runs verify.
- The doctor (step 1) and the readiness check (step 5) run in Claude Code.
- After setup, `python src/agents/cli.py autopilot check --live` confirms every preset the team's playbook reads exists in the account.

## Before you start

| What | Used for | Cost |
| --- | --- | --- |
| Your DataSift Open API key, as `REISIFT_API_KEY` in `.env` | every step | included with DataSift; this chain spends $0 and adds no records |
| The email of the DataSift user the key belongs to | the check that refuses any other account | none |
| An account admin in DataSift | the key (Settings, then API), statuses, boards, sequences | none |
| Two test records you add by hand: never marketed, no tags, no status | the sequence tests (step 4) and the readiness check (step 5) | none |

Install the component skills once:

```
/plugin marketplace add DataSift-Ty-Personal/SiftStack
/plugin install siftstack-core@siftstack
/plugin install account-blueprint@siftstack
/plugin install sequential-presets@siftstack
/plugin install sift-sequences@siftstack
/plugin install sift-operations@siftstack
```

The scripts run from a SiftStack folder: the bootcamp starter folder, or a clone.

```
git clone https://github.com/DataSift-Ty-Personal/SiftStack
cd SiftStack
pip install -r requirements.txt
```

Put the line `REISIFT_API_KEY=<your key>` in a `.env` file in that folder and run every command from there. The key goes in the file, never into the chat.

## Parameters

| Parameter | Meaning | Default |
| --- | --- | --- |
| `target_email` | email of the DataSift user whose key is in `.env`; a write to any other account is refused | none, required |
| `blueprint` | the blueprint file to apply | the newest `.json` in `skills/account-blueprint/blueprints/` |
| `families` | what the key creates (passed as `--only` when narrower than the default) | `lists,tags,custom_fields,task_presets,presets` |
| `keep_neighborhoods` | keep the source market's neighborhood exclusions | false |
| `siftmap_presets` | also create the blueprint's SiftMap presets, always with auto-add off (drop `--skip siftmap` from step 2) | false |
| `extra_lists` | lists you market that need their own lanes (step 3) | none |
| `lead_manager` | the teammate the new-lead sequence assigns to | the account owner |
| `sequence_test_record` | uuid of the record the sequence tests change, from its address bar (`/records/properties/<uuid>`) | none, required for step 4 |
| `test_record` | uuid of the record the readiness check uses, found the same way; never the sequence test record | none, required for step 5 |
| `dry_run` | plan and read only; nothing is written | true |

The chain's own files go in `output/account-setup-chain/`.

## Steps

Every command that writes carries `--commit`. With `dry_run` true, the chain runs only the commands without it and shows you what the rest would do.

### 1. Check the install (siftstack-core doctor)

Ask for the SiftStack doctor, or run this in the SiftStack folder:

```
python install.py --doctor
```

It reads which key names are set (never a value) and lists what works now, what needs a key, and what each key costs.

- **In:** your `.env`. **Out:** the doctor's three groups; Claude saves them to `output/account-setup-chain/doctor.txt`.
- You MUST NOT open or print `.env`, because a key that lands in the chat stays in the conversation history.
- If the doctor asks a DataSift skill for anything other than the Open API key, the installed copy is older than the key-only release. Update with `/plugin marketplace update siftstack`, then run the doctor again.
- **Check:** `REISIFT_API_KEY` reads as set, and the DataSift skills (account-blueprint, sequential-presets, sift-sequences) ask for nothing else.
- **Then:** step 2 runs on that key.

### 2. Build the structure (account-blueprint)

The blueprint is a reference account's structure in one file: the folders and presets for three marketing flows (foreclosures, estates, a Top 1,000), the agency queue, deep prospecting, reactivation, and the lead management, acquisitions and transactions task views. It names every object by its title and carries no phone numbers or emails, so it is safe to share.

Work in this order, because each later family names things the earlier ones create.

1. Validate the file. This is offline and works today:

   ```
   python src/clone_account.py --phase validate --blueprint <blueprint>
   ```

2. Plan on your key (read only):

   ```
   python src/clone_account.py --phase plan --blueprint <blueprint> --target <target_email> --report output/account-setup-chain/plan
   ```

   `output/account-setup-chain/plan.md` lists what would be created, every gap, and the items to build by hand in the app.

3. In the app, build the custom statuses and the boards with their columns from that list (see Built in the app). The QC presets name the Lead Management, Acquisitions and Transactions boards, and a preset whose reference does not resolve is not created.

4. One family first. Lists change nothing on any record, so they go first:

   ```
   python src/clone_account.py --phase apply --blueprint <blueprint> --target <target_email> --only lists --commit --skip siftmap --report output/account-setup-chain/apply_lists --state output/account-setup-chain/apply_state.json
   ```

   Read the report: every list is read back after it is created.

5. The rest. It resumes from the state file, so nothing is created twice:

   ```
   python src/clone_account.py --phase apply --blueprint <blueprint> --target <target_email> --commit --skip siftmap --report output/account-setup-chain/apply --state output/account-setup-chain/apply_state.json
   ```

   Add `--keep-neighborhoods` only if your market is the source market.

6. Verify (read only):

   ```
   python src/clone_account.py --phase verify --blueprint <blueprint> --target <target_email> --report output/account-setup-chain/verify
   ```

- **In:** the blueprint and your key. **Out:** the plan, apply and verify reports (`.md` and `.json`) in `output/account-setup-chain/`.
- Exit codes: 0 means verified (any gaps are named in the report), 1 an invalid blueprint, 2 a read-back mismatch, 3 a refusal before any write.
- You MUST NOT apply with a `target_email` that is not the key's own user, because the check that refuses the wrong account rests on that match.
- You MUST NOT switch on auto-add for a SiftMap preset the blueprint carries, because they point at the source market and auto-add starts spending your record allowance the moment it is on.
- The caller queue presets (folder `20. CALLER QUEUES`) point at the key's own user until you set each one's assignee in the app.
- Until the account-blueprint key-only release is in your copy, plan, apply and verify stop with "no target credential". Use Atlas's `account_setup` on the Slack team, which already runs on the key, or build the folders by hand from `docs/account-structure.md` (Steps 3 to 5).
- **Check:** verify exits 0, and the report's gaps are only the items you chose to leave out.
- **Then:** step 3 adds lanes for the lists the blueprint does not cover.

### 3. Give every list its lanes (sequential-presets)

The blueprint brings lanes for its three flows. Any other list you market (a county list, a list the best-lists chain pulls) needs a lane family too, or its records sit in no working view.

A lane family, the way the blueprint builds one:

| Folder | Presets | Moves on |
| --- | --- | --- |
| TEXT | 01 Ready to Text, 02 Texted, Not Yet Called | DataSift's texts-sent counter: 0, then 1 or more |
| CALL | 00 Needs Skipped, 01 Skipped No Numbers, 02 Ready to Call, 03 to 06 Call Attempt 1 to 4 | the dialer's own call attempt counter |
| MAIL | Mail 01 to Mail 06 | DataSift's mail attempt counter, about a month apart |

Every lane preset carries the same exclusions: the finished and lead statuses (sold, closed, not interested, do not call, opted out, every lead stage), the tags recently sold, Do Not Market, Litigator, Not Single Family and Heir Identified (plus Mail Only on the text and call lanes, and Return Mail and Do Not Mail on the mail lanes), and the DNC, Low Equity and Negative Equity lists. Each shows one property per owner, so an owner gets one text, not one per house.

Pick one of two ways for each list in `extra_lists`:

- **Route it into a flow you already have.** Tag its records `Top 1000` and the Top 1,000 lanes (folders 08 to 10) work them, with no new presets. Leave the tag off and the records wait in `Agency - 01 Queue` for the calling agency. If you run the Slack team's monthly Top 1,000 ranking, that stage owns the tag and removes it from owners outside its ranking, so give the list its own lanes instead.
- **Give it its own lanes.** Ask the sequential-presets skill to design the family for the list: the anchor is the list itself, the counters and exclusions as above. Number the folders so they sort in working order (`22. <LIST> - TEXT`, `23. <LIST> - CALL`, `24. <LIST> - MAIL`). Claude writes the plan as `output/account-setup-chain/lanes_<list>.json`, in the shape of `skills/sequential-presets/references/preset-plan-example.json` (tags and lists by title, `must_not` inside `must`), plus `lanes_<list>_first.json` holding only the list's Ready to Text preset. Then:

  ```
  python skills/sequential-presets/scripts/manage_presets.py --discover
  python skills/sequential-presets/scripts/manage_presets.py --create output/account-setup-chain/lanes_<list>.json --count
  python skills/sequential-presets/scripts/manage_presets.py --create output/account-setup-chain/lanes_<list>_first.json --commit --output-dir output/account-setup-chain
  python skills/sequential-presets/scripts/manage_presets.py --create output/account-setup-chain/lanes_<list>.json --commit --output-dir output/account-setup-chain
  ```

  The first lists what exists. The second is the dry run, with each new preset's record count today. The third makes one preset and reads it back. The fourth makes the rest: the preset already made reads as "already", and a title that exists with a different filter is refused, never overwritten. Every write is read back, and the run stops at the first one that differs. Each run logs what it wrote in `output/account-setup-chain/`.

- The list and the tags a plan names must already exist in the account (a pull or an upload creates the list).
- The newer teaching works five full call attempts. Add `07 Call Attempt 5` if you do.
- Until the sequential-presets key-only release is in your copy, build the family in the app: open each Top 1,000 preset, change its anchor to the list, and click Save New with the new title.
- **Check:** each list's Needs Skipped or Ready to Text preset shows its records, and one record you open sits in exactly one lane preset of its list.
- **Then:** step 4 builds the sequences that act when these records change.

### 4. Build the sequences in the app (sift-sequences, sift-operations)

Sequences are not in DataSift's published API, so this step is a click-through. The sift-sequences skill carries every trigger, condition and action. The sift-operations skill explains how sequences, tasks, boards and drips fit together. Your plan caps the count (Essentials 3 sequences, Professional 8, Business unlimited), so build these first:

| Sequence | Trigger | Condition | Actions |
| --- | --- | --- | --- |
| Recently Sold to Sold Status | Property Tags Added | has the tag recently sold | Change Property Status: Sold |
| Call New Lead | Property Status Change | changed to New Lead | Assign Property: your lead manager; Create New Task: Call New Lead; Create New Card: Lead Management, New Lead (Unqualified) |
| Make Offer (a card move) | SiftLine Card Moved | lands in Lead Management, Send to Acquisitions | Move Card: Acquisitions, Appointment Booked; Clear Property Tasks; Create New Task: Make Offer |
| Under Contract (a card move) | SiftLine Card Moved | lands in Acquisitions, Under Contract | Move Card: Transactions, New Contract; Clear Property Tasks; Create New Task: Process Contract; Change Property Status: Under Contract |

The task presets these actions pick (Call New Lead, Make Offer, Process Contract) came in with the blueprint in step 2. The rest of the reference account's sequences (the follow-up loops for hot, warm and cold leads, ghosting, dead lead, close deal) are listed in the plan report and in `docs/account-structure.md`, Step 7.

Before you build, check the account holds everything the four need; after, read back what you built. Both only read:

```
python skills/sift-sequences/scripts/manage_sequences.py --check --tags "recently sold" --boards "Lead Management,Acquisitions,Transactions"
python skills/sift-sequences/scripts/manage_sequences.py --list
```

(These run on the key once the sift-sequences key-only release is in your copy; its `--help` then says it reads on your Open API key.)

- **In:** the plan report's sequence list. **Out:** the sequences in your account; Claude saves the `--list` output to `output/account-setup-chain/sequences.md`.
- You MUST NOT add a Send SMS or Send Email action before that integration is connected (Settings, then Integrations) and tested on the sequence test record, because an action with no integration fails on every lead without a word.
- You SHOULD test each sequence on `sequence_test_record` only: make the change its trigger names, then open the record's activity log; the sequence's name shows beside the change. Each test leaves a status, a tag, a card or a task behind, which is why the readiness check uses the other record.
- **Check:** `--list` shows all four, active, in their folders, and each fires once on the sequence test record and does what its row says.
- **Then:** step 5 checks the account end to end.

### 5. Readiness check

Three checks on the test record. Nothing here reaches a homeowner.

1. **One record read, one tag written and removed.** The first command reads the record and says what it would do. The second adds the tag `Proof Test`, reads it back, removes it and reads again:

   ```
   python src/agents/autopilot/proofs.py run --key-env REISIFT_API_KEY --allow-production --record <test_record> --only tag_round_trip
   python src/agents/autopilot/proofs.py run --key-env REISIFT_API_KEY --allow-production --record <test_record> --only tag_round_trip --commit
   ```

   `--allow-production` is how this tool lets your own account's key through. Use it only with a record made for the test.

2. **One preset counted.** Read only. Compare the number with the count the app shows for the same preset:

   ```
   python -c "import sys; sys.path.insert(0, 'src'); from agents.autopilot.crm import CRM; c = CRM(); print(c.count(c.preset_must('Priority - 02 Ready to Call')))"
   ```

   Count a preset with no date window: the records search refuses a saved preset's relative dates.

3. **Optional, before the best-lists chain traces anything:** add `--only scoped_trace_estimate` to the first command. It asks DataSift's skip trace for a dry estimate scoped to the test record, and it must cover exactly 1 record.

- **In:** `test_record`. **Out:** the proof results in `output/autopilot/proofs/<date>.jsonl`; Claude writes the three results to `output/account-setup-chain/readiness.md`.
- You MUST NOT run the tag check on a record that carries other tags or a status, because adding any tag fires every Property Tags Added sequence whose condition that record already meets (a record tagged recently sold would be set to Sold).
- **Check:** the tag check prints `ok`, and the preset count matches the app.
- **Then:** the account is ready. The best-lists chain or the first-to-market chain fills it.

## Stand it up

**First run, about 10 records.**

1. Run steps 1 to 5 as dry runs first, then again with your yes at each write.
2. Bring in about 10 records: the best-lists chain's first run with `--pool investor:10`, or 10 county records.
3. Watch them land. Each sits in exactly one lane preset: Needs Skipped first, then Ready to Text once DataSift's skip trace reaches it. Open two of them and read their tags and lists.

Working looks like this: verify exits 0, the by-hand list from the plan is done, the readiness check reads `ok`, and the 10 records sit in the lanes you expect.

**On a schedule.** The setup runs once. Each week, run verify (step 2, item 6) to catch drift, and run `manage_sequences.py --list` (step 4) after anyone edits a sequence.

- In Claude Code: a routine (`/schedule`) that runs the verify command each Monday and reports any new gap or mismatch. The key stays in the routine's environment, never in its prompt.
- On the Slack team: ask Atlas for `account_setup` with the phase verify each week, and run `python src/agents/cli.py autopilot check --live` after anyone renames a preset.

Check weekly that mismatches stay at 0, the gaps match the first run, and no lane preset was renamed (the team and the chains find presets by their exact title).

## What to watch

- **Read-back mismatches in verify:** 0. A 2 exit code means a saved object differs from what was sent; stop and read the report.
- **By-hand items left from the plan report:** 0 once the statuses, boards, columns and sequences are built.
- **Readiness check:** all three checks pass.
- **Records on a marketed list that sit in no lane preset:** 0. A list with records and no lanes is a list nobody works.

## Built in the app

What the published API cannot build, as a click-through:

- **The Open API key:** Settings, then API (an account admin creates it).
- **Custom statuses:** the four the plan report lists (Deep Prospecting, Auction Date Passed, Exhausted, Return Mail), each with the color the report gives.
- **Boards and columns:** left sidebar, SiftLine. Accounts made after April 16, 2025 already have Lead Management, Acquisitions, Transactions, Wholesale, Flips and Rentals; check their columns against the report. Add Deep Prospecting, First to Market and Curative Title with the columns the report lists (`docs/account-structure.md`, Step 6, has every column).
- **Sequences:** left sidebar, Sequences, Create. Enter the title and folder, drag a trigger card onto the canvas, drag a condition if the row has one, click Set the Following Actions, drag the first action, click Add new Action + before each next one, then Save Sequence. A title that already exists is refused; add V2.
- **Caller queue presets:** set each preset's assignee to the caller it belongs to.
- **Integrations:** Settings, then Integrations: connect your texting tool and email before any sequence sends.
- **Lanes by hand** (until the sequential-presets key-only release): open a lane preset, change its anchor tag or list, and click Save New. Save without New overwrites the preset you opened.
- **The deep prospecting webhook (Slack team only):** Settings, Integrations, Webhooks. `python src/agents/cli.py setup-datasift` prints the steps for your box.

## Where it works

Any DataSift account with an Open API key, in any county. The blueprint was exported from an account that works Knox and Blount counties in Tennessee, so three things belong to that market and are handled:

- Its SiftMap auto-add presets point at Knox and Blount. They are left out by default; build feeders for your own county.
- Its neighborhood exclusions are stripped by default.
- A few tags carry its county names (`Buy Box - Knox`, `Buy Box - Blount`). Rename them for your county or leave them unused.

Statuses, boards, columns and sequences are by hand in every account.

## Gotchas

- **A preset with a reference that does not resolve is not created.** A Ready to Call preset minus its anchor tag would select every record with a phone, so the apply skips it and names it. Build the boards first, or apply again after you build them.
- **The blueprint brings every list the reference account had,** about a dozen of them one-off pulls that mean nothing in your account. They arrive empty; delete the ones you will not use.
- **Titles are unique per account.** A preset title cannot repeat. A list or board column title cannot repeat even with different capitals or a trailing space. A re-run adopts the existing object instead of making a second one.
- **Status names are stored two ways.** System statuses are stored lowercase with underscores (`new_lead`); custom ones by their label (`Hot Lead`). A filter on the label "New Lead" matches no record.
- **The lanes move on DataSift's own counters,** never on flow tags. Dial one record and send it one text and one letter, then check that its call attempts, texts sent and mail attempts each went up. A tool that does not raise the counter leaves the record in its lane forever.
- **Tag and list triggers fire on a person's change, not on an upload.** Use a Property Status Change trigger for anything that must run on uploaded records.
- **A sequence cannot start another sequence.** A chain of follow-up tasks needs one sequence per step, each on a Task Completed trigger.
- **A card moved over the API fires the board's card-moved sequences** within a second, the same as a person dragging it.
- **Never put a fixed start date on a "recently sold" auto-tag.** "Recently" grows every month, and a new lead's own purchase starts reading as a sale. A sale counts only when it happened after the record became a lead.
- **SiftMap auto-add spends record allowance the moment it is on.** Point a feeder at your own county and buy box before you switch it on.
- **Smarter Contact cannot drive DataSift drip campaigns.** Drips need smrtPhone, Twilio or Plivo.
- **The count index lags behind writes.** Do not prove a write by a count; read the record back.
