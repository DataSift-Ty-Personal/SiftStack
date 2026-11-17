# Team SOP Library

Agent-executable SOPs (`.sop.md`) live here. Each one is the machine twin of a human SOP produced by the `playbook-creator` skill, following the open Agent SOPs standard (strands-agents/agent-sop).

## How these get used

- **In Claude Code:** the project `.mcp.json` serves this directory through the `strands-agents-sops` MCP server. Every file here becomes a slash prompt (`/agent-sops:<name>`); the agent asks for the SOP's parameters and runs the steps.
- **Anywhere else:** paste the file's contents to any capable agent. The format is self-executing: parameters up top, RFC 2119 constraints per step.

## Adding an SOP

1. Create it with the playbook-creator skill (it produces the human doc and this twin together), or write it by hand from `skills/playbook-creator/references/agent-sop-format.md`.
2. Name it kebab-case, ending `.sop.md`.
3. Validate, and re-validate after every edit:

```bash
python skills/playbook-creator/scripts/validate_sop.py sops/<name>.sop.md
```

4. Fix every error before committing. Warnings are judgment calls; read them.

## Rules of the house

- Every negative constraint carries a because.
- Every step that produces something names its artifact path.
- Parameters name env vars when a credential is involved, never the value.
- The human Word doc is the training material; this file is the runbook. Keep both when both exist.
