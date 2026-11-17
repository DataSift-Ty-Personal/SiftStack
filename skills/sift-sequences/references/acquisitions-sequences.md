# Acquisitions Sequences

This reference contains sequence configurations for acquisitions workflows. It clearly distinguishes between what is included in default accounts versus what you need to build yourself.

## Table of Contents
1. Understanding Default vs. Custom Sequences
2. Default Acquisitions Setup (Already Included)
3. Custom Sequences You Need to Build
4. Offer Follow-Up Chain (Custom Build Required)
5. Offer Outcome Sequences (Custom Build Required)
6. Best Practices

---

## 1. Understanding Default vs. Custom Sequences

Before building sequences, understand what your account already includes.

### What's INCLUDED in Default Accounts (After 4/16/2025)

| Item | Description | Action Required |
|------|-------------|-----------------|
| Acquisitions Board | SiftLine board with phases for offer workflow | None, ready to use |
| Default Sequences | Automations for acquisitions workflow | Review and toggle on/off |
| Task Presets | Make Offer, Offer Follow-Up, Send Back to LM | Customize assignees if needed |

### What's NOT INCLUDED (Custom Build Required)

| Item | Description | Why Build It |
|------|-------------|--------------|
| Send to Acquisitions sequence | Duplicate card from Lead Management to Acquisitions | Tracks KPIs across boards |
| Offer follow-up chain | Task completion triggers next follow-up | Automates offer cadence |
| Offer outcome sequences | Actions when offer accepted/declined/canceled | Automates next steps |
| Board-to-board workflows | Move deals to Transactions board | Seamless workflow transitions |

---

## 2. Default Acquisitions Setup (Already Included)

Your default account includes sequences and task presets for basic acquisitions workflow.

### Default Acquisitions Task Presets

| Task Preset | Purpose | Default Due |
|-------------|---------|-------------|
| Make Offer | Present offer to qualified lead | Same day |
| Offer Follow-Up | Follow up after offer presented | 1-2 days |
| Send Back to LM | Return declined offers to Lead Management | 1 day |

### Default Acquisitions Board Phases

The default Acquisitions board includes these phases:
- Make Offer
- Offer Follow Up
- Offer Accepted
- Offer Declined
- Under Contract

### Customizing Default Acquisitions

To adjust the default sequences:
1. Go to Sequences page
2. Open the Acquisitions folder
3. Click on a sequence name
4. Select "Make Changes"
5. Adjust settings (assignee, task due date, etc.)
6. Save Sequence

**Common Customizations**:
- Change assignee from Sensei to Acquisitions Manager
- Enable round-robin for team distribution
- Add notification actions (SMS/Email)

---

## 3. Custom Sequences You Need to Build

The following sequences are recommended patterns but are NOT included in default accounts.

### Send to Acquisitions Sequence (Custom Build Required)

**Purpose**: When a lead is ready for an offer, automatically move them to the Acquisitions board.

**Important**: This sequence is NOT included in default accounts. You must build it yourself.

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved from Lead Management board, Any phase, to Lead Management board, "Send to Acquisitions" phase |

**Actions**:

1. **Duplicate Card**
   - Original board: Lead Management
   - Destination board: Acquisitions
   - Destination phase: Make Offer

2. **Assign Property**
   - Select: Acquisitions Manager (or round-robin)

3. **Create New Task**
   - Task: "Make New Offer"
   - Due: 0 days (same day)
   - Toggle: Assign this task to the property

**Why Duplicate Instead of Move?**

Duplicating (instead of moving) the card keeps the original on the Lead Management board. This helps track KPIs by showing how many leads progressed to Acquisitions.

### Make Offer Sequence (Custom Enhancement)

**Purpose**: When a card enters the "Make Offer" phase, ensure an offer task is created.

**Note**: Default accounts may include a version of this. Build if you need custom behavior.

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions board, "Make Offer" phase |

**Actions**:

1. **Create New Task**
   - Task: "Make New Offer"
   - Due: 0 days (same day)
   - Toggle: Assign this task to the property

**Recommendation**: This is Tyler's recommended "one sequence to start with" for acquisitions. It ensures every lead in the Make Offer phase has an active task.

---

## 4. Offer Follow-Up Chain (Custom Build Required)

**Important**: These sequences are NOT included in default accounts. You must build each one individually.

### How the Offer Follow-Up Chain Works

```
Task "Make New Offer" completed
    ↓
Sequence "Offer Made" triggers
    ↓
Moves card to "Offer Follow Up 1" phase
Creates Task "Offer Follow Up 1" (Due: 1 day)
    ↓
Task "Offer Follow Up 1" completed
    ↓
Sequence "Follow Up 1 Complete" triggers
    ↓
Creates Task "Offer Follow Up 2" (Due: 1 day)
    ↓
(Pattern continues...)
```

### Building the Offer Follow-Up Chain

**Sequence 1: Offer Made**

| Component | Setting |
|-----------|---------|
| **Trigger** | Task Completed |
| **Condition** | Task Is: "Make New Offer" |

**Actions**:
1. Move Card to "Offer Follow Up 1" phase
2. Create Task: "Offer Follow Up 1" (Due: 1 day)

**Sequence 2: Follow Up 1 Complete**

| Component | Setting |
|-----------|---------|
| **Trigger** | Task Completed |
| **Condition** | Task Is: "Offer Follow Up 1" |

**Actions**:
1. Move Card to "Offer Follow Up 2" phase
2. Create Task: "Offer Follow Up 2" (Due: 1 day)

Continue this pattern for each follow-up stage.

### Recommended Offer Follow-Up Cadence

| Task | Day | Purpose | Sequence to Build |
|------|-----|---------|-------------------|
| Make New Offer | 0 | Initial offer presentation | Offer Made |
| Follow Up 1 | 1 | First check-in | Follow Up 1 Complete |
| Follow Up 2 | 2 | Second check-in | Follow Up 2 Complete |
| Follow Up 3 | 3 | Third check-in | Follow Up 3 Complete |
| Follow Up 4 | 5 | Final follow-up before decision | (End of chain) |

**Total sequences required for Offer chain**: 4 sequences

---

## 5. Offer Outcome Sequences (Custom Build Required)

**Important**: These sequences are NOT included in default accounts.

### Offer Accepted Sequence

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions board, "Offer Accepted" phase |

**Actions**:
1. Change Property Status to "Under Contract"
2. Duplicate Card to Transactions board, "Under Contract" phase
3. Create Task: "Send Contract to Title" (Due: 0 days)
4. (Optional) Send SMS/Email notification to team

### Offer Declined Sequence

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions board, "Offer Declined" phase |

**Actions**:
1. Create Task: "Offer Rejected - Send Back to LM" (Due: 1 day)
2. Assign Property to Lead Manager

This task prompts the Lead Manager to review whether to:
- Re-engage the lead later
- Change status to Dead Lead
- Attempt a different approach

### Offer Canceled Sequence

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions board, "Offer Canceled" phase |

**Actions**:
1. Create Task: "Offer Canceled - Follow Up with LM" (Due: 1 day)
2. Assign Property to Lead Manager

---

## 6. Acquisitions Board Phases

### Recommended Phases for Your Acquisitions Board

| Phase | Purpose | Sequence Trigger |
|-------|---------|------------------|
| Make Offer | Ready to present offer | Card enters phase |
| Offer Follow Up 1 | First follow-up pending | Offer Made sequence |
| Offer Follow Up 2 | Second follow-up pending | Follow Up 1 Complete |
| Offer Follow Up 3 | Third follow-up pending | Follow Up 2 Complete |
| Offer Accepted | Deal moving forward | Manual move |
| Offer Declined | Seller said no | Manual move |
| Offer Canceled | Deal fell through | Manual move |
| Under Contract | Contract signed | Offer Accepted sequence |

---

## 7. Best Practices

### Getting Started

1. **Start with Make Offer sequence**: This single sequence ensures every lead in acquisitions has an active task.

2. **Use defaults first**: Test the default acquisitions workflow for 2-4 weeks before customizing.

3. **Build follow-up chain gradually**: Add one follow-up sequence at a time and test before adding the next.

### Building Custom Sequences

1. **Use Duplicate for board transfers**: Keep cards on original boards for KPI tracking.

2. **Assign to specific roles**: Use round-robin assignment if you have multiple acquisitions team members.

3. **Create notification sequences**: Consider adding SMS/Email actions to alert team members of accepted offers.

4. **Test on a single record**: Before enabling a sequence, manually trigger it on a test record.

### Sequence Priority

If you have limited sequences available, prioritize in this order:

| Priority | Sequence | Why |
|----------|----------|-----|
| 1 | Make Offer (card enters phase) | Ensures every lead has an active task |
| 2 | Offer Made (task complete) | Starts follow-up chain |
| 3 | Offer Accepted | Automates transition to Transactions |
| 4 | Follow Up 1-3 Complete | Completes follow-up automation |
| 5 | Send to Acquisitions | Automates board transition |
| 6 | Offer Declined/Canceled | Handles negative outcomes |

### Sequence Limits Consideration

| Plan | Limit | Recommended Acquisitions Sequences |
|------|-------|-----------------------------------|
| Essentials | 3 sequences | Make Offer, Offer Made, Offer Accepted |
| Professional | 8 sequences | All core sequences |
| Business | Unlimited | Full automation |
