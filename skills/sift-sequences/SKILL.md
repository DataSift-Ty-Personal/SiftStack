---
name: sift-sequences
description: Complete guide for creating, managing, and ideating Sift sequences (automations). Use when user needs help setting up sequences, understanding sequence triggers/conditions/actions, troubleshooting sequences, designing automation workflows, or understanding how Events, Tasks, Sequences, and Drip Campaigns work together in Sift.
---

# Sift Sequences

This skill both **guides** and **helps execute** sequence creation in DataSift. It is not just consultative -- it provides step-by-step UI walkthrough instructions, pre-built TCA templates verified against source code, and hands-on support for building sequences directly inside the DataSift interface.

Sequences are automations in Sift that trigger actions when specific events occur. They ensure no lead slips through the cracks by automatically creating tasks, changing statuses, moving cards, and sending notifications.

## Execution Mode Detection

Before starting, detect the execution environment:

**Check 1:** Does `scripts/manage_sequences.py` exist in this skill's directory?
**Check 2:** Is Playwright available? Run: `python -c "from playwright.sync_api import sync_playwright; print('OK')"`
**Check 3:** Are credentials set? Check for `DATASIFT_EMAIL` and `DATASIFT_PASSWORD` in `.env` or environment.

### Automated Mode (Claude Code CLI — all 3 checks pass)

Run sequence management directly:

```bash
# List all existing sequences
python scripts/manage_sequences.py --list

# Create the Sold Property Cleanup sequence
python scripts/manage_sequences.py --create-sold-sequence
```

The script handles login, navigation to /sequences, React DnD drag-and-drop (which requires slow mouse-based dragging, not Playwright's native drag), and sequence configuration. After running, proceed to the template design section below for additional sequences.

### Manual Mode (Co-Work or no Playwright — any check fails)

Follow the step-by-step UI walkthrough instructions below. Claude will guide you through each drag-and-drop operation in the DataSift sequence builder.

## Quick Reference

| Component | Purpose | Required |
|-----------|---------|----------|
| Trigger | Event that starts the automation | Yes |
| Condition | Additional rules that filter which records the sequence acts on | No |
| Action | What happens when triggered and conditions are met | Yes |

## When to Use This Skill

Use this skill when the user wants to:
1. **Create** a new sequence from scratch (includes UI walkthrough)
2. **Deploy** one or more of the 26 TCA sequence templates
3. **Ideate** sequence workflows for their business
4. **Troubleshoot** why a sequence is not working
5. **Understand** how sequences interact with SiftLine, tasks, and statuses
6. **Learn** how Events, Tasks, Sequences, and Drip Campaigns work together
7. **Build** follow-up chain sequences (HOT A01-A16)
8. **Configure** SMS/Email integrations for sequence actions

## The Sift Automation Ecosystem

Understanding how the four core components work together is essential before building sequences.

### How Events, Tasks, Sequences, and Drip Campaigns Connect

| Component | What It Is | How It Connects |
|-----------|-----------|-----------------|
| **Events** | Container for Tasks and Appointments in your account | Tasks created by sequences appear here |
| **Tasks** | Individual action items with deadlines | Created manually or automatically via sequences |
| **Task Presets** | Reusable task templates | Used by sequences to auto-create consistent tasks |
| **Sequences** | Automations triggered by status/card/tag changes | Create tasks, move cards, add to drip campaigns |
| **Drip Campaigns** | Delayed SMS/Email sequences over time | Added to records via sequence actions |

### The Integration Flow

```
Status Change -> Sequence Triggers -> Creates Task (from Preset) -> Task appears in Events
                                   -> Adds to Drip Campaign -> Drip sends SMS/Email over days
                                   -> Moves Card on SiftLine

Task Completed -> Can trigger another Sequence -> Creates next Task in chain
```

### Events Section Overview

The **Events** section of Sift is where you manage all appointments and tasks. Key features:

| Feature | Description |
|---------|-------------|
| All Events Tab | View appointments and tasks combined |
| Filtering | Filter by date range, assigned user, or assigner |
| Task Presets | Access reusable task templates |
| Google Calendar | Sync appointments and tasks with your calendar |

**Appointments vs. Tasks**: Appointments include a location and outcome tracking. Tasks are action items with deadlines and recurrence options.

For complete Events documentation, see: `references/events-overview.md`

---

## Creating a Sequence: Step-by-Step UI Walkthrough

This section walks through the exact DataSift interface steps for creating a sequence. Follow these steps in order.

### Step 1: Navigate to Sequences

Navigate to `/sequences` in DataSift (left sidebar, click "Sequences").

### Step 2: Create New Sequence

Click the **"Create"** button. You will see:
- **Title field**: Enter a descriptive name (e.g., "LM - New Lead Assignment")
- **Folder dropdown**: Select or create a folder to organize the sequence (e.g., "Lead Management", "Acquisitions")

### Step 3: Add a Trigger (Required)

Drag a trigger card from the left sidebar onto the sequence canvas.

**DataSift UI note**: Sidebar cards can scroll out of view. If you do not see the trigger you need, scroll down within the sidebar panel.

Available triggers:

| Trigger | Use When |
|---------|----------|
| Property Status Change | Lead status updates (most common) |
| Property Assignee Change | Ownership transfers |
| Property Tags Added/Removed | Tag-based workflows |
| Property Lists Added/Removed | List membership changes |
| Task Created/Completed | Task-driven workflows |
| SiftLine Card Created | New cards on boards |
| SiftLine Card Moved | Cards change phases |

**Best first trigger recommendation**: If you are new to sequences, start with **Property Status Change -> New Lead**. This is the highest-value trigger because it fires every time a record enters your pipeline, ensuring no new lead goes unworked.

### Step 4: Add a Condition (Optional)

Conditions filter WHICH records the sequence acts on after the trigger fires. Drag a condition card from the sidebar if needed.

**Condition logic rules**:
- Only **one condition block** per sequence (you cannot add multiple separate condition blocks)
- Within a condition block, all criteria use **AND logic** -- every condition must match for the sequence to proceed
- If no condition is set, the sequence fires for ALL records matching the trigger

**Common conditions**:

| Condition | Example Use |
|-----------|-------------|
| has_tag | Only fire if record has "Courthouse Data" tag |
| has_status | Only fire if current status is "Hot Lead" |
| in_list | Only fire if record is on "Foreclosure" list |
| has_phone | Only fire if record has a phone number (for SMS sequences) |
| phone_tier | Only fire if phone is Tier 1 (mobile/direct) |
| Property Status Change | From "Any" to "New Lead" (directional filter) |
| Property Tags | Has or doesn't have specific tags |
| Card Board & Column | Card is on specific board/phase |

### Step 5: Navigate to Actions

Click the **"Set the Following Actions"** button to navigate to the actions tab. Alternatively, if creating a new sequence, you can navigate directly via the URL pattern `/sequences/new/actions`.

### Step 6: Add Action(s) (Required)

Drag action cards from the sidebar onto the actions area.

**Important UI patterns**:
- The **first action** uses the initial drop zone on the canvas
- For the **2nd action and beyond**, you must click the **"Add new Action +"** button before dragging
- **React DnD behavior**: Action cards have `draggable="false"` set in the DOM. Standard browser drag will not work. DataSift uses a slow mouse drag pattern internally -- click and hold the card, then drag slowly to the drop zone

Available actions:

| Action | Common Use |
|--------|------------|
| Change Property Status | Update lead temperature |
| Assign Property | Route to team member (supports round-robin) |
| Add/Remove Property Tags | Categorization |
| Add/Remove Property Lists | List management |
| Clear Property Tasks | Reset task slate |
| Create New Task | Assign follow-up work (from Task Preset) |
| Create New Card | Add to SiftLine board |
| Move/Duplicate/Delete Card | SiftLine workflow automation |
| Send SMS/Email | Immediate notifications (leads only) |
| Add to Drip Campaign | Long-term nurture |

### Step 7: Save the Sequence

Click **"Save Sequence"** at the bottom of the page.

**Duplicate name handling**: If a sequence with the same name already exists, DataSift will show an error toast message ("different sequence title"). When this happens, retry with a " V2" suffix appended to the title (e.g., "New Lead Assignment V2").

### Editing Existing Sequences

To edit existing default sequences: Open sequence folder -> Click sequence name -> Select **"Make Changes"** -> Edit -> Save Sequence.

---

## Default Account Setup (Accounts Created After 4/16/2025)

**Important**: Default accounts include pre-built sequences, task presets, and boards. Understanding what comes included versus what you need to build yourself prevents confusion.

### What's INCLUDED in Default Accounts

| Category | Included Items |
|----------|---------------|
| **SiftLine Boards** | Lead Management, Acquisitions, Transactions, Wholesale, Flips, Rentals |
| **Sequences** | Lead Management, Acquisitions, and Transactions automations |
| **Task Presets** | Call New Lead, No Contact New Lead, Nurture New Lead, Cold/Warm/Hot Follow-up, Make Offer, Offer Follow-Up, Send Back to LM |
| **Filter Presets** | My Tasks, Acquisitions, Lead Management, Transactions, REISift Base Presets |
| **Property Statuses** | New Lead, No Contact New Lead, Cold/Warm/Hot Lead, Ghosting Lead, Dead Lead, Not Interested, Listed, Sold, Under Contract, Closed |

### Default Task Presets (Customizable)

These presets come pre-configured but can be adjusted to match your workflow.

| Task Preset | Purpose | Default Due | Frequency |
|-------------|---------|-------------|-----------|
| Call New Lead | Initial contact with new lead | Same day | Once |
| No Contact New Lead | Follow-up when lead hasn't responded | 3-5 days | Daily |
| Nurture New Lead | Long-term nurture for unqualified leads | 3-6 months | Weekly |
| Cold Follow-up | Re-engage cold leads | 45 days | Every 45 days |
| Warm Follow-up | Maintain warm lead relationships | 14 days | Every 14 days |
| Hot Follow-up | Aggressive follow-up for hot leads | 7 days | Every 7 days |
| Make Offer | Present offer to qualified lead | Same day | Once |
| Offer Follow-Up | Follow up after offer presented | 1-2 days | Daily |
| Send Back to LM | Return declined offers to Lead Management | 1 day | Once |

**Creating custom task presets**: Go to **Settings -> Task Presets** (or Events page -> Configure Presets). Create a new group to organize presets, then click "Add New Preset" to configure task name, assignment, deadline, and recurrence. Custom presets appear alongside defaults in the sequence action dropdown.

### What's NOT INCLUDED (Must Build Yourself)

**Clarification**: The following are recommended patterns from this skill documentation, NOT pre-built defaults:

| Pattern | Description | Why Build It |
|---------|-------------|--------------|
| Follow-up chain sequences (HOT A01-A16) | Task completion triggers next task | Creates automated cadence without manual intervention |
| Custom temperature cadences | Specific timing for your market | Tailors follow-up frequency to your business |
| Board-to-board workflows | Duplicate cards between boards | Tracks KPIs across workflow stages |
| Conditional routing | Route leads based on tags/value | Assigns high-value leads to senior team members |
| Drip campaign triggers | Add to drips on status change | Automates long-term nurture sequences |
| 26 TCA sequence templates | Pre-designed automations from source code | Full pipeline automation from intake to close |

---

## Core Philosophy

Sequences should be **rewards for consistency**, not crutches for forgetfulness. The recommended approach:
1. Get consistent with a manual process first
2. Prove the process works
3. Then automate with sequences to save time

---

## Sequence Anatomy

### Triggers (Required)

The event that starts the automation:

| Trigger | Use When |
|---------|----------|
| Property Status Change | Lead status updates (most common) |
| Property Assignee Change | Ownership transfers |
| Property Tags Added/Removed | Tag-based workflows |
| Property Lists Added/Removed | List membership changes |
| Task Created/Completed | Task-driven workflows |
| SiftLine Card Created | New cards on boards |
| SiftLine Card Moved | Cards change phases |

### Conditions (Optional)

Conditions filter WHICH records the sequence acts on. They are evaluated after the trigger fires and before any actions execute.

**Key rules**:
- **AND logic**: When multiple conditions are set, ALL must match
- **One condition block per sequence**: You cannot have separate OR condition groups
- If no condition is set, the sequence acts on every record that matches the trigger

| Condition | Example |
|-----------|---------|
| Property Status Change | From "Any" to "New Lead" |
| Property Assignee | Assignee is specific user |
| Property Tags | Has or doesn't have specific tags |
| Property Lists | On or not on specific lists |
| Card Board & Column | Card is on specific board/phase |
| SiftLine Card Moved | From specific board/phase to another |
| has_phone | Record has a phone number populated |
| phone_tier | Phone is Tier 1 (mobile/direct line) |

### Actions (Required)

What happens when trigger fires and conditions are met:

| Action | Common Use |
|--------|------------|
| Change Property Status | Update lead temperature |
| Assign Property | Route to team member (supports round-robin) |
| Add/Remove Property Tags | Categorization |
| Add/Remove Property Lists | List management |
| Clear Property Tasks | Reset task slate |
| Create New Task | Assign follow-up work |
| Create New Card | Add to SiftLine board |
| Move/Duplicate/Delete Card | SiftLine workflow automation |
| Send SMS/Email | Immediate notifications (leads only) |
| Add to Drip Campaign | Long-term nurture |

---

## Follow-Up Chain Sequences (HOT A01-A16)

Follow-up chain sequences are 16 sequential automations that fire in order, creating an automated follow-up cadence for hot leads. Each sequence creates a task and, upon that task's completion, triggers the next sequence in the chain.

**How it works**: Completing task A01 triggers the sequence that creates task A02. Completing A02 triggers the sequence that creates A03. This continues through all 16 steps, giving you a 26-day automated follow-up cadence with no manual scheduling.

**Why 16 separate sequences**: DataSift sequences cannot chain -- one sequence cannot trigger another sequence directly. Instead, each step uses a "Task Completed" trigger to detect when the previous step finishes, then creates the next task. This requires one sequence per transition.

```
Task "HOT Follow-Up A01" completed
    |
Sequence "Hot A01 Complete" triggers
    |
Creates Task "HOT Follow-Up A02" (Due: 1 day)
    |
Task "HOT Follow-Up A02" completed
    |
Sequence "Hot A02 Complete" triggers
    |
Creates Task "HOT Follow-Up A03" (Due: 1 day)
    |
(Pattern continues through A16...)
```

### Building a Follow-Up Chain

**Step 1**: Create Task Presets for each step (HOT Follow-Up A01, A02, A03, etc.) in Settings -> Task Presets.

**Step 2**: Create a sequence for each transition:

**Sequence: Hot Follow-Up A01 Complete**

| Component | Setting |
|-----------|---------|
| **Trigger** | Task Completed |
| **Condition** | Task Is: "HOT Follow-Up A01" |
| **Action** | Create Task: "HOT Follow-Up A02" (Due: 1 day) |

**Sequence: Hot Follow-Up A02 Complete**

| Component | Setting |
|-----------|---------|
| **Trigger** | Task Completed |
| **Condition** | Task Is: "HOT Follow-Up A02" |
| **Action** | Create Task: "HOT Follow-Up A03" (Due: 1 day) |

Repeat this pattern for each follow-up task in your cadence.

### Recommended Hot Lead Cadence (26 days, 16 sequences required)

| Task | Day | Due After Previous | Sequence to Build |
|------|-----|-------------------|-------------------|
| A01 | 1 | 1 day | Hot A01 Complete |
| A02 | 2 | 1 day | Hot A02 Complete |
| A03 | 3 | 1 day | Hot A03 Complete |
| A04 | 5 | 2 days | Hot A04 Complete |
| A05 | 7 | 2 days | Hot A05 Complete |
| A06 | 9 | 2 days | Hot A06 Complete |
| A07 | 11 | 2 days | Hot A07 Complete |
| A08 | 13 | 2 days | Hot A08 Complete |
| A09 | 15 | 2 days | Hot A09 Complete |
| A10 | 17 | 2 days | Hot A10 Complete |
| A11 | 19 | 2 days | Hot A11 Complete |
| A12 | 21 | 2 days | Hot A12 Complete |
| A13 | 23 | 2 days | Hot A13 Complete |
| A14 | 25 | 2 days | Hot A14 Complete |
| A15 | 26 | 1 day | Hot A15 Complete |
| A16 | 26 | 0 days | (End of chain) |

**Total sequences required for Hot Lead chain**: 15 sequences (A16 is the final task, no sequence needed after it)

**Naming convention**: "HOT A01" through "HOT A16" for task presets; "Hot A01 Complete" through "Hot A15 Complete" for sequences.

For complete cadence configurations (Warm, Cold, Dead), see: `references/lead-management-sequences.md`

---

## 26 TCA Sequence Templates (Verified Against Source Code)

These 26 templates use the TCA (Trigger-Condition-Action) model. Accounts created after 4/16/2025 come with these pre-configured. For older accounts, create them manually using the step-by-step UI walkthrough above.

### When NOT to Use Sequences

Do not automate before you understand the manual process. Sequences amplify mistakes at scale.

- **Don't automate lead routing until you've manually routed 20+ leads** — you need to understand your own criteria before encoding them
- **Don't build follow-up chains until you've manually followed up for 2+ weeks** — cadence and messaging need real-world testing first
- **Don't auto-assign records until roles are clearly defined** — a sequence that assigns to the wrong person creates more work than it saves

### Lead Management Folder (6 Sequences)

| # | Name | Trigger | Actions |
|---|------|---------|---------|
| 01 | New Lead Assignment | status_change -> "New" | Create task: "Initial contact -- call/text within 1 hour", Add tag: new_lead |
| 02 | Hot Lead Alert | tag_added -> "hot" | Change status: Hot Lead, Create task: "URGENT: Contact hot lead immediately", Add tag: priority_contact |
| 03 | Follow-Up Reminder | task_completed -> "Initial contact" | Create task: "Follow-up call/text" (3 day delay), Add tag: contacted |
| 04 | Stale Lead Recycler | tag_added -> "stale_30d" | Change status: Nurture, Add to list: Nurture, Add tag: recycled, Create task: "Nurture check-in" (30 day delay) |
| 05 | Qualification Complete | tag_added -> "qualified" | Change status: Qualified, Create task: "Schedule appointment -- qualified lead", Remove tag: new_lead |
| 06 | DNC -- Do Not Contact | tag_added -> "DNC" | Change status: Do Not Contact, Clear tasks, Clear assignee, Remove from lists: Foreclosure/Probate/Tax Sale |

### Acquisitions Folder (6 Sequences)

| # | Name | Trigger | Actions |
|---|------|---------|---------|
| 07 | Offer Sent | status_change -> "Offer Sent" | Create task: "Follow up on offer" (2 day delay), Add tag: offer_pending |
| 08 | Counter Received | status_change -> "Counter" | Create task: "Analyze counter-offer -- run updated comps", Add tag: counter_received |
| 09 | Under Contract | status_change -> "Under Contract" | Create tasks: "Order title search" (0d), "Schedule inspection" (1d), "Verify financing" (2d), Add tag: under_contract, Remove tag: offer_pending |
| 10 | Contract Fallen Through | status_change -> "Dead Deal" | Add to list: Nurture, Add tag: dead_deal, Remove tag: under_contract, Create task: "90-day follow-up on dead deal" (90d delay) |
| 11 | Closing Scheduled | status_change -> "Closing" | Create tasks: "Confirm closing date and location" (0d), "Verify clear title" (1d), "Wire transfer / cashier's check" (2d), Add tag: closing_scheduled |
| 12 | Deal Closed | status_change -> "Closed" | Add tag: closed_deal, Remove tags: under_contract/closing_scheduled, Create task: "Record deed / update records" |

### Transactions Folder (6 Sequences)

| # | Name | Trigger | Actions |
|---|------|---------|---------|
| 13 | Sold Property Cleanup | tag_added -> "Sold" | Change status: Sold, Remove from lists: Foreclosure/Probate/Tax Sale, Clear tasks, Clear assignee. **Note: Already built in build 1.0.23** |
| 14 | Disposition Started | status_change -> "For Sale" | Create tasks: "Create marketing package" (0d), "List on MLS / send to buyer list" (1d), Add tag: disposition_active |
| 15 | Buyer Assigned | tag_added -> "buyer_matched" | Create tasks: "Draft assignment contract" (0d), "Coordinate buyer inspection" (1d), Add tag: assignment_pending |
| 16 | Assignment Complete | status_change -> "Assigned" | Create task: "Calculate and record profit", Add tag: deal_done, Remove tag: assignment_pending |
| 17 | Rehab Started | status_change -> "In Rehab" | Create task: "Weekly rehab progress check", Add tag: rehab_active |
| 18 | Rehab Complete | tag_added -> "rehab_complete" | Change status: For Sale, Create task: "Schedule final walkthrough and photos", Remove tag: rehab_active |

### Deep Prospecting Folder (4 Sequences)

| # | Name | Trigger | Actions |
|---|------|---------|---------|
| 19 | Needs Deep Prospecting | tag_added -> "needs_dp" | Create task: "Deep prospecting -- start Level 1 skip trace", Add tag: dp_in_progress |
| 20 | DP Complete | tag_added -> "dp_complete" | Remove tag: dp_in_progress, Create task: "Review DP findings -- route to marketing or archive" |
| 21 | Heir Located | tag_added -> "heir_found" | Create task: "Contact heir -- begin niche sequential", Add tag: heir_contact_pending, Remove tag: needs_dp |
| 22 | Title Issue Flagged | tag_added -> "title_issue" | Create task: "REFER TO TITLE ATTORNEY -- curative work needed", Change status: Title Review, Add tag: attorney_needed |

### Default Folder (4 Sequences)

| # | Name | Trigger | Condition | Actions |
|---|------|---------|-----------|---------|
| 23 | Welcome SMS | tag_added -> "new_record" | has_phone | Send SMS: intro message, Add tag: sms_sent |
| 24 | Skip Trace Needed | tag_added -> "no_phone" | (none) | Add to list: Skip Trace Queue, Create task: "Run skip trace for contact info" |
| 25 | Duplicate Detected | tag_added -> "duplicate" | (none) | Create task: "Review and merge duplicate record", Change status: Duplicate |
| 26 | Archive Inactive | tag_added -> "inactive_180d" | (none) | Change status: Archived, Remove from lists: Foreclosure/Probate/Tax Sale, Clear tasks |

### Deploying Templates

**New accounts (created after 4/16/2025):** These sequences are already set up — verify they exist under Sequences in your account.

**Older accounts:** Build each sequence manually using the UI walkthrough in this skill. Use the tables above as your configuration guide — they list the exact trigger, condition, and actions for each sequence.

**Recommended build order:** Start with Lead Management (sequences 01-06), then Acquisitions (07-12), then Default (23-26). Add Transactions and Deep Prospecting only after your pipeline is processing deals.

---

## SMS/Email Integration Setup

### Phone Provider Requirements

SMS sequences require an integrated phone provider. Without one, SMS actions in sequences will silently fail.

**Recommended integration**: **smrtPhone** is the recommended SMS integration for DataSift. It provides the best compatibility with sequences and drip campaigns.

**Supported providers for sequences and drip campaigns**:
- smrtPhone (recommended)
- Twilio
- Plivo

**Not supported for drip campaigns** (sequence direct SMS only):
- Kixie
- Smarter Contact
- Launch Control

### Setup Steps

1. Navigate to **Settings -> Integrations** in DataSift
2. Find your phone provider (smrtPhone, Twilio, or Plivo)
3. Click **Connect** and follow the provider-specific authentication flow
4. Once connected, SMS actions will appear as available sequence actions
5. Test by creating a simple sequence with an SMS action on a test record

### Email Requirements

- Requires **Gmail integration** connected in Settings -> Integrations
- Use for confirmed email addresses only
- For delayed follow-ups, use Drip Campaigns instead of direct Email actions

### Important SMS Notes

- SMS and Email actions are for **leads only**, not cold outreach
- Use **@ variables** for personalization (e.g., @contact_first_name, @property_address)
- Comply with **A2P 10DLC regulations** for SMS
- Messages only send between **8 AM and 9 PM** in your account timezone

---

## Recommended Starting Points

When helping users get started, provide discovery about what they already have and what they need to build.

### Starting Point 1: Use Default Sequences As-Is

**Best for**: New users who want to test the system before customizing.

**What you already have**:
- Default sequences for Lead Management, Acquisitions, Transactions
- Task presets that auto-assign based on status changes
- Tasks loop automatically (daily -> weekly -> ghosting)

**Action**: Review your existing sequences under Sequences page. Toggle them on/off as needed.

### Starting Point 2: Customize Default Sequences

**Best for**: Users who want to adjust timing or assignees.

**What to customize**:
- Task due dates and frequencies
- Assignees (change from Sensei to specific users/roles)
- Round-robin distribution for teams

**Action**: Open each default sequence -> Make Changes -> Adjust settings -> Save.

### Starting Point 3: Deploy TCA Templates

**Best for**: Users who want comprehensive pipeline automation quickly.

**What you get**: 26 pre-built sequence configurations covering Lead Management, Acquisitions, Transactions, Deep Prospecting, and Default workflows.

**Action**: Build these sequences using the template tables and UI walkthrough in this skill. New accounts (after 4/16/2025) already have them pre-configured.

### Starting Point 4: Build Follow-Up Chain Sequences (Custom Build Required)

**Best for**: Users who want automated task chains where completing one task creates the next.

**What you need to build**: Individual sequences for each step in your cadence. For example, a Hot Lead follow-up chain requires 16 separate sequences (A01 through A16).

**See**: `references/lead-management-sequences.md` for complete configurations.

### Starting Point 5: Build Board-to-Board Workflows (Custom Build Required)

**Best for**: Users who want cards to automatically move/duplicate between boards.

**What you need to build**: Sequences triggered by card movement to transition phases.

**See**: `references/board-workflows.md` for complete configurations.

---

## Common Sequence Patterns

For ready-to-use sequence configurations (that you build yourself), see:
- **Lead Management sequences**: `references/lead-management-sequences.md`
- **Acquisitions sequences**: `references/acquisitions-sequences.md`
- **Board-to-board workflows**: `references/board-workflows.md`
- **Drip campaign integration**: `references/drip-campaigns.md`
- **Sequence ideation framework**: `references/sequence-ideation.md`
- **Events overview**: `references/events-overview.md`

---

## Ideating Sequences

When helping users design sequences, ask these questions:

1. **What event should trigger the automation?**
   - Status change? Card movement? Task completion?

2. **Are there any conditions that must be met?**
   - Only for certain statuses? Specific team members?
   - Remember: one condition block per sequence, AND logic only

3. **What should happen automatically?**
   - Create task? Move card? Send notification?

4. **Who should be assigned?**
   - Specific user? Round-robin? Property assignee?

---

## Troubleshooting

### Sequence Won't Save
- Verify at least one trigger AND one action exist
- Ensure sequence has a name
- Check for duplicate name (will show error toast -- add " V2" suffix)

### Sequence Triggered But Didn't Run
- Large batch triggers may take a few minutes
- Check if other actions are running simultaneously

### Card Not Moving/Adding to Board
- Card may already exist on target board (action skips)
- Verify card is on the expected source board/phase

### Sequence Didn't Run on Upload
- Tag/List triggers only work for manual additions, not uploads
- Use Property Status Change trigger for upload-based automations

### Sequence Loop Error
- Sequences cannot trigger other sequences
- Combine related automations into a single sequence

### SMS Not Sending
- Verify phone provider (smrtPhone, Twilio, or Plivo) is connected in Settings -> Integrations
- Check that the record has a valid phone number
- Confirm account timezone is set (messages only send 8 AM - 9 PM)
- Ensure A2P 10DLC compliance

---

## Drip Campaigns vs. Sequence Actions

| Feature | Sequence SMS/Email | Drip Campaigns |
|---------|-------------------|----------------|
| Timing | Immediate | Delayed (minutes/hours/days) |
| Best For | New lead alerts | Long-term nurture |
| Integration | Any SMS provider | smrtPhone, Twilio, Plivo only |

For complete drip campaign setup, see: `references/drip-campaigns.md`

---

## Sequence Limits by Plan

| Plan | Limit |
|------|-------|
| Essentials (grandfathered) | 3 sequences |
| Professional | 8 sequences |
| Business | Unlimited |

**Planning note**: A full Hot Lead follow-up chain alone requires 15 sequences. If you are on Essentials or Professional, prioritize the highest-impact sequences first (New Lead Assignment, Hot Lead Alert, and one or two chain sequences).

---

## User Permissions

These roles can create/edit sequences:
- Sensei (account owner)
- Super Admin
- Admin
- Marketer

---

## Viewing Sequence Activity

To see what sequences have run on a record:
1. Open the property record
2. Click "Activity Log"
3. Sequence names appear next to automated updates
