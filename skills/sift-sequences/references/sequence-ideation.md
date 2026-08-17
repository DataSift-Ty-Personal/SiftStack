# Sequence Ideation Guide

This reference helps users design custom sequences tailored to their specific business workflows.

## Table of Contents
1. The Ideation Framework
2. Discovery Questions
3. Common Use Case Templates
4. Advanced Sequence Patterns
5. Sequence Naming Conventions

---

## 1. The Ideation Framework

### The Three Core Questions

Every sequence should answer at least one of these questions:

1. **What new data needs to be processed?**
   - New leads entering the system
   - New records from uploads
   - New contacts from integrations

2. **What data is ready for its first marketing touch?**
   - Leads that have been skip traced
   - Records that need initial outreach
   - Contacts ready for first call

3. **What data has been marketed to but requires follow-up?**
   - Leads that didn't respond
   - Offers that need follow-up
   - Deals in progress

### The Automation Readiness Test

Before creating a sequence, confirm:

| Question | Required Answer |
|----------|-----------------|
| Is this process proven to work manually? | Yes |
| Are you doing this consistently already? | Yes |
| Will automation save significant time? | Yes |
| Can you clearly define the trigger point? | Yes |

If any answer is "No", focus on manual consistency first.

---

## 2. Discovery Questions

Use these questions to help users define their sequences:

### Understanding the Trigger

- "What event should kick off this automation?"
- "When does this need to happen? (status change, card move, task completion)"
- "Is this triggered by something the user does, or something that happens automatically?"

### Defining Conditions

- "Should this happen for ALL records, or only specific ones?"
- "Are there any exceptions where this shouldn't run?"
- "Does the record need to have certain tags, be on certain lists, or be assigned to certain people?"

### Specifying Actions

- "What should happen automatically when this triggers?"
- "Who should be assigned? One person, or rotate between team members?"
- "Should a task be created? What should it be called and when is it due?"
- "Should the record move to a different board or phase?"
- "Should anyone be notified? (SMS, email)"

### Validating the Design

- "Walk me through what happens step by step when this sequence runs."
- "What could go wrong? How would you know if it didn't work?"
- "How will you test this before going live?"

---

## 3. Common Use Case Templates

### Template: New Record Intake

**When to use**: New records enter the system and need initial setup.

```
Trigger: Property Status Change
Condition: From Any to [Intake Status]
Actions:
  1. Assign Property to [Team Member/Round-Robin]
  2. Create New Card on [Board], [Phase]
  3. Create Task: [Initial Task Name] (Due: 0 days)
  4. (Optional) Send SMS/Email notification
```

### Template: Status-Based Task Creation

**When to use**: Different statuses require different follow-up cadences.

```
Trigger: Property Status Change
Condition: From Any to [Target Status]
Actions:
  1. Move Card to [Phase]
  2. Create Task: [Follow-Up Task] (Due: [X] days)
```

### Template: Board Transition

**When to use**: Records move from one workflow stage to another.

```
Trigger: SiftLine Card Moved
Condition: To [Source Board], [Transition Phase]
Actions:
  1. Duplicate Card to [Destination Board], [Starting Phase]
  2. Assign Property to [New Owner]
  3. Create Task: [First Task on New Board] (Due: 0 days)
```

### Template: Task Completion Chain

**When to use**: Completing one task should create the next task.

```
Trigger: Task Completed
Condition: Task Is [Completed Task Name]
Actions:
  1. Create Task: [Next Task Name] (Due: [X] days)
  2. (Optional) Move Card to [Next Phase]
```

### Template: Team Notification

**When to use**: Team members need to be alerted of important events.

```
Trigger: [Any trigger type]
Condition: [Relevant conditions]
Actions:
  1. Send SMS to [Phone Number]: "[Notification Message]"
  2. (Or) Send Email to [Email]: "[Subject]" "[Body]"
```

---

## 4. Advanced Sequence Patterns

### Pattern: Conditional Assignment

Route records to different team members based on criteria.

**High-Value Lead Routing**
```
Sequence 1: High Value Leads
  Trigger: Property Status Change to New Lead
  Condition: Property has tag "High Value"
  Action: Assign to Senior Lead Manager

Sequence 2: Standard Leads
  Trigger: Property Status Change to New Lead
  Condition: Property doesn't have tag "High Value"
  Action: Round-robin assign to Lead Team
```

### Pattern: Escalation Workflow

Escalate when tasks become overdue.

```
Sequence: Overdue Task Escalation
  Trigger: Task Created
  Condition: Task Is "Escalation Review"
  Actions:
    1. Assign Property to Manager
    2. Add Tag "Needs Attention"
    3. Send SMS to Manager
```

Note: This requires manually creating the escalation task when original task is overdue.

### Pattern: Re-engagement Campaign

Bring dead leads back into the funnel.

```
Sequence: Dead Lead Revival
  Trigger: Property Status Change
  Condition: From Any to Dead Lead
  Actions:
    1. Move Card to "Dead" phase
    2. Add to Drip Campaign "Dead Lead Re-engagement"
    3. Create Task "Dead Lead Check-In" (Due: 90 days)
```

### Pattern: Multi-Board Sync

Keep a record visible on multiple boards simultaneously.

```
Sequence: Hot Lead Visibility
  Trigger: Property Status Change to Hot Lead
  Actions:
    1. Create Card on "Hot Leads" board (if not exists)
    2. Create Card on "Lead Management" board (if not exists)
```

---

## 5. Sequence Naming Conventions

### Recommended Format

```
[Board Abbreviation] - [Trigger Description] - [Action Summary]
```

### Examples

| Name | Meaning |
|------|---------|
| LM - New Lead - Intake Setup | Lead Management, triggers on new lead, sets up intake |
| ACQ - Offer Accepted - Send to TX | Acquisitions, triggers on accepted offer, sends to Transactions |
| TX - Closed - Celebrate | Transactions, triggers on close, sends celebration notification |

### Board Abbreviations

| Abbreviation | Board |
|--------------|-------|
| LM | Lead Management |
| ACQ | Acquisitions |
| TX | Transactions |
| MKT | Marketing |
| DISP | Dispositions |

### Folder Organization

Organize sequences into folders by:
- Board (Lead Management, Acquisitions, Transactions)
- Function (Intake, Follow-Up, Notifications)
- Team (Lead Team, Acquisitions Team, Admin)
