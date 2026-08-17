---
description: Get a Sift operational walkthrough
allowed-tools: Read, Write, Edit, Bash
argument-hint: [what you want to do in Sift]
---

The user wants a walkthrough for a specific Sift operation. Follow this process:

1. Read the skill file at `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/SKILL.md` to load the domain routing table.

2. Based on the user's description ($ARGUMENTS), identify what they want to do and read the appropriate reference(s):
   - Sequences: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/sequences-core.md`
   - Lead management: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/lead-management-sequences.md`
   - Acquisitions: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/acquisitions-sequences.md`
   - Board workflows: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/board-workflows.md`
   - Drip campaigns: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/drip-campaigns.md`
   - Events/tasks: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/events-and-tasks.md`
   - Filter presets: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/filter-configurations.md`
   - General operations: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/general-operations.md`
   - Troubleshooting: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/troubleshooting.md`

3. Provide the response in hybrid format:
   a. **Concept Brief** (2-4 sentences) — explain what this feature is and why it matters
   b. **Step-by-Step Walkthrough** — numbered, click-by-click instructions with exact menu locations and field names
   c. **Configuration Details** — tables showing exact settings
   d. **Best Practices** — 2-3 actionable tips
   e. **Related Features** — mention connected features they might also want to set up

4. If the walkthrough involves multiple interconnected features (e.g., setting up a full lead management system), generate a comprehensive reference document saved to the workspace folder.
