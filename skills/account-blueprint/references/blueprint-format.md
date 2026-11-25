# Blueprint file format (version 1)

A blueprint is a JSON file describing an account's structure by TITLE. It
carries exactly one uuid, `source.account_uuid`, which exists so the apply
step can refuse to clone an account onto itself. Everything that was a uuid in
the source account is a reference the apply step resolves against the target.

## Top level

```json
{
  "blueprint_version": 1,
  "exported_at": "2026-09-10T17:55:00Z",
  "source": {"label": "account", "email": "...", "account_uuid": "<uuid>",
             "auth_by_family": {"presets": "api_key", "...": "..."}},
  "statuses": [...], "lists": [...], "tags": [...],
  "custom_field_groups": [...], "custom_fields": [...],
  "task_groups": [...], "task_presets": [...],
  "boards": [...], "users": [...],
  "sequence_folders": [...], "sequences": [...],
  "preset_folders": [...], "siftmap_presets": [...],
  "export_log": [...], "warnings": [...]
}
```

## References

```json
{"$ref": "tag", "title": "Priority 1"}
{"$ref": "list", "title": "Auction"}
{"$ref": "status", "title": "Hot Lead"}
{"$ref": "board", "title": "Lead Management"}
{"$ref": "column", "board": "Lead Management", "title": "New Lead (Unqualified)"}
{"$ref": "task_preset", "group": "Lead Management", "title": "Call New Lead"}
{"$ref": "user", "title": "Emerson"}
{"$ref": "self"}
{"$unresolved": "<uuid>", "path": "actions[0].payload.values.column"}
```

`$unresolved` means the export could not name that uuid (a deleted column,
for instance). The apply step treats it as a gap and never copies the value.

## Families

- `statuses[]`: `title, color, is_active, order, system`. System statuses
  (`system: true`) exist in every account and are never created. A missing
  custom status is a checklist step on the Open API key (title and color),
  since no published route creates one.
- `lists[]`: `title`.
- `tags[]`: `title, why[]`. Only tags referenced by presets, sequences or
  SiftMap presets, plus a small anchor allowlist. Cohort and import-batch
  tags never ship.
- `custom_field_groups[]`: `title, entity_type, position`.
- `custom_fields[]`: `label, field_type, entity_type, group, required,
  placeholder, position, options[{label, value}]`.
- `task_groups[]`: `title`. `task_presets[]`: `group, title, notes,
  round_robin, expires_in, all_day, due_time, assigned_to_user, assigned_to_users,
  assigned_to_role, order, skip_weekends`.
- `boards[]`: `title, columns[]`. On the Open API key, a missing board or
  column is a checklist step in the report (no published route creates them).
- `users[]`: `first_name, role, is_active`. First name is the join key for
  assignee references. No last names, no emails.
- `sequence_folders[]`: `title`. `sequences[]`: `title, folder, is_active,
  trigger, conditions[], actions[], manual[]`. Actions that sent SMS or email in
  the source are replaced by `{"action": "send-sms", "$manual": true, "kept": {...}}`.
  Sequences are not in DataSift's published API, so on the Open API key each
  missing folder and sequence becomes Sequence Builder steps in the report.
- `preset_folders[]`: `title, type, numbered, presets[{title, quick_filter,
  filters, source_count, source_count_approx}]`. `filters.must` is the saved
  filter with references in place of uuids; `filters.account` is never present.
- `siftmap_presets[]`: `name, description, auto_add_enabled,
  replace_owners_enabled, email_enabled, lists, tags, filter_data, counties`.
  Lists and tags here are titles already.

## Validation

`python clone_account.py --phase validate --blueprint <file>` checks: the
version, every family key present, every `$ref` resolves inside the file,
every status has a color, no preset has an empty `must`, preset and sequence
titles are unique, no `filters.account`, no junk-shaped tag, and a scan of the
whole file for uuids (other than the source account), phone numbers and
emails. A hit on the scan is an error, not a warning.
