# Acquisitions Sequences

Complete configurations for acquisitions workflows. Clearly distinguishes between default and custom sequences.

## Default vs. Custom

### Included in Default Accounts (After 4/16/2025)

| Item | Description | Action Required |
|------|-------------|-----------------|
| Acquisitions Board | SiftLine board with phases for offer workflow | None, ready to use |
| Default Sequences | Automations for acquisitions workflow | Review and toggle on/off |
| Task Presets | Make Offer, Offer Follow-Up, Send Back to LM | Customize assignees if needed |

### NOT Included (Custom Build Required)

| Item | Description | Why Build It |
|------|-------------|--------------|
| Send to Acquisitions sequence | Duplicate card from LM to ACQ | Tracks KPIs across boards |
| Offer follow-up chain | Task completion triggers next follow-up | Automates offer cadence |
| Offer outcome sequences | Actions when offer accepted/declined/canceled | Automates next steps |
| Board-to-board workflows | Move deals to Transactions board | Seamless workflow transitions |

## Default Acquisitions Board Phases

- Make Offer
- Offer Follow Up
- Offer Accepted
- Offer Declined
- Under Contract

## Custom Sequence Configurations

### Send to Acquisitions Sequence

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Lead Management, Any phase → To: Lead Management, "Send to Acquisitions" |

**Actions**:
1. **Duplicate Card** — Original: Lead Management → Destination: Acquisitions, Phase: Make Offer
2. **Assign Property** — Acquisitions Manager or round-robin
3. **Create Task** — "Make New Offer" (Due: 0 days), Toggle: Assign to property

**Why Duplicate?** Keeps original on LM board for KPI tracking.

### Make Offer Sequence (Tyler's #1 Recommendation)

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions board, "Make Offer" phase |

**Actions**:
1. **Create Task** — "Make New Offer" (Due: 0 days), Toggle: Assign to property

**This is the single most important acquisitions sequence.** Ensures every lead in Make Offer has an active task.

## Offer Follow-Up Chain (Custom Build Required)

### How It Works

```
Task "Make New Offer" completed
    → Sequence "Offer Made" triggers
    → Moves card to "Offer Follow Up 1" phase
    → Creates Task "Offer Follow Up 1" (Due: 1 day)
    → Task completed → Next sequence triggers → Pattern continues
```

### Building the Chain

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

Continue pattern for each follow-up.

### Recommended Offer Cadence

| Task | Day | Purpose | Sequence to Build |
|------|-----|---------|-------------------|
| Make New Offer | 0 | Initial offer | Offer Made |
| Follow Up 1 | 1 | First check-in | Follow Up 1 Complete |
| Follow Up 2 | 2 | Second check-in | Follow Up 2 Complete |
| Follow Up 3 | 3 | Third check-in | Follow Up 3 Complete |
| Follow Up 4 | 5 | Final before decision | (End of chain) |

**Total: 4 sequences**

## Offer Outcome Sequences (Custom Build Required)

### Offer Accepted

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions, "Offer Accepted" phase |

**Actions**:
1. Change Property Status to "Under Contract"
2. Duplicate Card to Transactions board, "Under Contract" phase
3. Create Task: "Send Contract to Title" (Due: 0 days)
4. (Optional) Send SMS/Email notification

### Offer Declined

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions, "Offer Declined" phase |

**Actions**:
1. Create Task: "Offer Rejected - Send Back to LM" (Due: 1 day)
2. Assign Property to Lead Manager

### Offer Canceled

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | Card moved to Acquisitions, "Offer Canceled" phase |

**Actions**:
1. Create Task: "Offer Canceled - Follow Up with LM" (Due: 1 day)
2. Assign Property to Lead Manager

## Recommended Acquisitions Board Phases

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

## Sequence Priority (Limited Plans)

| Priority | Sequence | Why |
|----------|----------|-----|
| 1 | Make Offer (card enters phase) | Ensures every lead has a task |
| 2 | Offer Made (task complete) | Starts follow-up chain |
| 3 | Offer Accepted | Automates transition to Transactions |
| 4 | Follow Up 1-3 Complete | Completes follow-up automation |
| 5 | Send to Acquisitions | Automates board transition |
| 6 | Offer Declined/Canceled | Handles negative outcomes |

### Plan Recommendations

| Plan | Limit | Best Acquisitions Sequences |
|------|-------|-----------------------------|
| Essentials | 3 | Make Offer, Offer Made, Offer Accepted |
| Professional | 8 | All core sequences |
| Business | Unlimited | Full automation |
