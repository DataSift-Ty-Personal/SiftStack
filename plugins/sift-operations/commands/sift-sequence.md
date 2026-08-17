---
description: Build a Sift sequence step-by-step
allowed-tools: Read, Write, Edit, Bash
argument-hint: [description of what you want to automate]
---

The user wants to build a Sift sequence. Follow this process:

1. Read the skill file at `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/SKILL.md` to load the domain routing table.

2. Based on the user's description ($ARGUMENTS), determine which type of sequence they need and read the appropriate reference files:
   - For lead management: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/lead-management-sequences.md`
   - For acquisitions: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/acquisitions-sequences.md`
   - For board workflows: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/board-workflows.md`
   - For general ideation: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/sequence-ideation.md`
   - For drip integration: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/drip-campaigns.md`

3. If the user's description is vague, use the ideation discovery questions to ask clarifying questions:
   - What event should trigger this?
   - What conditions should filter it?
   - What actions should happen?
   - Who should be assigned?

4. Present the sequence configuration in a clear table format:
   - Trigger (with exact settings)
   - Condition(s) (with exact settings)
   - Action(s) (in order, with exact settings)

5. Provide the step-by-step walkthrough for building it in Sift:
   - Navigate to Sequences → Create New Sequence
   - Add each component with exact field values
   - Name and save

6. Include best practices and common pitfalls specific to this sequence type.

7. If the sequence is complex (multi-sequence chain, board workflow), generate a markdown reference document saved to the workspace folder.
