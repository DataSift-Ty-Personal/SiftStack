# Lead Management Sequences

Sequence configurations for the lead lifecycle: intake, temperature changes, follow-up chains, and dead lead revival. Clearly distinguishes between what a default account already ships and what you have to build.

## Default vs. Custom

### Included in Default Accounts (After 4/16/2025)

| Item | Description | Action Required |
|------|-------------|-----------------|
| Lead Management Board | SiftLine board with phases for the lead lifecycle | None, ready to use |
| Default Sequences | Automations for status changes and card movements | Review and toggle on/off |
| Task Presets | Call New Lead, No Contact New Lead, Nurture New Lead, Cold/Warm/Hot Follow-Up | Customize assignees |
| Property Statuses | New Lead, No Contact New Lead, Cold/Warm/Hot Lead, Ghosting Lead, Dead Lead | None, ready to use |

### NOT Included (Custom Build Required)

| Item | Description | Why Build It |
|------|-------------|--------------|
| Follow-up chain sequences | Task completion triggers the next task (A01 to A02 to A03) | Automates the cadence with no manual step |
| Custom cadence timing | Intervals tuned to your market | Default timing is generic |
| Temperature-based drip triggers | Add to a drip campaign on status change | Automates long-term nurture |
| Dead lead revival | Quarterly re-touch on dead leads | Dead is usually just "not yet" |

## Default Lead Flow

| Status | Default Task Created | Default Frequency |
|--------|---------------------|-------------------|
| New Lead | Call New Lead | Due immediately (1 day) |
| No Contact New Lead | No Contact New Lead | Due daily (3-5 days) |
| Cold Lead | Cold Follow-Up | Every 45 days |
| Warm Lead | Warm Follow-Up | Every 14 days |
| Hot Lead | Hot Follow-Up | Every 7 days |

Default sequences create the task from the preset, create or move the board card, and assign to the Sensei (account owner). **Reassigning those tasks off the Sensei is the most common customization**, and skipping it is why a whole team's tasks pile up on one person.

### Editing a Default Sequence

1. Go to **Sequences** in the left sidebar
2. Open the **Lead Management** folder
3. Click the sequence name
4. Select **"Make Changes"**
5. Adjust assignee, task due date, or actions
6. Click **"Save Sequence"**

## New Lead Intake Sequence

Default accounts already ship a version of this. Build your own only if you need behavior the default does not cover.

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | From "Any" to "New Lead" |

**Actions (in order)**:
1. **Assign Property** to the Lead Manager, or round-robin across the lead team
2. **Clear Property Tasks**, which removes leftovers from a previous workflow
3. **Create New Card** on Board: Lead Management, Phase: New Lead
4. **Create New Task** "Call New Lead" (Due: 0 days), Toggle: Assign to property

**Optional add-ons**: SMS the lead manager, send an internal email, add to a welcome drip.

**Speed to lead is the point.** The task and the alert both have to land the moment the status flips, so put the notification action in this sequence rather than relying on someone watching the board.

## Temperature Change Sequences

Build these when you want more than the default task creation.

### Hot Lead

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | From "Any" to "Hot Lead" |

**Actions**:
1. Move Card to "Hot" phase on Lead Management
2. Create Task: "HOT Follow-Up A01" (Due: 1 day)
3. (Optional) Add to Drip Campaign: "Hot Lead Nurture"

### Warm Lead

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | From "Any" to "Warm Lead" |

**Actions**:
1. Move Card to "Warm" phase
2. Create Task: "WARM Follow-Up A01" (Due: 15 days)

### Cold Lead

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | From "Any" to "Cold Lead" |

**Actions**:
1. Move Card to "Cold" phase
2. Create Task: "COLD Follow-Up A01" (Due: 45 days)

## Follow-Up Chains

A chain is one sequence per step. Completing a task fires the sequence that creates the next task.

```
Task "HOT Follow-Up A01" completed
    -> Sequence "Hot A01 Complete" triggers
    -> Creates Task "HOT Follow-Up A02" (Due: 1 day)
    -> Task completed -> next sequence -> pattern continues
```

### Building One Link

**Step 1**: Create a task preset for every step (HOT Follow-Up A01, A02, A03 and so on).

**Step 2**: Create one sequence per transition.

| Component | Setting |
|-----------|---------|
| **Trigger** | Task Completed |
| **Condition** | Task Is: "HOT Follow-Up A01" |
| **Action** | Create Task: "HOT Follow-Up A02" (Due: 1 day) |

Repeat for each step. **The last task in a chain gets no sequence**, which is what ends it.

### Hot Lead Cadence (26 days, 15 sequences)

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
| A16 | 26 | 0 days | (end of chain) |

### Warm Lead Cadence (180 days)

| Task | Day | Due After Previous |
|------|-----|-------------------|
| A01 | 15 | 15 days |
| A02 | 25 | 10 days |
| A03 | 30 | 5 days |
| A04 | 45 | 15 days |
| A05 | 55 | 10 days |
| A06 | 60 | 5 days |

Pattern repeats every 30 days.

### Cold Lead Cadence (360 days)

| Task | Day | Due After Previous |
|------|-----|-------------------|
| A01 | 45 | 45 days |
| A02 | 60 | 15 days |
| A03 | 90 | 30 days |
| A04 | 135 | 45 days |
| A05 | 150 | 15 days |
| A06 | 180 | 30 days |

Pattern repeats every 90 days.

## Dead Lead Revival

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | From "Any" to "Dead Lead" |

**Actions**:
1. Move Card to "Dead" phase
2. Create Task: "DEAD Follow-Up A01" (Due: 90 days)
3. (Optional) Add to Drip Campaign: "Dead Lead Re-engagement"

### Dead Lead Cadence (360 days, 4 sequences)

| Task | Day | Due After Previous |
|------|-----|-------------------|
| A01 | 90 | 90 days |
| A02 | 180 | 90 days |
| A03 | 270 | 90 days |
| A04 | 360 | 90 days |

## Sequence Priority (Limited Plans)

| Plan | Limit | Best Lead Management Sequences |
|------|-------|-------------------------------|
| Essentials (grandfathered) | 3 | New Lead Intake, Hot Lead temperature change, Hot A01 Complete |
| Professional | 8 | The three above plus Hot A02 through A05 and Warm Lead |
| Business | Unlimited | Full chains for hot, warm, cold and dead |

A full hot chain alone is 15 sequences, so on a capped plan build the front of the chain first. The first five touches carry most of the contact rate.

## Best Practices

1. **Run the defaults untouched for 2-4 weeks** before customizing. Automate a process you have already proven by hand.
2. **Fix assignees first.** Every default task lands on the Sensei until you change it.
3. **Add one link at a time** and test it on a real record before adding the next.
4. **Verify in the Activity Log.** Open the property record, click Activity Log, and confirm the sequence name appears next to the automated update.
5. **Name consistently**: "LM - New Lead Intake", "LM - Hot A01 Complete". See `sequences-core.md` for the full convention.
6. **Assign the record, not just the task.** Acquisitions, Dispositions, Researchers and Prospectors only see records assigned to them, so a task on an unassigned record is invisible to them.
