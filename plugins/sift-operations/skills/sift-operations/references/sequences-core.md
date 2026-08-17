# Sequences — Core Reference

Sequences are automations in Sift that trigger actions when specific events occur. They ensure no lead slips through the cracks by automatically creating tasks, changing statuses, moving cards, and sending notifications.

## Core Philosophy

Sequences should be **rewards for consistency**, not crutches for forgetfulness:
1. Get consistent with a manual process first
2. Prove the process works
3. Then automate with sequences to save time

## Sequence Anatomy

Every sequence has three components:

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

Additional rules that filter when the action executes:

| Condition | Example |
|-----------|---------|
| Property Status Change | From "Any" to "New Lead" |
| Property Assignee | Assignee is specific user |
| Property Tags | Has or doesn't have specific tags |
| Property Lists | On or not on specific lists |
| Card Board & Column | Card is on specific board/phase |
| SiftLine Card Moved | From specific board/phase to another |

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

## Creating a Sequence — Step by Step

1. Navigate to **Sequences** in left sidebar
2. Click **"Create New Sequence"**
3. **Add Trigger** — drag and drop from the trigger panel
4. **Add Condition** if needed — drag and drop from the condition panel
5. **Add Action(s)** — drag and drop from the action panel
6. **Name the sequence** — use clear naming (see naming conventions below)
7. **Select folder** — organize by board or function
8. Click **"Save Sequence"**

### Editing Default Sequences

1. Open sequence folder
2. Click sequence name
3. Select **"Make Changes"**
4. Edit trigger, conditions, or actions
5. Click **"Save Sequence"**

## Sequence Naming Conventions

### Recommended Format

```
[Board Abbreviation] - [Trigger Description] - [Action Summary]
```

### Board Abbreviations

| Abbreviation | Board |
|---|---|
| LM | Lead Management |
| ACQ | Acquisitions |
| TX | Transactions |
| MKT | Marketing |
| DISP | Dispositions |

### Examples

| Name | Meaning |
|---|---|
| LM - New Lead - Intake Setup | Lead Management, triggers on new lead, sets up intake |
| ACQ - Offer Accepted - Send to TX | Acquisitions, triggers on accepted offer, sends to Transactions |
| TX - Closed - Celebrate | Transactions, triggers on close, sends celebration notification |

## Folder Organization

Organize sequences into folders by:
- **Board** (Lead Management, Acquisitions, Transactions)
- **Function** (Intake, Follow-Up, Notifications)
- **Team** (Lead Team, Acquisitions Team, Admin)

## SMS/Email in Sequences

**Important**: SMS and Email actions are for leads only, not cold outreach.

- **SMS**: Requires smrtPhone, Twilio, or Plivo integration. Use @ variables for personalization.
- **Email**: Requires Gmail integration. Use for confirmed email addresses only.
- For delayed follow-ups, use **Drip Campaigns** instead of direct SMS/Email actions.

## Drip Campaigns vs. Sequence Actions (Quick Comparison)

| Feature | Sequence SMS/Email | Drip Campaigns |
|---------|-------------------|----------------|
| Timing | Immediate | Delayed (minutes/hours/days) |
| Best For | New lead alerts | Long-term nurture |
| Integration | Any SMS provider | smrtPhone, Twilio, Plivo only |

## Viewing Sequence Activity

1. Open the property record
2. Click **"Activity Log"**
3. Sequence names appear next to automated updates

## Sequence Limits by Plan

| Plan | Limit |
|------|-------|
| Essentials (grandfathered) | 3 sequences |
| Professional | 8 sequences |
| Business | Unlimited |

## User Permissions

Roles that can create/edit sequences: Sensei, Super Admin, Admin, Marketer.
