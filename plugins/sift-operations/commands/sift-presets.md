---
description: Design sequential marketing filter presets
allowed-tools: Read, Write, Edit, Bash
argument-hint: [niche or bulk]
---

The user wants to design sequential marketing filter presets. Follow the consultative workflow:

1. Read the skill file at `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/SKILL.md` for context.

2. Determine if they need Niche or Bulk sequential presets based on $ARGUMENTS. Read the appropriate reference:
   - For Niche: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/niche-sequential-presets.md`
   - For Bulk: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/bulk-sequential-presets.md`
   - Always also read: `${CLAUDE_PLUGIN_ROOT}/skills/sift-operations/references/filter-configurations.md`

3. **Discovery** — Ask the user:
   - Strategy: Niche Sequential (first-to-market) or Bulk Sequential (stacked data)?
   - Niche Lists: What specific lists are they targeting?
   - Marketing Channels: Which channels, in what order?
   - Team Structure: Who handles each part?
   - Data Tags: What tag identifies their marketing list?
   - Attempt Cadence: How many call attempts before moving to next stage?

4. **Design** — Start from the base template and customize:
   - Adjust lists and tags to match their specific data
   - Modify attempt thresholds based on their cadence
   - Add or remove presets based on their marketing channels

5. **Present** — Create a complete preset map document with:
   - Folder name
   - Numbered preset table with names and purposes
   - Exact filter block configurations for each preset
   - Save as a markdown file to the workspace folder

6. **Implementation Guidance** — Walk them through building each preset:
   - Start with "00. Needs Skipped"
   - Build in numerical order
   - Test each preset after creation
