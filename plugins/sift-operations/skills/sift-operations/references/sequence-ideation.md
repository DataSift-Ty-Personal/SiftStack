# Sequence Ideation Guide

Framework for designing custom sequences tailored to specific business workflows.

## The Ideation Framework

### The Three Core Questions

Every sequence should answer at least one:

1. **What new data needs to be processed?** — New leads, new records from uploads, new contacts
2. **What data is ready for its first marketing touch?** — Skip traced leads, records ready for outreach
3. **What data has been marketed to but requires follow-up?** — Non-responders, pending offers, in-progress deals

### The Automation Readiness Test

Before creating a sequence, confirm all four:

| Question | Required Answer |
|----------|-----------------|
| Is this process proven to work manually? | Yes |
| Are you doing this consistently already? | Yes |
| Will automation save significant time? | Yes |
| Can you clearly define the trigger point? | Yes |

If any answer is "No", focus on manual consistency first.

## Discovery Questions

Use these to help define sequences:

### Understanding the Trigger
- "What event should kick off this automation?"
- "When does this need to happen? (status change, card move, task completion)"
- "Is this triggered by something the user does, or something automatic?"

### Defining Conditions
- "Should this happen for ALL records, or only specific ones?"
- "Are there exceptions where this shouldn't run?"
- "Does the record need certain tags, lists, or assignments?"

### Specifying Actions
- "What should happen automatically when this triggers?"
- "Who should be assigned? One person, or rotate between team members?"
- "Should a task be created? What name and due date?"
- "Should the record move to a different board or phase?"
- "Should anyone be notified? (SMS, email)"

### Validating the Design
- "Walk me through what happens step by step when this runs."
- "What could go wrong? How would you know if it didn't work?"
- "How will you test this before going live?"

## Common Use Case Templates

### Template: New Record Intake

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

```
Trigger: Property Status Change
Condition: From Any to [Target Status]
Actions:
  1. Move Card to [Phase]
  2. Create Task: [Follow-Up Task] (Due: [X] days)
```

### Template: Board Transition

```
Trigger: SiftLine Card Moved
Condition: To [Source Board], [Transition Phase]
Actions:
  1. Duplicate Card to [Destination Board], [Starting Phase]
  2. Assign Property to [New Owner]
  3. Create Task: [First Task on New Board] (Due: 0 days)
```

### Template: Task Completion Chain

```
Trigger: Task Completed
Condition: Task Is [Completed Task Name]
Actions:
  1. Create Task: [Next Task Name] (Due: [X] days)
  2. (Optional) Move Card to [Next Phase]
```

### Template: Team Notification

```
Trigger: [Any trigger type]
Condition: [Relevant conditions]
Actions:
  1. Send SMS to [Phone Number]: "[Notification Message]"
  2. (Or) Send Email to [Email]: "[Subject]" "[Body]"
```

## Advanced Sequence Patterns

### Pattern: Conditional Assignment

Route records to different team members based on criteria:

**Sequence 1 — High Value Leads**:
- Trigger: Property Status Change to New Lead
- Condition: Property has tag "High Value"
- Action: Assign to Senior Lead Manager

**Sequence 2 — Standard Leads**:
- Trigger: Property Status Change to New Lead
- Condition: Property doesn't have tag "High Value"
- Action: Round-robin assign to Lead Team

### Pattern: Escalation Workflow

```
Sequence: Overdue Task Escalation
  Trigger: Task Created
  Condition: Task Is "Escalation Review"
  Actions:
    1. Assign Property to Manager
    2. Add Tag "Needs Attention"
    3. Send SMS to Manager
```

Note: Requires manually creating the escalation task when original is overdue.

### Pattern: Re-engagement Campaign

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

```
Sequence: Hot Lead Visibility
  Trigger: Property Status Change to Hot Lead
  Actions:
    1. Create Card on "Hot Leads" board (if not exists)
    2. Create Card on "Lead Management" board (if not exists)
```
