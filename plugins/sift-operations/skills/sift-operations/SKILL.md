---
name: sift-operations
description: >
  Operations encyclopedia for REI Sift (DataSift). Covers sequences and
  automations, niche and bulk sequential marketing, filter presets and the
  preset map, SiftLine boards and moving cards between them, drip campaigns,
  events, tasks and task presets, tags, filters, skip tracing workflows, list
  management and record import, property statuses, lead pipeline and speed to
  lead, acquisitions and transactions workflows, follow-up cadence,
  round-robin assignment, team setup, and the STABM daily routine. Use for
  how do I build a sequence, what sequences should I build, how to organize
  my marketing, pendulum theory, call or mail attempts filters, deep
  prospecting workflow, status workflow, Sift best practices, Sift
  walkthrough, how does Sift work, or any question about configuring,
  troubleshooting or optimizing workflows inside Sift.
version: 1.0.0
---

# REI Sift Operations Encyclopedia

Complete operational knowledge base for REI Sift (DataSift.ai). Covers sequences, filter presets, niche/bulk sequential marketing, SiftLine boards, drip campaigns, events/tasks, daily operations, and troubleshooting.

**When to use this skill:** Any time a user asks about configuring, building, troubleshooting, or understanding workflows inside REI Sift. This includes sequence setup, preset building, board management, lead pipeline configuration, daily operational routines, team setup, and import workflows.

## Execution Mode Detection

This is a routing/reference skill that directs to other skills for implementation. When Playwright automation is available (Claude Code CLI with `DATASIFT_EMAIL`/`DATASIFT_PASSWORD` set), the following companion skills provide automated execution:

| Task | Companion Skill | Automation Script |
|------|----------------|-------------------|
| Filter preset management | `sequential-presets.skill` | `sequential-presets/scripts/manage_presets.py` |
| Sequence creation | `sift-sequences.skill` | `sift-sequences/scripts/manage_sequences.py` |
| Phone tag upload | `phone-validator.skill` | `phone-validator/scripts/upload_phone_tags.py` |
| Market research | `sift-market-research.skill` | `sift-market-research/scripts/extract_market_finder.py` |

Those scripts live in the companion skill, not in this plugin. Each one uses `scripts/datasift_core.py` for login, cookie persistence, and popup dismissal, and this plugin ships its own copy of that helper. Credentials are loaded from `.env` or environment variables, never hardcoded.

## How to Use This Skill

When a user asks a Sift question:

1. **Identify the domain** -- determine which area of Sift the question falls into
2. **Load the right reference** -- read the appropriate reference file(s) for detailed configurations
3. **Respond in hybrid format** -- give a brief concept explanation, then provide the step-by-step walkthrough
4. **Generate docs when useful** -- for complex preset maps or sequence plans, create a markdown or Excel file the user can keep
5. **Fall back gracefully** -- if the question doesn't match a specific category, start with general-operations.md and the Core Concepts section below

## Domain Routing Table

| User Question About | Reference File to Read |
|---|---|
| Sequences (general), how sequences work, sequence anatomy | `references/sequences-core.md` |
| Lead management sequences, follow-up chains, temperature cadences | `references/lead-management-sequences.md` |
| Acquisitions sequences, offer follow-up, offer outcomes | `references/acquisitions-sequences.md` |
| Board-to-board workflows, moving/duplicating cards between boards | `references/board-workflows.md` |
| Drip campaigns, SMS/email nurture, delayed follow-ups | `references/drip-campaigns.md` |
| Events, tasks, task presets, appointments, Google Calendar | `references/events-and-tasks.md` |
| Designing/ideating custom sequences, naming conventions | `references/sequence-ideation.md` |
| Sequential presets (niche), first-to-market filter presets | `references/niche-sequential-presets.md` |
| Sequential presets (bulk), stacked data filter presets | `references/bulk-sequential-presets.md` |
| Filter configurations, exact filter block settings | `references/filter-configurations.md` |
| General Sift operations, navigation, tags, lists, statuses, skip tracing, STABM, daily routine, pipeline, import | `references/general-operations.md` |
| Troubleshooting sequences, drips, tasks, presets, or UI issues | `references/troubleshooting.md` |

**Important**: Always read the relevant reference file(s) before answering. Multiple references may be needed for complex questions.

## Response Format

Follow this hybrid format for every response:

### 1. Concept Brief (2-4 sentences)
Explain what the feature is and why it matters. Ground the user.

### 2. Step-by-Step Walkthrough
Provide numbered, click-by-click instructions they can follow inside Sift. Include exact field names, menu locations, and settings.

### 3. Configuration Details
For sequences: show the trigger, condition, and action(s) in a table.
For presets: show the filter blocks and their settings.
For workflows: show the full flow from trigger to outcome.

### 4. Best Practices & Tips
Include 2-3 actionable recommendations specific to what they're building.

### 5. File Output (when appropriate)
For complex configurations (preset maps with 5+ presets, multi-sequence workflows, full cadence plans), generate a markdown file the user can reference later.

### Document Generation Criteria

Generate a standalone file when:
- The user asks for a full preset map (5+ presets with filter configs)
- The user needs a multi-sequence workflow plan (3+ interconnected sequences)
- The user requests a cadence calendar or marketing timeline
- The output would exceed ~50 lines of configuration detail
- The user explicitly asks for something they can "save" or "reference later"

Do NOT generate a file when:
- The user asks a single how-to question
- The answer is a short walkthrough (under 20 steps)
- The user asks for explanation or concept clarification
- The answer is troubleshooting guidance

## Core Sift Concepts

### What is REI Sift (DataSift.ai)?

REI Sift is a CRM purpose-built for real estate investors. It manages leads from first contact through closing via automated workflows. The platform centers on five pillars: Records (property data), Sequences (automations), Filter Presets (segmentation), SiftLine (visual boards), and Events (tasks/appointments).

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### The Sift Automation Ecosystem

| Component | What It Is | Where to Find It |
|---|---|---|
| Sequences | Automations triggered by status/card/tag changes | Left sidebar -> Sequences |
| Drip Campaigns | Delayed SMS/Email sequences over time | Left sidebar -> Drip Campaigns |
| Events | Container for tasks and appointments | Left sidebar -> Events |
| Tasks | Individual action items with deadlines | Events section or property records |
| Task Presets | Reusable task templates used by sequences | Events -> Configure Presets |
| SiftLine | Kanban boards for visual workflow management | Left sidebar -> SiftLine |
| Filter Presets | Saved filter configurations for quick data segmentation | Properties -> Presets |

### How Components Connect

```
Record Upload -> Status set to "New Lead" -> Sequence fires
  -> Creates Task (from Preset) -> Task appears in Events
  -> Adds to Drip Campaign -> Drip sends SMS/Email over days
  -> Creates/Moves Card on SiftLine board

Task Completed -> Can trigger another Sequence -> Next step in pipeline
```

### Lead Pipeline Statuses

Sift uses these property statuses to track lead progression:

| Status | Meaning | Typical Next Status |
|---|---|---|
| New Lead | Just imported, no contact yet | Qualified or Not Interested |
| Qualified | Meets investment criteria, worth pursuing | In Progress |
| In Progress | Active negotiations or due diligence | Contract or Dead |
| Contract | Under contract, heading to close | Closed |
| Closed | Deal completed | (terminal) |
| Sold | Property sold (market change) | (terminal) |
| Dead | Deal fell through | (terminal, can reactivate) |
| Not Interested | Owner declined | (terminal, quarterly re-engagement) |
| DNC | Do Not Contact | (terminal) |

### Sequence Quick Reference

Every sequence has three parts:

1. **Trigger** (required) -- the event that starts it (status change, tag added, card moved, etc.)
2. **Condition** (optional) -- additional filter (e.g., "only if status changed FROM New Lead TO Qualified")
3. **Action** (required) -- what happens (create task, change status, add tag, send SMS, etc.)

**10 trigger types:** status_change, tag_added, tag_removed, list_added, list_removed, task_completed, task_created, assignee_change, card_created, card_moved

**9 action types:** change_status, add_tag, remove_tag, add_to_list, remove_from_list, clear_tasks, clear_assignee, create_task, send_sms

**26 pre-built sequences** across 5 folders: Lead Management (6), Acquisitions (6), Transactions (6), Deep Prospecting (4), Default (4).

To create a sequence: Left sidebar -> Sequences -> Create New Sequence -> drag Trigger -> drag Condition (optional) -> drag Action(s) -> name it -> select folder -> Save.

### Filter Preset Quick Reference

Filter presets are saved search configurations that segment your records. They use filter blocks combined with AND logic (all blocks must match). Within a single block like "Any Lists (OR)", items use OR logic.

**Key filter blocks:** Any Lists (OR), Any Tags (OR), All Tags (AND), Property Status, Call Attempts (min/max), Direct Mail Attempts (min/max), Phone Statuses, Params & Others (Numbers, Skiptraced, Vacant Mailing).

To create a preset: Properties -> open filter panel -> configure blocks -> scroll to bottom -> Filter Presets section -> Save New.

### Sequential Marketing Overview

Sequential marketing organizes leads into numbered filter presets that guide them through a multi-channel outreach cadence. Two folders, two strategies:

| Aspect | Niche Sequential | Bulk Sequential |
|---|---|---|
| Folder Name | **00 Niche Sequential Marketing** | **01. Bulk Sequential Marketing** |
| Data Type | First-to-market / Tier 1 (probates, foreclosures, courthouse data) | Tier 2/3 (stacked lists, AI-enriched, purchased lists) |
| Calling Method | Manual click-to-dial | Multi-line power dialer |
| Urgency | High | Low to Medium |
| Preset Count | 12 presets (00-11) | 9 presets (00-08) |
| All presets exclude | Property Status: Sold | Property Status: Sold |

**Niche presets (12):** 00. Needs Skip Traced, 01. Ready to Text, 02. Needs Called Day 1, 03. Needs Called Day 2, 04. Needs Called Day 3, 05. Needs Mailed, 06. Needs Deep Prospecting, 07. Callback Scheduled, 08. Hot Lead, 09. Not Interested, 10. Bad Data, 11. Completed Cycle

**Bulk presets (9):** 00. Bulk Needs Skipped, 01. Bulk Skipped NN, 02. Bulk Ready to Call, 03. Bulk Call Follow Up, 04. Bulk Needs 1st Mail, 05. Bulk Mail Monthly, 06. Bulk Not Interested, 07. Exhausted CC -> DP, 08. Bulk Return Mail -> DP

**Niche 3-day calling cycle:**
- Day 1: SMS (text first, warm up the lead)
- Day 2: Call attempt 1 (manual click-to-dial)
- Day 3: Final call attempt + final SMS if no answer

**Phone tier dialing order:** Dial First (score 81-100) -> Dial Second (61-80) -> Dial Third (41-60) -> Dial Fourth (21-40) -> Drop/skip (0-20)

### The Pendulum Theory

Marketing activities sequenced from lowest to highest cost per touch:
SMS -> Cold Calling -> Direct Mail -> Deep Prospecting -> Door Knocking

Start cheap, escalate only when cheaper channels are exhausted.

### The 3 Core Workflow Questions

Every daily session should answer:
1. What new data needs to be processed (skip traced)?
2. What data is ready for its first marketing touch?
3. What data has been marketed to but requires follow-up?

### Sequence Limits by Plan

| Plan | Sequence Limit |
|---|---|
| Essentials (grandfathered) | 3 |
| Professional | 8 |
| Business | Unlimited |

### User Permissions for Sequences

Roles that can create/edit sequences: Sensei, Super Admin, Admin, Marketer.

### Default Account Setup (Accounts After 4/16/2025)

Included by default: Lead Management, Acquisitions, and Transactions boards with pre-built sequences, task presets, and filter presets. See `references/general-operations.md` for the complete default inventory.

## DataSift UI Automation Notes

These patterns matter when automating Sift via Playwright or discussing UI behavior with users:

- **No native HTML controls**: All dropdowns are styled-components (`[class*="Selectstyles__Select"]` containers), not native `<select>` elements
- **Beamer NPS survey**: The `#npsIframeContainer` iframe can block all pointer events globally -- must be dismissed/removed before interacting with the UI
- **Panel scrolling**: The filter panel is a scrollable `<div>`, NOT the viewport. Standard scroll methods fail; use `el.scrollIntoView({behavior: 'instant', block: 'center'})` via JS
- **Filter Presets location**: The Filter Presets section is at the BOTTOM of the filter panel -- must scroll the container down to reveal it
- **Preset naming pattern**: All preset names follow the pattern `^\d{2}\.` (e.g., "00. Needs Skipped", "01. Skipped No Numbers")
- **Folder naming**: Two folders exist: "00 Niche Sequential Marketing" (12 presets) and "01. Bulk Sequential Marketing" (9 presets). Note: "00 Niche" has no period after the number; "01." does
- **Sold exclusion**: All 21 presets across both folders have Property Status set to "Do not include" -> "Sold"
- **Sidebar width**: Sidebar occupies 0-400px. Use `x > 450` bounds check in JS queries to avoid matching sidebar elements when targeting main-panel dropdowns

## Fallback Guidance

If a user's question does not clearly fit one of the routing categories:

1. Start with `references/general-operations.md` for broad operational questions
2. Check `references/troubleshooting.md` for "it's not working" type questions
3. For "what should I build" questions, check `references/sequence-ideation.md`
4. For questions about a specific feature you are unsure about, explain what you know from the Core Concepts above, and suggest the user check Sift's in-app help or contact support for the most current UI details
5. Never guess at exact menu paths or button names -- if unsure, say so and provide the closest known reference
