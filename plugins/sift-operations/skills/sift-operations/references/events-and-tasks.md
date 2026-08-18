# Events and Tasks

Events is the container for every action item in the account: appointments, tasks, task presets, and the Google Calendar sync. Sequences create the tasks, Events is where they show up, and completing one is what fires the next sequence.

## The Events Section

Navigate to **Events** in the left sidebar.

| Feature | Description |
|---------|-------------|
| All Events tab | Appointments and tasks together |
| Appointments tab | Appointments only |
| Tasks tab | Tasks only |
| Completed tab | Everything already closed out |
| Date filtering | Today, Tomorrow, Overdue, or a custom range |
| User filtering | By assigned user, or by who assigned it |
| Configure Presets | Create and edit reusable task templates |
| Google Calendar sync | Pushes events into your calendar |

## Appointments vs. Tasks

| Feature | Appointment | Task |
|---------|-------------|------|
| Location | Yes (address or virtual) | No |
| Outcome tracking | Yes, prompted on completion | No |
| Recurrence | No | Yes |
| Due date/time | Yes | Yes |
| Property association | Optional | Optional |

### Appointment Types

| Type | When to Use |
|------|-------------|
| Property Walkthrough | Visiting a property in person |
| Contract Signing | Meeting to sign paperwork |
| Inspection | Property inspection |
| Other | Anything else scheduled |

### Creating an Appointment

1. Go to **Events**, or open the property record
2. Click **"Create"** or **"Add new Event"**
3. Select the **Appointment** tab
4. Enter the name and pick the type
5. (Optional) Associate a property record
6. Set the location, which auto-fills from the property address when one is associated
7. Set date and time, then **Save**

Completing an appointment prompts you for the outcome. **Offer Information is separate**, so an offer appointment still needs the offer fields updated by hand.

## Tasks

| Feature | Description |
|---------|-------------|
| Deadline | Due date and time, or All Day |
| Recurrence | Daily, weekly, bi-weekly, monthly |
| Skip weekends | Moves a weekend task to Monday |
| Assignment | User, role, or round-robin |
| Property association | Links the task to a record |

### Assignment Options

| Type | How It Works |
|------|--------------|
| Specific User | One person |
| Role | Everyone holding that role |
| Users Round-Robin | Even distribution across selected users |
| Role Round-Robin | Even distribution across a role |

### Permissions, and the Trap

| Role | Record Access |
|------|---------------|
| Sensei, Super Admin, Admin, Marketer | All records |
| Lead Managers | Records assigned to themselves or others, not unassigned |
| Acquisitions, Dispositions, Researchers, Prospectors | Only records assigned to them |

**Assigning a task to a restricted role is not enough.** Acquisitions, Dispositions, Researchers and Prospectors cannot open a record that is not assigned to them, so the task is unreachable. Every sequence that creates a task for one of those roles must also carry an **Assign Property** action to the same person.

## Task Presets

Task presets are reusable task templates. Sequences reference them by name, which is what keeps automated tasks consistent instead of ad hoc.

### Creating a Preset

1. Go to **Events** in the left sidebar
2. Click **Configure Presets** (gear icon)
3. (Optional) Create a group to organize presets
4. Click **"Add New Preset"**
5. Set name, description, assignment, deadline, recurrence, and priority
6. **Save**

### Default Presets (Accounts After 4/16/2025)

| Preset Group | Presets Included |
|--------------|------------------|
| Lead Management | Call New Lead, No Contact New Lead, Nurture New Lead, Cold Follow-Up, Warm Follow-Up, Hot Follow-Up |
| Acquisitions | Make Offer, Offer Follow-Up, Send Back to LM |
| Transactions | Contract and title follow-ups, seller follow-ups |

**Every default preset is assigned to the Sensei (account owner).** Reassigning them is the first thing a team should do, or all the work lands on one person.

### Typical Deadlines

| Preset | Purpose | Deadline |
|--------|---------|----------|
| Call New Lead | First contact on a fresh lead | Same day (speed to lead) |
| No Contact New Lead | First call got no answer | Next business day |
| Nurture New Lead | Not ready yet | 7 days |
| Hot Follow-Up | Highly motivated seller | Same day or next day |
| Warm Follow-Up | Moderate interest | 3-7 days |
| Cold Follow-Up | No recent activity | 14-45 days |
| Make Offer | Present the offer | 1-3 days |
| Offer Follow-Up | Check in after the offer | 2-3 days after |
| Send Back to LM | Return to Lead Management | Same day |

## How Events Connect to Sequences

```
Trigger fires (status change to "New Lead")
    -> conditions checked
    -> "Create New Task" action executes
    -> task created from the task preset
    -> task appears in Events, assigned to the user or role
```

### Task Triggers

| Trigger | Fires When |
|---------|------------|
| Task Created | Any task is created on a record |
| Task Completed | A specific task is marked complete |

**Task Completed is what makes chains possible.** One sequence per link: complete A01, the sequence creates A02. Full cadences and the sequence count for each are in `lead-management-sequences.md` and `acquisitions-sequences.md`.

### Using a Preset in a Sequence

1. Drag the **Create New Task** action into the sequence
2. Pick the task preset from the dropdown
3. Toggle **"Assign this task to the property"** ON
4. Optionally set **Assign to** (specific user, role, or round-robin)

## How Events Connect to Drip Campaigns

| Component | Description |
|-----------|-------------|
| SMS step | Requires smrtPhone, Twilio, or Plivo |
| Email step | Requires Gmail integration |
| Task step | Creates a task on the record |
| Delay step | Waits before the next step |

A sequence adds the record to a drip with the **Add to Drip Campaign** action, the drip runs its steps over days or weeks, and a **task step** hands the record back to a human. Ending a nurture drip with a task is the difference between a campaign that runs and a campaign someone acts on.

## The Complete Integration Flow

| From | To | Connection |
|------|-----|-----------|
| Sequence | Task | "Create New Task" action |
| Sequence | Drip Campaign | "Add to Drip Campaign" action |
| Drip Campaign | Task | Task step inside the drip |
| Task completion | Sequence | "Task Completed" trigger |
| Every task | Events | All tasks surface in the Events section |

```
1. Status changes to "New Lead"
2. Sequence creates task "Call New Lead", creates the LM card, adds to the welcome drip
3. Drip runs: day 0 SMS, day 1 SMS, day 3 SMS, day 7 creates "Call Lead - Final Attempt"
4. "Call New Lead" completed, user sets status to "Hot Lead"
5. Sequence creates "HOT Follow-Up A01" and moves the card to the Hot phase
6. A01 completed -> chain sequence creates A02, and the cadence carries itself
```

## Viewing History

- **One record**: open the property, go to **Assigned Events**, then the **Completed** tab
- **All records**: **Events** section, **Completed** tab, filter by date or user
- **Audit trail**: the property record's **Activity Log** shows task creation, completion, appointments, and the name of the sequence that caused each one

## Best Practices

1. **Use presets for anything repeated.** Ad hoc task names break every sequence condition that looks for a task by name.
2. **Assign the record whenever you assign a task** to a restricted role.
3. **End drips with a task** so a human picks the record back up.
4. **Work Events daily.** It is the operational dashboard, and Overdue is the number that matters. See `general-operations.md` for the STABM routine.
5. **Enable Google Calendar** if the team lives in a calendar rather than a task list.
6. **Verify in the Activity Log** after building any sequence that creates a task, rather than assuming it fired.
