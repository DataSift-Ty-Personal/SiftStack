# Events Overview

This reference provides comprehensive documentation on the Events section of Sift and how Events integrate with Tasks, Sequences, and Drip Campaigns.

## Table of Contents
1. What Are Events?
2. Appointments
3. Tasks and Task Presets
4. How Events Connect to Sequences
5. How Events Connect to Drip Campaigns
6. The Complete Integration Flow

---

## 1. What Are Events?

The **Events** section of your Sift account is the central hub for managing all appointments and tasks. Think of Events as the container that holds every action item in your business.

### Events Section Features

| Feature | Description |
|---------|-------------|
| All Events Tab | View appointments and tasks combined in one view |
| Appointments Tab | View only appointments |
| Tasks Tab | View only tasks |
| Date Filtering | Filter by Today, Tomorrow, Overdue, or custom date range |
| User Filtering | Filter by assigned user or the user who assigned the event |
| Task Presets | Access and manage reusable task templates |
| Google Calendar Sync | Automatically sync events with your Google Calendar |

### Accessing Events

Navigate to **Events** in the left sidebar. From here you can:
- View all upcoming and overdue events
- Create new appointments or tasks
- Access and configure task presets
- Filter events by date, user, or type

---

## 2. Appointments

Appointments are similar to tasks but include additional features for scheduled meetings.

### Appointment vs. Task Comparison

| Feature | Appointment | Task |
|---------|-------------|------|
| Location | Yes (address or virtual) | No |
| Outcome Tracking | Yes (select outcome when completed) | No |
| Recurrence | No | Yes |
| Due Date/Time | Yes | Yes |
| Property Association | Optional | Optional |

### Appointment Types

| Type | When to Use |
|------|-------------|
| Property Walkthrough | Visiting a property in person |
| Contract Signing | Meeting to sign paperwork |
| Inspection | Property inspection appointment |
| Other | Any other scheduled meeting |

### Creating Appointments

1. Go to Events section or open a property record
2. Click "Create" or "Add new Event"
3. Select the Appointment tab
4. Enter appointment name and select type
5. (Optional) Associate with a property record
6. Set location (auto-fills from property address if associated)
7. Set date and time
8. Save

### Completing Appointments

When you complete an appointment, you will be prompted to select the outcome. This helps track the results of your meetings.

**Note**: For offer appointments, you still need to update the Offer Information separately.

---

## 3. Tasks and Task Presets

Tasks are the action items that drive your daily workflow. Task Presets allow you to create reusable templates.

### Task Features

| Feature | Description |
|---------|-------------|
| Deadline | Set due date and time (or "All Day") |
| Recurrence | Daily, weekly, bi-weekly, or monthly |
| Skip Weekends | Auto-move weekend tasks to Monday |
| Assignment | Assign to user, role, or round-robin |
| Property Association | Link task to specific property record |

### Task Assignment Options

| Assignment Type | How It Works |
|-----------------|--------------|
| Specific User | Task assigned to one person |
| Role | Task assigned to all users with that role |
| Users Round-Robin | Evenly distributes tasks among selected users |
| Role Round-Robin | Evenly distributes tasks among users in a role |

### Important Permission Notes

| Role | Record Access |
|------|---------------|
| Acquisitions, Dispositions, Researchers, Prospectors | Can only see records assigned to them |
| Lead Managers | Can see records assigned to themselves and others (not unassigned) |
| Sensei, Super Admin, Admin | Can see all records |

**Critical**: When assigning tasks to Acquisitions, Dispositions, Researchers, or Prospectors, you must also assign the property record to them. Otherwise, they cannot access the record to complete the task.

### Task Presets

Task Presets are reusable task templates that save time and ensure consistency.

**Why Use Task Presets**:
- Create tasks without re-entering details each time
- Use in sequences to auto-assign tasks based on triggers
- Maintain consistent task naming and settings across your team

**Creating Task Presets**:
1. Go to Events page
2. Click "Configure Presets" or "Preset" option
3. Create a new group to organize presets (optional)
4. Click "Add New Preset"
5. Configure task name, assignment, deadline, and recurrence
6. Save

### Default Task Presets (Included in Account)

Accounts created after 4/16/2025 include these task presets:

| Preset Group | Task Presets Included |
|--------------|----------------------|
| Lead Management | Call New Lead, No Contact New Lead, Nurture New Lead, Cold Follow-up, Warm Follow-up, Hot Follow-up |
| Acquisitions | Make Offer, Offer Follow-Up, Send Back to LM |
| Transactions | Contract and title follow-ups, Seller follow-ups |

**Note**: All default tasks are initially assigned to the Sensei (Account Owner). Teams should edit task assignees to distribute work appropriately.

---

## 4. How Events Connect to Sequences

Sequences create tasks automatically based on triggers. Understanding this connection is essential for building effective automations.

### Sequence to Task Flow

```
Trigger fires (e.g., status change to "New Lead")
    ↓
Sequence conditions checked
    ↓
"Create New Task" action executes
    ↓
Task created using Task Preset
    ↓
Task appears in Events section
    ↓
Task assigned to specified user/role
```

### Task Triggers in Sequences

Sequences can be triggered by task events:

| Trigger | When It Fires |
|---------|---------------|
| Task Created | When any task is created on a record |
| Task Completed | When a specific task is marked complete |

### Building Task Chain Sequences

You can create automated follow-up chains where completing one task triggers the next:

**Example: Hot Lead Follow-Up Chain**

| Sequence | Trigger | Action |
|----------|---------|--------|
| Hot A01 Complete | Task Completed: "HOT Follow-Up A01" | Create Task: "HOT Follow-Up A02" (Due: 1 day) |
| Hot A02 Complete | Task Completed: "HOT Follow-Up A02" | Create Task: "HOT Follow-Up A03" (Due: 1 day) |
| (Continue pattern...) | | |

**Important**: These follow-up chain sequences are NOT included in default accounts. You must build them yourself. See `lead-management-sequences.md` for complete configurations.

---

## 5. How Events Connect to Drip Campaigns

Drip Campaigns send delayed SMS/Email messages over time. They connect to Events through sequences and can also create tasks.

### Drip Campaign Components

| Component | Description |
|-----------|-------------|
| SMS Step | Send text message (requires smrtPhone, Twilio, or Plivo) |
| Email Step | Send email (requires Gmail integration) |
| Task Step | Create a task on the record |
| Delay Step | Wait specified time before next step |

### Sequence to Drip Flow

```
Trigger fires (e.g., status change to "Dead Lead")
    ↓
Sequence executes "Add to Drip Campaign" action
    ↓
Record added to specified drip campaign
    ↓
Drip campaign executes steps over time
    ↓
SMS/Email sent at scheduled intervals
    ↓
Final task created (appears in Events)
```

### Drip Campaign Task Creation

Drip campaigns can create tasks as part of their sequence. This is useful for:
- Creating a manual follow-up task at the end of an automated sequence
- Scheduling a review task after a nurture period
- Triggering human intervention at key points

**Example**: A 30-day cold lead re-engagement drip might end with a "Review Cold Lead Status" task that appears in Events.

---

## 6. The Complete Integration Flow

Understanding how all four components work together helps you design effective automations.

### The Four Components

| Component | Role | Where to Find |
|-----------|------|---------------|
| Events | Container for tasks and appointments | Left sidebar > Events |
| Tasks | Individual action items | Events section or property records |
| Sequences | Automations that create tasks/move cards | Left sidebar > Sequences |
| Drip Campaigns | Delayed SMS/Email sequences | Left sidebar > Drip Campaigns |

### Complete Integration Example

**Scenario**: New lead comes in and needs full automation.

```
1. Property status changes to "New Lead"
    ↓
2. Sequence triggers:
   - Creates task "Call New Lead" (appears in Events)
   - Creates card on Lead Management board
   - Adds to "New Lead Welcome" drip campaign
    ↓
3. Drip campaign runs:
   - Day 0: Sends welcome SMS
   - Day 1: Sends follow-up SMS
   - Day 3: Sends final SMS
   - Day 7: Creates task "Call Lead - Final Attempt"
    ↓
4. Task completed "Call New Lead"
    ↓
5. User changes status to "Hot Lead"
    ↓
6. New sequence triggers:
   - Creates task "HOT Follow-Up A01"
   - Moves card to "Hot" phase
    ↓
7. Task completed "HOT Follow-Up A01"
    ↓
8. Chain sequence triggers:
   - Creates task "HOT Follow-Up A02"
    ↓
(Pattern continues through entire follow-up cadence)
```

### Key Integration Points

| From | To | Connection |
|------|-----|------------|
| Sequence | Task | "Create New Task" action |
| Sequence | Drip Campaign | "Add to Drip Campaign" action |
| Drip Campaign | Task | Task step in drip sequence |
| Task Completion | Sequence | "Task Completed" trigger |
| All Tasks | Events | All tasks appear in Events section |

---

## Viewing Event History

### Activity Log

All event activity is logged to the property record's Activity Log:
- Task creation
- Task completion
- Appointment creation
- Appointment completion
- Sequence-triggered events

### Completed Events

View completed events in two ways:

1. **For a specific record**: Open property record > Assigned Events section > Completed tab
2. **For all records**: Events section > Completed tab > Filter by date or user

---

## Best Practices

1. **Use Task Presets**: Create presets for any task you use repeatedly to ensure consistency.

2. **Connect Sequences to Presets**: When building sequences, use existing task presets rather than creating ad-hoc tasks.

3. **End Drips with Tasks**: Add a task step at the end of drip campaigns to trigger human follow-up.

4. **Check Events Daily**: Use the Events section as your daily dashboard to see all pending tasks.

5. **Assign Records with Tasks**: When assigning tasks to restricted roles, always assign the property record too.

6. **Use Google Calendar**: Enable the integration to see all tasks and appointments in your calendar.
