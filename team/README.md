# The team folder

Four AI employees work here: Atlas (Analysis), Diggs (Research), Roma (Sales Assistant) and
Quinn (Operations). You talk to them in Slack by name ("Roma, quick screen 123 Main St") or in
their channel; DataSift sequences and schedules wake them too. Each one owns specific workflows,
hands everything else to the teammate who owns it, and asks you in the thread when it needs an
answer.

| File | What it is | Who edits it |
|---|---|---|
| `roster.yaml` | Who works here, their channels, models and skills, and every workflow with its owner, trigger, spend class and status | You |
| `<role>/CLAUDE.md` | One employee's brief: how it works and the traps it must avoid | You |
| `<role>/brain/` | Optional reference docs that employee reads (methods, baselines) | You |
| `TEAM.md` | The org chart and shared rules every employee reads. Generated, never edit by hand | `cli.py team` |

The `<role>` folders are named by role (`analysis`, `research`, `sales`, `ops`), so a persona can
be renamed without touching anything else.

## Common changes

- **Rename an employee:** change `display_name` (and its `introduction`) in `roster.yaml`.
- **Teach an employee a method:** add the skill's folder name to its `skills:` list. Skills live in
  the repo's `skills/` library and are copied into the employee's workspace at run time.
- **Add an employee:** add a block under `employees:`, create `<id>/CLAUDE.md`, and give it the
  workflows it owns (`owner: <id>`).
- **Wire a workflow:** write its handler, register it in `src/agents/worker.py` `HANDLERS`, and set
  `status: live`. Until then the employee says plainly that the workflow runs by hand today.

After any change:

```bash
python src/agents/cli.py team        # validates everything at once and regenerates TEAM.md
python src/agents/cli.py selftest    # zero network
python src/agents/cli.py hello       # the team introduces itself (free, no model)
python src/agents/cli.py ask "Roma, quick screen 123 Main St"   # one real turn
```

## Rules the code enforces (not just the briefs)

- An employee only has its own workflows as tools; a teammate's work is a handoff.
- No shell, no file writes, no edits. Reads stay inside the employee's own workspace.
- Handoffs stay on one thread and are counted; a chain that runs long is stopped.
- Paid work runs inside the per-run and per-day caps. Pulling records into an account needs your
  yes. Anything that contacts a homeowner or buyer is staged for a person to release.
- Dry mode (the default) pays no vendor and writes nothing to DataSift.

## Deploy

```bash
python deploy/agents_up.py            # the plan; changes nothing
python deploy/agents_up.py --commit   # Fly app, volume, secrets, deploy, Slack link, DataSift, hello
```

It needs the DataSift Open API key and an Anthropic key in `.env`, and `fly auth login`. It never
asks for a DataSift password.
