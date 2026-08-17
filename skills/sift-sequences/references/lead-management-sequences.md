# Lead Management Sequences

This reference contains sequence configurations for lead management workflows. It clearly distinguishes between what is included in default accounts versus what you need to build yourself.

## Table of Contents
1. Understanding Default vs. Custom Sequences
2. Default Lead Management Setup (Already Included)
3. Custom Sequences You Need to Build
4. Follow-Up Chain Sequences (Custom Build Required)
5. Dead Lead Revival Sequence (Custom Build Required)
6. Best Practices

---

## 1. Understanding Default vs. Custom Sequences

Before building sequences, understand what your account already includes.

### What's INCLUDED in Default Accounts (After 4/16/2025)

| Item | Description | Action Required |
|------|-------------|-----------------|
| Lead Management Board | SiftLine board with phases for lead lifecycle | None, ready to use |
| Default Sequences | Automations for status changes and card movements | Review and toggle on/off |
| Task Presets | Call New Lead, No Contact New Lead, Nurture New Lead, Cold/Warm/Hot Follow-up | Customize assignees if needed |
| Property Statuses | New Lead, No Contact New Lead, Cold/Warm/Hot Lead, Ghosting Lead, Dead Lead | None, ready to use |

### What's NOT INCLUDED (Custom Build Required)

| Item | Description | Why Build It |
|------|-------------|--------------|
| Follow-up chain sequences | Task completion triggers next task (A01 → A02 → A03) | Automates cadence without manual intervention |
| Custom cadence timing | Specific intervals for your market | Tailors follow-up frequency to your business |
| Temperature-based drip triggers | Add to drip campaigns on status change | Automates long-term nurture |

---

## 2. Default Lead Management Setup (Already Included)

Your default account includes sequences that work with the default task presets and statuses. Here is what happens automatically:

### Default Lead Flow

| Status | Default Task Created | Default Frequency |
|--------|---------------------|-------------------|
| New Lead | Call New Lead | Due immediately (1 day) |
| No Contact New Lead | No Contact New Lead | Due daily (3-5 days) |
| Cold Lead | Cold Follow-up | Due every 45 days |
| Warm Lead | Warm Follow-up | Due every 14 days |
| Hot Lead | Hot Follow-up | Due every 7 days |

### How Default Sequences Work

The default sequences trigger based on status changes and automatically:
- Create the appropriate task from the task preset
- Move or create cards on the Lead Management board
- Assign tasks to the Sensei (account owner) by default

### Customizing Default Sequences

To adjust the default sequences:
1. Go to Sequences page
2. Open the Lead Management folder
3. Click on a sequence name
4. Select "Make Changes"
5. Adjust settings (assignee, task due date, etc.)
6. Save Sequence

**Common Customizations**:
- Change assignee from Sensei to specific user or role
- Enable round-robin for team distribution
- Adjust task due dates to match your workflow

---

## 3. Custom Sequences You Need to Build

The following sequences are recommended patterns but are NOT included in default accounts. You must build them yourself.

### New Lead Intake Sequence (If Not Using Default)

**Purpose**: When a property status changes to "New Lead", automatically set up the lead for follow-up.

**Note**: Default accounts already include a version of this. Only build if you need custom behavior.

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | Property Status Change: From "Any" to "New Lead" |

**Actions (in order)**:

1. **Assign Property**
   - Select: Lead Manager (or round-robin if multiple)

2. **Clear Property Tasks**
   - Removes any existing tasks from previous workflows

3. **Create New Card**
   - Board: Lead Management
   - Phase: New Lead

4. **Create New Task**
   - Task: "Call New Lead"
   - Due: 0 days (same day)
   - Toggle: Assign this task to the property

**Optional Add-ons**:
- Send SMS: Notify lead manager of new lead
- Send Email: Send internal notification
- Add to Drip Campaign: Start welcome sequence

### Temperature Change Sequences (Custom Enhancement)

Create these if you want additional actions beyond the default task creation.

**Hot Lead Sequence**:

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | Property Status Change: From "Any" to "Hot Lead" |

**Actions**:
1. Move Card to "Hot" phase on Lead Management board
2. Create Task: "HOT Follow-Up A01" (Due: 1 day)
3. (Optional) Add to Drip Campaign: "Hot Lead Nurture"

**Warm Lead Sequence**:

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | Property Status Change: From "Any" to "Warm Lead" |

**Actions**:
1. Move Card to "Warm" phase on Lead Management board
2. Create Task: "WARM Follow-Up A01" (Due: 15 days)

**Cold Lead Sequence**:

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | Property Status Change: From "Any" to "Cold Lead" |

**Actions**:
1. Move Card to "Cold" phase on Lead Management board
2. Create Task: "COLD Follow-Up A01" (Due: 45 days)

---

## 4. Follow-Up Chain Sequences (Custom Build Required)

**Important**: These sequences are NOT included in default accounts. You must build each one individually.

Follow-up chain sequences create automated cadences where completing one task triggers the creation of the next task. This requires building a separate sequence for each step in your cadence.

### How Follow-Up Chains Work

```
Task "HOT Follow-Up A01" completed
    ↓
Sequence "Hot A01 Complete" triggers
    ↓
Creates Task "HOT Follow-Up A02" (Due: 1 day)
    ↓
Task "HOT Follow-Up A02" completed
    ↓
Sequence "Hot A02 Complete" triggers
    ↓
Creates Task "HOT Follow-Up A03" (Due: 1 day)
    ↓
(Pattern continues...)
```

### Building a Follow-Up Chain

**Step 1**: Create Task Presets for each step (HOT Follow-Up A01, A02, A03, etc.)

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

**Total sequences required for Hot Lead chain**: 15 sequences

### Recommended Warm Lead Cadence (180 days)

| Task | Day | Due After Previous |
|------|-----|-------------------|
| A01 | 15 | 15 days |
| A02 | 25 | 10 days |
| A03 | 30 | 5 days |
| A04 | 45 | 15 days |
| A05 | 55 | 10 days |
| A06 | 60 | 5 days |
| (Pattern repeats every 30 days) |

### Recommended Cold Lead Cadence (360 days)

| Task | Day | Due After Previous |
|------|-----|-------------------|
| A01 | 45 | 45 days |
| A02 | 60 | 15 days |
| A03 | 90 | 30 days |
| A04 | 135 | 45 days |
| A05 | 150 | 15 days |
| A06 | 180 | 30 days |
| (Pattern repeats every 90 days) |

---

## 5. Dead Lead Revival Sequence (Custom Build Required)

**Important**: This sequence is NOT included in default accounts.

**Purpose**: Periodically check in on dead leads to see if circumstances have changed.

### Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | Property Status Change |
| **Condition** | Property Status Change: From "Any" to "Dead Lead" |

### Actions

1. **Move Card** to "Dead" phase on Lead Management board
2. **Create Task**: "DEAD Follow-Up A01" (Due: 90 days)
3. (Optional) **Add to Drip Campaign**: "Dead Lead Re-engagement"

### Dead Lead Cadence (360 days, 4 sequences required)

| Task | Day | Due After Previous |
|------|-----|-------------------|
| A01 | 90 | 90 days |
| A02 | 180 | 90 days |
| A03 | 270 | 90 days |
| A04 | 360 | 90 days |

---

## 6. Best Practices

### Getting Started

1. **Start with defaults**: Use the default sequences as-is for 2-4 weeks before customizing.

2. **Customize assignees first**: The most common change is updating task assignees from Sensei to your team.

3. **Build chains gradually**: Add one follow-up chain sequence at a time and test before adding the next.

### Building Custom Sequences

1. **Test thoroughly**: After creating each sequence, manually trigger it on a test record to verify it works correctly.

2. **Check Activity Log**: Always verify sequence execution by checking the Activity Log in the property record.

3. **Name consistently**: Use clear naming conventions like "LM - New Lead Intake" or "LM - Hot A01 Complete".

4. **Organize with folders**: Create a "Lead Management" folder to group all related sequences.

### Sequence Limits

Remember your plan limits when building follow-up chains:

| Plan | Limit | Can Build Full Hot Chain? |
|------|-------|---------------------------|
| Essentials | 3 sequences | No |
| Professional | 8 sequences | No |
| Business | Unlimited | Yes |

If you have limited sequences, prioritize:
1. New Lead Intake (if customizing default)
2. Hot Lead temperature change
3. Hot A01-A05 chain (first 5 follow-ups)
