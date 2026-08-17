# Board-to-Board Workflow Sequences

Sequences that move or duplicate cards between SiftLine boards, creating seamless workflow transitions.

## The Three Core Boards

| Board | Purpose | Key Phases |
|-------|---------|------------|
| Lead Management | Nurture and qualify leads | New Lead, Engage, Cold/Warm/Hot, Send to Acquisitions |
| Acquisitions | Make and negotiate offers | Make Offer, Follow Up 1-3, Accepted/Declined |
| Transactions | Manage deals to close | Under Contract, Pending Assignment, Clear to Close, Closed |

## Move vs. Duplicate

| Action | When to Use | KPI Impact |
|--------|-------------|------------|
| **Move Card** | Card should only exist on one board | Loses history on original board |
| **Duplicate Card** | Need to track progression metrics | Shows how many leads moved forward |

**Recommendation**: Use Duplicate for board-to-board transfers to preserve KPI tracking.

## Lead Management → Acquisitions

### Trigger Point
Card moves to "Send to Acquisitions" phase on Lead Management board.

### Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Lead Management, Any phase → To: Lead Management, "Send to Acquisitions" |

### Actions
1. **Duplicate Card** — Original: Lead Management → Destination: Acquisitions, Phase: Make Offer
2. **Assign Property** — Acquisitions Manager or round-robin
3. **Create Task** — "Make New Offer" (Due: 0 days)

### Result
- Original card stays on Lead Management board (for tracking)
- New card appears on Acquisitions board
- Acquisitions team member is assigned
- Task is created for immediate action

## Acquisitions → Transactions

### Trigger Point
Card moves to "Offer Accepted" phase on Acquisitions board.

### Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Acquisitions, Any phase → To: Acquisitions, "Offer Accepted" |

### Actions
1. **Duplicate Card** — Original: Acquisitions → Destination: Transactions, Phase: Under Contract
2. **Change Property Status** — "Under Contract"
3. **Assign Property** — Transaction Coordinator or round-robin
4. **Create Task** — "Send Contract to Title" (Due: 0 days)
5. **(Optional) Send SMS/Email** — Notify team of accepted offer

## Transactions → Closed

### Trigger Point
Card moves to "Closed $$$" phase on Transactions board.

### Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Transactions, Any phase → To: Transactions, "Closed $$$" |

### Actions
1. **Change Property Status** — "Closed Deal"
2. **Add Property Tags** — "Closed 2025" (or current year)
3. **(Optional) Send SMS/Email** — Team celebration notification
4. **(Optional) Create Task** — "Post-Close Follow Up" (Due: 30 days)

## Common Patterns

### Pattern: Handoff with Notification

When transferring between team members:
1. Duplicate Card to new board
2. Assign Property to new team member
3. Create Task for new team member
4. Send SMS to new assignee: "New deal assigned: @property_address"

### Pattern: Conditional Routing

Route to different team members based on criteria:

**Sequence 1 — High-Value Deals**:
- Condition: Property has tag "High Value"
- Action: Assign to Senior Acquisitions Manager

**Sequence 2 — Standard Deals**:
- Condition: Property doesn't have tag "High Value"
- Action: Round-robin assign to Acquisitions team

### Pattern: Status Sync

Keep property status in sync with board position:

| Board Phase | Property Status |
|-------------|-----------------|
| New Lead | New Lead |
| Hot | Hot Lead |
| Make Offer | Making Offer |
| Offer Accepted | Under Contract |
| Closed $$$ | Closed Deal |

## Best Practices

1. **Always test with one record first** — manually trigger on a test record before enabling
2. **Use consistent naming** — e.g., "LM to ACQ - Send to Acquisitions"
3. **Document your workflow** — keep a diagram of which sequences trigger at which phases
4. **Check for existing cards** — "Create New Card" skips if card already exists on target board
5. **Avoid sequence chains** — sequences cannot trigger other sequences; combine related actions into one sequence

## Troubleshooting

**Card not appearing on destination board?**
- Check if card already exists on that board
- Verify the condition matches exact board and phase names

**Wrong team member assigned?**
- Check round-robin configuration
- Verify assignee selection in the sequence

**Task not created?**
- Ensure task preset exists
- Check if "Assign this task to the property" is toggled correctly
